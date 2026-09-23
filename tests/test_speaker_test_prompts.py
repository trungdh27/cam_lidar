import tempfile
import unittest
from pathlib import Path
import wave

from desktop_app.audio.audio_models import build_speaker_channel_test_command
from desktop_app.audio.speaker_test_prompts import (
    PromptWavPreparationError,
    build_remote_prompt_preparation_command,
    parse_remote_prompt_preparation,
    prepare_prompt_wavs,
)


def _write_test_wav(path: Path, sample_rate: int, channels: int = 2) -> None:
    frame_count = max(1, sample_rate // 100)
    frames = bytearray()
    for index in range(frame_count):
        sample = ((index % 32) - 16) * 512
        for _channel in range(channels):
            frames.extend(int(sample).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames)


class SpeakerTestPromptTests(unittest.TestCase):
    def _source_directory(self, rate: int) -> tempfile.TemporaryDirectory:
        temporary = tempfile.TemporaryDirectory()
        source = Path(temporary.name)
        _write_test_wav(source / "Front_Left.wav", rate)
        _write_test_wav(source / "Front_Right.wav", rate)
        return temporary

    def test_matching_rate_uses_system_prompt_directory_without_resampling(self):
        with self._source_directory(16000) as source_directory, tempfile.TemporaryDirectory() as cache:
            result = prepare_prompt_wavs(source_directory, cache, 16000)
            self.assertEqual(result.directory, source_directory)
            self.assertEqual(result.source_rates, (16000, 16000))
            self.assertFalse(result.resampled)
            self.assertFalse(result.cache_reused)

    def test_48000_prompts_are_resampled_to_16000(self):
        with self._source_directory(48000) as source_directory, tempfile.TemporaryDirectory() as cache:
            result = prepare_prompt_wavs(source_directory, cache, 16000)
            self.assertEqual(result.directory, cache)
            self.assertTrue(result.resampled)
            for filename in ("Front_Left.wav", "Front_Right.wav"):
                with wave.open(str(Path(cache) / filename), "rb") as wav_file:
                    self.assertEqual(wav_file.getframerate(), 16000)
                    self.assertEqual(wav_file.getnchannels(), 2)
                    self.assertEqual(wav_file.getsampwidth(), 2)

    def test_cache_is_reused(self):
        with self._source_directory(48000) as source_directory, tempfile.TemporaryDirectory() as cache:
            first = prepare_prompt_wavs(source_directory, cache, 16000)
            paths = [Path(cache) / filename for filename in ("Front_Left.wav", "Front_Right.wav")]
            mtimes = [path.stat().st_mtime_ns for path in paths]
            second = prepare_prompt_wavs(source_directory, cache, 16000)
            self.assertTrue(first.resampled)
            self.assertTrue(second.cache_reused)
            self.assertEqual(mtimes, [path.stat().st_mtime_ns for path in paths])

    def test_other_native_rates_create_matching_prompts(self):
        for rate in (44100, 48000):
            with self.subTest(rate=rate):
                with self._source_directory(48000) as source_directory, tempfile.TemporaryDirectory() as cache:
                    result = prepare_prompt_wavs(source_directory, cache, rate)
                    self.assertEqual(result.target_rate_hz, rate)
                    with wave.open(str(Path(result.directory) / "Front_Left.wav"), "rb") as wav_file:
                        self.assertEqual(wav_file.getframerate(), rate)

    def test_missing_or_malformed_prompt_fails_safely(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as cache:
            source_path = Path(source)
            _write_test_wav(source_path / "Front_Left.wav", 48000)
            with self.assertRaises(PromptWavPreparationError):
                prepare_prompt_wavs(source, cache, 16000)
            (source_path / "Front_Right.wav").write_bytes(b"not a wav")
            with self.assertRaises(PromptWavPreparationError):
                prepare_prompt_wavs(source, cache, 16000)

    def test_spoken_command_uses_native_rate_and_prompt_directory(self):
        command = build_speaker_channel_test_command(
            16000, "/home/agx/.cache/cam_lidar/audio_speaker_test/16000"
        )
        self.assertIn("-r 16000", command)
        self.assertIn("-t wav", command)
        self.assertIn("-W /home/agx/.cache/cam_lidar/audio_speaker_test/16000", command)
        self.assertNotIn("-t sine", command)
        self.assertNotIn("hw:", command)
        self.assertNotIn("PCM1", command)

    def test_remote_helper_command_has_no_alsa_card_selection(self):
        command = build_remote_prompt_preparation_command(
            "/usr/share/sounds/alsa",
            "/home/agx/.cache/cam_lidar/audio_speaker_test/16000",
            16000,
        )
        self.assertNotIn("hw:", command)
        self.assertNotIn("PCM1", command)
        self.assertIn("/usr/share/sounds/alsa", command)

    def test_remote_metadata_is_parsed(self):
        result = parse_remote_prompt_preparation(
            '{"directory":"/home/user/cache/16000","target_rate_hz":16000,'
            '"source_rates":[48000,48000],"resampled":true,"cache_reused":false}'
        )
        self.assertEqual(result.directory, "/home/user/cache/16000")
        self.assertEqual(result.source_rates, (48000, 48000))
        self.assertTrue(result.resampled)


if __name__ == "__main__":
    unittest.main()
