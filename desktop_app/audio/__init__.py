"""Audio discovery support for the desktop application."""

from desktop_app.audio.audio_models import (
    AudioDevice,
    AudioDeviceSnapshot,
    AudioRoutingResult,
    AudioPlaybackFile,
    AudioVolumeState,
)
from desktop_app.audio.audio_automation import AudioActionResult, AudioExecutionLogger, AudioLogEvent
from desktop_app.audio.audio_runtime import (
    AudioDurationRunner,
    AudioIterationCampaignResult,
    AudioIterationResult,
    AudioIterationRunner,
    AudioRuntimeEvent,
    AudioRuntimeMonitor,
    AudioRuntimeSample,
)

__all__ = (
    "AudioDevice",
    "AudioDeviceSnapshot",
    "AudioRoutingResult",
    "AudioPlaybackFile",
    "AudioVolumeState",
    "AudioActionResult",
    "AudioExecutionLogger",
    "AudioLogEvent",
    "AudioRuntimeEvent",
    "AudioRuntimeSample",
    "AudioRuntimeMonitor",
    "AudioIterationResult",
    "AudioIterationCampaignResult",
    "AudioIterationRunner",
    "AudioDurationRunner",
    "AudioManager",
)


def __getattr__(name: str):
    if name == "AudioManager":
        from desktop_app.audio.audio_manager import AudioManager

        return AudioManager
    raise AttributeError(name)
