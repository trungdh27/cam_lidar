"""Prepare spoken ALSA speaker-test prompts for a PulseAudio sink rate.

The Jetson normally has the ALSA prompt files installed under
``/usr/share/sounds/alsa``.  Those files are not guaranteed to have the same
sample rate as the current PulseAudio sink, so this module also provides a
small standard-library resampler and a self-contained remote helper command.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import tempfile
import textwrap
import wave

try:  # audioop was removed from newer Python versions.
    import audioop  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the interpreter.
    audioop = None


PROMPT_FILENAMES = ("Front_Left.wav", "Front_Right.wav")
DEFAULT_PROMPT_SOURCE_DIRECTORY = "/usr/share/sounds/alsa"


class PromptWavPreparationError(RuntimeError):
    """Raised when the spoken speaker-test prompts cannot be prepared."""


@dataclass(frozen=True)
class PromptWavPreparation:
    """Result of validating or creating a compatible prompt directory."""

    directory: str
    target_rate_hz: int | None
    source_rates: tuple[int, ...]
    resampled: bool
    cache_reused: bool


def _read_pcm_wav(path: Path) -> tuple[int, int, int, bytes]:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            compression = wav_file.getcomptype()
            frames = wav_file.readframes(wav_file.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise PromptWavPreparationError(f"Invalid WAV prompt {path}: {exc}") from exc
    if channels <= 0 or sample_width not in {1, 2, 3, 4} or sample_rate <= 0:
        raise PromptWavPreparationError(f"Unsupported WAV format for prompt {path}.")
    if compression != "NONE":
        raise PromptWavPreparationError(
            f"Compressed WAV prompts are not supported: {path}."
        )
    if not frames:
        raise PromptWavPreparationError(f"WAV prompt has no audio frames: {path}.")
    return channels, sample_width, sample_rate, frames


def _decode_sample(data: bytes, offset: int, sample_width: int) -> int:
    if sample_width == 1:
        return data[offset] - 128
    value = int.from_bytes(data[offset : offset + sample_width], "little", signed=False)
    if value & (1 << (sample_width * 8 - 1)):
        value -= 1 << (sample_width * 8)
    return value


def _encode_sample(value: int, sample_width: int) -> bytes:
    if sample_width == 1:
        value = max(-128, min(127, value)) + 128
        return bytes((value,))
    minimum = -(1 << (sample_width * 8 - 1))
    maximum = (1 << (sample_width * 8 - 1)) - 1
    value = max(minimum, min(maximum, value))
    return int(value).to_bytes(sample_width, "little", signed=True)


def _resample_pcm_linear(
    frames: bytes,
    channels: int,
    sample_width: int,
    source_rate: int,
    target_rate: int,
) -> bytes:
    """Resample interleaved PCM without third-party packages.

    The spoken prompts are short, so a linear frame interpolator is small,
    deterministic, and adequate for preparing a test announcement.  It keeps
    the original channel count and sample width intact.
    """
    frame_width = channels * sample_width
    frame_count = len(frames) // frame_width
    if frame_count == 0:
        return b""
    target_count = max(1, int(round(frame_count * target_rate / source_rate)))
    output = bytearray(target_count * frame_width)
    for output_index in range(target_count):
        source_position = output_index * source_rate / target_rate
        left_index = min(int(source_position), frame_count - 1)
        right_index = min(left_index + 1, frame_count - 1)
        fraction = source_position - left_index
        left_offset = left_index * frame_width
        right_offset = right_index * frame_width
        output_offset = output_index * frame_width
        for channel in range(channels):
            channel_offset = channel * sample_width
            left = _decode_sample(frames, left_offset + channel_offset, sample_width)
            right = _decode_sample(frames, right_offset + channel_offset, sample_width)
            value = int(round(left + (right - left) * fraction))
            output[output_offset + channel_offset : output_offset + channel_offset + sample_width] = _encode_sample(
                value, sample_width
            )
    return bytes(output)


def _resample_pcm(
    frames: bytes,
    channels: int,
    sample_width: int,
    source_rate: int,
    target_rate: int,
) -> bytes:
    if audioop is not None:
        converted, _state = audioop.ratecv(
            frames,
            sample_width,
            channels,
            source_rate,
            target_rate,
            None,
        )
        return converted
    return _resample_pcm_linear(
        frames, channels, sample_width, source_rate, target_rate
    )


def _write_pcm_wav(
    path: Path,
    channels: int,
    sample_width: int,
    sample_rate: int,
    frames: bytes,
) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.setcomptype("NONE", "not compressed")
        wav_file.writeframes(frames)


def _cache_is_valid(cache_directory: Path, target_rate_hz: int) -> bool:
    try:
        for filename in PROMPT_FILENAMES:
            channels, sample_width, rate, frames = _read_pcm_wav(
                cache_directory / filename
            )
            if channels <= 0 or sample_width not in {1, 2, 3, 4}:
                return False
            if rate != target_rate_hz or not frames:
                return False
    except PromptWavPreparationError:
        return False
    return True


def prepare_prompt_wavs(
    source_directory: str | os.PathLike[str],
    cache_directory: str | os.PathLike[str],
    target_rate_hz: int | None,
) -> PromptWavPreparation:
    """Validate source prompts and create/reuse rate-compatible cache files."""
    source_path = Path(source_directory)
    cache_path = Path(cache_directory)
    source_metadata: list[tuple[int, int, int, bytes]] = []
    for filename in PROMPT_FILENAMES:
        source_metadata.append(_read_pcm_wav(source_path / filename))
    source_rates = tuple(item[2] for item in source_metadata)

    if target_rate_hz is None or all(rate == target_rate_hz for rate in source_rates):
        return PromptWavPreparation(
            str(source_path), target_rate_hz, source_rates, False, False
        )

    if _cache_is_valid(cache_path, target_rate_hz):
        return PromptWavPreparation(
            str(cache_path), target_rate_hz, source_rates, True, True
        )

    try:
        cache_path.mkdir(parents=True, exist_ok=True)
        for filename, (channels, sample_width, source_rate, frames) in zip(
            PROMPT_FILENAMES, source_metadata
        ):
            converted = (
                frames
                if source_rate == target_rate_hz
                else _resample_pcm(
                    frames,
                    channels,
                    sample_width,
                    source_rate,
                    target_rate_hz,
                )
            )
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=f".{filename}.",
                    suffix=".tmp",
                    dir=cache_path,
                    delete=False,
                ) as temporary_file:
                    temporary_path = temporary_file.name
                _write_pcm_wav(
                    Path(temporary_path),
                    channels,
                    sample_width,
                    target_rate_hz,
                    converted,
                )
                os.replace(temporary_path, cache_path / filename)
                temporary_path = None
            finally:
                if temporary_path:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass
    except (OSError, PromptWavPreparationError) as exc:
        raise PromptWavPreparationError(
            f"Unable to create cached speaker-test prompts: {exc}"
        ) from exc
    return PromptWavPreparation(
        str(cache_path), target_rate_hz, source_rates, True, False
    )


_REMOTE_PROMPT_PREPARER_SCRIPT = textwrap.dedent(
    (r'''\
    import json
    import os
    from pathlib import Path
    import tempfile
    import wave
    try:
        import audioop
    except ImportError:
        audioop = None

    PROMPTS = ("Front_Left.wav", "Front_Right.wav")

    def fail(message):
        raise RuntimeError(message)

    def read_wav(path):
        try:
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                width = wav_file.getsampwidth()
                rate = wav_file.getframerate()
                compression = wav_file.getcomptype()
                frames = wav_file.readframes(wav_file.getnframes())
        except (OSError, EOFError, wave.Error) as exc:
            fail(f"Invalid WAV prompt {path}: {exc}")
        if channels <= 0 or width not in {1, 2, 3, 4} or rate <= 0:
            fail(f"Unsupported WAV format for prompt {path}")
        if compression != "NONE":
            fail(f"Compressed WAV prompt is not supported: {path}")
        if not frames:
            fail(f"WAV prompt has no audio frames: {path}")
        return channels, width, rate, frames

    def decode(data, offset, width):
        if width == 1:
            return data[offset] - 128
        value = int.from_bytes(data[offset:offset + width], "little", signed=False)
        if value & (1 << (width * 8 - 1)):
            value -= 1 << (width * 8)
        return value

    def encode(value, width):
        if width == 1:
            value = max(-128, min(127, value)) + 128
            return bytes((value,))
        minimum = -(1 << (width * 8 - 1))
        maximum = (1 << (width * 8 - 1)) - 1
        value = max(minimum, min(maximum, value))
        return int(value).to_bytes(width, "little", signed=True)

    def linear_resample(frames, channels, width, source_rate, target_rate):
        frame_width = channels * width
        frame_count = len(frames) // frame_width
        if frame_count == 0:
            return b""
        target_count = max(1, int(round(frame_count * target_rate / source_rate)))
        output = bytearray(target_count * frame_width)
        for output_index in range(target_count):
            position = output_index * source_rate / target_rate
            left_index = min(int(position), frame_count - 1)
            right_index = min(left_index + 1, frame_count - 1)
            fraction = position - left_index
            left_offset = left_index * frame_width
            right_offset = right_index * frame_width
            output_offset = output_index * frame_width
            for channel in range(channels):
                offset = channel * width
                left = decode(frames, left_offset + offset, width)
                right = decode(frames, right_offset + offset, width)
                value = int(round(left + (right - left) * fraction))
                output[output_offset + offset:output_offset + offset + width] = encode(value, width)
        return bytes(output)

    def resample(frames, channels, width, source_rate, target_rate):
        if audioop is not None:
            converted, _state = audioop.ratecv(
                frames, width, channels, source_rate, target_rate, None
            )
            return converted
        return linear_resample(frames, channels, width, source_rate, target_rate)

    def write_wav(path, channels, width, rate, frames):
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(width)
            wav_file.setframerate(rate)
            wav_file.setcomptype("NONE", "not compressed")
            wav_file.writeframes(frames)

    def valid_cache(directory, target_rate):
        try:
            for filename in PROMPTS:
                channels, width, rate, frames = read_wav(directory / filename)
                if rate != target_rate or not frames:
                    return False
        except RuntimeError:
            return False
        return True

    def prepare(source, cache, target_rate):
        metadata = [read_wav(source / filename) for filename in PROMPTS]
        source_rates = [item[2] for item in metadata]
        if target_rate == 0 or all(rate == target_rate for rate in source_rates):
            return {
                "directory": str(source),
                "target_rate_hz": target_rate or None,
                "source_rates": source_rates,
                "resampled": False,
                "cache_reused": False,
            }
        if valid_cache(cache, target_rate):
            return {
                "directory": str(cache),
                "target_rate_hz": target_rate,
                "source_rates": source_rates,
                "resampled": True,
                "cache_reused": True,
            }
        cache.mkdir(parents=True, exist_ok=True)
        for filename, (channels, width, source_rate, frames) in zip(PROMPTS, metadata):
            converted = frames if source_rate == target_rate else resample(
                frames, channels, width, source_rate, target_rate
            )
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix="." + filename + ".", suffix=".tmp", dir=cache, delete=False
                ) as temporary_file:
                    temporary = temporary_file.name
                write_wav(Path(temporary), channels, width, target_rate, converted)
                os.replace(temporary, cache / filename)
                temporary = None
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        return {
            "directory": str(cache),
            "target_rate_hz": target_rate,
            "source_rates": source_rates,
            "resampled": True,
            "cache_reused": False,
        }

    def main():
        source, cache, target = Path(os.sys.argv[1]), Path(os.sys.argv[2]), int(os.sys.argv[3])
        print(json.dumps(prepare(source, cache, target), separators=(",", ":")))

    try:
        main()
    except Exception as exc:
        print(str(exc), file=os.sys.stderr)
        raise SystemExit(1)
    ''').replace("\\\n", "", 1)
).strip()


def build_remote_prompt_preparation_command(
    source_directory: str,
    cache_directory: str,
    target_rate_hz: int | None,
) -> str:
    """Build a quoted, dependency-free command for Jetson prompt preparation."""
    target = 0 if target_rate_hz is None else int(target_rate_hz)
    if target < 0:
        raise ValueError("Speaker-test target rate must not be negative.")
    return "python3 -c {script} {source} {cache} {target}".format(
        script=shlex.quote(_REMOTE_PROMPT_PREPARER_SCRIPT),
        source=shlex.quote(source_directory),
        cache=shlex.quote(cache_directory),
        target=target,
    )


def parse_remote_prompt_preparation(output: str) -> PromptWavPreparation:
    """Parse and validate the JSON emitted by the remote helper."""
    try:
        payload = json.loads(output)
        directory = payload["directory"]
        target_rate = payload.get("target_rate_hz")
        source_rates = tuple(int(rate) for rate in payload.get("source_rates", []))
        resampled = bool(payload.get("resampled", False))
        cache_reused = bool(payload.get("cache_reused", False))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PromptWavPreparationError(
            "Remote prompt preparation returned invalid metadata."
        ) from exc
    if not isinstance(directory, str) or not directory.startswith("/"):
        raise PromptWavPreparationError("Remote prompt directory is not absolute.")
    if target_rate is not None:
        try:
            target_rate = int(target_rate)
        except (TypeError, ValueError) as exc:
            raise PromptWavPreparationError("Remote prompt rate is invalid.") from exc
    return PromptWavPreparation(
        directory, target_rate, source_rates, resampled, cache_reused
    )
