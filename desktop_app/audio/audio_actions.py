"""Reusable remote Audio automation actions built on the shared SSH session."""
from __future__ import annotations

import base64
from datetime import datetime
import shlex
from typing import Any
import binascii
import wave
from types import SimpleNamespace

from desktop_app.audio.audio_automation import (
    AudioActionResult,
    analyze_wav_bytes,
    make_action_result,
    parse_pulse_summary,
    parse_usb_devices,
)
from desktop_app.audio.audio_models import parse_pactl_info, parse_respeaker_alsa_card
from desktop_app.audio.respeaker_mixer import ensure_respeaker_playback_mixer_ready


async def _run(ssh, command: str, timeout: float = 15):
    try:
        return await ssh.run(command, timeout=timeout)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _command_success(result) -> bool:
    return result is not None and not isinstance(result, tuple) and result.exit_status == 0


async def run_audio_precheck(ssh) -> AudioActionResult:
    """Run a generic USB, ALSA, PulseAudio, routing, and mixer pre-check."""
    started_at = datetime.now()
    outputs: list[str] = []
    errors: list[str] = []

    usb_result = await _run(ssh, "lsusb")
    usb, usb_error = usb_result if isinstance(usb_result, tuple) else (usb_result, None)
    if usb_error:
        errors.append(usb_error)
    usb_data = parse_usb_devices(usb.stdout if usb else "")
    usb_ok = bool(usb and _command_success(usb) and usb_data["detected"])
    outputs.append(usb.stdout if usb else "")

    alsa_result = await _run(ssh, "cat /proc/asound/cards")
    alsa, alsa_error = alsa_result if isinstance(alsa_result, tuple) else (alsa_result, None)
    if alsa_error:
        errors.append(alsa_error)
    alsa_card = parse_respeaker_alsa_card(alsa.stdout if alsa else "")
    alsa_ok = bool(alsa and _command_success(alsa) and alsa_card)
    outputs.append(alsa.stdout if alsa else "")

    sources_result = await _run(ssh, "pactl list short sources")
    sources, sources_error = sources_result if isinstance(sources_result, tuple) else (sources_result, None)
    if sources_error:
        errors.append(sources_error)
    sinks_result = await _run(ssh, "pactl list short sinks")
    sinks, sinks_error = sinks_result if isinstance(sinks_result, tuple) else (sinks_result, None)
    if sinks_error:
        errors.append(sinks_error)
    default_source_result = await _run(ssh, "pactl get-default-source")
    default_source, default_source_error = (
        default_source_result
        if isinstance(default_source_result, tuple)
        else (default_source_result, None)
    )
    if default_source_error:
        errors.append(default_source_error)
    default_sink_result = await _run(ssh, "pactl get-default-sink")
    default_sink, default_sink_error = (
        default_sink_result
        if isinstance(default_sink_result, tuple)
        else (default_sink_result, None)
    )
    if default_sink_error:
        errors.append(default_sink_error)

    # Older PulseAudio releases expose defaults through ``pactl info`` only.
    if not _command_success(default_source) or not _command_success(default_sink):
        info_result = await _run(ssh, "pactl info")
        info, info_error = info_result if isinstance(info_result, tuple) else (info_result, None)
        if info_error:
            errors.append(info_error)
        if info and _command_success(info):
            info_source, info_sink = parse_pactl_info(info.stdout or "")
            if not _command_success(default_source) and info_source:
                default_source = SimpleNamespace(stdout=info_source, exit_status=0)
            if not _command_success(default_sink) and info_sink:
                default_sink = SimpleNamespace(stdout=info_sink, exit_status=0)

    pulse = parse_pulse_summary(
        sources.stdout if sources else "",
        sinks.stdout if sinks else "",
        default_source.stdout if default_source else "",
        default_sink.stdout if default_sink else "",
    )
    source_ok = bool(sources and _command_success(sources) and pulse["source_detected"])
    sink_ok = bool(sinks and _command_success(sinks) and pulse["sink_detected"])
    default_source_ok = bool(default_source and _command_success(default_source) and pulse["default_source_ok"])
    default_sink_ok = bool(default_sink and _command_success(default_sink) and pulse["default_sink_ok"])
    outputs.extend(
        result.stdout if result else ""
        for result in (sources, sinks, default_source, default_sink)
    )

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
        "ready": mixer.ready,
        "changed": mixer.changed,
        "switch_changed": mixer.switch_changed,
        "messages": list(mixer.messages),
    }
    steps: dict[str, dict[str, Any]] = {
        "usb": {"state": "PASS" if usb_ok else "FAIL", "data": usb_data},
        "alsa": {
            "state": "PASS" if alsa_ok else "FAIL",
            "data": {
                "detected": alsa_card is not None,
                "card_index": alsa_card[0] if alsa_card else None,
                "card_name": alsa_card[1] if alsa_card else None,
            },
        },
        "pulse_source": {"state": "PASS" if source_ok else "FAIL", "data": pulse},
        "pulse_sink": {"state": "PASS" if sink_ok else "FAIL", "data": pulse},
        "default_source": {
            "state": "PASS" if default_source_ok else "FAIL",
            "data": {"name": pulse["default_source"]},
        },
        "default_sink": {
            "state": "PASS" if default_sink_ok else "FAIL",
            "data": {"name": pulse["default_sink"]},
        },
        "mixer": {"state": "PASS" if mixer_data["ready"] else "FAIL", "data": mixer_data},
    }
    overall = all(step["state"] == "PASS" for step in steps.values())
    return make_action_result(
        action="audio_precheck",
        success=overall,
        message="Audio pre-check passed" if overall else "Audio pre-check found unavailable components",
        started_at=started_at,
        data={
            "overall": "PASS" if overall else "FAIL",
            "steps": steps,
            "usb": usb_data,
            "alsa": steps["alsa"]["data"],
            "pulse": pulse,
            "mixer": mixer_data,
        },
        stdout="\n".join(output for output in outputs if output),
        stderr="\n".join(errors),
        state="PASS" if overall else "FAIL",
    )


async def analyze_remote_wav(ssh, path: str) -> AudioActionResult:
    """Fetch one remote WAV through the existing SSH connection for analysis."""
    started_at = datetime.now()
    quoted = shlex.quote(path)
    result = await _run(ssh, f"base64 -w 0 -- {quoted}", timeout=30)
    command_result, error = result if isinstance(result, tuple) else (result, None)
    if error or not _command_success(command_result):
        message = error or "Remote WAV could not be read"
        return make_action_result(
            action="analyze_wav",
            success=False,
            message=message,
            started_at=started_at,
            data={"path": path},
            stderr=(command_result.stderr if command_result else error or ""),
            return_code=command_result.exit_status if command_result else None,
        )
    try:
        payload = base64.b64decode((command_result.stdout or "").encode("ascii"), validate=True)
        data = analyze_wav_bytes(payload)
        data["path"] = path
        return make_action_result(
            action="analyze_wav",
            success=True,
            message="WAV analysis completed",
            started_at=started_at,
            data=data,
            return_code=command_result.exit_status,
        )
    except (ValueError, OSError, EOFError, binascii.Error, wave.Error) as exc:
        return make_action_result(
            action="analyze_wav",
            success=False,
            message=f"WAV analysis failed: {exc}",
            started_at=started_at,
            data={"path": path},
            return_code=command_result.exit_status,
        )
