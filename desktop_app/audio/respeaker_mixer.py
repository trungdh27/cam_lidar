"""Remote ALSA mixer discovery and safe XVF3800 playback initialization."""
from __future__ import annotations

import asyncio

from desktop_app.audio.audio_models import (
    RespeakerMixerResult,
    has_respeaker_pcm_control,
    parse_respeaker_alsa_card,
    parse_respeaker_pcm1_state,
)


_MIXER_CONTROL = "'PCM',1"
_MIXER_RETRY_COUNT = 3
_MIXER_RETRY_DELAY_SECONDS = 0.2


def _command_detail(result) -> str:
    return (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()


async def _run_read_with_retries(ssh, command: str, acceptable=None):
    """Run a short-lived read command while ALSA finishes enumerating."""
    result = None
    for attempt in range(_MIXER_RETRY_COUNT):
        result = await ssh.run(command, timeout=15)
        if result.exit_status == 0 and (acceptable is None or acceptable(result)):
            return result
        if attempt + 1 < _MIXER_RETRY_COUNT:
            await asyncio.sleep(_MIXER_RETRY_DELAY_SECONDS)
    return result


async def _ensure_respeaker_playback_mixer_ready(ssh) -> RespeakerMixerResult:
    """Ensure the XVF3800's secondary playback control is usable.

    ``PCM,1`` is a hardware playback baseline, not the user-facing volume
    control.  A non-zero value is preserved.  A zero value is initialized to
    70%, then read back from ALSA before the result is marked ready.
    """
    messages: list[str] = []

    cards = await _run_read_with_retries(ssh, "cat /proc/asound/cards")
    detected = (
        parse_respeaker_alsa_card(cards.stdout or "")
        if cards and cards.exit_status == 0
        else None
    )
    if detected is None:
        detail = _command_detail(cards) if cards else "no command result"
        warning = "reSpeaker ALSA card not found"
        if detail:
            warning = f"{warning}: {detail}"
        return RespeakerMixerResult(
            False,
            warning=warning,
            messages=(f"[MIXER][WARN] {warning}",),
        )

    card_index, card_name = detected
    messages.append(f"[MIXER] reSpeaker ALSA card detected: card {card_index}")
    messages.append(f"[MIXER] reSpeaker ALSA card name: {card_name}")

    controls_command = f"amixer -c {card_index} scontrols"
    controls = await _run_read_with_retries(
        ssh,
        controls_command,
        lambda result: has_respeaker_pcm_control(result.stdout or "", 1),
    )
    if controls is None or controls.exit_status != 0:
        detail = _command_detail(controls)
        warning = "Unable to inspect reSpeaker ALSA mixer controls"
        if detail:
            warning = f"{warning}: {detail}"
        return RespeakerMixerResult(
            True,
            card_index=card_index,
            card_name=card_name,
            warning=warning,
            messages=tuple(messages + [f"[MIXER][WARN] {warning}"]),
        )

    if not has_respeaker_pcm_control(controls.stdout or "", 1):
        warning = "PCM,1 control not found"
        return RespeakerMixerResult(
            True,
            card_index=card_index,
            card_name=card_name,
            warning=warning,
            messages=tuple(messages + [f"[MIXER][WARN] {warning}"]),
        )

    # PCM,0 is retained as read-only diagnostic data for existing Audio
    # Evidence/precheck consumers. It is never written by this recovery path.
    pcm0_available = has_respeaker_pcm_control(controls.stdout or "", 0)
    pcm0_level_percent = None
    pcm0_switch_on = None
    if pcm0_available:
        pcm0_result = await ssh.run(
            f"amixer -c {card_index} sget 'PCM',0", timeout=15
        )
        if pcm0_result.exit_status == 0:
            pcm0_state = parse_respeaker_pcm1_state(pcm0_result.stdout or "")
            pcm0_level_percent = pcm0_state.level_percent
            pcm0_switch_on = pcm0_state.switch_on

    state_command = f"amixer -c {card_index} sget {_MIXER_CONTROL}"
    state_result = await _run_read_with_retries(
        ssh,
        state_command,
        lambda result: parse_respeaker_pcm1_state(result.stdout or "").level_percent is not None,
    )
    state = (
        parse_respeaker_pcm1_state(state_result.stdout or "")
        if state_result and state_result.exit_status == 0
        else None
    )
    if state is None or state.level_percent is None:
        detail = _command_detail(state_result) if state_result else "no command result"
        warning = "Unable to read PCM,1 state"
        if detail:
            warning = f"{warning}: {detail}"
        return RespeakerMixerResult(
            True,
            card_index=card_index,
            card_name=card_name,
            pcm0_available=pcm0_available,
            pcm0_level_percent=pcm0_level_percent,
            pcm0_switch_on=pcm0_switch_on,
            pcm1_available=True,
            warning=warning,
            messages=tuple(messages + [f"[MIXER][WARN] {warning}"]),
        )

    current_level = state.level_percent
    current_switch = state.switch_on
    messages.append(f"[MIXER] PCM,1 current level: {current_level}%")
    warnings: list[str] = []
    level_changed = False
    switch_changed = False
    requested_level_change = current_level == 0

    if requested_level_change:
        messages.append("[MIXER] PCM,1 is not initialized")

    # A disabled playback switch is also unusable, but enabling it still only
    # touches PCM,1. Keep this separate from the level baseline decision.
    if current_switch is False:
        enable = await ssh.run(
            f"amixer -c {card_index} sset {_MIXER_CONTROL} on", timeout=15
        )
        if enable.exit_status == 0:
            switch_changed = True
        else:
            warnings.append("Failed to enable PCM,1 playback switch")

    if requested_level_change:
        messages.append("[MIXER] Initializing PCM,1 -> 70%")
        set_level = await ssh.run(
            f"amixer -c {card_index} sset {_MIXER_CONTROL} 70%", timeout=15
        )
        if set_level.exit_status != 0:
            detail = _command_detail(set_level)
            message = "Failed to initialize PCM,1"
            warnings.append(f"{message}: {detail}" if detail else message)

    # A command's exit status is not proof that the USB mixer changed. Always
    # read the exact control back after any attempted change.
    final_state = state
    if requested_level_change or current_switch is False:
        readback = await _run_read_with_retries(ssh, state_command)
        if readback is None or readback.exit_status != 0:
            detail = _command_detail(readback) if readback else "no command result"
            message = "Failed to read back PCM,1 after initialization"
            warnings.append(f"{message}: {detail}" if detail else message)
        else:
            final_state = parse_respeaker_pcm1_state(readback.stdout or "")

    final_level = final_state.level_percent
    final_switch = final_state.switch_on
    if requested_level_change:
        readback_message = (
            f"[MIXER] PCM,1 read-back level: {final_level}%"
            if final_level is not None
            else "[MIXER] PCM,1 read-back level: unavailable"
        )
        messages.append(readback_message)

    if final_level is None or final_level <= 0:
        warnings.append("PCM,1 read-back remains 0% or unavailable")
    if final_switch is False:
        warnings.append("PCM,1 playback switch remains off")

    ready = final_level is not None and final_level > 0 and final_switch is not False and not warnings
    if requested_level_change:
        level_changed = ready and final_level != current_level
    if current_switch is False:
        switch_changed = switch_changed and final_switch is True

    if ready and requested_level_change:
        messages.append("[MIXER] PCM,1 hardware playback mixer ready")
    elif ready:
        messages.append(
            f"[MIXER] PCM,1 already ready; preserving current value ({current_level}%)"
        )
    else:
        messages.append("[MIXER][ERROR] Failed to initialize PCM,1")
    messages.extend(f"[MIXER][ERROR] {warning}" for warning in warnings)

    return RespeakerMixerResult(
        True,
        card_index=card_index,
        card_name=card_name,
        pcm0_available=pcm0_available,
        pcm0_level_percent=pcm0_level_percent,
        pcm0_switch_on=pcm0_switch_on,
        pcm1_available=True,
        ready=ready,
        current_level_percent=current_level,
        final_level_percent=final_level,
        switch_on=final_switch,
        changed=level_changed,
        switch_changed=switch_changed,
        warning=warnings[0] if warnings else None,
        messages=tuple(messages),
    )


async def ensure_respeaker_playback_mixer_ready(ssh) -> RespeakerMixerResult:
    """Run mixer readiness without allowing remote mixer errors to crash Audio."""
    try:
        return await _ensure_respeaker_playback_mixer_ready(ssh)
    except Exception as exc:
        warning = f"PCM,1 initialization unavailable: {type(exc).__name__}: {exc}"
        return RespeakerMixerResult(
            detected=False,
            warning=warning,
            messages=(f"[MIXER][ERROR] {warning}",),
        )
