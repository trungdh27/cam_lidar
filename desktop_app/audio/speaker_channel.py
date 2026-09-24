"""Reusable semi-automated physical speaker channel validation.

The software portion verifies the route and executes a channel-specific tone.
Only the operator can verify which physical transducer was audible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from desktop_app.audio.audio_models import (
    parse_pactl_short_devices,
    parse_respeaker_alsa_card,
    parse_sink_mute,
    parse_sink_channel_state,
)
from desktop_app.audio.respeaker_mixer import ensure_respeaker_playback_mixer_ready


class SpeakerChannel(str, Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"

    @property
    def speaker_number(self) -> int:
        return 1 if self is SpeakerChannel.LEFT else 2

    @classmethod
    def parse(cls, value: str | "SpeakerChannel") -> "SpeakerChannel":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().upper()
        if normalized in {"LEFT", "L", "1", "FRONT LEFT", "FRONT-LEFT"}:
            return cls.LEFT
        if normalized in {"RIGHT", "R", "2", "FRONT RIGHT", "FRONT-RIGHT"}:
            return cls.RIGHT
        raise ValueError("Speaker channel must be LEFT or RIGHT.")


class SpeakerPhysicalVerification(str, Enum):
    NOT_VERIFIED = "NOT_VERIFIED"
    HEARD_CORRECT_SIDE = "HEARD_CORRECT_SIDE"
    HEARD_WRONG_SIDE = "HEARD_WRONG_SIDE"
    BOTH_SPEAKERS = "BOTH_SPEAKERS"
    NO_SOUND = "NO_SOUND"


@dataclass(frozen=True)
class SpeakerChannelPrecheck:
    channel: SpeakerChannel
    device: str
    sink_name: str
    sink_display_name: str
    sample_rate_hz: int
    frequency_hz: int
    command: str
    channel_map: tuple[str, ...]
    left_volume_percent: int | None
    right_volume_percent: int | None
    balance: float | None
    sink_mute: bool | None
    pcm1_percent: int | None
    mixer: dict[str, Any] = field(default_factory=dict)
    pulse_state: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "device": self.device,
            "sink_name": self.sink_name,
            "sink_display_name": self.sink_display_name,
            "sample_rate_hz": self.sample_rate_hz,
            "frequency_hz": self.frequency_hz,
            "command": self.command,
            "channel_map": list(self.channel_map),
            "left_volume_percent": self.left_volume_percent,
            "right_volume_percent": self.right_volume_percent,
            "balance": self.balance,
            "sink_mute": self.sink_mute,
            "pcm1_percent": self.pcm1_percent,
            "mixer": self.mixer,
            "pulse_state": self.pulse_state,
            "commands": self.commands,
        }


@dataclass(frozen=True)
class SpeakerChannelExecutionResult:
    """Software execution only; physical hearing is intentionally separate."""

    success: bool
    channel: SpeakerChannel
    device: str
    sample_rate_hz: int
    frequency_hz: int
    requested_duration_sec: int
    command: str
    return_code: int | None
    stdout: str = ""
    stderr: str = ""
    sink_name: str | None = None
    sink_mute: bool | None = None
    left_volume_percent: int | None = None
    right_volume_percent: int | None = None
    balance: float | None = None
    channel_map: tuple[str, ...] = ()
    pcm1_percent: int | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime = field(default_factory=datetime.now)
    execution_status: str = "PASS"

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "channel": self.channel.value,
            "device": self.device,
            "sample_rate_hz": self.sample_rate_hz,
            "frequency_hz": self.frequency_hz,
            "requested_duration_sec": self.requested_duration_sec,
            "command": self.command,
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "sink_name": self.sink_name,
            "sink_mute": self.sink_mute,
            "left_volume_percent": self.left_volume_percent,
            "right_volume_percent": self.right_volume_percent,
            "balance": self.balance,
            "channel_map": list(self.channel_map),
            "pcm1_percent": self.pcm1_percent,
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "finished_at": self.finished_at.isoformat(timespec="milliseconds"),
            "duration_sec": self.duration_sec,
            "execution_status": self.execution_status,
        }


def build_speaker_channel_command(
    channel: str | SpeakerChannel,
    *,
    frequency_hz: int = 500,
    sample_rate_hz: int = 16000,
) -> str:
    """Build the exact PulseAudio-routed tone command."""
    selected = SpeakerChannel.parse(channel)
    if not isinstance(frequency_hz, int) or not 100 <= frequency_hz <= 5000:
        raise ValueError("Tone frequency must be an integer between 100 and 5000 Hz.")
    if not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
        raise ValueError("Sample rate must be a positive integer.")
    return (
        "speaker-test -D pulse -c 2 "
        f"-r {sample_rate_hz} -t sine -f {frequency_hz} "
        f"-s {selected.speaker_number} -l 1"
    )


def overall_speaker_channel_result(
    left_execution: str = "NOT RUN",
    left_verification: str = SpeakerPhysicalVerification.NOT_VERIFIED.value,
    right_execution: str = "NOT RUN",
    right_verification: str = SpeakerPhysicalVerification.NOT_VERIFIED.value,
) -> str:
    """Map independent execution and physical observations to one result."""
    executions = {str(left_execution).upper(), str(right_execution).upper()}
    verifications = {str(left_verification).upper(), str(right_verification).upper()}
    if "BLOCKED" in executions:
        return "BLOCKED"
    if "FAIL" in executions or "STOPPED" in executions:
        return "FAIL"
    if verifications & {
        SpeakerPhysicalVerification.HEARD_WRONG_SIDE.value,
        SpeakerPhysicalVerification.BOTH_SPEAKERS.value,
        SpeakerPhysicalVerification.NO_SOUND.value,
    }:
        return "FAIL"
    if (
        str(left_execution).upper() == "PASS"
        and str(right_execution).upper() == "PASS"
        and left_verification == SpeakerPhysicalVerification.HEARD_CORRECT_SIDE.value
        and right_verification == SpeakerPhysicalVerification.HEARD_CORRECT_SIDE.value
    ):
        return "PASS"
    if "PASS" in executions:
        return "MANUAL VERIFY"
    return "NOT RUN"


def _command_record(command: str, result: Any) -> dict[str, Any]:
    return {
        "command": command,
        "return_code": getattr(result, "exit_status", None),
        "stdout": getattr(result, "stdout", "") or "",
        "stderr": getattr(result, "stderr", "") or "",
    }


def _failure_message(command: str, result: Any) -> str:
    return (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip() or (
        f"{command} exited with status {getattr(result, 'exit_status', 'unknown')}"
    )


async def prepare_speaker_channel_test(
    ssh,
    channel: str | SpeakerChannel,
    *,
    frequency_hz: int = 500,
    sample_rate_hz: int = 16000,
) -> SpeakerChannelPrecheck:
    """Run the lightweight pre-check shared by every channel playback."""
    selected = SpeakerChannel.parse(channel)
    command = build_speaker_channel_command(
        selected, frequency_hz=frequency_hz, sample_rate_hz=sample_rate_hz
    )
    commands: dict[str, dict[str, Any]] = {}

    async def run(label: str, text: str, timeout: float = 15):
        result = await ssh.run(text, timeout=timeout)
        commands[label] = _command_record(text, result)
        if getattr(result, "exit_status", 1) != 0:
            raise RuntimeError(f"{text}: {_failure_message(text, result)}")
        return result

    await run("speaker_test_available", "command -v speaker-test")
    default_result = await run("pulse_default_sink", "pactl get-default-sink")
    default_sink = (default_result.stdout or "").strip() or None
    sinks_result = await run("pulse_sinks", "pactl list short sinks")
    sinks = parse_pactl_short_devices(sinks_result.stdout or "")
    target = next(
        (item for item in sinks if "respeaker" in f"{item.identifier} {item.display_name}".casefold()),
        None,
    )
    if target is None:
        raise RuntimeError("reSpeaker output sink is unavailable.")
    if default_sink != target.identifier:
        raise RuntimeError(
            f"reSpeaker is not the current default sink ({default_sink or 'not available'})."
        )
    volume = await run("pulse_volume", "pactl get-sink-volume @DEFAULT_SINK@")
    mute = await run("pulse_mute", "pactl get-sink-mute @DEFAULT_SINK@")
    sink_details = await run("pulse_sink_details", "pactl list sinks")
    pulse_state = parse_sink_channel_state(volume.stdout or "", sink_details.stdout or "")
    if not pulse_state["stereo"]:
        raise RuntimeError("Default sink does not expose front-left/front-right stereo channels.")
    if pulse_state["left_volume_percent"] is None or pulse_state["right_volume_percent"] is None:
        raise RuntimeError("Both PulseAudio channel volumes are unavailable.")
    sink_mute = parse_sink_mute(mute.stdout or "")
    if sink_mute is not False:
        raise RuntimeError("Default reSpeaker sink is muted or its mute state is unavailable.")
    cards = await run("alsa_cards", "cat /proc/asound/cards")
    if parse_respeaker_alsa_card(cards.stdout or "") is None:
        raise RuntimeError("reSpeaker ALSA card is unavailable.")
    mixer = await ensure_respeaker_playback_mixer_ready(ssh)
    if not mixer.ready:
        raise RuntimeError(mixer.warning or "PCM,1 hardware playback control is not ready.")
    mixer_data = {
        "detected": mixer.detected,
        "card_index": mixer.card_index,
        "card_name": mixer.card_name,
        "pcm1_available": mixer.pcm1_available,
        "pcm1_percent": mixer.final_level_percent,
        "ready": mixer.ready,
        "switch_on": mixer.switch_on,
        "messages": list(mixer.messages),
    }
    return SpeakerChannelPrecheck(
        channel=selected,
        device="reSpeaker XVF3800",
        sink_name=target.identifier,
        sink_display_name=target.display_name,
        sample_rate_hz=sample_rate_hz,
        frequency_hz=frequency_hz,
        command=command,
        channel_map=tuple(pulse_state["channel_map"]),
        left_volume_percent=pulse_state["left_volume_percent"],
        right_volume_percent=pulse_state["right_volume_percent"],
        balance=pulse_state["balance"],
        sink_mute=sink_mute,
        pcm1_percent=mixer.final_level_percent,
        mixer=mixer_data,
        pulse_state={**pulse_state, "mute": sink_mute, "default_sink": default_sink},
        commands=commands,
    )


async def run_speaker_channel_test(
    ssh,
    channel: str | SpeakerChannel,
    *,
    frequency_hz: int = 500,
    duration_sec: int = 3,
    sample_rate_hz: int = 16000,
) -> SpeakerChannelExecutionResult:
    """Execute one channel using a shared SSH operation.

    The AudioManager uses the same pre-check and command with its managed
    remote-process API so the GUI can stop the exact process. This function is
    also useful to future orchestration layers that already own an SSH worker.
    """
    started_at = datetime.now()
    selected = SpeakerChannel.parse(channel)
    precheck = await prepare_speaker_channel_test(
        ssh,
        selected,
        frequency_hz=frequency_hz,
        sample_rate_hz=sample_rate_hz,
    )
    result = await ssh.run(precheck.command, timeout=max(15, int(duration_sec) + 15))
    finished_at = datetime.now()
    return SpeakerChannelExecutionResult(
        success=getattr(result, "exit_status", 1) == 0,
        channel=selected,
        device=precheck.device,
        sample_rate_hz=sample_rate_hz,
        frequency_hz=frequency_hz,
        requested_duration_sec=duration_sec,
        command=precheck.command,
        return_code=getattr(result, "exit_status", None),
        stdout=getattr(result, "stdout", "") or "",
        stderr=getattr(result, "stderr", "") or "",
        sink_name=precheck.sink_name,
        sink_mute=precheck.sink_mute,
        left_volume_percent=precheck.left_volume_percent,
        right_volume_percent=precheck.right_volume_percent,
        balance=precheck.balance,
        channel_map=precheck.channel_map,
        pcm1_percent=precheck.pcm1_percent,
        started_at=started_at,
        finished_at=finished_at,
        execution_status="PASS" if getattr(result, "exit_status", 1) == 0 else "FAIL",
    )

