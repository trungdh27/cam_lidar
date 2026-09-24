"""Local evidence collection for manual Audio test sessions.

The audio controls remain owned by :mod:`audio_manager`.  This module only
records the observable state and operation results while a session is active.
Remote paths are kept as references; evidence itself is written locally so it
does not require a second SSH/file-transfer implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import csv
from typing import Any


class SessionState(str, Enum):
    """Lifecycle state of an Audio evidence session."""

    NONE = "NO_SESSION"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True)
class AudioOperationRecord:
    """One observable Audio operation or manual note."""

    timestamp: str
    operation: str
    status: str
    details: dict[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "operation": self.operation,
            "status": self.status,
            "details": self.details,
        }
        if self.error:
            result["error"] = self.error
        return result


def _now() -> datetime:
    return datetime.now().astimezone()


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _safe_json_value(value: Any) -> Any:
    """Remove credential-shaped fields before writing connection metadata."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"password|passwd|secret|token|credential|private[_ -]?key", key_text, re.I):
                continue
            safe[key_text] = _safe_json_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class AudioEvidenceManager:
    """Collects local, append-safe evidence for one manual Audio session."""

    def __init__(self, root_dir: str | Path | None = None):
        self.root_dir = Path(root_dir or (Path.home() / "audio_test_evidence")).expanduser()
        self.state = SessionState.NONE
        self.session_id: str | None = None
        self.session_dir: Path | None = None
        self.start_time: str | None = None
        self.end_time: str | None = None
        self._summary: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def is_active(self) -> bool:
        return self.state in {SessionState.ACTIVE, SessionState.INTERRUPTED}

    @property
    def is_interrupted(self) -> bool:
        return self.state == SessionState.INTERRUPTED

    @property
    def evidence_path(self) -> Path | None:
        return self.session_dir

    @property
    def local_display_path(self) -> str:
        if self.session_dir is None:
            return "-"
        try:
            return "~" + str(self.session_dir.relative_to(Path.home()))
        except ValueError:
            return str(self.session_dir)

    @property
    def last_capture_path(self) -> Path | None:
        value = self._summary.get("last_capture_path")
        return Path(value) if value else None

    @property
    def baseline_path(self) -> Path | None:
        return self.session_dir / "baseline" if self.session_dir else None

    @property
    def capture_path(self) -> Path | None:
        return self.session_dir / "capture" if self.session_dir else None

    @property
    def analysis_path(self) -> Path | None:
        return self.session_dir / "analysis" if self.session_dir else None

    @property
    def speaker_channel_path(self) -> Path | None:
        return self.session_dir / "speaker_channel" if self.session_dir else None

    @property
    def monitor_path(self) -> Path | None:
        return self.session_dir / "monitor" if self.session_dir else None

    @property
    def reliability_path(self) -> Path | None:
        return self.session_dir / "reliability" if self.session_dir else None

    @staticmethod
    def generate_session_id(now: datetime | None = None, prefix: str = "AUDIO") -> str:
        return f"{prefix}_{(now or _now()).strftime('%Y%m%d_%H%M%S')}"

    def _new_session_id(self, prefix: str = "AUDIO") -> str:
        base = self.generate_session_id(prefix=prefix)
        candidate = base
        suffix = 1
        while (self.root_dir / candidate).exists():
            candidate = f"{base}_{suffix:02d}"
            suffix += 1
        return candidate

    def start_session(
        self,
        *,
        host: str = "",
        jetson_info: dict[str, Any] | None = None,
        jetson_connected: bool = True,
        default_output: str | None = None,
        default_input: str | None = None,
        default_output_display: str | None = None,
        default_input_display: str | None = None,
        speaker_volume: int | None = None,
        speaker_muted: bool | None = None,
        session_prefix: str = "AUDIO",
    ) -> str:
        if self.is_active:
            raise RuntimeError("An Audio evidence session is already active.")
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = self._new_session_id(session_prefix)
        self.session_dir = self.root_dir / self.session_id
        self.session_dir.mkdir(parents=False, exist_ok=False)
        self.start_time = _timestamp()
        self.end_time = None
        self.state = SessionState.ACTIVE
        self._summary = {
            "session_id": self.session_id,
            "start_time": self.start_time,
            "end_time": None,
            "host": host,
            "jetson_connected": bool(jetson_connected),
            "jetson_info": _safe_json_value(jetson_info or {}),
            "evidence_location": "local_test_laptop",
            "local_evidence_dir": str(self.session_dir),
            "remote_evidence_dir": None,
            "default_output": default_output,
            "default_input": default_input,
            "default_output_display": default_output_display,
            "default_input_display": default_input_display,
            "speaker_volume": speaker_volume,
            "speaker_muted": speaker_muted,
            "operation_count": 0,
            "notes_count": 0,
            "recordings": [],
            "captures": [],
            "analyses": [],
            "last_capture_path": None,
            "latest_analysis": None,
            "baseline": {"collected": False, "path": "baseline/"},
            "runtime": None,
            "reliability": {"campaigns": []},
            "result_summary": {"success": 0, "failed": 0},
            "session_status": self.state.value,
            "application_closed": False,
        }
        self._write_text("session.log", f"[{_now().strftime('%H:%M:%S')}] Session started: {self.session_id}\n")
        self._write_text("execution.log", "")
        self._write_json("session.json", self._summary)
        self._write_json("summary.json", self._summary_view())
        self._append_jsonl("operations.jsonl", {
            "timestamp": self.start_time,
            "operation": "SESSION_START",
            "status": "SUCCESS",
            "details": {"session_id": self.session_id},
        })
        return self.session_id

    def start_automation_session(self, **kwargs: Any) -> str:
        """Start the automatically managed session used by Automated Audio."""
        kwargs["session_prefix"] = "AUTO_AUDIO"
        return self.start_session(**kwargs)

    def _require_session(self) -> Path:
        if self.state == SessionState.NONE or self.session_dir is None:
            raise RuntimeError("No active Audio evidence session.")
        return self.session_dir

    def _write_text(self, filename: str, content: str) -> None:
        directory = self._require_session()
        path = directory / filename
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _write_text_at(directory: Path, filename: str, content: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(content, encoding="utf-8")
        return path

    def _write_json(self, filename: str, value: Any) -> None:
        directory = self._require_session()
        target = directory / filename
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_safe_json_value(value), handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
            os.replace(temporary_name, target)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _append_jsonl(self, filename: str, value: dict[str, Any]) -> None:
        directory = self._require_session()
        with (directory / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_safe_json_value(value), ensure_ascii=False) + "\n")

    def _save_summary(self) -> None:
        if self.is_active or self.state in {SessionState.COMPLETED, SessionState.INTERRUPTED}:
            self._write_json("session.json", self._summary)
            self._write_json("summary.json", self._summary_view())

    def _summary_view(self) -> dict[str, Any]:
        return {
            "session_id": self._summary.get("session_id"),
            "started_at": self._summary.get("start_time"),
            "ended_at": self._summary.get("end_time"),
            "session_status": self._summary.get("session_status"),
            "host": self._summary.get("host"),
            "audio_device": self._summary.get("audio_device"),
            "vid_pid": self._summary.get("vid_pid"),
            "baseline": self._summary.get("baseline", {"collected": False, "path": "baseline/"}),
            "captures": self._summary.get("captures", []),
            "analyses": self._summary.get("analyses", []),
            "latest_analysis": self._summary.get("latest_analysis"),
            "runtime": self._summary.get("runtime"),
            "reliability": self._summary.get("reliability", {"campaigns": []}),
        }

    def start_runtime_evidence(self, fieldnames: tuple[str, ...] | None = None) -> Path:
        """Create runtime evidence files lazily for the active session."""
        directory = self.monitor_path
        if directory is None:
            raise RuntimeError("No active Audio evidence session.")
        directory.mkdir(parents=True, exist_ok=True)
        csv_path = directory / "runtime.csv"
        fields = fieldnames or (
            "timestamp", "elapsed_sec", "cpu_percent", "ram_percent", "ram_used_mb",
            "ram_total_mb", "load_1m", "load_5m", "load_15m", "usb_present",
            "alsa_present", "pulse_source_present", "pulse_sink_present",
            "capture_running", "playback_running", "xrun_count", "usb_reset_count",
            "disconnect_count", "reconnect_count", "audio_error_count",
        )
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(fields)
        (directory / "events.log").touch(exist_ok=True)
        self._summary["runtime"] = {
            "runtime_csv": "monitor/runtime.csv",
            "events_log": "monitor/events.log",
            "summary": "monitor/runtime_summary.json",
        }
        self._save_summary()
        return directory

    def append_runtime_sample(self, sample: dict[str, Any]) -> None:
        if not self.is_active:
            return
        directory = self.monitor_path
        if directory is None:
            return
        if not (directory / "runtime.csv").exists():
            self.start_runtime_evidence(tuple(sample.keys()))
        with (directory / "runtime.csv").open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sample.keys()), extrasaction="ignore")
            writer.writerow({key: self._csv_value(value) for key, value in sample.items()})

    def append_runtime_event(self, event: dict[str, Any]) -> None:
        if not self.is_active or self.monitor_path is None:
            return
        timestamp = event.get("timestamp", _timestamp())
        category = event.get("category", "SYSTEM")
        message = event.get("message", "")
        with (self.monitor_path / "events.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} [{category}] {message}\n")

    def save_runtime_summary(self, summary: dict[str, Any]) -> Path | None:
        if not self.is_active or self.monitor_path is None:
            return None
        target = self.monitor_path / "runtime_summary.json"
        self._write_json_at(target, summary)
        self._summary["runtime"] = {
            "runtime_csv": "monitor/runtime.csv",
            "events_log": "monitor/events.log",
            "summary": "monitor/runtime_summary.json",
        }
        self._save_summary()
        return target

    def next_reliability_campaign_id(self) -> str:
        existing = self.reliability_path
        if existing is None:
            return "campaign_001"
        existing.mkdir(parents=True, exist_ok=True)
        index = 1
        while (existing / f"campaign_{index:03d}.json").exists():
            index += 1
        return f"campaign_{index:03d}"

    def record_reliability_iteration(self, campaign_id: str, iteration: dict[str, Any]) -> Path | None:
        if not self.is_active or self.reliability_path is None:
            return None
        iteration_number = int(iteration.get("iteration", 0) or 0)
        target = self.reliability_path / f"{campaign_id}_iteration_{iteration_number:03d}.json"
        self._write_json_at(target, iteration)
        return target

    def record_reliability_campaign(self, campaign: dict[str, Any]) -> Path | None:
        if not self.is_active or self.reliability_path is None:
            return None
        campaign_id = str(campaign.get("campaign_id", self.next_reliability_campaign_id()))
        target = self.reliability_path / f"{campaign_id}.json"
        references = []
        for item in campaign.get("iterations", []):
            number = int(item.get("iteration", 0) or 0)
            references.append(f"reliability/{campaign_id}_iteration_{number:03d}.json")
        payload = dict(campaign)
        payload["iteration_files"] = references
        self._write_json_at(target, payload)
        self._summary.setdefault("reliability", {"campaigns": []})
        self._summary["reliability"].setdefault("campaigns", []).append(str(target.relative_to(self._require_session())))
        self.record_operation("RELIABILITY_CAMPAIGN", "SUCCESS", details={"file": str(target.relative_to(self._require_session()))})
        return target

    @staticmethod
    def _csv_value(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return value

    def record_execution_event(self, event: Any) -> None:
        """Persist one centralized Audio execution-log event in this session."""
        if not self.is_active:
            return
        line = event.format_line() if hasattr(event, "format_line") else str(event)
        with self._lock:
            directory = self._require_session()
            execution_log = directory / "execution.log"
            if not execution_log.exists():
                execution_log.write_text("", encoding="utf-8")
            with execution_log.open("a", encoding="utf-8") as handle:
                handle.write(line.rstrip() + "\n")

    def record_speaker_channel_execution(
        self,
        channel: str,
        execution: dict[str, Any],
        *,
        summary: dict[str, Any],
    ) -> None:
        """Persist channel execution and the current semi-automated result."""
        if not self.is_active or self.speaker_channel_path is None:
            return
        directory = self.speaker_channel_path
        directory.mkdir(parents=True, exist_ok=True)
        normalized = str(channel).lower()
        self._write_json_at(directory / f"{normalized}_execution.txt", execution)
        pulse = execution.get("pulse_state") or {}
        self._write_json_at(directory / f"pulse_state_before_{normalized}.txt", pulse)
        self._write_json_at(directory / "mixer_state.txt", execution.get("mixer") or {})
        self._write_json_at(directory / "result.json", summary)
        self.record_operation(
            "SPEAKER_CHANNEL_EXECUTION",
            "SUCCESS" if execution.get("execution_status") == "PASS" else "FAILED",
            details={"channel": str(channel).upper(), "file": f"speaker_channel/{normalized}_execution.txt", "command": execution.get("command")},
        )

    def record_speaker_channel_verification(
        self,
        channel: str,
        verification: str,
        *,
        summary: dict[str, Any],
    ) -> None:
        """Atomically update result.json after an operator observation."""
        if not self.is_active or self.speaker_channel_path is None:
            return
        directory = self.speaker_channel_path
        directory.mkdir(parents=True, exist_ok=True)
        self._write_json_at(directory / "result.json", summary)
        self.record_operation(
            "SPEAKER_CHANNEL_VERIFICATION",
            "SUCCESS" if verification == "HEARD_CORRECT_SIDE" else "FAILED" if verification in {"HEARD_WRONG_SIDE", "BOTH_SPEAKERS", "NO_SOUND"} else "INFO",
            details={"channel": str(channel).upper(), "physical_verification": verification},
        )

    def next_capture_name(self, prefix: str = "capture") -> str:
        """Return a deterministic non-overwriting capture filename."""
        directory = self.capture_path
        if directory is None:
            raise RuntimeError("No active Audio evidence session.")
        directory.mkdir(parents=True, exist_ok=True)
        index = 1
        safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix).strip("_") or "capture"
        while (directory / f"{safe_prefix}_{index:03d}.wav").exists():
            index += 1
        return f"{safe_prefix}_{index:03d}.wav"

    def record_capture(
        self,
        *,
        remote_path: str,
        local_path: str | Path | None,
        size_bytes: int | None = None,
        status: str = "SUCCESS",
        error: str | None = None,
    ) -> None:
        if not self.is_active:
            return
        local_text = str(Path(local_path)) if local_path else None
        record = {
            "remote_path": remote_path,
            "local_path": local_text,
            "file": Path(local_text).relative_to(self._require_session()).as_posix() if local_text and Path(local_text).is_relative_to(self._require_session()) else None,
            "size_bytes": size_bytes,
        }
        self._summary.setdefault("captures", []).append(record)
        if local_text:
            self._summary["last_capture_path"] = local_text
        self.record_operation("CAPTURE_EVIDENCE", status, details=record, error=error)

    def record_analysis(self, analysis: dict[str, Any], source_path: str | None = None) -> Path | None:
        if not self.is_active:
            return None
        directory = self.analysis_path
        if directory is None:
            return None
        source = source_path or str(analysis.get("path") or analysis.get("source_path") or "capture.wav")
        stem = Path(source).stem or "capture"
        target = directory / f"{stem}.json"
        suffix = 1
        while target.exists() and target.read_text(encoding="utf-8").strip():
            target = directory / f"{stem}_{suffix:02d}.json"
            suffix += 1
        payload = {
            "timestamp": _timestamp(),
            "session_id": self.session_id,
            "source_wav": source,
            "metrics": _safe_json_value(analysis),
        }
        self._write_json_at(target, payload)
        reference = {"file": str(target.relative_to(self._require_session())), "source_wav": source}
        self._summary.setdefault("analyses", []).append(reference)
        self._summary["latest_analysis"] = _safe_json_value(analysis)
        self.record_operation("WAV_ANALYSIS", "SUCCESS", details=reference)
        return target

    def _write_json_at(self, target: Path, value: Any) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_safe_json_value(value), handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
            os.replace(temporary_name, target)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def update_state(
        self,
        *,
        default_output: str | None = None,
        default_input: str | None = None,
        default_output_display: str | None = None,
        default_input_display: str | None = None,
        speaker_volume: int | None = None,
        speaker_muted: bool | None = None,
    ) -> None:
        if not self.is_active:
            return
        if default_output is not None:
            self._summary["default_output"] = default_output
        if default_input is not None:
            self._summary["default_input"] = default_input
        if default_output_display is not None:
            self._summary["default_output_display"] = default_output_display
        if default_input_display is not None:
            self._summary["default_input_display"] = default_input_display
        if speaker_volume is not None:
            self._summary["speaker_volume"] = speaker_volume
        if speaker_muted is not None:
            self._summary["speaker_muted"] = speaker_muted
        self._save_summary()

    def log_message(self, message: str, timestamp: datetime | None = None) -> None:
        if not self.is_active:
            return
        directory = self._require_session()
        stamp = (timestamp or _now()).strftime("%H:%M:%S")
        with (directory / "session.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")

    def record_operation(
        self,
        operation: str,
        status: str,
        *,
        details: dict[str, Any] | None = None,
        error: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        if not self.is_active:
            return
        record = AudioOperationRecord(
            timestamp=_timestamp(timestamp),
            operation=operation,
            status=status.upper(),
            details=_safe_json_value(details or {}),
            error=error,
        )
        self._append_jsonl("operations.jsonl", record.as_dict())
        self._summary["operation_count"] += 1
        if record.status == "SUCCESS":
            self._summary["result_summary"]["success"] += 1
        elif record.status == "FAILED":
            self._summary["result_summary"]["failed"] += 1
        if record.operation in {"MANUAL_NOTE", "NOTE"}:
            self._summary["notes_count"] += 1
        remote_path = record.details.get("remote_path")
        if remote_path and record.operation.startswith("RECORDING"):
            recording = {"remote_path": remote_path}
            if "size_bytes" in record.details:
                recording["size_bytes"] = record.details["size_bytes"]
            if recording not in self._summary["recordings"]:
                self._summary["recordings"].append(recording)
        self._save_summary()

    def record_note(self, note: str) -> bool:
        note = str(note).strip()
        if not note or not self.is_active:
            return False
        self.record_operation("MANUAL_NOTE", "INFO", details={"note": note})
        return True

    def record_baseline(self, snapshot: Any) -> None:
        """Persist the existing discovery snapshot as readable evidence."""
        if not self.is_active:
            return
        if isinstance(snapshot, dict) and "commands" in snapshot:
            self.record_baseline_commands(snapshot)
            return
        raw = dict(getattr(snapshot, "raw_info", None) or {})
        errors = dict(getattr(snapshot, "command_errors", None) or {})
        device_summary = {
            "default_sink": getattr(snapshot, "default_sink", None),
            "default_source": getattr(snapshot, "default_source", None),
            "sinks": [getattr(item, "__dict__", {}) for item in getattr(snapshot, "sinks", [])],
            "sources": [getattr(item, "__dict__", {}) for item in getattr(snapshot, "sources", [])],
            "alsa_cards": list(getattr(snapshot, "alsa_cards", []) or []),
            "playback_devices": list(getattr(snapshot, "playback_devices", []) or []),
            "capture_devices": list(getattr(snapshot, "capture_devices", []) or []),
            "command_errors": errors,
        }
        self._write_json("device_snapshot.txt", device_summary)
        pulse_labels = {"pactl info", "pactl list sinks", "pactl list sources", "pactl list short sinks", "pactl list short sources"}
        alsa_labels = {"ALSA cards", "ALSA playback devices", "ALSA capture devices"}
        self._write_text("pulse_audio.txt", self._format_command_outputs(raw, errors, pulse_labels))
        self._write_text("alsa_devices.txt", self._format_command_outputs(raw, errors, alsa_labels))
        self.update_state(
            default_output=getattr(snapshot, "default_sink", None),
            default_input=getattr(snapshot, "default_source", None),
        )
        self.record_operation(
            "DEVICE_SNAPSHOT",
            "SUCCESS" if not errors else "FAILED",
            details={"command_errors": errors, "default_output": getattr(snapshot, "default_sink", None), "default_input": getattr(snapshot, "default_source", None)},
            error="; ".join(f"{key}: {value}" for key, value in errors.items()) if errors else None,
        )

    def record_baseline_commands(self, baseline: dict[str, Any]) -> None:
        """Write raw baseline command evidence using stable, reviewable names."""
        if not self.is_active:
            return
        directory = self.baseline_path
        if directory is None:
            return
        commands = baseline.get("commands", {})
        filenames = {
            "lsusb": "usb.txt",
            "lsusb -d 2886:001a": "usb.txt",
            "cat /proc/asound/cards": "alsa_cards.txt",
            "arecord -l": "arecord_devices.txt",
            "aplay -l": "aplay_devices.txt",
            "pactl list short sources": "pulse_sources.txt",
            "pactl list short sinks": "pulse_sinks.txt",
            "pactl get-default-source": "routing.txt",
            "pactl get-default-sink": "routing.txt",
            "pactl info": "routing.txt",
            "amixer -c <dynamic> scontrols": "mixer.txt",
        }
        mixer_records: list[dict[str, Any]] = []
        written: set[str] = set()
        for key, record in commands.items():
            if not isinstance(record, dict):
                continue
            command = str(record.get("command", key))
            filename = filenames.get(key)
            if filename is None:
                if command.startswith("amixer"):
                    filename = "mixer.txt"
                else:
                    continue
            formatted = self._format_command_record(record)
            if filename == "mixer.txt":
                mixer_records.append(record)
                continue
            mode = "a" if filename in written else "w"
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / filename).open(mode, encoding="utf-8") as handle:
                handle.write(formatted)
            written.add(filename)
        if mixer_records:
            self._write_text_at(
                directory,
                "mixer.txt",
                "\n".join(self._format_command_record(item) for item in mixer_records),
            )
        self._summary["baseline"] = {
            "collected": bool(commands),
            "path": "baseline/",
            "command_count": len(commands),
            "errors": baseline.get("errors", []),
        }
        usb_data = baseline.get("usb")
        if isinstance(usb_data, dict):
            self._summary["audio_device"] = usb_data.get("name")
            self._summary["vid_pid"] = usb_data.get("vid_pid")
        self.record_operation(
            "BASELINE",
            "SUCCESS" if not baseline.get("errors") else "FAILED",
            details={"path": "baseline/", "command_count": len(commands), "errors": baseline.get("errors", [])},
            error="; ".join(str(item) for item in baseline.get("errors", [])) or None,
        )

    @staticmethod
    def _format_command_record(record: dict[str, Any]) -> str:
        return (
            f"===== TIMESTAMP =====\n{record.get('timestamp', _timestamp())}\n\n"
            f"===== COMMAND =====\n{record.get('command', '')}\n\n"
            f"===== RETURN CODE =====\n{record.get('return_code')}\n\n"
            f"===== STDOUT =====\n{record.get('stdout', '')}\n\n"
            f"===== STDERR =====\n{record.get('stderr', '')}\n\n"
        )

    @staticmethod
    def _format_command_outputs(raw: dict[str, str], errors: dict[str, str], labels: set[str]) -> str:
        sections: list[str] = []
        for label, output in raw.items():
            if label not in labels:
                continue
            sections.append(f"### {label}\n{output.rstrip()}\n")
            if label in errors:
                sections.append(f"ERROR: {errors[label]}\n")
        return "\n".join(sections) or "No command output captured.\n"

    def mark_interrupted(self, reason: str) -> None:
        if not self.is_active:
            return
        self.state = SessionState.INTERRUPTED
        self._summary["session_status"] = self.state.value
        self._summary["jetson_connected"] = False
        self.record_operation("SESSION_INTERRUPTED", "FAILED", details={"reason": reason}, error=reason)
        self.log_message(reason)
        self._save_summary()

    def end_session(self, *, application_closed: bool = False) -> bool:
        if not self.is_active:
            return False
        self.end_time = _timestamp()
        self.log_message("Session ended" if not application_closed else "Application closed; session finalized")
        if application_closed:
            self._summary["application_closed"] = True
        if self.state != SessionState.INTERRUPTED:
            self.state = SessionState.COMPLETED
        self._summary["end_time"] = self.end_time
        self._summary["session_status"] = self.state.value
        self._save_summary()
        return True
