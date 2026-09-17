"""Audio discovery support for the desktop application."""

from desktop_app.audio.audio_models import (
    AudioDevice,
    AudioDeviceSnapshot,
    AudioRoutingResult,
    AudioPlaybackFile,
    AudioVolumeState,
)

__all__ = (
    "AudioDevice",
    "AudioDeviceSnapshot",
    "AudioRoutingResult",
    "AudioPlaybackFile",
    "AudioVolumeState",
    "AudioManager",
)


def __getattr__(name: str):
    if name == "AudioManager":
        from desktop_app.audio.audio_manager import AudioManager

        return AudioManager
    raise AttributeError(name)
