from __future__ import annotations

import re

from .models import EvidenceDefinition, ExecutionType


def _evidence(
    evidence_id: str,
    name: str,
    collector: str,
    description: str,
    *,
    evidence_type: str = "log",
    interval: float | None = None,
    manual: bool = False,
) -> EvidenceDefinition:
    return EvidenceDefinition(
        id=evidence_id,
        name=name,
        type=evidence_type,
        collector=collector,
        command_description=description,
        sample_interval=interval,
        manual_required=manual,
    )


COMMON_EVIDENCE = (
    _evidence("workload", "Workload / Test Action", "workload", "Capture the approved workload or guided action output."),
    _evidence("system_metrics", "System Metrics", "system_metrics", "CPU, per-core availability, load, RAM, swap, and uptime snapshots.", interval=5),
    _evidence("hardware_metrics", "Hardware / Thermal Metrics", "tegrastats", "Jetson hardware, frequency, power, and thermal metrics when available.", interval=5),
    _evidence("kernel_log", "Kernel Error Log", "dmesg", "Kernel messages without clearing the kernel buffer."),
    _evidence("journal", "System Journal", "journal", "System service and error messages."),
)


GROUP_EVIDENCE: dict[str, tuple[EvidenceDefinition, ...]] = {
    "cpu": (
        _evidence("function_cpu", "CPU / GPU / AI Measurements", "manual", "Record utilization, frequency, temperature, process load, and AI FPS/latency where applicable.", manual=True),
    ),
    "memory": (
        _evidence("memory_trend", "Memory Trend", "vmstat", "Used/available RAM, swap, RSS, and trend over time.", interval=5),
        _evidence("oom_review", "OOM Review", "manual", "Review OOM and killed-process evidence.", manual=True),
    ),
    "storage": (
        _evidence("storage_io", "Storage I/O", "iostat", "Throughput, IOPS, utilization, latency, and free space.", interval=5),
        _evidence("storage_artifact", "Integrity / ROS Bag Artifact", "manual", "Attach checksums or rosbag information when required.", manual=True),
    ),
    "communication": (
        _evidence("communication", "Communication Health", "manual", "Throughput, latency, jitter, loss, counters, or CAN traffic as applicable.", manual=True),
    ),
    "sensor": (
        _evidence("sensor_health", "Sensor Health", "manual", "FPS/Hz, bandwidth, timestamp, and dropped frame/message evidence.", manual=True),
        _evidence("sensor_sample", "Sensor Sample / Bag", "manual", "Attach a representative image, point cloud, or ROS bag when applicable.", evidence_type="artifact", manual=True),
    ),
    "motor": (
        _evidence("motor_feedback", "Motor Command / Feedback", "manual", "Position, velocity, current, temperature, and fault status.", manual=True),
        _evidence("video", "Video Evidence", "manual", "Attach video where physical motion or actuator behavior must be observed.", evidence_type="artifact", manual=True),
    ),
    "motion": (
        _evidence("motion_status", "Motion Status", "manual", "Duration, cycle count, faults, slip/fall observations, and robot state.", manual=True),
        _evidence("video", "Video Evidence", "manual", "Attach video of the complete motion scenario.", evidence_type="artifact", manual=True),
    ),
    "power": (
        _evidence("power_measurements", "Power / Battery Measurements", "manual", "Voltage, current, power, minima, maxima, and peaks.", manual=True),
        _evidence("power_waveform", "External Power Waveform", "manual", "Attach power-analyzer or oscilloscope evidence when required.", evidence_type="artifact", manual=True),
    ),
    "thermal": (
        _evidence("thermal_trend", "Thermal Trend", "manual", "Temperature, load, frequency, and throttling trend.", manual=True),
        _evidence("thermal_image", "Thermal Image", "manual", "Attach thermal-camera evidence when required.", evidence_type="artifact", manual=True),
    ),
    "system": (
        _evidence("sensor_health", "Sensor Health", "manual", "Periodic sensor FPS/Hz and drop checkpoints.", manual=True),
        _evidence("communication", "Communication Health", "manual", "Periodic network, CAN, and ROS communication checkpoints.", manual=True),
        _evidence("checkpoints", "Endurance Checkpoints", "manual", "Record scheduled system-health checkpoints.", manual=True),
    ),
    "recovery": (
        _evidence("recovery_timeline", "Recovery Timeline", "manual", "Normal state, failure detection, recovery start/duration, and post-recovery state.", manual=True),
    ),
}


def classify_group(group: str, test_name: str = "") -> str:
    value = f"{group} {test_name}".lower()
    if "memory" in value or "ram" in value:
        return "memory"
    if "storage" in value or "disk" in value or "i/o" in value:
        return "storage"
    if any(word in value for word in ("network", "communication", "ethernet", "wi-fi", "wifi", "can")):
        return "communication"
    if "sensor" in value or any(word in value for word in ("camera", "lidar", "imu")):
        return "sensor"
    if "motor" in value or "actuator" in value:
        return "motor"
    if "motion" in value or "walking" in value:
        return "motion"
    if "power" in value or "battery" in value:
        return "power"
    if "thermal" in value:
        return "thermal"
    if "recovery" in value:
        return "recovery"
    if "system" in value or "endurance" in value or "integration" in value:
        return "system"
    return "cpu"


def build_evidence_plan(group: str, test_name: str) -> tuple[EvidenceDefinition, ...]:
    key = classify_group(group, test_name)
    return COMMON_EVIDENCE + GROUP_EVIDENCE.get(key, ())


def infer_dependencies(group: str, precondition: str, procedure: str) -> frozenset[str]:
    # Generic safety preconditions (battery threshold, E-stop check, and the SSH
    # transport used for every remote test) are not functional dependencies of
    # every test case. Infer readiness dependencies from the test's functional
    # scope; the shared DUT connection and logging are baseline dependencies.
    value = f"{group} {procedure}".lower()
    dependencies = {"logging"}
    patterns = {
        "network": ("ethernet", "wi-fi", "wifi", "iperf", "ping"),
        "can": (" can ", "can0", "candump", "bus-off"),
        "ros2": ("ros 2", "ros2", "topic", "node"),
        "camera": ("camera",),
        "lidar": ("lidar", "livox"),
        "imu": ("imu",),
        "battery": ("battery", "power"),
        "motor": ("motor", "actuator", "joint"),
    }
    padded = f" {value} "
    for dependency, needles in patterns.items():
        if any(needle in padded for needle in needles):
            dependencies.add(dependency)
    return frozenset(dependencies)


def classify_execution(group: str, procedure: str, equipment: str) -> ExecutionType:
    value = f"{group} {procedure} {equipment}".lower()
    physical = ("motor", "walking", "motion", "obstacle", "slope", "robot movement")
    external = ("oscilloscope", "thermal camera", "power analyzer", "video")
    destructive = ("reboot", "power cycle", "disconnect", "bus-off", "storage full", "disk full")
    if any(word in value for word in destructive) or any(word in value for word in physical):
        return ExecutionType.MANUAL
    if any(word in value for word in external) or "ai" in value or "sensor" in value:
        return ExecutionType.GUIDED
    return ExecutionType.GUIDED


def safe_cpu_workload(test_id: str, test_name: str, duration_seconds: int | None) -> tuple[str | None, tuple[str, ...], str | None, float | None]:
    """Map only explicit, bounded CPU-load source cases to stress-ng."""
    if test_id not in {f"ST-CPU-{index:03d}" for index in range(1, 7)}:
        return None, (), None, None
    match = re.search(r"CPU\s+(\d{1,2})%\s+Load", test_name, re.IGNORECASE)
    if not match or duration_seconds is None:
        return None, (), None, None
    load = int(match.group(1))
    if not 1 <= load <= 95:
        return None, (), None, None
    return (
        "stress-ng",
        ("--cpu", "0", "--timeout", f"{duration_seconds}s", "--metrics-brief"),
        f"Adaptive stress-ng CPU assistance toward source-defined {load}% for {duration_seconds} seconds",
        float(load),
    )
