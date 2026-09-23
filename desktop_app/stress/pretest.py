from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .baseline import SystemBaselineCollector, write_baseline_capture
from .models import EnvironmentStatus, PreTestCheck, PreTestStatus, StressTestDefinition
from .session import atomic_write_json, utc_now


TOOLS = (
    "stress-ng",
    "htop",
    "top",
    "mpstat",
    "vmstat",
    "iostat",
    "sensors",
    "tegrastats",
    "iperf3",
    "ping",
    "ethtool",
    "candump",
    "ros2",
    "journalctl",
)

LOG_FILES = {
    "dut": "01_dut_info.log",
    "tools": "02_tool_check.log",
    "resources": "03_resource_baseline.log",
    "network": "04_network_baseline.log",
    "can": "05_can_baseline.log",
    "ros2": "06_ros2_baseline.log",
    "sensors": "07_sensor_baseline.log",
    "kernel": "08_kernel_baseline.log",
}

BASELINE_DEPENDENCIES = frozenset({"connection", "logging"})
BLOCKING_STATUSES = frozenset(
    {PreTestStatus.FAIL, PreTestStatus.MISSING, PreTestStatus.NOT_CONFIGURED}
)


@dataclass
class ReadinessResult:
    status: EnvironmentStatus
    checks: list[PreTestCheck]
    blocking_reasons: list[str]
    dut_info: dict[str, str]
    generated_at: str
    baseline: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "blocking_reasons": self.blocking_reasons,
            "dut_info": self.dut_info,
            "generated_at": self.generated_at,
            "baseline": self.baseline,
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_readiness(checks: Iterable[PreTestCheck], required_dependencies: set[str]) -> tuple[EnvironmentStatus, list[str]]:
    blockers = []
    for check in checks:
        required = bool(check.dependency and check.dependency in required_dependencies)
        if required and check.status in BLOCKING_STATUSES:
            blockers.append(f"{check.name}: {check.status.value} ({check.actual})")
    return (EnvironmentStatus.NOT_READY, blockers) if blockers else (EnvironmentStatus.READY, [])


def dependencies_for_tests(definitions: Iterable[StressTestDefinition]) -> set[str]:
    result: set[str] = set()
    for definition in definitions:
        result.update(definition.dependencies)
    return result


class PreTestEnvironmentRunner:
    """Safe, bounded baseline collector. Run this service outside the GUI thread."""

    def __init__(
        self,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        *,
        baseline_duration_sec: float = 30.0,
        baseline_interval_sec: float = 1.0,
        baseline_collector: SystemBaselineCollector | None = None,
    ):
        self._command_runner = command_runner or self._run_command
        self._baseline_collector = baseline_collector or SystemBaselineCollector(
            baseline_duration_sec, baseline_interval_sec
        )

    @staticmethod
    def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=8, check=False)

    def run_local(self, output_dir: Path, required_dependencies: set[str]) -> ReadinessResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        logs: dict[str, list[str]] = {name: [] for name in LOG_FILES}
        dut_info = self._dut_info(logs["dut"])
        checks: list[PreTestCheck] = [
            PreTestCheck(
                "target_connection",
                "DUT connection",
                "Target",
                "Running on local DUT",
                PreTestStatus.PASS,
                "connection",
            )
        ]

        for tool in TOOLS:
            found = shutil.which(tool)
            status = PreTestStatus.PASS if found else PreTestStatus.MISSING
            dependency = self._tool_dependency(tool)
            checks.append(PreTestCheck(f"tool_{tool}", f"Tool: {tool}", "Tools", found or "Not found in PATH", status, dependency))
            logs["tools"].append(f"[{status.value}] {tool}: {found or 'not found'}")

        logging_check = self._logging_check(output_dir)
        checks.append(logging_check)
        disk = shutil.disk_usage(output_dir)
        checks.extend(
            [
                PreTestCheck("cpu_baseline", "CPU baseline", "Resources", f"{os.cpu_count() or 0} logical cores", PreTestStatus.PASS),
                PreTestCheck("ram", "RAM availability", "Resources", self._memory_summary(), PreTestStatus.PASS),
                PreTestCheck("swap", "Swap", "Resources", self._swap_summary(), PreTestStatus.PASS),
                PreTestCheck("disk", "Disk free", "Resources", f"{disk.free / (1024 ** 3):.1f} GiB", PreTestStatus.PASS, "logging", True),
            ]
        )
        logs["resources"].extend([f"cpu_count={os.cpu_count()}", self._memory_summary(), self._swap_summary(), f"disk_free={disk.free}"])

        if shutil.which("ip"):
            for command in (["ip", "-br", "addr"], ["ip", "route"], ["ip", "-s", "link"]):
                try:
                    captured = self._command_runner(command)
                    logs["network"].append(f"$ {' '.join(command)}\n{captured.stdout}{captured.stderr}")
                except (OSError, subprocess.SubprocessError) as exc:
                    logs["network"].append(f"$ {' '.join(command)}\nERROR: {exc}")
        else:
            logs["network"].append("ip command is unavailable.")
        if shutil.which("tegrastats") and shutil.which("timeout"):
            try:
                captured = self._command_runner(["timeout", "3", "tegrastats", "--interval", "1000"])
                logs["resources"].append(f"$ timeout 3 tegrastats --interval 1000\n{captured.stdout}{captured.stderr}")
            except (OSError, subprocess.SubprocessError) as exc:
                logs["resources"].append(f"tegrastats sanity check failed: {exc}")

        configured_checks = [
            ("ethernet", "Ethernet / target network", "network", "No stress target interface/profile configured"),
            ("can", "CAN", "can", "No CAN interface configured"),
            ("ros2", "ROS 2 graph", "ros2", "No required nodes/topics configured"),
            ("camera", "Camera", "camera", "No camera readiness probe configured"),
            ("lidar", "LiDAR", "lidar", "No LiDAR readiness probe configured"),
            ("imu", "IMU", "imu", "No IMU topic configured"),
            ("battery", "Battery", "battery", "No battery source/range configured"),
            ("motor", "Motor status", "motor", "No motor feedback/fault source configured"),
            ("estop", "E-stop", "estop", "Requires deliberate physical verification"),
        ]
        for check_id, name, dependency, actual in configured_checks:
            checks.append(PreTestCheck(check_id, name, "Target", actual, PreTestStatus.NOT_CONFIGURED, dependency))

        kernel_status, kernel_actual = self._kernel_check(logs["kernel"])
        checks.append(PreTestCheck("kernel", "Kernel/system errors", "Kernel", kernel_actual, kernel_status))
        baseline_capture = self._baseline_collector.collect_local()
        self._write_system_baseline(output_dir, baseline_capture)
        baseline_status = PreTestStatus.PASS if baseline_capture.data.get("cpu", {}).get("sample_count", 0) >= 1 else PreTestStatus.WARNING
        checks.extend(
            [
                PreTestCheck("baseline_system_metrics", "System Metrics Baseline", "System Baseline", f"{baseline_capture.data.get('cpu', {}).get('sample_count', 0)} utilization samples", baseline_status),
                PreTestCheck("baseline_thermal", "Hardware/Thermal Baseline", "System Baseline", "Captured" if baseline_capture.tegrastats_raw else "Not available", PreTestStatus.PASS if baseline_capture.tegrastats_raw else PreTestStatus.NOT_APPLICABLE),
                PreTestCheck("baseline_kernel", "Kernel Baseline", "System Baseline", "Captured", PreTestStatus.WARNING if baseline_capture.data.get("kernel", {}).get("warning_count", 0) else PreTestStatus.PASS),
                PreTestCheck("baseline_journal", "Journal Baseline", "System Baseline", "Captured", PreTestStatus.WARNING if baseline_capture.data.get("journal", {}).get("warning_count", 0) else PreTestStatus.PASS),
            ]
        )
        logs["network"].append("Target-specific required interface and peer IP addresses are not configured.")
        logs["can"].append("CAN interface is not configured.")
        logs["ros2"].append("Required ROS 2 nodes and topics are not configured.")
        logs["sensors"].append("Camera, LiDAR, IMU, battery, motor, and E-stop probes are not configured.")
        for key, filename in LOG_FILES.items():
            (output_dir / filename).write_text("\n".join(logs[key]) + "\n", encoding="utf-8", errors="replace")
        status, reasons = evaluate_readiness(checks, required_dependencies)
        result = ReadinessResult(status, checks, reasons, dut_info, utc_now(), baseline_capture.data)
        atomic_write_json(output_dir / "readiness.json", result.to_dict())
        return result

    @staticmethod
    def disconnected_result(output_dir: Path) -> ReadinessResult:
        """Create honest evidence when the configured shared DUT connection is absent."""
        output_dir.mkdir(parents=True, exist_ok=True)
        message = "Jetson stress target is not connected. Connect from Dashboard first."
        for filename in LOG_FILES.values():
            (output_dir / filename).write_text(message + "\n", encoding="utf-8")
        checks = [
            PreTestCheck(
                "target_connection",
                "DUT connection",
                "Target",
                message,
                PreTestStatus.FAIL,
                "connection",
            )
        ]
        result = ReadinessResult(
            EnvironmentStatus.NOT_READY,
            checks,
            [f"DUT connection: FAIL ({message})"],
            {"connection": "not connected"},
            utc_now(),
        )
        atomic_write_json(output_dir / "readiness.json", result.to_dict())
        return result

    @staticmethod
    async def remote_operation(
        ssh,
        required_dependencies: set[str],
        baseline_duration_sec: float = 30.0,
        baseline_interval_sec: float = 1.0,
    ) -> dict:
        """Collect a bounded baseline through the app's existing SSH connection."""
        commands = {
            "dut": "date; hostname; uname -a; cat /etc/os-release 2>&1; uptime; nproc; free -h; df -h; test -r /etc/nv_tegra_release && cat /etc/nv_tegra_release || true",
            "tools": "for c in stress-ng htop top mpstat vmstat iostat sensors tegrastats iperf3 ping ethtool candump journalctl; do command -v \"$c\" >/dev/null 2>&1 && printf '[PASS] %s\\n' \"$c\" || printf '[MISSING] %s\\n' \"$c\"; done",
            "resources": "date; nproc; free -h; df -h; uptime; vmstat 1 2 2>&1",
            "network": "ip -br addr 2>&1; ip route 2>&1; ip -s link 2>&1",
            "can": "printf 'CAN interface must be supplied by a stress target profile.\\n'",
            "ros2": (
                "bash -lc '"
                "if [ ! -f /opt/ros/humble/setup.bash ]; then "
                "printf \"[MISSING] ros2: /opt/ros/humble/setup.bash not found\\n\"; exit 0; fi; "
                "source /opt/ros/humble/setup.bash >/dev/null 2>&1 || { "
                "printf \"[MISSING] ros2: failed to source Humble setup\\n\"; exit 0; }; "
                "if [ -f /opt/vindynamics/system/setup.bash ]; then "
                "source /opt/vindynamics/system/setup.bash >/dev/null 2>&1; "
                "fi; "
                "if ! command -v ros2 >/dev/null 2>&1 || ! ros2 --help >/dev/null 2>&1; then "
                "printf \"[MISSING] ros2: unavailable after sourcing Humble setup\\n\"; exit 0; fi; "
                "printf \"[PASS] ros2\\n\"; "
                "nodes=$(ros2 node list 2>&1); "
                "printf \"%s\\n\" \"$nodes\"; "
                "if [ -n \"$nodes\" ]; then "
                "printf \"[PASS] ros2_graph\\n\"; "
                "else "
                "printf \"[FAIL] ros2_graph\\n\"; "
                "fi'"
            ),
            "sensors": (
                "bash -lc '"
                "source /opt/ros/humble/setup.bash >/dev/null 2>&1 || exit 0; "
                "source /opt/vindynamics/system/setup.bash >/dev/null 2>&1 || true; "
                "for spec in "
                "CAMERA:/sensors/camera/zed_x_mini/rgb "
                "LIDAR:/sensors/lidar/mid360/pointcloud "
                "IMU:/control/state/imu_state "
                "BATTERY:/control/state/pmu_state "
                "MOTOR:/control/state/motor_state; "
                "do "
                "name=${spec%%:*}; "
                "topic=${spec#*:}; "
                "info=$(timeout 3 ros2 topic info \"$topic\" 2>&1); "
                "printf \"%s\\n\" \"$info\"; "
                "if printf \"%s\\n\" \"$info\" | grep -Eq \"Publisher count: [1-9]\"; then "
                "printf \"[PASS] %s %s\\n\" \"$name\" \"$topic\"; "
                "else "
                "printf \"[FAIL] %s %s\\n\" \"$name\" \"$topic\"; "
                "fi; "
                "done; "
                "printf \"[INFO] ESTOP_SOURCE /control/state/pmu_state field=hw_estop_state\\n\"'"
            ),
            "kernel": "dmesg --level=emerg,alert,crit,err,warn 2>&1 | tail -n 200; journalctl -p warning..alert -b -n 200 --no-pager 2>&1",
        }
        outputs = {}
        for key, command in commands.items():
            result = await ssh.run(command, timeout=12)
            outputs[key] = {"stdout": result.stdout, "stderr": result.stderr, "exit_status": result.exit_status}
        outputs["system_baseline"] = await SystemBaselineCollector(
            baseline_duration_sec, baseline_interval_sec
        ).collect_remote(ssh)
        outputs["required_dependencies"] = sorted(required_dependencies)
        return outputs

    @staticmethod
    def result_from_remote(payload: dict, output_dir: Path, required_dependencies: set[str]) -> ReadinessResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        for key, filename in LOG_FILES.items():
            item = payload.get(key, {})
            text = (item.get("stdout", "") + item.get("stderr", "")).rstrip() + "\n"
            (output_dir / filename).write_text(text, encoding="utf-8", errors="replace")
        tool_text = payload.get("tools", {}).get("stdout", "")
        ros2_item = payload.get("ros2", {})
        ros2_text = ros2_item.get("stdout", "") + ros2_item.get("stderr", "")

        sensor_item = payload.get("sensors", {})
        sensor_text = sensor_item.get("stdout", "") + sensor_item.get("stderr", "")
        checks = [
            PreTestCheck(
                "target_connection",
                "DUT connection",
                "Target",
                "Connected through shared Jetson SSH service",
                PreTestStatus.PASS,
                "connection",
            )
        ]
        for tool in TOOLS:
            passed = f"[PASS] {tool}" in (ros2_text if tool == "ros2" else tool_text)

            if tool == "ros2":
                actual = (
                    "Available after sourcing ROS 2 + VinDynamics environment"
                    if passed
                    else "Unavailable after sourcing ROS 2 + VinDynamics environment"
                )
                status = PreTestStatus.PASS if passed else PreTestStatus.MISSING

            elif tool == "sensors" and not passed:
                actual = "Optional on Jetson; thermal data available via tegrastats/sysfs"
                status = PreTestStatus.NOT_APPLICABLE

            else:
                actual = "Available" if passed else "Not found in PATH"
                status = PreTestStatus.PASS if passed else PreTestStatus.MISSING

            checks.append(
                PreTestCheck(
                    f"tool_{tool}",
                    f"Tool: {tool}",
                    "Tools",
                    actual,
                    status,
                    PreTestEnvironmentRunner._tool_dependency(tool),
                )
            )
        checks.append(PreTestCheck("logging", "Logging path writable", "Resources", str(output_dir), PreTestStatus.PASS, "logging", True))
        checks.append(
            PreTestCheck(
                "ethernet",
                "Ethernet / target network",
                "Target",
                "Interface captured; required peer/IP not configured",
                PreTestStatus.NOT_CONFIGURED,
                "network",
            )
        )

        checks.append(
            PreTestCheck(
                "can",
                "CAN",
                "Target",
                "CAN interface not configured",
                PreTestStatus.NOT_CONFIGURED,
                "can",
            )
        )

        ros_graph_ok = "[PASS] ros2_graph" in ros2_text
        checks.append(
            PreTestCheck(
                "ros2",
                "ROS 2 graph",
                "Target",
                "ROS 2 environment and graph available"
                if ros_graph_ok
                else "ROS 2 environment or graph unavailable",
                PreTestStatus.PASS if ros_graph_ok else PreTestStatus.FAIL,
                "ros2",
            )
        )

        probes = (
            ("camera", "Camera", "camera", "CAMERA", "/sensors/camera/zed_x_mini/rgb"),
            ("lidar", "LiDAR", "lidar", "LIDAR", "/sensors/lidar/mid360/pointcloud"),
            ("imu", "IMU", "imu", "IMU", "/control/state/imu_state"),
            ("battery", "Battery", "battery", "BATTERY", "/control/state/pmu_state"),
            ("motor", "Motor status", "motor", "MOTOR", "/control/state/motor_state"),
        )

        for check_id, name, dependency, marker, topic in probes:
            passed = f"[PASS] {marker} {topic}" in sensor_text
            checks.append(
                PreTestCheck(
                    check_id,
                    name,
                    "Target",
                    f"{topic}: publisher available"
                    if passed
                    else f"{topic}: publisher unavailable",
                    PreTestStatus.PASS if passed else PreTestStatus.FAIL,
                    dependency,
                )
            )

        checks.append(
            PreTestCheck(
                "estop",
                "E-stop",
                "Target",
                "Source available: /control/state/pmu_state field=hw_estop_state; expected safe state not configured",
                PreTestStatus.NOT_CONFIGURED,
                "estop",
            )
        )
        kernel_output = (payload.get("kernel", {}).get("stdout", "") + payload.get("kernel", {}).get("stderr", "")).strip()
        kernel_status = PreTestStatus.WARNING if kernel_output else PreTestStatus.PASS
        checks.append(PreTestCheck("kernel", "Kernel/system errors", "Kernel", "Review captured warnings" if kernel_output else "No warning/error output", kernel_status))
        baseline_capture = payload.get("system_baseline")
        baseline = baseline_capture.data if baseline_capture is not None else {}
        if baseline_capture is not None:
            PreTestEnvironmentRunner._write_system_baseline(output_dir, baseline_capture)
        sample_count = baseline.get("cpu", {}).get("sample_count", 0)
        checks.extend(
            [
                PreTestCheck("baseline_system_metrics", "System Metrics Baseline", "System Baseline", f"{sample_count} utilization samples", PreTestStatus.PASS if sample_count else PreTestStatus.WARNING),
                PreTestCheck("baseline_thermal", "Hardware/Thermal Baseline", "System Baseline", "Captured" if baseline_capture and baseline_capture.tegrastats_raw else "Not available", PreTestStatus.PASS if baseline_capture and baseline_capture.tegrastats_raw else PreTestStatus.NOT_APPLICABLE),
                PreTestCheck("baseline_kernel", "Kernel Baseline", "System Baseline", "Captured", PreTestStatus.WARNING if baseline.get("kernel", {}).get("warning_count", 0) else PreTestStatus.PASS),
                PreTestCheck("baseline_journal", "Journal Baseline", "System Baseline", "Captured", PreTestStatus.WARNING if baseline.get("journal", {}).get("warning_count", 0) else PreTestStatus.PASS),
            ]
        )
        status, reasons = evaluate_readiness(checks, required_dependencies)
        dut_text = payload.get("dut", {}).get("stdout", "")
        dut_info = {"summary": dut_text.strip()}
        result = ReadinessResult(status, checks, reasons, dut_info, utc_now(), baseline)
        atomic_write_json(output_dir / "readiness.json", result.to_dict())
        return result

    @staticmethod
    def _write_system_baseline(output_dir: Path, capture) -> None:
        write_baseline_capture(
            capture,
            json_path=output_dir / "baseline.json",
            summary_path=output_dir / "baseline_summary.log",
            metrics_path=output_dir / "09_system_metrics_baseline.log",
            tegrastats_path=output_dir / "10_tegrastats_baseline.log",
            kernel_path=output_dir / "11_kernel_baseline.log",
            journal_path=output_dir / "12_journal_baseline.log",
        )

    @staticmethod
    def _tool_dependency(tool: str) -> str | None:
        return {"stress-ng": "workload", "iperf3": "network", "ping": "network", "ethtool": "network", "candump": "can", "ros2": "ros2"}.get(tool)

    @staticmethod
    def _logging_check(output_dir: Path) -> PreTestCheck:
        probe = output_dir / ".write_probe"
        try:
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return PreTestCheck("logging", "Logging path writable", "Resources", str(output_dir), PreTestStatus.PASS, "logging", True)
        except OSError as exc:
            return PreTestCheck("logging", "Logging path writable", "Resources", str(exc), PreTestStatus.FAIL, "logging", True)

    @staticmethod
    def _dut_info(log: list[str]) -> dict[str, str]:
        info = {
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "kernel": platform.release(),
            "cpu_count": str(os.cpu_count() or "unknown"),
            "ram": PreTestEnvironmentRunner._memory_summary(),
            "storage_free": PreTestEnvironmentRunner._storage_summary(),
            "uptime": PreTestEnvironmentRunner._uptime(),
        }
        log.extend(f"{key}: {value}" for key, value in info.items())
        release = Path("/etc/os-release")
        if release.is_file():
            log.append(release.read_text(encoding="utf-8", errors="replace"))
        l4t = Path("/etc/nv_tegra_release")
        if l4t.is_file():
            text = l4t.read_text(encoding="utf-8", errors="replace").strip()
            info["jetson_l4t"] = text
            log.append(text)
        return info

    @staticmethod
    def _uptime() -> str:
        try:
            return f"{float(Path('/proc/uptime').read_text().split()[0]):.0f} seconds"
        except (OSError, ValueError, IndexError):
            return "unknown"

    @staticmethod
    def _memory_summary() -> str:
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                values[key] = value.strip()
            return f"MemAvailable={values.get('MemAvailable', 'unknown')}"
        except OSError:
            return "Memory data unavailable"

    @staticmethod
    def _swap_summary() -> str:
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                values[key] = value.strip()
            return f"SwapTotal={values.get('SwapTotal', 'unknown')}; SwapFree={values.get('SwapFree', 'unknown')}"
        except OSError:
            return "Swap data unavailable"

    @staticmethod
    def _storage_summary() -> str:
        try:
            usage = shutil.disk_usage(Path.cwd())
            return f"{usage.free / (1024 ** 3):.1f} GiB free"
        except OSError:
            return "Storage data unavailable"

    def _kernel_check(self, log: list[str]) -> tuple[PreTestStatus, str]:
        if not shutil.which("dmesg"):
            log.append("dmesg is missing")
            return PreTestStatus.MISSING, "dmesg is missing"
        result = self._command_runner(["dmesg", "--level=emerg,alert,crit,err,warn"])
        output = (result.stdout or "") + (result.stderr or "")
        log.append(output.strip())
        if result.returncode != 0:
            return PreTestStatus.WARNING, "dmesg unavailable or permission denied; see log"
        return (PreTestStatus.WARNING, "Kernel warnings require review") if output.strip() else (PreTestStatus.PASS, "No warning/error output")
