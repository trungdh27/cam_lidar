"""Structured audio device discovery results."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
import shlex
from collections.abc import Mapping
from pathlib import PurePosixPath


@dataclass(frozen=True)
class PulseAudioSampleSpec:
    """Structured PulseAudio sample specification."""

    sample_format: str | None
    channels: int | None
    sample_rate_hz: int | None


@dataclass(frozen=True)
class AudioDevice:
    """A PulseAudio-compatible device and a label suitable for the UI."""

    identifier: str
    display_name: str
    index: int | None = None
    sample_format: str | None = None
    channels: int | None = None
    sample_rate_hz: int | None = None
    state: str | None = None

    @property
    def name(self) -> str:
        return self.identifier

    @property
    def description(self) -> str:
        return self.display_name


@dataclass
class AudioDeviceSnapshot:
    """Audio devices reported by PulseAudio-compatible and ALSA commands."""

    default_sink: str | None = None
    default_source: str | None = None
    sinks: list[AudioDevice] = field(default_factory=list)
    sources: list[AudioDevice] = field(default_factory=list)
    alsa_cards: list[str] = field(default_factory=list)
    playback_devices: list[str] = field(default_factory=list)
    capture_devices: list[str] = field(default_factory=list)
    command_errors: dict[str, str] = field(default_factory=dict)
    raw_info: dict[str, str] | None = None

    @property
    def has_devices(self) -> bool:
        return bool(
            self.sinks
            or self.sources
            or self.alsa_cards
            or self.playback_devices
            or self.capture_devices
        )


@dataclass(frozen=True)
class AudioVolumeState:
    """Current level and mute state for the PulseAudio default sink."""

    volume_percent: int
    muted: bool
    sink_name: str | None = None


@dataclass(frozen=True)
class AudioRoutingResult:
    """Verified PulseAudio defaults after a routing operation."""

    kind: str
    requested_name: str
    default_sink: str | None
    default_source: str | None


@dataclass(frozen=True)
class AudioPlaybackFile:
    """A validated WAV path on the Jetson."""

    path: str
    filename: str
    exists: bool
    size_bytes: int | None = None


@dataclass(frozen=True)
class RespeakerPcm1State:
    """Parsed state for the XVF3800's additional mono playback control."""

    level_percent: int | None = None
    switch_on: bool | None = None
    raw_value: int | None = None
    raw_min: int | None = None
    raw_max: int | None = None


@dataclass(frozen=True)
class RespeakerMixerResult:
    """Outcome of a best-effort reSpeaker PCM,1 initialization check."""

    detected: bool
    card_index: int | None = None
    card_name: str | None = None
    pcm1_available: bool = False
    pcm0_available: bool = False
    pcm0_level_percent: int | None = None
    pcm0_switch_on: bool | None = None
    current_level_percent: int | None = None
    final_level_percent: int | None = None
    switch_on: bool | None = None
    changed: bool = False
    switch_changed: bool = False
    warning: str | None = None
    messages: tuple[str, ...] = ()
    control: str = "PCM,1"
    ready: bool = False


@dataclass(frozen=True)
class AudioPlaybackPreparation:
    """Validated playback file plus the hardware mixer preflight result."""

    playback_file: AudioPlaybackFile
    mixer: RespeakerMixerResult


@dataclass(frozen=True)
class AudioSpeakerTestPreparation:
    """Fresh output information and command selected for a speaker test."""

    command: str
    output_name: str | None
    output_display_name: str | None
    sample_format: str | None
    channels: int | None
    native_sample_rate_hz: int | None
    command_rate_hz: int | None
    warning: str | None = None
    prompt_directory: str | None = None
    prompt_source_rates: tuple[int, ...] = ()
    prompt_resampled: bool = False
    prompt_cache_reused: bool = False
    hardware_mixer: RespeakerMixerResult | None = None

    def as_dict(self) -> dict[str, object | None]:
        return {
            "command": self.command,
            "output": self.output_name,
            "output_display": self.output_display_name,
            "sample_format": self.sample_format,
            "channels": self.channels,
            "native_sample_rate_hz": self.native_sample_rate_hz,
            "command_rate_hz": self.command_rate_hz,
            "warning": self.warning,
            "prompt_directory": self.prompt_directory,
            "prompt_source_rates": list(self.prompt_source_rates),
            "prompt_resampled": self.prompt_resampled,
            "prompt_cache_reused": self.prompt_cache_reused,
            "hardware_mixer": (
                {
                    "detected": self.hardware_mixer.detected,
                    "card_index": self.hardware_mixer.card_index,
                    "card_name": self.hardware_mixer.card_name,
                    "control": self.hardware_mixer.control,
                    "pcm1_available": self.hardware_mixer.pcm1_available,
                    "ready": self.hardware_mixer.ready,
                    "pcm0_available": self.hardware_mixer.pcm0_available,
                    "pcm0_level_percent": self.hardware_mixer.pcm0_level_percent,
                    "level_percent": self.hardware_mixer.final_level_percent,
                    "changed": self.hardware_mixer.changed,
                }
                if self.hardware_mixer
                else None
            ),
        }


class RecordingState(str, Enum):
    """Lifecycle states for the managed microphone recording process."""

    IDLE = "Idle"
    VALIDATING = "Validating"
    STARTING = "Starting"
    RECORDING = "Recording"
    STOPPING = "Stopping"
    COMPLETED = "Completed"
    FAILED = "Failed"
    DISCONNECTED = "Disconnected"


class PlaybackSource(str, Enum):
    """Origin of the single managed Audio playback process."""

    NORMAL_FILE = "normal_file"
    RECORDED_FILE = "recorded_file"


@dataclass(frozen=True)
class AudioRecordingConfig:
    """Validated-at-the-boundary configuration for a remote WAV recording."""

    source_name: str
    output_path: str
    sample_rate: int
    channels: int
    sample_format: str
    duration_seconds: int | None = None


@dataclass(frozen=True)
class AudioRecordingVerification:
    """Basic remote verification result for a recorded WAV file."""

    path: str
    exists: bool
    size_bytes: int | None
    valid: bool
    error: str | None = None


RECORDING_SAMPLE_RATES = (
    8000,
    11025,
    16000,
    22050,
    32000,
    44100,
    48000,
    88200,
    96000,
    176400,
    192000,
)
RECORDING_CHANNELS = (1, 2, 4, 6)
RECORDING_FORMATS = {"S16_LE": "s16le"}
MINIMUM_WAV_BYTES = 44
MIN_AUDIO_SAMPLE_RATE_HZ = 8000
MAX_AUDIO_SAMPLE_RATE_HZ = 192000


def validate_audio_sample_rate(sample_rate_hz: int) -> int:
    """Validate a discovered rate before placing it in a remote command."""
    if (
        isinstance(sample_rate_hz, bool)
        or not isinstance(sample_rate_hz, int)
        or not MIN_AUDIO_SAMPLE_RATE_HZ <= sample_rate_hz <= MAX_AUDIO_SAMPLE_RATE_HZ
    ):
        raise ValueError("Audio sample rate must be an integer between 8000 and 192000 Hz.")
    return sample_rate_hz


def parse_pulse_sample_spec(value: str) -> PulseAudioSampleSpec | None:
    """Parse common PulseAudio specs such as ``s16le 2ch 16000Hz``."""
    if not isinstance(value, str):
        return None
    match = re.search(
        r"(?P<format>[A-Za-z0-9_]+)\s+"
        r"(?P<channels>\d+)ch\s+"
        r"(?P<rate>\d+)Hz\b",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    sample_format = match.group("format").lower()
    channels = int(match.group("channels"))
    sample_rate_hz = int(match.group("rate"))
    if channels <= 0 or sample_rate_hz <= 0:
        return None
    return PulseAudioSampleSpec(sample_format, channels, sample_rate_hz)


def parse_sink_volume(output: str) -> int | None:
    """Return the maximum channel percentage from a sink volume response."""
    values = [int(value) for value in re.findall(r"(?<!\d)(\d{1,3})\s*%", output)]
    valid = [value for value in values if 0 <= value <= 100]
    return max(valid) if valid else None


def parse_sink_mute(output: str) -> bool | None:
    """Parse PulseAudio's `Mute: yes|no` response."""
    match = re.search(
        r"^\s*Mute\s*:\s*(yes|no)\s*$",
        output,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return None
    return match.group(1).casefold() == "yes"


def validate_volume(percent: int) -> int:
    """Validate a GUI volume value before command construction."""
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        raise ValueError("Volume must be an integer between 0 and 100.")
    return percent


def validate_recording_config(config: AudioRecordingConfig) -> AudioRecordingConfig:
    """Validate all GUI-controlled recording values before shell construction."""
    if not isinstance(config, AudioRecordingConfig):
        raise ValueError("Invalid microphone recording configuration.")
    if not isinstance(config.source_name, str) or not config.source_name.strip():
        raise ValueError("No input audio device available.")
    if "\n" in config.source_name or "\r" in config.source_name:
        raise ValueError("PulseAudio source name must be a single-line value.")
    if not isinstance(config.output_path, str) or not config.output_path.strip():
        raise ValueError("Enter a remote Jetson WAV output path.")
    output_path = config.output_path.strip()
    if "\n" in output_path or "\r" in output_path or "\x00" in output_path:
        raise ValueError("Recording output path must be a single-line path.")
    if not output_path.casefold().endswith(".wav"):
        raise ValueError("Phase 3A supports WAV recording only.")
    if config.sample_rate not in RECORDING_SAMPLE_RATES:
        raise ValueError("Select a supported recording sample rate.")
    if config.channels not in RECORDING_CHANNELS:
        raise ValueError("Select a supported recording channel count.")
    if config.sample_format not in RECORDING_FORMATS:
        raise ValueError("Select a supported recording format.")
    if config.duration_seconds is not None:
        if (
            isinstance(config.duration_seconds, bool)
            or not isinstance(config.duration_seconds, int)
            or not 1 <= config.duration_seconds <= 3600
        ):
            raise ValueError("Recording duration must be between 1 and 3600 seconds.")
    return AudioRecordingConfig(
        source_name=config.source_name.strip(),
        output_path=output_path,
        sample_rate=config.sample_rate,
        channels=config.channels,
        sample_format=config.sample_format,
        duration_seconds=config.duration_seconds,
    )


def quote_remote_path(path: str) -> str:
    """Quote a remote POSIX path while preserving a leading ``~/`` expansion."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def normalize_remote_audio_path(path: str, remote_home: str) -> str:
    """Return an absolute remote path for an audio file.

    ``remote_home`` is obtained from the connected Jetson; it is never inferred
    from the local machine or hard-coded to a particular user.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Remote audio path must be a non-empty string.")
    if not isinstance(remote_home, str) or not remote_home.strip():
        raise ValueError("Remote home directory is unavailable.")
    normalized_path = path.strip()
    normalized_home = remote_home.strip()
    if "\n" in normalized_path or "\r" in normalized_path or "\x00" in normalized_path:
        raise ValueError("Remote audio path must be a single-line path.")
    if "\n" in normalized_home or "\r" in normalized_home or "\x00" in normalized_home:
        raise ValueError("Remote home directory must be a single-line path.")
    if not normalized_home.startswith("/"):
        raise ValueError("Remote home directory must be absolute.")
    if normalized_path.startswith("/"):
        return normalized_path
    if normalized_path == "~":
        relative_path = ""
    elif normalized_path.startswith("~/"):
        relative_path = normalized_path[2:]
    else:
        relative_path = normalized_path
    return str(PurePosixPath(normalized_home) / relative_path) if relative_path else str(PurePosixPath(normalized_home))


def build_recording_command(
    config: AudioRecordingConfig,
    remote_home: str | None = None,
) -> str:
    """Build a safely quoted PulseAudio-compatible ``parecord`` command."""
    config = validate_recording_config(config)
    output_path = normalize_remote_audio_path(config.output_path, remote_home) if remote_home else config.output_path
    if not output_path.startswith("/"):
        raise ValueError("Recording output path must be absolute before command construction.")
    return (
        "parecord "
        f"--device={shlex.quote(config.source_name)} "
        "--file-format=wav "
        f"--format={RECORDING_FORMATS[config.sample_format]} "
        f"--rate={config.sample_rate} "
        f"--channels={config.channels} "
        f"{shlex.quote(output_path)}"
    )


def verify_recording_size(path: str, size_bytes: int | None) -> AudioRecordingVerification:
    """Return whether a remote file size is sufficient to be a non-empty WAV."""
    if size_bytes is None:
        return AudioRecordingVerification(path, False, None, False, "Recorded WAV file size could not be read.")
    if size_bytes <= MINIMUM_WAV_BYTES:
        return AudioRecordingVerification(path, True, size_bytes, False, "Recorded WAV file is empty or invalid.")
    return AudioRecordingVerification(path, True, size_bytes, True)


def parse_pactl_info(output: str) -> tuple[str | None, str | None]:
    """Return the configured PulseAudio-compatible sink and source."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip().casefold()] = value.strip()

    def configured(key: str) -> str | None:
        value = values.get(key)
        return value if value and value.casefold() not in {"(null)", "n/a"} else None

    return configured("default sink"), configured("default source")


def readable_device_name(identifier: str) -> str:
    """Make common PulseAudio identifiers useful without device-specific rules."""
    name = identifier.strip()
    if not name:
        return "Unknown device"

    stripped = re.sub(r"^(?:alsa_(?:output|input)|bluez_(?:output|input))\.", "", name)
    stripped = re.sub(r"\.monitor$", " monitor", stripped)
    stripped = re.sub(
        r"(?:[-.]|_)\d+\.(?:analog|iec958|surround|pro-audio).*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(
        r"(?:[-.]|_)(?:analog|iec958|surround|pro-audio).*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(r"^usb[-_.]", "", stripped, flags=re.IGNORECASE)
    stripped = stripped.replace("_", " ").replace(".", " ").replace("-", " ")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped.casefold().startswith("pci "):
        return "Built-in Audio"
    return stripped or name


def parse_pactl_short_devices(output: str) -> list[AudioDevice]:
    """Parse short sink/source rows, including sample spec and state."""
    devices: list[AudioDevice] = []
    seen: set[str] = set()
    for line in output.splitlines():
        columns = line.split("\t")
        if len(columns) < 2:
            columns = line.split()
        if len(columns) < 2:
            continue
        identifier = columns[1].strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        try:
            index = int(columns[0])
        except (TypeError, ValueError):
            index = None
        sample_spec = parse_pulse_sample_spec(line)
        state = None
        if sample_spec:
            spec_match = re.search(
                r"[A-Za-z0-9_]+\s+\d+ch\s+\d+Hz\b.*?(?:\s+)(\S+)\s*$",
                line,
                re.IGNORECASE,
            )
            if spec_match:
                state = spec_match.group(1)
        devices.append(
            AudioDevice(
                identifier,
                readable_device_name(identifier),
                index,
                sample_spec.sample_format if sample_spec else None,
                sample_spec.channels if sample_spec else None,
                sample_spec.sample_rate_hz if sample_spec else None,
                state,
            )
        )
    return devices


def parse_pactl_devices(output: str) -> list[AudioDevice]:
    """Parse `pactl list sinks|sources` Name/Description blocks."""
    devices: list[AudioDevice] = []
    block: dict[str, str] = {}
    index: int | None = None

    def finish() -> None:
        if not block.get("name"):
            return
        identifier = block["name"]
        sample_spec = parse_pulse_sample_spec(block.get("sample specification", ""))
        devices.append(
            AudioDevice(
                identifier,
                block.get("description") or readable_device_name(identifier),
                index,
                sample_spec.sample_format if sample_spec else None,
                sample_spec.channels if sample_spec else None,
                sample_spec.sample_rate_hz if sample_spec else None,
                block.get("state"),
            )
        )

    for line in output.splitlines():
        header = re.match(r"^\s*(?:Sink|Source)\s+#(\d+)\s*$", line, re.IGNORECASE)
        if header:
            finish()
            block = {}
            index = int(header.group(1))
            continue
        key, separator, value = line.partition(":")
        normalized_key = key.strip().casefold().replace("_", " ")
        if separator and normalized_key in {
            "name",
            "description",
            "sample specification",
            "state",
        }:
            block[normalized_key] = value.strip()
    finish()
    return devices


def resolve_device_description(devices: list[AudioDevice], name: str | None) -> str | None:
    """Map an authoritative internal PulseAudio name to its description."""
    if not name:
        return None
    device = next((item for item in devices if item.name == name), None)
    return device.description if device else None


def preferred_recording_configuration(
    device: AudioDevice | None,
) -> tuple[int | None, int | None, str | None]:
    """Return native recording preferences supported by the current GUI."""
    if device is None:
        return None, None, None
    sample_format = "S16_LE" if device.sample_format == "s16le" else None
    return device.sample_rate_hz, device.channels, sample_format


def build_default_sink_command(sink_name: str) -> str:
    return _build_default_command("sink", sink_name)


def build_default_source_command(source_name: str) -> str:
    return _build_default_command("source", source_name)


def validate_playback_path(path: str) -> str:
    """Validate a remote playback path without touching the local filesystem."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Enter a remote Jetson audio file path.")
    normalized = path.strip()
    if not normalized.casefold().endswith(".wav"):
        raise ValueError("Phase 2B supports WAV playback only.")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("Audio file path must be a single-line path.")
    return normalized


def build_playback_command(path: str, remote_home: str | None = None) -> str:
    """Build a safely quoted paplay command for an absolute remote path."""
    normalized_path = validate_playback_path(path)
    if not normalized_path.startswith("/"):
        if remote_home is None:
            raise ValueError("Playback path must be absolute before command construction.")
        normalized_path = normalize_remote_audio_path(normalized_path, remote_home)
    return f"paplay {shlex.quote(normalized_path)}"


def build_speaker_channel_test_command(
    sample_rate_hz: int | None = None,
    prompt_directory: str | None = None,
) -> str:
    """Build a PulseAudio-routed spoken left/right WAV test."""
    rate_option = ""
    if sample_rate_hz is not None:
        rate_option = f" -r {validate_audio_sample_rate(sample_rate_hz)}"
    prompt_option = ""
    if prompt_directory is not None:
        if (
            not isinstance(prompt_directory, str)
            or not prompt_directory.strip()
            or not prompt_directory.startswith("/")
            or "\n" in prompt_directory
            or "\r" in prompt_directory
            or "\x00" in prompt_directory
        ):
            raise ValueError("Speaker-test prompt directory must be an absolute path.")
        prompt_option = f" -W {shlex.quote(prompt_directory)}"
    return f"speaker-test -D pulse -c 2{rate_option} -t wav{prompt_option} -l 1"


def prepare_speaker_channel_test(
    default_sink: str | None,
    devices: list[AudioDevice],
    prompt_directory: str | None = None,
    prompt_source_rates: tuple[int, ...] = (),
    prompt_resampled: bool = False,
    prompt_cache_reused: bool = False,
) -> AudioSpeakerTestPreparation:
    """Select a fresh sink's native rate without assuming a device identity."""
    output_device = next(
        (device for device in devices if device.identifier == default_sink),
        None,
    )
    native_rate = output_device.sample_rate_hz if output_device else None
    warning = None
    if native_rate is not None:
        try:
            validate_audio_sample_rate(native_rate)
        except ValueError:
            native_rate = None
    if native_rate is None:
        warning = (
            "Output native sample rate could not be detected; "
            "using speaker-test default rate."
        )
    return AudioSpeakerTestPreparation(
        command=build_speaker_channel_test_command(native_rate, prompt_directory),
        output_name=default_sink,
        output_display_name=output_device.display_name if output_device else None,
        sample_format=output_device.sample_format if output_device else None,
        channels=output_device.channels if output_device else None,
        native_sample_rate_hz=native_rate,
        command_rate_hz=native_rate,
        warning=warning,
        prompt_directory=prompt_directory,
        prompt_source_rates=prompt_source_rates,
        prompt_resampled=prompt_resampled,
        prompt_cache_reused=prompt_cache_reused,
    )


def playback_file_metadata(path: str, size_bytes: int | None = None) -> AudioPlaybackFile:
    path = validate_playback_path(path)
    return AudioPlaybackFile(path, PurePosixPath(path).name or path, True, size_bytes)


def routing_target_matches(
    kind: str,
    requested_name: str,
    default_sink: str | None,
    default_source: str | None,
) -> bool:
    """Return whether `pactl info` confirms the requested route."""
    actual = default_sink if kind == "sink" else default_source if kind == "source" else None
    return actual == requested_name


def _build_default_command(kind: str, name: str) -> str:
    if not isinstance(name, str) or not name.strip() or "\n" in name or "\r" in name:
        raise ValueError("PulseAudio device name must be a non-empty single-line string.")
    return f"pactl set-default-{kind} {shlex.quote(name)}"


def parse_alsa_cards(output: str) -> list[str]:
    """Extract one readable entry per card from /proc/asound/cards."""
    cards: list[str] = []
    pattern = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.+)$")
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            description = match.group(3).strip()
            if " - " in description:
                description = description.rsplit(" - ", 1)[1].strip()
            cards.append(f"Card {match.group(1)}: {description}")
    return cards


RESPEAKER_CARD_NAME = "reSpeaker XVF3800 4-Mic Array"


def _compact_alsa_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def parse_respeaker_alsa_card(output: str) -> tuple[int, str] | None:
    """Find the reSpeaker card without relying on its ALSA card index."""
    target = _compact_alsa_name(RESPEAKER_CARD_NAME)
    card_pattern = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.+)$")
    current: tuple[int, str, list[str]] | None = None
    entries: list[tuple[int, str, list[str]]] = []
    for line in output.splitlines():
        match = card_pattern.match(line)
        if match:
            if current is not None:
                entries.append(current)
            current = (int(match.group(1)), match.group(3).strip(), [line])
        elif current is not None:
            current[2].append(line)
    if current is not None:
        entries.append(current)

    for card_index, description, lines in entries:
        block = " ".join(lines)
        if target not in _compact_alsa_name(block):
            continue
        card_name = description.rsplit(" - ", 1)[-1].strip()
        return card_index, card_name or RESPEAKER_CARD_NAME
    return None


def has_respeaker_pcm1_control(output: str) -> bool:
    """Return whether ``amixer scontrols`` exposes only the desired PCM,1."""
    return has_respeaker_pcm_control(output, 1)


def has_respeaker_pcm_control(output: str, index: int) -> bool:
    """Return whether a named PCM playback control exists."""
    return any(
        re.search(
            rf"Simple mixer control\s+'PCM',\s*{index}\b", line, re.IGNORECASE
        )
        for line in output.splitlines()
    )


def parse_respeaker_pcm1_state(output: str) -> RespeakerPcm1State:
    """Parse the mono PCM,1 playback level and optional switch state."""
    limits_match = re.search(
        r"Limits:\s*Playback\s+(-?\d+)\s*-\s*(-?\d+)", output, re.IGNORECASE
    )
    raw_min = int(limits_match.group(1)) if limits_match else None
    raw_max = int(limits_match.group(2)) if limits_match else None
    playback_lines = [line for line in output.splitlines() if "playback" in line.casefold()]
    playback_text = " ".join(playback_lines) or output
    raw_match = re.search(r"(?:Mono\s*:\s*)?Playback\s+(-?\d+)", playback_text, re.IGNORECASE)
    raw_value = int(raw_match.group(1)) if raw_match else None
    percent_match = re.search(r"\[(-?\d+)%\]", playback_text)
    level_percent = int(percent_match.group(1)) if percent_match else None
    if level_percent is None and raw_value is not None and raw_min is not None and raw_max is not None:
        span = raw_max - raw_min
        if span > 0:
            level_percent = round((raw_value - raw_min) * 100 / span)
    switch_match = re.search(r"\[(on|off)\]", playback_text, re.IGNORECASE)
    switch_on = switch_match.group(1).casefold() == "on" if switch_match else None
    return RespeakerPcm1State(
        level_percent=level_percent,
        switch_on=switch_on,
        raw_value=raw_value,
        raw_min=raw_min,
        raw_max=raw_max,
    )


def parse_alsa_devices(output: str) -> list[str]:
    """Extract card/device rows shared by `aplay -l` and `arecord -l`."""
    devices: list[str] = []
    pattern = re.compile(
        r"^card\s+(\d+):\s*([^\[]+)\[([^\]]+)\],\s*device\s+"
        r"(\d+):\s*([^\[]+)\[([^\]]+)\]",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match:
            card_number, _short_card, card_name, device_number, _short_device, device_name = match.groups()
            devices.append(
                f"Card {card_number}, device {device_number}: "
                f"{card_name.strip()} — {device_name.strip()}"
            )
    return devices


def snapshot_from_outputs(
    outputs: Mapping[str, str],
    errors: Mapping[str, str] | None = None,
) -> AudioDeviceSnapshot:
    """Convert command output into the model consumed by the Audio page."""
    default_sink, default_source = parse_pactl_info(outputs.get("pactl info", ""))
    return AudioDeviceSnapshot(
        default_sink=default_sink,
        default_source=default_source,
        sinks=(parse_pactl_devices(outputs.get("pactl list sinks", ""))
               or parse_pactl_short_devices(outputs.get("pactl list short sinks", ""))),
        sources=(parse_pactl_devices(outputs.get("pactl list sources", ""))
                 or parse_pactl_short_devices(outputs.get("pactl list short sources", ""))),
        alsa_cards=parse_alsa_cards(outputs.get("ALSA cards", "")),
        playback_devices=parse_alsa_devices(outputs.get("ALSA playback devices", "")),
        capture_devices=parse_alsa_devices(outputs.get("ALSA capture devices", "")),
        command_errors=dict(errors or {}),
        raw_info=dict(outputs),
    )
