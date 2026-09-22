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

    @staticmethod
    def generate_session_id(now: datetime | None = None) -> str:
        return f"AUDIO_{(now or _now()).strftime('%Y%m%d_%H%M%S')}"

    def _new_session_id(self) -> str:
        base = self.generate_session_id()
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
    ) -> str:
        if self.is_active:
            raise RuntimeError("An Audio evidence session is already active.")
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = self._new_session_id()
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
            "result_summary": {"success": 0, "failed": 0},
            "session_status": self.state.value,
            "application_closed": False,
        }
        self._write_text("session.log", f"[{_now().strftime('%H:%M:%S')}] Session started: {self.session_id}\n")
        self._write_json("session.json", self._summary)
        self._append_jsonl("operations.jsonl", {
            "timestamp": self.start_time,
            "operation": "SESSION_START",
            "status": "SUCCESS",
            "details": {"session_id": self.session_id},
        })
        return self.session_id

    def _require_session(self) -> Path:
        if self.state == SessionState.NONE or self.session_dir is None:
            raise RuntimeError("No active Audio evidence session.")
        return self.session_dir

    def _write_text(self, filename: str, content: str) -> None:
        directory = self._require_session()
        path = directory / filename
        path.write_text(content, encoding="utf-8")

    def _write_json(self, filename: str, value: Any) -> None:
        directory = self._require_session()
        target = directory / filename
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, ensure_ascii=False)
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
