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
CLIPPING_THRESHOLD = 0.999
WAV_CHUNK_FRAMES = 4096


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


def _dbfs(value: float) -> float | None:
    """Return finite dBFS, using JSON-safe ``None`` for a zero signal."""
    return 20.0 * math.log10(value) if value > 0.0 else None


def _analysis_from_wave(wav: wave.Wave_read, file_size_bytes: int | None = None) -> dict[str, Any]:
    """Analyze a WAV stream incrementally, without retaining all sample data."""
    channels = wav.getnchannels()
    sample_rate = wav.getframerate()
    sample_width = wav.getsampwidth()
    declared_frames = wav.getnframes()
    compression = wav.getcomptype()
    if compression != "NONE":
        raise ValueError(f"Unsupported WAV compression: {compression}")
    if channels <= 0 or sample_rate <= 0:
        raise ValueError("WAV contains invalid channel or sample-rate metadata")

    metadata = {
        "file_size_bytes": file_size_bytes,
        "file_size": file_size_bytes,
        "duration_sec": declared_frames / sample_rate,
        "sample_rate_hz": sample_rate,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "bit_depth": sample_width * 8,
        "frame_count": declared_frames,
        "frames": declared_frames,
    }
    if sample_width != 2:
        return {
            **metadata,
            "signal_analysis_supported": False,
            "signal_analysis_reason": "Only PCM16 signal metrics are currently implemented.",
            "channels_data": [],
            "clipping_detected": None,
            "channel_delta_db": None,
            "ch2_minus_ch1_db": None,
        }

    peaks = [0.0] * channels
    sums = [0.0] * channels
    clipping_counts = [0] * channels
    sample_counts = [0] * channels
    frames_read = 0
    bytes_per_frame = channels * sample_width
    while True:
        raw = wav.readframes(WAV_CHUNK_FRAMES)
        if not raw:
            break
        if len(raw) % bytes_per_frame:
            raise ValueError("WAV PCM frame data is truncated")
        frame_count = len(raw) // bytes_per_frame
        offset = 0
        for _frame in range(frame_count):
            for channel in range(channels):
                sample = struct.unpack_from("<h", raw, offset)[0]
                offset += sample_width
                normalized = sample / 32768.0
                absolute = abs(normalized)
                peaks[channel] = max(peaks[channel], absolute)
                sums[channel] += normalized * normalized
                sample_counts[channel] += 1
                if absolute >= CLIPPING_THRESHOLD:
                    clipping_counts[channel] += 1
        frames_read += frame_count

    if frames_read != declared_frames:
        raise ValueError(
            f"WAV is truncated: header declares {declared_frames} frames, read {frames_read}"
        )

    channels_data: list[dict[str, Any]] = []
    rms_values: list[float] = []
    for index in range(channels):
        count = sample_counts[index]
        rms = math.sqrt(sums[index] / count) if count else 0.0
        rms_values.append(rms)
        channels_data.append(
            {
                "channel": index + 1,
                "peak": peaks[index],
                "peak_dbfs": _dbfs(peaks[index]),
                "rms": rms,
                "rms_dbfs": _dbfs(rms),
                "rms_db": _dbfs(rms),
                "clipping_count": clipping_counts[index],
                "clipping_ratio": clipping_counts[index] / count if count else 0.0,
                "clipping_detected": clipping_counts[index] > 0,
                "zero_signal": peaks[index] == 0.0,
                "sample_count": count,
            }
        )

    delta_db = None
    signed_delta_db = None
    if len(rms_values) >= 2 and rms_values[0] > 0 and rms_values[1] > 0:
        signed_delta_db = 20.0 * math.log10(rms_values[1] / rms_values[0])
        delta_db = abs(signed_delta_db)
    result: dict[str, Any] = {
        **metadata,
        "signal_analysis_supported": True,
        "signal_analysis_reason": None,
        "channels_data": channels_data,
        "clipping_detected": any(item["clipping_detected"] for item in channels_data),
        "clipping": any(item["clipping_detected"] for item in channels_data),
        "clipping_count": sum(item["clipping_count"] for item in channels_data),
        "channel_delta_db": delta_db,
        "ch2_minus_ch1_db": signed_delta_db,
    }
    # Keep the original Phase 4B.1 keys as compatibility aliases for callers.
    for item in channels_data:
        result[f"channel_{item['channel']}"] = item
    return result


def analyze_wav_bytes(payload: bytes) -> dict[str, Any]:
    """Analyze WAV bytes without using ``audioop``.

    This helper remains useful for small unit-test fixtures. Runtime analysis
    of robot captures uses :func:`analyze_wav`, which is chunked.
    """
    with wave.open(io.BytesIO(payload), "rb") as wav:
        return _analysis_from_wave(wav, len(payload))


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
                data={"path": str(path), "file_path": str(path), "file_exists": False},
            )
        with wave.open(str(path), "rb") as wav:
            data = _analysis_from_wave(wav, path.stat().st_size)
        data.update({"path": str(path), "file_path": str(path), "file_exists": True})
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
            data={"path": str(path), "file_path": str(path), "file_exists": path.is_file()},
        )
