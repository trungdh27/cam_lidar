from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .models import CollectorStatus, EvidenceDefinition, EvidenceRecord, StressTestDefinition
from .load_strategy import SUPPORTED_ADAPTIVE_CPU_IDS, LoadStrategy
from .session import utc_now


PERFORMANCE_AUTO_SCRIPT = r"""
import re
import subprocess
import sys
import time

interval = float(sys.argv[1])

ENV = (
    "source /opt/ros/humble/setup.bash >/dev/null 2>&1; "
    "source /opt/vindynamics/system/setup.bash >/dev/null 2>&1; "
)

def run(command, timeout=5):
    try:
        result = subprocess.run(
            ["bash", "-lc", ENV + command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.stdout + result.stderr
    except Exception:
        return ""

while True:
    values = []

    device = run(
        "timeout 3 ros2 topic echo /systemdiag/metrics/device --once",
        timeout=5,
    )

    def metric(key):
        pattern = (
            r"key:\s*" + re.escape(key) +
            r"\s+value:\s*([-\d.]+)"
        )
        match = re.search(pattern, device)
        return None if not match else float(match.group(1))

    gpu = metric("device.gpu.usage_percent")
    ram = metric("device.ram.used_percent")
    cpu_temp = metric("device.temperature.cpu_celsius")

    cpu_values = [
        float(value)
        for value in re.findall(
            r"key:\s*device\.cpu\.usage_percent\s+value:\s*([-\d.]+)",
            device,
        )
    ]

    if cpu_values:
        values.append(f"cpu_avg={sum(cpu_values)/len(cpu_values):.2f}")
        values.append(f"cpu_min={min(cpu_values):.2f}")
        values.append(f"cpu_max={max(cpu_values):.2f}")

    if gpu is not None:
        values.append(f"gpu_usage={gpu:.2f}")

    if ram is not None:
        values.append(f"ram_usage={ram:.2f}")

    if cpu_temp is not None:
        values.append(f"cpu_temp={cpu_temp:.2f}")

    nodes = run("ros2 node list", timeout=5)
    ai_nodes = [
        line.strip()
        for line in nodes.splitlines()
        if line.strip()
        and any(
            token in line.lower()
            for token in ("ai", "infer", "detect", "vision", "model")
        )
    ]

    values.append(f"ai_nodes={len(ai_nodes)}")
    values.append(f"ai_runtime_detected={1 if ai_nodes else 0}")

    print(" ".join(values), flush=True)
    time.sleep(interval)
""".strip()


ROS_SENSOR_HEALTH_SCRIPT = r"""
import concurrent.futures
import re
import subprocess
import sys
import time

interval = float(sys.argv[1])

ENV = (
    "source /opt/ros/humble/setup.bash >/dev/null 2>&1; "
    "source /opt/vindynamics/system/setup.bash >/dev/null 2>&1; "
)

TOPICS = {
    "camera_fps": "/sensors/camera/zed_x_mini/rgb",
    "lidar_hz": "/sensors/lidar/mid360/pointcloud",
    "imu_hz": "/control/state/imu_state",
}

def run(command, timeout=5):
    try:
        result = subprocess.run(
            ["bash", "-lc", ENV + command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.stdout + result.stderr
    except Exception:
        return ""

def topic_rate(name_topic):
    name, topic = name_topic
    output = run(f"timeout 3 ros2 topic hz {topic}", timeout=5)

    matches = re.findall(
        r"average rate:\s*([\d.]+)",
        output,
        re.IGNORECASE,
    )

    if not matches:
        return name, None

    try:
        return name, float(matches[-1])
    except ValueError:
        return name, None

while True:
    values = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(topic_rate, TOPICS.items()))

    for name, rate in results:
        if rate is not None:
            values.append(f"{name}={rate:.2f}")

    nodes_output = run("ros2 node list", timeout=5)
    nodes = [
        line.strip()
        for line in nodes_output.splitlines()
        if line.strip().startswith("/")
    ]

    values.append(f"ros_nodes={len(nodes)}")
    values.append(f"ros_health={1 if nodes else 0}")

    print(" ".join(values), flush=True)
    time.sleep(interval)
""".strip()


ROS_ROBOT_METRICS_SCRIPT = r"""
import concurrent.futures
import subprocess, time, sys, re

interval = float(sys.argv[1])

def run(cmd):
    try:
        result = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout
    except Exception:
        return ""

env = (
    "source /opt/ros/humble/setup.bash >/dev/null 2>&1; "
    "source /opt/vindynamics/system/setup.bash >/dev/null 2>&1; "
)

while True:
    commands = (
        env + "timeout 2 ros2 topic echo /control/state/pmu_state --once",
        env + "timeout 2 ros2 topic echo /control/state/motor_state --once",
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pmu, motor = list(pool.map(run, commands))

    values = []

    def one(pattern, text, key):
        match = re.search(pattern, text)
        if match:
            values.append(f"{key}={match.group(1)}")

    one(r"batt1_soc:\s*([\d.]+)", pmu, "battery_soc")
    one(r"batt1_soh:\s*([\d.]+)", pmu, "battery_soh")
    one(r"batt1_pack_voltage:\s*([-\d.]+)", pmu, "battery_pack_voltage")
    one(r"batt1_pack_current:\s*([-\d.]+)", pmu, "battery_pack_current")
    one(r"motor_bus_voltage:\s*([-\d.]+)", pmu, "motor_bus_voltage")
    one(r"motor_bus_current:\s*([-\d.]+)", pmu, "motor_bus_current")
    one(r"motor_bus_error_code:\s*(\d+)", pmu, "motor_bus_error_code")

    # Battery temperature array from PMU
    batt_temp_match = re.search(
        r"batt1_temperatures:\s*(.*?)(?=\n\S|\Z)",
        pmu,
        re.DOTALL,
    )

    battery_temperatures = []
    if batt_temp_match:
        battery_temperatures = [
            float(value)
            for value in re.findall(
                r"-\s*([-\d.]+)",
                batt_temp_match.group(1),
            )
        ]

    if battery_temperatures:
        values.append(
            f"battery_temp_min={min(battery_temperatures):.2f}"
        )
        values.append(
            f"battery_temp_avg={sum(battery_temperatures)/len(battery_temperatures):.2f}"
        )
        values.append(
            f"battery_temp_max={max(battery_temperatures):.2f}"
        )

        for index, temperature in enumerate(
            battery_temperatures,
            start=1,
        ):
            values.append(
                f"battery_temp_{index:02d}={temperature:.2f}"
            )

    temperatures = [
        float(value)
        for value in re.findall(
            r"temperature_fb:\s*([-\d.]+)",
            motor,
        )
    ]

    alive = re.findall(
        r"motor_alive:\s*(true|false)",
        motor,
        re.IGNORECASE,
    )
    errors = [
        int(value)
        for value in re.findall(r"error_code:\s*(\d+)", motor)
    ]

    if temperatures:
        values.append(
            f"motor_temp_min={min(temperatures):.2f}"
        )
        values.append(
            f"motor_temp_avg={sum(temperatures)/len(temperatures):.2f}"
        )
        values.append(
            f"motor_temp_max={max(temperatures):.2f}"
        )

        for index, temperature in enumerate(
            temperatures,
            start=1,
        ):
            values.append(
                f"motor_temp_{index:02d}={temperature:.2f}"
            )

    if alive:
        alive_count = sum(1 for value in alive if value.lower() == "true")
        values.append(f"motor_alive={alive_count}")
        values.append(f"motor_total={len(alive)}")

    if errors:
        error_count = sum(1 for value in errors if value != 0)
        values.append(f"motor_errors={error_count}")

    print(" ".join(values), flush=True)
    time.sleep(interval)
""".strip()


THERMAL_ZONES_SCRIPT = r"""
import datetime, glob, os, time, sys

interval = float(sys.argv[1])

while True:
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    values = []

    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            name = open(os.path.join(zone, "type"), encoding="ascii").read().strip()
            raw = open(os.path.join(zone, "temp"), encoding="ascii").read().strip()
            temp_c = float(raw) / 1000.0

            safe_name = (
                name.replace("-", "_")
                    .replace(" ", "_")
                    .replace("/", "_")
                    .lower()
            )
            values.append(f"{safe_name}={temp_c:.2f}")
        except (OSError, ValueError):
            continue

    print(f"{stamp} " + " ".join(values), flush=True)
    time.sleep(interval)
""".strip()


SYSTEM_METRICS_SCRIPT = r"""
import datetime, os, time
interval=float(__import__('sys').argv[1])
previous=None
while True:
    stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
    load=os.getloadavg()
    mem={}
    try:
        for line in open('/proc/meminfo', encoding='ascii'):
            key, value=line.split(':', 1); mem[key]=value.strip()
    except OSError as exc:
        mem['error']=str(exc)
    uptime='unknown'
    try: uptime=open('/proc/uptime', encoding='ascii').read().split()[0]
    except OSError: pass
    cpu=[]
    try:
        current=[]
        for line in open('/proc/stat', encoding='ascii'):
            if not line.startswith('cpu'): break
            parts=line.split(); values=[int(value) for value in parts[1:]]
            current.append((parts[0], sum(values), values[3] + (values[4] if len(values) > 4 else 0)))
        if previous:
            for (name,total,idle),(_,old_total,old_idle) in zip(current, previous):
                delta=max(1,total-old_total); cpu.append(f"{name}={100.0*(delta-(idle-old_idle))/delta:.1f}%")
        previous=current
    except (OSError, ValueError): pass
    print(f"{stamp} {' '.join(cpu)} load1={load[0]:.2f} load5={load[1]:.2f} load15={load[2]:.2f} "
          f"mem_available={mem.get('MemAvailable','unknown')} swap_free={mem.get('SwapFree','unknown')} uptime_sec={uptime}", flush=True)
    time.sleep(interval)
""".strip()


ADAPTIVE_CPU_SCRIPT = r"""
import os, signal, subprocess, sys, time
target=float(sys.argv[1]); baseline=float(sys.argv[2]); duration=max(1, int(float(sys.argv[3])))
separate=sys.argv[4]
step=5.0; contribution=0.0; child=None; stopping=False; started=time.monotonic(); last_adjust=0.0
output=open(separate, 'a', encoding='utf-8') if separate != '-' else None
def emit(message): print(message, flush=True)
def stop_child():
    global child
    if child is not None and child.poll() is None:
        child.terminate()
        try: child.wait(timeout=3)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
    if child is not None and child.stdout is not None:
        for line in child.stdout:
            emit('STRESS_NG_OUTPUT '+line.rstrip())
    child=None
def shutdown(*_args):
    global stopping
    stopping=True; stop_child()
signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)
def cpu_sample():
    def read():
        values=[int(value) for value in open('/proc/stat', encoding='ascii').readline().split()[1:9]]
        return sum(values), values[3] + values[4]
    total1,idle1=read(); time.sleep(1); total2,idle2=read(); delta=max(1,total2-total1)
    return 100.0*(delta-(idle2-idle1))/delta
def configure(value, reason):
    global child, contribution
    stop_child(); contribution=max(0.0,min(target,100.0,value))
    elapsed=int(time.monotonic()-started)
    emit(f'ADJUSTMENT T+{elapsed:05d}s injected={contribution:.1f}% reason={reason}')
    if contribution > 0:
        remaining=max(1,duration-elapsed)
        destination=output if output is not None else subprocess.PIPE
        child=subprocess.Popen(['stress-ng','--cpu','0','--cpu-load',str(max(1,int(round(contribution)))),'--timeout',f'{remaining}s','--metrics-brief'],stdout=destination,stderr=subprocess.STDOUT,text=True)
emit(f'Target CPU: {target:.1f}%')
emit(f'Immediate Baseline Avg CPU: {baseline:.1f}%')
emit('Execution Strategy: LOAD_ASSIST')
emit(f'Initial Gap: {max(0.0,target-baseline):.1f} percentage points')
configure(min(step,max(0.0,target-baseline)), 'initial conservative step')
while not stopping and time.monotonic()-started < duration:
    actual=cpu_sample(); elapsed=int(time.monotonic()-started)
    state='HOLDING' if actual == target else 'BELOW_TARGET' if actual < target else 'ABOVE_TARGET'
    emit(f'MEASUREMENT T+{elapsed:05d}s actual={actual:.1f}% injected={contribution:.1f}% state={state}')
    if elapsed-last_adjust >= 20:
        if actual < target and contribution < target:
            configure(min(target, contribution+step), 'actual CPU below target')
        elif actual > target and contribution > 0:
            configure(max(0.0, contribution-step), 'actual CPU above target; reduce artificial contribution only')
        last_adjust=elapsed
stop_child()
if output is not None: output.close()
emit('Adaptive workload monitoring complete.')
""".strip()


class EvidenceCollector(QObject):
    status_changed = Signal(object)
    finished = Signal(object)

    def __init__(self, record: EvidenceRecord, log_path: Path, parent=None):
        super().__init__(parent)
        self.record = record
        self.log_path = log_path

    @property
    def running(self) -> bool:
        return self.record.status in {CollectorStatus.STARTING, CollectorStatus.RUNNING}

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def _set_status(self, status: CollectorStatus, error: str | None = None) -> None:
        self.record.status = status
        self.record.error = error
        self.record.last_update = utc_now()
        if status == CollectorStatus.RUNNING and self.record.started_at is None:
            self.record.started_at = self.record.last_update
        if status in {
            CollectorStatus.COMPLETED,
            CollectorStatus.WARNING,
            CollectorStatus.ERROR,
            CollectorStatus.STOPPED,
            CollectorStatus.NOT_APPLICABLE,
            CollectorStatus.MANUAL_REQUIRED,
        }:
            self.record.ended_at = self.record.last_update
        self.status_changed.emit(self.record)


class ManualEvidenceCollector(EvidenceCollector):
    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self._set_status(CollectorStatus.MANUAL_REQUIRED)
        self.finished.emit(self.record)

    def stop(self) -> None:
        return


class ObservationEvidenceCollector(EvidenceCollector):
    def __init__(self, record, log_path: Path, duration_sec: int, decision, parent=None):
        super().__init__(record, log_path, parent)
        self.duration_sec = max(1, int(duration_sec))
        self.decision = decision
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._complete)

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            f"Target CPU: {self.decision.target_percent:g}%\n"
            f"Immediate Baseline Avg CPU: {self.decision.baseline_percent:.1f}%\n"
            "Execution Strategy: OBSERVE_ONLY\n"
            "Artificial CPU Load Added: 0%\n"
            f"Reason: {self.decision.reason}\n",
            encoding="utf-8",
        )
        self._set_status(CollectorStatus.RUNNING)
        self.timer.start(self.duration_sec * 1000)

    def stop(self) -> None:
        if not self.running:
            return
        self.timer.stop()
        self._set_status(CollectorStatus.STOPPED)
        self.finished.emit(self.record)

    def _complete(self) -> None:
        self._set_status(CollectorStatus.COMPLETED)
        self.finished.emit(self.record)


class QProcessEvidenceCollector(EvidenceCollector):
    def __init__(self, record: EvidenceRecord, log_path: Path, program: str, arguments: list[str], *, unavailable_status: CollectorStatus = CollectorStatus.ERROR, failure_status: CollectorStatus = CollectorStatus.ERROR, parent=None):
        super().__init__(record, log_path, parent)
        self.program = program
        self.arguments = arguments
        self.unavailable_status = unavailable_status
        self.failure_status = failure_status
        self.process = QProcess(self)
        self.process.started.connect(self._on_started)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)
        self._stop_requested = False
        self._terminal_emitted = False

    @property
    def running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        executable = self.program if os.path.isabs(self.program) else shutil.which(self.program)
        if not executable:
            self.log_path.write_text(f"Collector unavailable: {self.program} was not found in PATH.\n", encoding="utf-8")
            self._set_status(self.unavailable_status, f"{self.program} not found")
            self._emit_finished_once()
            return
        self._set_status(CollectorStatus.STARTING)
        self.process.setProgram(executable)
        self.process.setArguments(self.arguments)
        self.process.setStandardOutputFile(str(self.log_path), QProcess.OpenModeFlag.Append)
        self.process.setStandardErrorFile(str(self.log_path), QProcess.OpenModeFlag.Append)
        self.process.start()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop_requested = True
        self.process.terminate()
        QTimer.singleShot(2000, self._force_stop)

    def _force_stop(self) -> None:
        if self.running:
            self.process.kill()

    def _on_started(self) -> None:
        self._set_status(CollectorStatus.RUNNING)

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        if self._stop_requested:
            status = CollectorStatus.STOPPED
            error = None
        elif exit_code == 0:
            status = CollectorStatus.COMPLETED
            error = None
        else:
            status = self.failure_status
            error = f"Process exited with code {exit_code}"
        self._set_status(status, error)
        self._emit_finished_once()

    def _on_error(self, error) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning and not self._stop_requested:
            self._set_status(CollectorStatus.ERROR, self.process.errorString())
            self._emit_finished_once()

    def _emit_finished_once(self) -> None:
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self.finished.emit(self.record)


class RemoteEvidenceCollector(EvidenceCollector):
    """Collector adapter for the application's existing shared SSH service."""

    def __init__(self, record: EvidenceRecord, log_path: Path, service, command: str, *, failure_status: CollectorStatus = CollectorStatus.ERROR, mirrored_output_path: Path | None = None, parent=None):
        super().__init__(record, log_path, parent)
        self.service = service
        self.command = command
        self.failure_status = failure_status
        self.mirrored_output_path = mirrored_output_path
        self.request_id: str | None = None
        self._stop_requested = False
        service.remote_process_started.connect(self._on_started)
        service.remote_process_output.connect(self._on_output)
        service.remote_process_finished.connect(self._on_finished)
        service.remote_process_failed.connect(self._on_failed)

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self._set_status(CollectorStatus.STARTING)
        self.request_id = self.service.start_remote_process(f"stress_{self.record.id}", self.command)
        if self.request_id is None:
            self._set_status(CollectorStatus.ERROR, "Stress target is not connected")
            self.finished.emit(self.record)

    def stop(self) -> None:
        if self.request_id and self.running:
            self._stop_requested = True
            self.service.stop_remote_process(self.request_id)

    def _matches(self, request_id: str) -> bool:
        return bool(self.request_id and request_id == self.request_id)

    def _on_started(self, request_id: str) -> None:
        if self._matches(request_id):
            self._set_status(CollectorStatus.RUNNING)

    def _on_output(self, request_id: str, stream_name: str, line: str) -> None:
        if not self._matches(request_id):
            return
        with self.log_path.open("a", encoding="utf-8", errors="replace") as stream:
            stream.write(f"[{stream_name}] {line}\n")
        if self.mirrored_output_path is not None and line.startswith("STRESS_NG_OUTPUT "):
            with self.mirrored_output_path.open("a", encoding="utf-8", errors="replace") as stream:
                stream.write(line.removeprefix("STRESS_NG_OUTPUT ") + "\n")
        self.record.last_update = utc_now()

    def _on_finished(self, request_id: str, exit_code: int) -> None:
        if not self._matches(request_id):
            return
        status = CollectorStatus.STOPPED if self._stop_requested else CollectorStatus.COMPLETED if exit_code == 0 else self.failure_status
        self._set_status(status, None if exit_code == 0 or self._stop_requested else f"Remote process exited with code {exit_code}")
        self.finished.emit(self.record)

    def _on_failed(self, request_id: str, error: str) -> None:
        if self._matches(request_id):
            self._set_status(CollectorStatus.ERROR, error)
            self.finished.emit(self.record)


def _record_for(definition: EvidenceDefinition, filename: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=definition.id,
        name=definition.name,
        type=definition.type,
        collector=definition.collector,
        status=CollectorStatus.MANUAL_REQUIRED if definition.manual_required else CollectorStatus.WAITING,
        file=filename,
        command_description=definition.command_description,
        sample_interval=definition.sample_interval,
        manual_required=definition.manual_required,
    )


class EvidenceManager(QObject):
    evidence_changed = Signal(object)
    all_stopped = Signal()

    def __init__(self, definition: StressTestDefinition, attempt_dir: Path, *, remote_service=None, strategy_decision=None, parent=None):
        super().__init__(parent)
        self.definition = definition
        self.attempt_dir = attempt_dir
        self.remote_service = remote_service
        self.strategy_decision = strategy_decision
        self.collectors: dict[str, EvidenceCollector] = {}
        self.records: list[EvidenceRecord] = []
        self._build_collectors()

    def _build_collectors(self) -> None:
        for index, evidence in enumerate(self.definition.evidence_plan, start=1):
            filename = f"logs/{index:02d}_{evidence.id}.log"
            record = _record_for(evidence, filename)
            log_path = self.attempt_dir / filename
            collector = self._make_collector(evidence, record, log_path)
            collector.status_changed.connect(self.evidence_changed.emit)
            collector.finished.connect(self._collector_finished)
            self.records.append(record)
            self.collectors[record.id] = collector

    def _make_collector(self, evidence: EvidenceDefinition, record: EvidenceRecord, log_path: Path) -> EvidenceCollector:
        if evidence.collector == "workload" and self.strategy_decision is not None:
            if self.strategy_decision.strategy == LoadStrategy.OBSERVE_ONLY:
                return ObservationEvidenceCollector(
                    record,
                    log_path,
                    self.definition.duration_seconds or 1,
                    self.strategy_decision,
                    self,
                )
            if self.strategy_decision.strategy == LoadStrategy.LOAD_ASSIST:
                separate = log_path.parent / "stress_ng_output.log"
                program = "python3" if self.remote_service is not None else sys.executable
                arguments = [
                    "-u",
                    "-c",
                    ADAPTIVE_CPU_SCRIPT,
                    str(self.strategy_decision.target_percent),
                    str(self.strategy_decision.baseline_percent),
                    str(self.definition.duration_seconds or 1),
                    "-" if self.remote_service is not None else str(separate),
                ]
                remote_command = "exec " + shlex.join([program, *arguments])
                if self.remote_service is not None:
                    return RemoteEvidenceCollector(
                        record,
                        log_path,
                        self.remote_service,
                        remote_command,
                        mirrored_output_path=separate,
                        parent=self,
                    )
                return QProcessEvidenceCollector(record, log_path, program, arguments, parent=self)
        if evidence.collector == "workload" and self.definition.test_id in SUPPORTED_ADAPTIVE_CPU_IDS:
            # The six adaptive CPU cases must never fall back to the legacy
            # direct stress-ng command without an immediate-baseline decision.
            record.manual_required = True
            record.status = CollectorStatus.MANUAL_REQUIRED
            return ManualEvidenceCollector(record, log_path, self)
        if evidence.manual_required or (evidence.collector == "workload" and not self.definition.workload_program):
            record.manual_required = True
            record.status = CollectorStatus.MANUAL_REQUIRED
            return ManualEvidenceCollector(record, log_path, self)
        program, arguments, remote_command, unavailable = self._command_for(evidence)
        if self.remote_service is not None and remote_command:
            return RemoteEvidenceCollector(
                record,
                log_path,
                self.remote_service,
                remote_command,
                failure_status=unavailable,
                parent=self,
            )
        return QProcessEvidenceCollector(
            record,
            log_path,
            program,
            arguments,
            unavailable_status=unavailable,
            failure_status=unavailable,
            parent=self,
        )

    def _command_for(self, evidence: EvidenceDefinition) -> tuple[str, list[str], str | None, CollectorStatus]:
        interval = str(evidence.sample_interval or 5)
        if evidence.collector == "workload":
            if self.definition.workload_program:
                program = self.definition.workload_program
                args = list(self.definition.workload_arguments)
                return program, args, "exec " + shlex.join([program, *args]), CollectorStatus.ERROR
            return sys.executable, ["-u", "-c", "print('Guided/manual test action; no automatic workload was started.')"], "printf '%s\\n' 'Guided/manual test action; no automatic workload was started.'", CollectorStatus.ERROR
        if evidence.collector == "system_metrics":
            remote = "trap 'exit 0' TERM INT; while true; do date --iso-8601=seconds; head -n \"$(( $(nproc) + 1 ))\" /proc/stat; uptime; free -b; sleep 5; done"
            return sys.executable, ["-u", "-c", SYSTEM_METRICS_SCRIPT, interval], remote, CollectorStatus.ERROR
        if evidence.collector == "tegrastats":
            return "tegrastats", ["--interval", str(int(float(interval) * 1000))], f"exec tegrastats --interval {int(float(interval) * 1000)}", CollectorStatus.NOT_APPLICABLE
        if evidence.collector == "thermal_zones":
            remote = "exec " + shlex.join([
                "python3",
                "-u",
                "-c",
                THERMAL_ZONES_SCRIPT,
                interval,
            ])
            return sys.executable, ["-u", "-c", THERMAL_ZONES_SCRIPT, interval], remote, CollectorStatus.NOT_APPLICABLE

        if evidence.collector == "robot_metrics":
            remote = "exec " + shlex.join([
                "python3",
                "-u",
                "-c",
                ROS_ROBOT_METRICS_SCRIPT,
                interval,
            ])
            return sys.executable, ["-u", "-c", ROS_ROBOT_METRICS_SCRIPT, interval], remote, CollectorStatus.NOT_APPLICABLE

        if evidence.collector == "sensor_health_auto":
            remote = "exec " + shlex.join([
                "python3",
                "-u",
                "-c",
                ROS_SENSOR_HEALTH_SCRIPT,
                interval,
            ])
            return sys.executable, ["-u", "-c", ROS_SENSOR_HEALTH_SCRIPT, interval], remote, CollectorStatus.NOT_APPLICABLE

        if evidence.collector == "performance_auto":
            remote = "exec " + shlex.join([
                "python3",
                "-u",
                "-c",
                PERFORMANCE_AUTO_SCRIPT,
                interval,
            ])
            return sys.executable, ["-u", "-c", PERFORMANCE_AUTO_SCRIPT, interval], remote, CollectorStatus.NOT_APPLICABLE
        if evidence.collector == "dmesg":
            return "dmesg", ["-wT"], "exec dmesg -wT", CollectorStatus.WARNING
        if evidence.collector == "journal":
            return "journalctl", ["-f", "-n", "0", "--no-pager"], "exec journalctl -f -n 0 --no-pager", CollectorStatus.WARNING
        if evidence.collector == "vmstat":
            return "vmstat", [interval], f"exec vmstat {shlex.quote(interval)}", CollectorStatus.WARNING
        if evidence.collector == "iostat":
            return "iostat", ["-xz", interval], f"exec iostat -xz {shlex.quote(interval)}", CollectorStatus.WARNING
        return sys.executable, ["-u", "-c", f"print('No collector registered for {evidence.collector}')"], None, CollectorStatus.ERROR

    def start_all(self) -> None:
        ordered = sorted(
            self.collectors.values(),
            key=lambda collector: collector.record.id == "workload",
        )
        for collector in ordered:
            collector.start()

    def stop_all(self) -> None:
        for collector in self.collectors.values():
            collector.stop()
        self._emit_if_stopped()

    def active_count(self) -> int:
        return sum(record.status in {CollectorStatus.STARTING, CollectorStatus.RUNNING} for record in self.records)

    def warning_count(self) -> int:
        return sum(record.status == CollectorStatus.WARNING for record in self.records)

    def error_count(self) -> int:
        return sum(record.status == CollectorStatus.ERROR for record in self.records)

    def _collector_finished(self, _record: EvidenceRecord) -> None:
        self._emit_if_stopped()

    def _emit_if_stopped(self) -> None:
        if all(not collector.running for collector in self.collectors.values()):
            self.all_stopped.emit()
