"""Reusable runtime monitoring and reliability runners for Audio automation.

The monitor deliberately submits short snapshots through the existing
``JetsonConnectionService`` worker.  A long-running monitor coroutine would
occupy the shared SSH operation lock and prevent capture/playback actions
from running.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import shlex
import time
from typing import Any, Callable, Mapping

from PySide6.QtCore import QObject, QTimer, Signal

from desktop_app.audio.audio_automation import (
    AudioActionResult,
    parse_pulse_summary,
    parse_usb_devices,
)
from desktop_app.audio.audio_models import parse_respeaker_alsa_card


RUNTIME_EVENT_CATEGORIES = {
    "XRUN",
    "USB_RESET",
    "USB_DISCONNECT",
    "USB_RECONNECT",
    "AUDIO_ERROR",
    "IO_ERROR",
    "SYSTEM",
}


@dataclass(frozen=True)
class AudioRuntimeEvent:
    timestamp: str
    category: str
    severity: str
    source: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def format_line(self) -> str:
        return f"{self.timestamp} [{self.category}] {self.message}"


@dataclass(frozen=True)
class AudioRuntimeSample:
    timestamp: str
    elapsed_sec: float
    cpu_percent: float | None = None
    ram_percent: float | None = None
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    usb_present: bool | None = None
    alsa_present: bool | None = None
    pulse_source_present: bool | None = None
    pulse_sink_present: bool | None = None
    capture_running: bool = False
    playback_running: bool = False
    xrun_count: int = 0
    usb_reset_count: int = 0
    disconnect_count: int = 0
    reconnect_count: int = 0
    audio_error_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AudioIterationResult:
    iteration: int
    started_at: str
    finished_at: str
    duration_sec: float
    status: str
    action_result: dict[str, Any] | None = None
    capture_file: str | None = None
    analysis_file: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AudioIterationCampaignResult:
    campaign_id: str
    action: str
    started_at: str
    finished_at: str
    total: int
    completed: int
    pass_count: int
    fail_count: int
    blocked_count: int
    stopped_count: int
    state: str
    iterations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """Return cumulative CPU total and idle ticks from ``/proc/stat``."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu" and len(parts) >= 5:
            try:
                values = [int(value) for value in parts[1:]]
            except ValueError:
                return None
            if not values:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return sum(values), idle
    return None


def calculate_cpu_percent(
    previous: tuple[int, int] | None,
    current: tuple[int, int] | None,
) -> float | None:
    """Calculate CPU busy percentage between two ``/proc/stat`` samples."""
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))


def parse_meminfo(text: str) -> dict[str, float] | None:
    """Parse memory totals from ``/proc/meminfo`` in MiB."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        tokens = raw.split()
        if not tokens:
            continue
        try:
            value = float(tokens[0])
        except ValueError:
            continue
        if len(tokens) > 1 and tokens[1].lower() == "kb":
            value /= 1024.0
        values[key.strip()] = value
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    if total is None or available is None or total <= 0:
        return None
    used = max(0.0, total - available)
    return {
        "total_mb": total,
        "available_mb": available,
        "used_mb": used,
        "used_percent": max(0.0, min(100.0, used / total * 100.0)),
    }


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    try:
        values = [float(item) for item in text.split()[:3]]
    except ValueError:
        return None
    if len(values) != 3:
        return None
    return values[0], values[1], values[2]


def classify_kernel_event(line: str) -> str | None:
    """Classify one kernel/audio line without asserting a test outcome."""
    text = line.casefold()
    if any(token in text for token in ("xrun", "underrun", "overrun", "broken pipe")):
        return "XRUN"
    if "usb" in text and any(token in text for token in ("reset", "resetting")):
        return "USB_RESET"
    if "usb" in text and any(token in text for token in ("disconnect", "disconnected")):
        return "USB_DISCONNECT"
    if "usb" in text and any(token in text for token in ("connect", "new device", "attached")):
        return "USB_RECONNECT"
    if any(token in text for token in ("i/o error", "io error", "cannot submit", "device descriptor")):
        return "IO_ERROR"
    if any(token in text for token in ("cannot open", "no such device", "audio", "snd", "pulseaudio")):
        return "AUDIO_ERROR"
    return None


def _event_severity(category: str) -> str:
    return "WARN" if category in {"XRUN", "USB_RESET"} else "ERROR"


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class AudioRuntimeMonitor(QObject):
    """Poll remote runtime state and persist samples/events for one session."""

    sample_received = Signal(object)
    event_detected = Signal(object)
    state_changed = Signal(str)
    failed = Signal(str)
    finished = Signal(object)

    def __init__(
        self,
        jetson_service,
        evidence_manager=None,
        process_state_provider: Callable[[], Mapping[str, bool]] | None = None,
        interval_sec: float = 2.0,
        slow_check_every: int = 5,
        parent=None,
    ):
        super().__init__(parent)
        self.jetson_service = jetson_service
        self.evidence_manager = evidence_manager
        self.process_state_provider = process_state_provider or (lambda: {})
        self.interval_sec = max(0.5, float(interval_sec))
        self.slow_check_every = max(1, int(slow_check_every))
        self._timer = QTimer(self)
        self._timer.setInterval(round(self.interval_sec * 1000))
        self._timer.timeout.connect(self._poll)
        self._request_id: str | None = None
        self._poll_count = 0
        self._active = False
        self._started_monotonic = 0.0
        self._started_at = ""
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_devices: dict[str, bool] | None = None
        self._seen_kernel_lines: set[str] = set()
        self._seen_process_lines: set[str] = set()
        self._samples: list[AudioRuntimeSample] = []
        self._events: list[AudioRuntimeEvent] = []
        self._last_summary: dict[str, Any] | None = None
        self._xrun_count = 0
        self._usb_reset_count = 0
        self._disconnect_count = 0
        self._reconnect_count = 0
        self._audio_error_count = 0
        self._connection_loss_reported = False
        self.jetson_service.operation_succeeded.connect(self._on_operation_succeeded)
        self.jetson_service.operation_failed.connect(self._on_operation_failed)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def state(self) -> str:
        return "RUNNING" if self._active else "IDLE"

    @property
    def latest_sample(self) -> AudioRuntimeSample | None:
        return self._samples[-1] if self._samples else None

    @property
    def last_summary(self) -> dict[str, Any] | None:
        return self._last_summary

    def start(self) -> bool:
        if self._active:
            return True
        if not self.jetson_service.is_connected:
            self.state_changed.emit("BLOCKED")
            self.failed.emit("Jetson is not connected")
            return False
        self._active = True
        self._request_id = None
        self._poll_count = 0
        self._started_monotonic = time.monotonic()
        self._started_at = _iso_now()
        self._previous_cpu = None
        self._previous_devices = None
        self._seen_kernel_lines.clear()
        self._seen_process_lines.clear()
        self._samples.clear()
        self._events.clear()
        self._last_summary = None
        self._xrun_count = self._usb_reset_count = 0
        self._disconnect_count = self._reconnect_count = 0
        self._audio_error_count = 0
        self._connection_loss_reported = False
        try:
            if self.evidence_manager is not None and self.evidence_manager.is_active:
                self.evidence_manager.start_runtime_evidence()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._active = False
            self.state_changed.emit("FAIL")
            self.failed.emit(f"Runtime evidence unavailable: {exc}")
            return False
        self.state_changed.emit("RUNNING")
        self._poll()
        self._timer.start()
        return True

    def stop(self, state: str = "STOPPED") -> bool:
        if not self._active:
            return False
        self._active = False
        self._timer.stop()
        self._request_id = None
        summary = self._build_summary()
        summary["state"] = state
        self._last_summary = summary
        try:
            if self.evidence_manager is not None and self.evidence_manager.is_active:
                self.evidence_manager.save_runtime_summary(summary)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.failed.emit(f"Runtime summary save failed: {exc}")
        self.state_changed.emit(state)
        self.finished.emit(summary)
        return True

    def record_process_output(self, text: str, source: str = "process") -> None:
        """Feed tracked capture/playback stderr into XRUN/error classification."""
        for line in str(text).splitlines():
            if line in self._seen_process_lines:
                continue
            self._seen_process_lines.add(line)
            category = classify_kernel_event(line)
            if category in {"XRUN", "IO_ERROR", "AUDIO_ERROR"}:
                self._record_event(category, _event_severity(category), source, line)

    def _poll(self) -> None:
        if not self._active or self._request_id is not None:
            return
        if not self.jetson_service.is_connected:
            if not self._connection_loss_reported:
                self._record_event("SYSTEM", "ERROR", "connection", "Jetson connection lost")
                self._connection_loss_reported = True
            return
        if self._connection_loss_reported:
            self._record_event("SYSTEM", "INFO", "connection", "Jetson connection recovered")
            self._connection_loss_reported = False
        self._poll_count += 1
        include_slow = self._poll_count == 1 or self._poll_count % self.slow_check_every == 0
        commands = self._monitor_commands(include_slow)

        async def operation(ssh):
            outputs: dict[str, dict[str, Any]] = {}
            for key, command in commands.items():
                try:
                    result = await ssh.run(command, timeout=10)
                    outputs[key] = {
                        "command": command,
                        "return_code": result.exit_status,
                        "stdout": result.stdout or "",
                        "stderr": result.stderr or "",
                    }
                except Exception as exc:
                    outputs[key] = {
                        "command": command,
                        "return_code": None,
                        "stdout": "",
                        "stderr": f"{type(exc).__name__}: {exc}",
                    }
            kernel = outputs.get("kernel", {})
            if kernel.get("return_code") != 0:
                try:
                    fallback = await ssh.run("dmesg --color=never --time-format iso", timeout=10)
                    outputs["kernel"] = {
                        "command": "dmesg --color=never --time-format iso",
                        "return_code": fallback.exit_status,
                        "stdout": fallback.stdout or "",
                        "stderr": fallback.stderr or "",
                    }
                except Exception as exc:
                    outputs["kernel"] = {
                        "command": "dmesg --color=never --time-format iso",
                        "return_code": None,
                        "stdout": "",
                        "stderr": f"{type(exc).__name__}: {exc}",
                    }
            return outputs

        request_id = self.jetson_service.submit_operation("audio_runtime_monitor", operation)
        if request_id is None:
            self._record_event("SYSTEM", "ERROR", "connection", "Runtime monitor could not submit snapshot")
            return
        self._request_id = request_id

    def _monitor_commands(self, include_slow: bool) -> dict[str, str]:
        commands = {
            "proc_stat": "cat /proc/stat",
            "meminfo": "cat /proc/meminfo",
            "loadavg": "cat /proc/loadavg",
            "pulse_sources": "pactl list short sources",
            "pulse_sinks": "pactl list short sinks",
            "kernel": f"journalctl -k --since {shlex.quote(self._started_at)} --no-pager -o short-iso",
        }
        if include_slow:
            commands["usb"] = "lsusb -d 2886:001a"
            commands["alsa"] = "cat /proc/asound/cards"
        return commands

    def _on_operation_succeeded(self, request_id: str, result: object) -> None:
        if request_id != self._request_id:
            return
        self._request_id = None
        if not self._active or not isinstance(result, dict):
            return
        self._consume_snapshot(result)

    def _on_operation_failed(self, request_id: str, error: str) -> None:
        if request_id != self._request_id:
            return
        self._request_id = None
        if self._active:
            self._record_event("SYSTEM", "ERROR", "monitor", f"Runtime snapshot failed: {error}")

    @staticmethod
    def _stdout(snapshot: Mapping[str, Mapping[str, Any]], key: str) -> str:
        return str(snapshot.get(key, {}).get("stdout", ""))

    def _consume_snapshot(self, snapshot: dict[str, dict[str, Any]]) -> None:
        cpu = parse_proc_stat(self._stdout(snapshot, "proc_stat"))
        cpu_percent = calculate_cpu_percent(self._previous_cpu, cpu)
        self._previous_cpu = cpu
        memory = parse_meminfo(self._stdout(snapshot, "meminfo"))
        loads = parse_loadavg(self._stdout(snapshot, "loadavg"))
        devices = self._device_state(snapshot)
        for key in ("pulse_sources", "pulse_sinks"):
            command_result = snapshot.get(key, {})
            if command_result.get("return_code") not in (None, 0):
                detail = str(command_result.get("stderr") or f"{key} command failed")
                self.record_process_output(detail, key)
        kernel_categories = self._consume_kernel_events(self._stdout(snapshot, "kernel"))
        self._consume_device_transitions(devices, kernel_categories)
        process_state = dict(self.process_state_provider() or {})
        elapsed = max(0.0, time.monotonic() - self._started_monotonic)
        sample = AudioRuntimeSample(
            timestamp=_iso_now(),
            elapsed_sec=elapsed,
            cpu_percent=cpu_percent,
            ram_percent=memory.get("used_percent") if memory else None,
            ram_used_mb=memory.get("used_mb") if memory else None,
            ram_total_mb=memory.get("total_mb") if memory else None,
            load_1m=loads[0] if loads else None,
            load_5m=loads[1] if loads else None,
            load_15m=loads[2] if loads else None,
            usb_present=devices.get("usb_present"),
            alsa_present=devices.get("alsa_present"),
            pulse_source_present=devices.get("pulse_source_present"),
            pulse_sink_present=devices.get("pulse_sink_present"),
            capture_running=bool(process_state.get("capture_running", False)),
            playback_running=bool(process_state.get("playback_running", False)),
            xrun_count=self._xrun_count,
            usb_reset_count=self._usb_reset_count,
            disconnect_count=self._disconnect_count,
            reconnect_count=self._reconnect_count,
            audio_error_count=self._audio_error_count,
        )
        self._samples.append(sample)
        try:
            if self.evidence_manager is not None and self.evidence_manager.is_active:
                self.evidence_manager.append_runtime_sample(sample.as_dict())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.failed.emit(f"Runtime sample save failed: {exc}")
        self.sample_received.emit(sample)

    def _device_state(self, snapshot: Mapping[str, Mapping[str, Any]]) -> dict[str, bool]:
        usb_output = self._stdout(snapshot, "usb")
        alsa_output = self._stdout(snapshot, "alsa")
        source_output = self._stdout(snapshot, "pulse_sources")
        sink_output = self._stdout(snapshot, "pulse_sinks")
        return {
            "usb_present": bool(usb_output.strip()) if "usb" in snapshot else True,
            "alsa_present": parse_respeaker_alsa_card(alsa_output) is not None if "alsa" in snapshot else True,
            "pulse_source_present": bool(parse_pulse_summary(source_output, "", "", "")["source_detected"]),
            "pulse_sink_present": bool(parse_pulse_summary("", sink_output, "", "")["sink_detected"]),
        }

    def _consume_kernel_events(self, output: str) -> set[str]:
        categories: set[str] = set()
        for line in output.splitlines():
            normalized = line.strip()
            if not normalized or normalized in self._seen_kernel_lines:
                continue
            self._seen_kernel_lines.add(normalized)
            category = classify_kernel_event(normalized)
            if category:
                categories.add(category)
                self._record_event(category, _event_severity(category), "kernel", normalized)
        if len(self._seen_kernel_lines) > 10000:
            self._seen_kernel_lines = set(list(self._seen_kernel_lines)[-5000:])
        return categories

    def _consume_device_transitions(self, devices: dict[str, bool], kernel_categories: set[str]) -> None:
        previous = self._previous_devices
        self._previous_devices = devices
        if previous is None:
            return
        if previous.get("usb_present") and not devices.get("usb_present") and "USB_DISCONNECT" not in kernel_categories:
            self._record_event("USB_DISCONNECT", "ERROR", "device", "reSpeaker USB device disappeared")
        elif not previous.get("usb_present") and devices.get("usb_present") and "USB_RECONNECT" not in kernel_categories:
            self._record_event("USB_RECONNECT", "INFO", "device", "reSpeaker USB device recovered")
        for key, label in (
            ("pulse_source_present", "Pulse source"),
            ("pulse_sink_present", "Pulse sink"),
        ):
            if previous.get(key) and not devices.get(key):
                self._record_event("AUDIO_ERROR", "ERROR", "device", f"{label} disappeared")
            elif not previous.get(key) and devices.get(key):
                self._record_event("SYSTEM", "INFO", "device", f"{label} recovered")

    def _record_event(self, category: str, severity: str, source: str, message: str) -> None:
        if category not in RUNTIME_EVENT_CATEGORIES:
            category = "SYSTEM"
        event = AudioRuntimeEvent(_iso_now(), category, severity, source, message)
        self._events.append(event)
        if category == "XRUN":
            self._xrun_count += 1
        elif category == "USB_RESET":
            self._usb_reset_count += 1
        elif category == "USB_DISCONNECT":
            self._disconnect_count += 1
        elif category == "USB_RECONNECT":
            self._reconnect_count += 1
        elif category in {"AUDIO_ERROR", "IO_ERROR"}:
            self._audio_error_count += 1
        try:
            if self.evidence_manager is not None and self.evidence_manager.is_active:
                self.evidence_manager.append_runtime_event(event.as_dict())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.failed.emit(f"Runtime event save failed: {exc}")
        self.event_detected.emit(event)

    def _build_summary(self) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - self._started_monotonic)
        cpu = [item.cpu_percent for item in self._samples if item.cpu_percent is not None]
        ram = [item.ram_percent for item in self._samples if item.ram_percent is not None]
        return {
            "started_at": self._started_at,
            "duration_sec": elapsed,
            "samples": len(self._samples),
            "cpu": {"avg_percent": sum(cpu) / len(cpu) if cpu else None, "max_percent": max(cpu) if cpu else None},
            "ram": {"avg_percent": sum(ram) / len(ram) if ram else None, "max_percent": max(ram) if ram else None},
            "events": {
                "xrun": self._xrun_count,
                "usb_reset": self._usb_reset_count,
                "disconnect": self._disconnect_count,
                "reconnect": self._reconnect_count,
                "audio_error": self._audio_error_count,
            },
        }


class AudioIterationRunner(QObject):
    """Run one reusable Audio action repeatedly without blocking Qt."""

    progress_changed = Signal(int, int)
    iteration_started = Signal(int, int)
    iteration_finished = Signal(object)
    state_changed = Signal(str)
    finished = Signal(object)

    def __init__(self, evidence_manager=None, log_callback: Callable[..., None] | None = None, parent=None):
        super().__init__(parent)
        self.evidence_manager = evidence_manager
        self.log_callback = log_callback
        self._timer: QTimer | None = None
        self._active = False
        self._awaiting_result = False
        self._stop_action: Callable[[], bool] | None = None
        self._start_action: Callable[[int], bool] | None = None
        self._action = "capture"
        self._total = 0
        self._current = 0
        self._interval_sec = 0.0
        self._stop_on_failure = False
        self._started_at = ""
        self._iteration_started_at = 0.0
        self._iteration_started_wall = ""
        self._results: list[AudioIterationResult] = []
        self._campaign_id = "campaign_001"

    @property
    def active(self) -> bool:
        return self._active

    @property
    def campaign_id(self) -> str:
        return self._campaign_id

    def start(
        self,
        *,
        action: str,
        iterations: int,
        interval_sec: float,
        start_action: Callable[[int], bool],
        stop_action: Callable[[], bool] | None = None,
        stop_on_failure: bool = False,
    ) -> bool:
        if self._active or iterations < 1:
            return False
        self._active = True
        self._awaiting_result = False
        self._action = action
        self._total = int(iterations)
        self._current = 0
        self._interval_sec = max(0.0, float(interval_sec))
        self._start_action = start_action
        self._stop_action = stop_action
        self._stop_on_failure = bool(stop_on_failure)
        self._started_at = _iso_now()
        self._results = []
        if self.evidence_manager is not None and hasattr(self.evidence_manager, "next_reliability_campaign_id"):
            self._campaign_id = self.evidence_manager.next_reliability_campaign_id()
        else:
            self._campaign_id = "campaign_001"
        self._log("START", f"Campaign: {action} x{self._total}")
        self.state_changed.emit("RUNNING")
        self._start_next()
        return True

    def handle_action_result(self, result: AudioActionResult) -> None:
        if not self._active or not self._awaiting_result or result.action != self._action:
            return
        self._awaiting_result = False
        finished_at = _iso_now()
        status = result.state if result.state in {"PASS", "FAIL", "BLOCKED", "STOPPED"} else ("PASS" if result.success else "FAIL")
        iteration = AudioIterationResult(
            iteration=self._current,
            started_at=self._iteration_started_wall,
            finished_at=finished_at,
            duration_sec=max(0.0, time.monotonic() - self._iteration_started_at),
            status=status,
            action_result=result.as_dict(),
            capture_file=result.data.get("local_path") or result.data.get("output_file"),
            analysis_file=result.data.get("analysis_file"),
            error=None if result.success else result.message,
        )
        self._results.append(iteration)
        self.iteration_finished.emit(iteration)
        self.progress_changed.emit(len(self._results), self._total)
        self._log(status, f"Cycle {self._current}/{self._total}" + (f": {result.message}" if not result.success else ""))
        if not result.success and self._stop_on_failure:
            self._finish("FAIL")
        elif self._current >= self._total:
            self._finish("PASS" if all(item.status == "PASS" for item in self._results) else "FAIL")
        elif self._interval_sec:
            QTimer.singleShot(round(self._interval_sec * 1000), self._start_next)
        else:
            self._start_next()

    def stop(self) -> bool:
        if not self._active:
            return False
        if self._stop_action is not None:
            self._stop_action()
        if self._awaiting_result:
            now = _iso_now()
            stopped = AudioIterationResult(
                iteration=self._current,
                started_at=self._iteration_started_wall,
                finished_at=now,
                duration_sec=max(0.0, time.monotonic() - self._iteration_started_at),
                status="STOPPED",
                error="Campaign stopped by user",
            )
            self._results.append(stopped)
            self.iteration_finished.emit(stopped)
        self._finish("STOPPED")
        return True

    def _start_next(self) -> None:
        if not self._active or self._start_action is None:
            return
        if self._current >= self._total:
            self._finish("PASS")
            return
        self._current += 1
        self._iteration_started_at = time.monotonic()
        self._iteration_started_wall = _iso_now()
        self._awaiting_result = True
        self.iteration_started.emit(self._current, self._total)
        self._log("START", f"Cycle {self._current}/{self._total}")
        started = self._start_action(self._current)
        if not started and self._awaiting_result:
            self._awaiting_result = False
            result = AudioIterationResult(
                iteration=self._current,
                started_at=_iso_now(),
                finished_at=_iso_now(),
                duration_sec=0.0,
                status="BLOCKED",
                error="Action could not be started",
            )
            self._results.append(result)
            self.iteration_finished.emit(result)
            self.progress_changed.emit(len(self._results), self._total)
            if self._current >= self._total or self._stop_on_failure:
                self._finish("BLOCKED" if self._current >= self._total else "FAIL")
            else:
                QTimer.singleShot(round(self._interval_sec * 1000), self._start_next)

    def _finish(self, state: str) -> None:
        if not self._active:
            return
        self._active = False
        self._awaiting_result = False
        result = AudioIterationCampaignResult(
            campaign_id=self._campaign_id,
            action=self._action,
            started_at=self._started_at,
            finished_at=_iso_now(),
            total=self._total,
            completed=len(self._results),
            pass_count=sum(item.status == "PASS" for item in self._results),
            fail_count=sum(item.status == "FAIL" for item in self._results),
            blocked_count=sum(item.status == "BLOCKED" for item in self._results),
            stopped_count=sum(item.status == "STOPPED" for item in self._results),
            state=state,
            iterations=[item.as_dict() for item in self._results],
        )
        try:
            if self.evidence_manager is not None and self.evidence_manager.is_active:
                for item in self._results:
                    self.evidence_manager.record_reliability_iteration(self._campaign_id, item.as_dict())
                self.evidence_manager.record_reliability_campaign(result.as_dict())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._log("ERROR", f"Reliability evidence save failed: {exc}")
        self._log(state, f"Campaign complete: {result.completed}/{result.total}")
        self.state_changed.emit(state)
        self.finished.emit(result)

    def _log(self, level: str, message: str) -> None:
        if self.log_callback is not None:
            self.log_callback("ITERATION", level, message)


class AudioDurationRunner(QObject):
    """Small duration-runner foundation for future long Audio actions."""

    progress_changed = Signal(float, float, float)
    state_changed = Signal(str)
    finished = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._active = False
        self._duration = 0.0
        self._started = 0.0
        self._stop_action: Callable[[], bool] | None = None

    @property
    def active(self) -> bool:
        return self._active

    def start(
        self,
        duration_sec: float,
        start_action: Callable[[], bool],
        stop_action: Callable[[], bool] | None = None,
    ) -> bool:
        if self._active or duration_sec <= 0:
            return False
        if not start_action():
            self.state_changed.emit("BLOCKED")
            self.finished.emit({"state": "BLOCKED", "duration_sec": 0.0})
            return False
        self._active = True
        self._duration = float(duration_sec)
        self._started = time.monotonic()
        self._stop_action = stop_action
        self._timer.start()
        self.state_changed.emit("RUNNING")
        return True

    def stop(self) -> bool:
        if not self._active:
            return False
        if self._stop_action is not None:
            self._stop_action()
        self._finish("STOPPED")
        return True

    def _tick(self) -> None:
        if not self._active:
            return
        elapsed = max(0.0, time.monotonic() - self._started)
        remaining = max(0.0, self._duration - elapsed)
        percent = min(100.0, elapsed / self._duration * 100.0)
        self.progress_changed.emit(elapsed, remaining, percent)
        if elapsed >= self._duration:
            if self._stop_action is not None:
                self._stop_action()
            self._finish("PASS")

    def _finish(self, state: str) -> None:
        if not self._active:
            return
        elapsed = max(0.0, time.monotonic() - self._started)
        self._active = False
        self._timer.stop()
        self.state_changed.emit(state)
        self.finished.emit({"state": state, "duration_sec": elapsed})
