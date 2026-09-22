"""Reusable models, parsers, logging, and WAV analysis for Audio automation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import io
import json
import math
from pathlib import Path
import re
import struct
import threading
import wave
from collections.abc import Callable, Mapping
from typing import Any

from desktop_app.audio.audio_models import parse_pactl_short_devices


AUTOMATION_STATES = ("IDLE", "RUNNING", "PASS", "FAIL", "BLOCKED", "STOPPED")


@dataclass(frozen=True)
class AudioActionResult:
    """A stable result contract for future Audio test-case actions."""

    success: bool
    action: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    return_code: int | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime = field(default_factory=datetime.now)
    duration_sec: float = 0.0
    state: str = "PASS"
    requires_manual_verification: bool = False
    verification_status: str = "NOT_REQUIRED"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["started_at"] = self.started_at.isoformat(timespec="milliseconds")
        result["finished_at"] = self.finished_at.isoformat(timespec="milliseconds")
        return result


@dataclass(frozen=True)
class AudioLogEvent:
    """Structured event rendered by the automated Audio execution log."""

    timestamp: datetime
    module: str
    level: str
    message: str
    details: dict[str, Any] | None = None

    def format_line(self) -> str:
        stamp = self.timestamp.strftime("%H:%M:%S")
        return f"{stamp} [{self.module}] [{self.level}] {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(timespec="milliseconds"),
            "module": self.module,
            "level": self.level,
            "message": self.message,
            "details": self.details,
        }


class AudioExecutionLogger:
    """Thread-safe structured logger shared by the Audio automated page."""

    def __init__(self):
        self._events: list[AudioLogEvent] = []
        self._results: list[AudioActionResult] = []
        self._listeners: list[Callable[[AudioLogEvent], None]] = []
        self._lock = threading.RLock()
        self.session_started_at = datetime.now()

    @property
    def events(self) -> tuple[AudioLogEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def results(self) -> tuple[AudioActionResult, ...]:
        with self._lock:
            return tuple(self._results)

    def subscribe(self, listener: Callable[[AudioLogEvent], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[AudioLogEvent], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def log(
        self,
        module: str,
        level: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> AudioLogEvent:
        event = AudioLogEvent(
            timestamp=datetime.now(),
            module=module,
            level=level,
            message=message,
            details=dict(details) if details else None,
        )
        with self._lock:
            self._events.append(event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(event)
        return event

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._results.clear()
            self.session_started_at = datetime.now()

    def record_result(self, result: AudioActionResult) -> None:
        with self._lock:
            self._results.append(result)

    def save_session(self, directory: str | Path) -> Path:
        """Save the current event stream and a compact JSON summary."""
        target = Path(directory).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        events = self.events
        (target / "execution.log").write_text(
            "\n".join(event.format_line() for event in events) + ("\n" if events else ""),
            encoding="utf-8",
        )
        (target / "summary.json").write_text(
            json.dumps(
                {
                    "session_started_at": self.session_started_at.isoformat(),
                    "event_count": len(events),
                    "events": [event.as_dict() for event in events],
                    "action_results": [result.as_dict() for result in self.results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return target


def make_action_result(
    *,
    action: str,
    success: bool,
    message: str,
    started_at: datetime,
    data: Mapping[str, Any] | None = None,
    stdout: str = "",
    stderr: str = "",
    return_code: int | None = None,
    state: str | None = None,
    requires_manual_verification: bool = False,
    verification_status: str = "NOT_REQUIRED",
) -> AudioActionResult:
    finished_at = datetime.now()
    return AudioActionResult(
        success=success,
        action=action,
        message=message,
        data=dict(data or {}),
        stdout=stdout,
        stderr=stderr,
        return_code=return_code,
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=max(0.0, (finished_at - started_at).total_seconds()),
        state=state or ("PASS" if success else "FAIL"),
        requires_manual_verification=requires_manual_verification,
        verification_status=verification_status,
    )


def parse_usb_devices(output: str, target_vid_pid: str = "2886:001a") -> dict[str, Any]:
    """Parse ``lsusb`` into a small target-device structure."""
    target = target_vid_pid.casefold()
    devices: list[dict[str, str]] = []
    for line in output.splitlines():
        match = re.search(r"ID\s+([0-9a-f]{4}:[0-9a-f]{4})\s+(.+)$", line, re.IGNORECASE)
        if match:
            devices.append({"vid_pid": match.group(1).lower(), "name": match.group(2).strip()})
    match = next((device for device in devices if device["vid_pid"] == target), None)
    return {
        "detected": match is not None,
        "vid_pid": match["vid_pid"] if match else target_vid_pid,
        "name": match["name"] if match else None,
        "devices": devices,
    }


def parse_pulse_summary(
    sources_output: str,
    sinks_output: str,
    default_source_output: str,
    default_sink_output: str,
) -> dict[str, Any]:
    """Parse PulseAudio-compatible source/sink and default-routing output."""
    sources = parse_pactl_short_devices(sources_output)
    sinks = parse_pactl_short_devices(sinks_output)
    default_source = default_source_output.strip() or None
    default_sink = default_sink_output.strip() or None
    return {
        "source_detected": bool(sources),
        "sink_detected": bool(sinks),
        "sources": [device.identifier for device in sources],
        "sinks": [device.identifier for device in sinks],
        "default_source": default_source,
        "default_sink": default_sink,
        "default_source_ok": default_source in {device.identifier for device in sources},
        "default_sink_ok": default_sink in {device.identifier for device in sinks},
    }


def _decode_sample(raw: bytes, sample_width: int) -> int:
    if sample_width == 1:
        return raw[0] - 128
    if sample_width == 2:
        return struct.unpack("<h", raw)[0]
    if sample_width == 3:
        value = int.from_bytes(raw, "little", signed=False)
        return value - (1 << 24) if value & (1 << 23) else value
    if sample_width == 4:
        return struct.unpack("<i", raw)[0]
    raise ValueError(f"Unsupported PCM sample width: {sample_width} bytes")


def analyze_wav_bytes(payload: bytes) -> dict[str, Any]:
    """Analyze PCM WAV bytes without using the removed ``audioop`` module."""
    with wave.open(io.BytesIO(payload), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        frame_count = wav.getnframes()
        compression = wav.getcomptype()
        if compression != "NONE":
            raise ValueError(f"Unsupported WAV compression: {compression}")
        if channels <= 0 or sample_rate <= 0:
            raise ValueError("WAV contains invalid channel or sample-rate metadata")
        raw = wav.readframes(frame_count)

    bytes_per_frame = channels * sample_width
    if bytes_per_frame <= 0 or len(raw) % bytes_per_frame:
        raise ValueError("WAV PCM frame data is incomplete")
    max_amplitude = float((1 << (sample_width * 8 - 1)) - 1)
    peak = [0.0] * channels
    sum_squares = [0.0] * channels
    sample_counts = [0] * channels
    clipping = 0
    offset = 0
    for _frame in range(frame_count):
        for channel in range(channels):
            sample = _decode_sample(raw[offset : offset + sample_width], sample_width)
            offset += sample_width
            normalized = abs(sample) / max_amplitude if max_amplitude else 0.0
            peak[channel] = max(peak[channel], normalized)
            sum_squares[channel] += normalized * normalized
            sample_counts[channel] += 1
            if abs(sample) >= max_amplitude:
                clipping += 1

    channel_data: dict[str, dict[str, float]] = {}
    rms_values: list[float] = []
    for index, count in enumerate(sample_counts):
        rms = math.sqrt(sum_squares[index] / count) if count else 0.0
        rms_values.append(rms)
        channel_data[f"channel_{index + 1}"] = {
            "peak": peak[index],
            "rms": rms,
            "rms_db": 20.0 * math.log10(rms) if rms > 0 else -120.0,
        }
    delta_db = None
    if len(rms_values) >= 2 and rms_values[0] > 0 and rms_values[1] > 0:
        delta_db = abs(20.0 * math.log10(rms_values[1] / rms_values[0]))
    return {
        "file_size": len(payload),
        "duration_sec": frame_count / sample_rate,
        "sample_rate": sample_rate,
        "channels": channels,
        "bit_depth": sample_width * 8,
        "frames": frame_count,
        "clipping": clipping > 0,
        "clipping_count": clipping,
        "channel_delta_db": delta_db,
        **channel_data,
    }


def analyze_wav(path: str | Path) -> AudioActionResult:
    """Analyze a local WAV and return the standard action result."""
    started_at = datetime.now()
    path = Path(path)
    try:
        if not path.is_file():
            return make_action_result(
                action="analyze_wav",
                success=False,
                message="WAV file does not exist",
                started_at=started_at,
                data={"path": str(path)},
            )
        data = analyze_wav_bytes(path.read_bytes())
        data["path"] = str(path)
        return make_action_result(
            action="analyze_wav",
            success=True,
            message="WAV analysis completed",
            started_at=started_at,
            data=data,
        )
    except (OSError, ValueError, wave.Error) as exc:
        return make_action_result(
            action="analyze_wav",
            success=False,
            message=f"WAV analysis failed: {exc}",
            started_at=started_at,
            data={"path": str(path)},
        )
