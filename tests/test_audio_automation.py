import asyncio
from datetime import datetime
import io
import math
import struct
import tempfile
import unittest
import wave

from desktop_app.audio.audio_actions import run_audio_precheck
from desktop_app.audio.audio_automation import (
    AudioExecutionLogger,
    analyze_wav,
    analyze_wav_bytes,
    parse_pulse_summary,
    parse_usb_devices,
)


class _Result:
    def __init__(self, command, stdout="", stderr="", exit_status=0):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _SSH:
    def __init__(self, outputs):
        self.outputs = outputs
        self.commands = []

    async def run(self, command, timeout=15):
        self.commands.append(command)
        value = self.outputs.get(command)
        if value is None:
            return _Result(command, stderr="unexpected command", exit_status=1)
        if isinstance(value, _Result):
            return value
        return _Result(command, stdout=value)


CARDS = """ 0 [PCH            ]: HDA-Intel - HDA Intel PCH
 2 [Array          ]: USB-Audio - reSpeaker XVF3800 4-Mic Array
"""
CONTROLS = "Simple mixer control 'PCM',0\nSimple mixer control 'PCM',1\nSimple mixer control 'Headset',0\n"


def precheck_outputs(*, cards=CARDS):
    return {
        "lsusb": "Bus 001 Device 002: ID 2886:001a reSpeaker XVF3800 4-Mic Array\n",
        "cat /proc/asound/cards": cards,
        "pactl list short sources": "1\talsa_input.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\tmodule\n",
        "pactl list short sinks": "2\talsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\tmodule\n",
        "pactl get-default-source": "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\n",
        "pactl get-default-sink": "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\n",
        "amixer -c 2 scontrols": CONTROLS,
        "amixer -c 2 sget 'PCM',0": "Mono: Playback 30 [50%] [on]\n",
        "amixer -c 2 sget 'PCM',1": "Playback channels: Mono\nLimits: Playback 0 - 60\nMono: Playback 42 [70%] [on]\n",
    }


class AudioAutomationTests(unittest.TestCase):
    def test_usb_parser_detects_target_and_handles_missing(self):
        result = parse_usb_devices("Bus 001 Device 002: ID 2886:001a reSpeaker XVF3800\n")
        self.assertTrue(result["detected"])
        self.assertEqual(result["vid_pid"], "2886:001a")
        self.assertFalse(parse_usb_devices("Bus 001 Device 003: ID 1234:5678 Other\n")["detected"])

    def test_pulse_source_sink_and_defaults_are_structured(self):
        summary = parse_pulse_summary(
            "1\talsa_input.usb-test\tmodule\n",
            "2\talsa_output.usb-test\tmodule\n",
            "alsa_input.usb-test\n",
            "alsa_output.usb-test\n",
        )
        self.assertTrue(summary["source_detected"])
        self.assertTrue(summary["sink_detected"])
        self.assertTrue(summary["default_sink_ok"])

    def test_precheck_passes_with_dynamic_card_and_mixer(self):
        result = asyncio.run(run_audio_precheck(_SSH(precheck_outputs())))
        self.assertTrue(result.success)
        self.assertEqual(result.data["alsa"]["card_index"], 2)
        self.assertEqual(result.data["mixer"]["final_level_percent"], 70)
        self.assertEqual(result.data["mixer"]["pcm0_level_percent"], 50)

    def test_precheck_fails_cleanly_when_alsa_is_missing(self):
        result = asyncio.run(run_audio_precheck(_SSH(precheck_outputs(cards=" 0 [PCH]: HDA-Intel - HDA Intel PCH\n"))))
        self.assertFalse(result.success)
        self.assertEqual(result.data["alsa"]["detected"], False)
        self.assertEqual(result.state, "FAIL")

    @staticmethod
    def _wav_bytes(samples, sample_rate=16000, channels=2):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
        return stream.getvalue()

    def test_wav_metadata_analysis(self):
        payload = self._wav_bytes([1000, -1000] * 160)
        data = analyze_wav_bytes(payload)
        self.assertEqual(data["sample_rate"], 16000)
        self.assertEqual(data["channels"], 2)
        self.assertEqual(data["bit_depth"], 16)
        self.assertAlmostEqual(data["duration_sec"], 0.01, places=6)

    def test_wav_peak_rms_and_clipping_analysis(self):
        payload = self._wav_bytes([1000, 2000] * 10)
        data = analyze_wav_bytes(payload)
        self.assertAlmostEqual(data["channel_1"]["peak"], 1000 / 32767, places=6)
        self.assertAlmostEqual(data["channel_2"]["peak"], 2000 / 32767, places=6)
        self.assertGreater(data["channel_2"]["rms"], data["channel_1"]["rms"])
        self.assertFalse(data["clipping"])

    def test_wav_analysis_result_and_logger_are_structured(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as file:
            file.write(self._wav_bytes([100, -100] * 8))
            file.flush()
            result = analyze_wav(file.name)
        self.assertTrue(result.success)
        self.assertEqual(result.action, "analyze_wav")
        logger = AudioExecutionLogger()
        received = []
        logger.subscribe(received.append)
        event = logger.log("ANALYSIS", "PASS", "WAV metadata parsed")
        self.assertEqual(received, [event])
        self.assertIn("[ANALYSIS] [PASS]", event.format_line())


if __name__ == "__main__":
    unittest.main()
