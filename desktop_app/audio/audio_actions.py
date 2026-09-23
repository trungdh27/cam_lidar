"""Reusable remote Audio automation actions built on the shared SSH session."""
from __future__ import annotations

from datetime import datetime
from typing import Any
import wave

from desktop_app.audio.audio_automation import (
    AudioActionResult,
    analyze_wav,
    make_action_result,
    parse_pulse_summary,
    parse_usb_devices,
)
from desktop_app.audio.audio_models import parse_pactl_info, parse_respeaker_alsa_card
from desktop_app.audio.respeaker_mixer import ensure_respeaker_playback_mixer_ready
from desktop_app.audio.speaker_channel import (
    SpeakerChannel,
    SpeakerChannelExecutionResult,
    SpeakerPhysicalVerification,
    build_speaker_channel_command,
    overall_speaker_channel_result,
    prepare_speaker_channel_test,
    run_speaker_channel_test,
)


async def _run(ssh, command: str, timeout: float = 15):
    try:
        return await ssh.run(command, timeout=timeout)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _command_success(result) -> bool:
    return result is not None and not isinstance(result, tuple) and result.exit_status == 0


def _command_record(command: str, result, error: str | None = None) -> dict[str, Any]:
    if isinstance(result, tuple):
        result, tuple_error = None, result[1]
        error = error or tuple_error
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "command": command,
        "return_code": getattr(result, "exit_status", None),
        "stdout": getattr(result, "stdout", "") or "",
        "stderr": getattr(result, "stderr", "") or "",
        "error": error,
    }


def _command_error(command: str, result, error: str | None = None) -> str:
    if error:
        return f"{command}: {error}"
    detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    status = getattr(result, "exit_status", "unknown")
    return f"{command}: {detail or f'exit status {status}'}"


async def collect_audio_baseline(ssh) -> AudioActionResult:
    """Collect raw USB, ALSA, PulseAudio, routing, and mixer evidence."""
    started_at = datetime.now()
    commands: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    outputs: list[str] = []

    async def collect(key: str, command: str, *, timeout: float = 15, required: bool = False):
        result = await _run(ssh, command, timeout)
        error = result[1] if isinstance(result, tuple) else None
        record = _command_record(command, result, error)
        commands[key] = record
        outputs.append(record["stdout"])
        if error or not _command_success(result):
            message = _command_error(command, result, error)
            record["error"] = message
            if required:
                errors.append(message)
        return result

    usb = await collect("lsusb", "lsusb", required=True)
    await collect("lsusb -d 2886:001a", "lsusb -d 2886:001a")
    cards = await collect("cat /proc/asound/cards", "cat /proc/asound/cards", required=True)
    await collect("arecord -l", "arecord -l")
    await collect("aplay -l", "aplay -l")
    sources = await collect("pactl list short sources", "pactl list short sources", required=True)
    sinks = await collect("pactl list short sinks", "pactl list short sinks", required=True)
    default_source = await collect("pactl get-default-source", "pactl get-default-source", required=True)
    default_sink = await collect("pactl get-default-sink", "pactl get-default-sink", required=True)
    info = None
    if not _command_success(default_source) or not _command_success(default_sink):
        info = await collect("pactl info", "pactl info")

    source_text = getattr(sources, "stdout", "") or ""
    sink_text = getattr(sinks, "stdout", "") or ""
    source_default_text = getattr(default_source, "stdout", "") or ""
    sink_default_text = getattr(default_sink, "stdout", "") or ""
    if info and _command_success(info):
        info_source, info_sink = parse_pactl_info(info.stdout or "")
        if not _command_success(default_source) and info_source:
            source_default_text = info_source
        if not _command_success(default_sink) and info_sink:
            sink_default_text = info_sink
    pulse = parse_pulse_summary(source_text, sink_text, source_default_text, sink_default_text)
    usb_data = parse_usb_devices(getattr(usb, "stdout", "") or "")
    alsa_card = parse_respeaker_alsa_card(getattr(cards, "stdout", "") or "")

    mixer = await ensure_respeaker_playback_mixer_ready(ssh)
    mixer_data = {
        "detected": mixer.detected,
        "card_index": mixer.card_index,
        "card_name": mixer.card_name,
        "control": mixer.control,
        "pcm1_available": mixer.pcm1_available,
        "ready": mixer.ready,
        "pcm0_available": mixer.pcm0_available,
        "pcm0_level_percent": mixer.pcm0_level_percent,
        "pcm0_switch_on": mixer.pcm0_switch_on,
        "current_level_percent": mixer.current_level_percent,
        "final_level_percent": mixer.final_level_percent,
        "switch_on": mixer.switch_on,
        "changed": mixer.changed,
        "switch_changed": mixer.switch_changed,
        "messages": list(mixer.messages),
    }
    if alsa_card:
        card_index = alsa_card[0]
        await collect("amixer scontrols", f"amixer -c {card_index} scontrols")
        await collect("amixer PCM,0", f"amixer -c {card_index} sget 'PCM',0")
        await collect("amixer PCM,1", f"amixer -c {card_index} sget 'PCM',1")

    source_ok = _command_success(sources) and pulse["source_detected"]
    sink_ok = _command_success(sinks) and pulse["sink_detected"]
    default_source_ok = bool(source_default_text.strip()) and pulse["default_source_ok"]
    default_sink_ok = bool(sink_default_text.strip()) and pulse["default_sink_ok"]
    steps: dict[str, dict[str, Any]] = {
        "usb": {"state": "PASS" if _command_success(usb) and usb_data["detected"] else "FAIL", "data": usb_data},
        "alsa": {
            "state": "PASS" if _command_success(cards) and alsa_card else "FAIL",
            "data": {"detected": alsa_card is not None, "card_index": alsa_card[0] if alsa_card else None, "card_name": alsa_card[1] if alsa_card else None},
        },
        "pulse_source": {"state": "PASS" if source_ok else "FAIL", "data": pulse},
        "pulse_sink": {"state": "PASS" if sink_ok else "FAIL", "data": pulse},
        "default_source": {"state": "PASS" if default_source_ok else "FAIL", "data": {"name": pulse["default_source"]}},
        "default_sink": {"state": "PASS" if default_sink_ok else "FAIL", "data": {"name": pulse["default_sink"]}},
        "mixer": {"state": "PASS" if mixer_data["ready"] else "FAIL", "data": mixer_data},
    }
    overall = all(step["state"] == "PASS" for step in steps.values())
    return make_action_result(
        action="audio_precheck",
        success=overall,
        message="Audio pre-check passed" if overall else "Audio pre-check found unavailable components",
        started_at=started_at,
        data={"overall": "PASS" if overall else "FAIL", "steps": steps, "usb": usb_data, "alsa": steps["alsa"]["data"], "pulse": pulse, "mixer": mixer_data, "commands": commands, "errors": errors},
        stdout="\n".join(output for output in outputs if output),
        stderr="\n".join(errors),
        state="PASS" if overall else "FAIL",
    )


async def run_audio_precheck(ssh) -> AudioActionResult:
    """Backward-compatible name for the reusable baseline collector."""
    return await collect_audio_baseline(ssh)


async def analyze_remote_wav(ssh, path: str, local_path: str | None = None) -> AudioActionResult:
    """Download and analyze a remote WAV using the shared SSH SFTP connection."""
    started_at = datetime.now()
    temporary_path: str | None = None
    target = local_path
    try:
        if target is None:
            import tempfile

            handle = tempfile.NamedTemporaryFile(prefix="audio-analysis-", suffix=".wav", delete=False)
            target = handle.name
            handle.close()
            temporary_path = target
        if not hasattr(ssh, "download_file"):
            raise RuntimeError("The shared SSH connection does not support SFTP downloads.")
        await ssh.download_file(path, target)
        result = analyze_wav(target)
        if not result.success:
            return result
        data = dict(result.data)
        data.update({"path": path, "source_path": path, "local_path": target})
        return make_action_result(action="analyze_wav", success=True, message="WAV analysis completed", started_at=started_at, data=data, return_code=0)
    except (OSError, RuntimeError, ValueError, EOFError, wave.Error) as exc:
        return make_action_result(action="analyze_wav", success=False, message=f"WAV analysis failed: {exc}", started_at=started_at, data={"path": path, "local_path": target})
    finally:
        if temporary_path:
            try:
                import os

                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
