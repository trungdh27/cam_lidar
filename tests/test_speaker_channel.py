import asyncio
import json
import tempfile
import unittest

from desktop_app.audio.audio_evidence import AudioEvidenceManager
from desktop_app.audio.audio_models import parse_sink_channel_state
from desktop_app.audio.speaker_channel import (
    SpeakerPhysicalVerification,
    build_speaker_channel_command,
    overall_speaker_channel_result,
    prepare_speaker_channel_test,
)
from desktop_app.audio.audio_manager import AudioManager


class _Result:
    def __init__(self, command, stdout="", stderr="", exit_status=0):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _SSH:
    def __init__(self):
        self.commands = []

    async def run(self, command, timeout=15):
        self.commands.append(command)
        outputs = {
            "command -v speaker-test": "/usr/bin/speaker-test\n",
            "pactl get-default-sink": "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\n",
            "pactl list short sinks": "2\talsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\tmodule\n",
            "pactl get-sink-volume @DEFAULT_SINK@": "Volume: front-left: 19660 / 30% / -31.00 dB, front-right: 19660 / 30% / -31.00 dB\nBalance: 0.00\n",
            "pactl get-sink-mute @DEFAULT_SINK@": "Mute: no\n",
            "pactl list sinks": "Name: alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\nChannel Map: front-left,front-right\n",
            "cat /proc/asound/cards": " 2 [Array]: USB-Audio - reSpeaker XVF3800 4-Mic Array\n",
            "amixer -c 2 scontrols": "Simple mixer control 'PCM',0\nSimple mixer control 'PCM',1\n",
            "amixer -c 2 sget 'PCM',0": "Mono: Playback 30 [50%] [on]\n",
            "amixer -c 2 sget 'PCM',1": "Playback channels: Mono\nLimits: Playback 0 - 60\nMono: Playback 42 [70%] [on]\n",
        }
        return _Result(command, outputs.get(command, ""), exit_status=0)


class _Signal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)


class _Service:
    def __init__(self):
        self.is_connected = True
        self.operation_succeeded = _Signal()
        self.operation_failed = _Signal()
        self.remote_process_started = _Signal()
        self.remote_process_output = _Signal()
        self.remote_process_finished = _Signal()
        self.remote_process_failed = _Signal()
        self.disconnected = _Signal()
        self.submitted = []
        self.processes = []

    def submit_operation(self, name, operation):
        request_id = f"{name}:{len(self.submitted) + 1}"
        self.submitted.append((request_id, operation))
        return request_id

    def start_remote_process(self, name, command):
        request_id = f"{name}:{len(self.processes) + 1}"
        self.processes.append((request_id, command))
        return request_id

    def stop_remote_process(self, request_id):
        return None


class SpeakerChannelTests(unittest.TestCase):
    def test_exact_left_and_right_commands(self):
        left = build_speaker_channel_command("LEFT")
        right = build_speaker_channel_command("RIGHT")
        self.assertIn("-D pulse -c 2 -r 16000 -t sine -f 500 -s 1", left)
        self.assertIn("-s 2", right)
        self.assertNotIn("-s 1", right)

    def test_pulse_channel_state_is_per_channel(self):
        state = parse_sink_channel_state(
            "Volume: front-left: 19660 / 30% / -31 dB, front-right: 19660 / 30% / -31 dB\nBalance: 0.00\n",
            "Channel Map: front-left,front-right\n",
        )
        self.assertEqual(state["left_volume_percent"], 30)
        self.assertEqual(state["right_volume_percent"], 30)
        self.assertTrue(state["stereo"])

    def test_precheck_is_allowed_and_pcm1_is_dynamic(self):
        ssh = _SSH()
        result = asyncio.run(prepare_speaker_channel_test(ssh, "RIGHT"))
        self.assertEqual(result.channel.value, "RIGHT")
        self.assertEqual(result.pcm1_percent, 70)
        self.assertIn("amixer -c 2 sget 'PCM',1", ssh.commands)
        self.assertNotIn("amixer -c 1 sget 'PCM',1", ssh.commands)

    def test_precheck_blocks_when_respeaker_sink_is_missing(self):
        class MissingSink(_SSH):
            async def run(self, command, timeout=15):
                result = await super().run(command, timeout)
                if command == "pactl list short sinks":
                    result.stdout = "1\talsa_output.pci-BuiltIn.analog-stereo\tmodule\n"
                return result

        with self.assertRaisesRegex(RuntimeError, "reSpeaker output sink"):
            asyncio.run(prepare_speaker_channel_test(MissingSink(), "LEFT"))

    def test_manager_disconnect_is_blocked_without_starting_process(self):
        service = _Service()
        service.is_connected = False
        manager = AudioManager(service)
        results = []
        manager.automation_action_finished.connect(results.append)
        self.assertFalse(manager.run_speaker_channel_test("LEFT"))
        self.assertEqual(results[-1].state, "BLOCKED")
        self.assertEqual(service.processes, [])

    def test_overall_result_requires_both_physical_confirmations(self):
        self.assertEqual(
            overall_speaker_channel_result("PASS", "HEARD_CORRECT_SIDE", "PASS", "NOT_VERIFIED"),
            "MANUAL VERIFY",
        )
        self.assertEqual(
            overall_speaker_channel_result("PASS", "HEARD_CORRECT_SIDE", "PASS", "NO_SOUND"),
            "FAIL",
        )
        self.assertEqual(
            overall_speaker_channel_result("PASS", "HEARD_CORRECT_SIDE", "PASS", "HEARD_CORRECT_SIDE"),
            "PASS",
        )

    def test_evidence_result_json_updates_after_manual_verification(self):
        with tempfile.TemporaryDirectory() as root:
            manager = AudioEvidenceManager(root)
            manager.start_automation_session(jetson_connected=True)
            summary = {
                "output": "reSpeaker XVF3800",
                "left": {"execution": "PASS", "physical_verification": "HEARD_CORRECT_SIDE"},
                "right": {"execution": "PASS", "physical_verification": "NO_SOUND"},
                "overall": "FAIL",
            }
            manager.record_speaker_channel_execution(
                "RIGHT",
                {"execution_status": "PASS", "command": build_speaker_channel_command("RIGHT"), "pulse_state": {}, "mixer": {}},
                summary=summary,
            )
            manager.record_speaker_channel_verification("RIGHT", "NO_SOUND", summary=summary)
            result_path = manager.speaker_channel_path / "result.json"
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["right"]["physical_verification"], "NO_SOUND")
            self.assertEqual(loaded["overall"], "FAIL")

    def test_manager_keeps_execution_and_physical_results_separate(self):
        service = _Service()
        manager = AudioManager(service)
        results = []
        manager.automation_action_finished.connect(results.append)
        self.assertTrue(manager.run_speaker_channel_test("RIGHT"))
        preflight_id, operation = service.submitted[-1]
        precheck = asyncio.run(operation(_SSH()))
        service.operation_succeeded.emit(preflight_id, precheck)
        process_id, command = service.processes[-1]
        self.assertIn("-s 2", command)
        service.remote_process_finished.emit(process_id, 0)
        self.assertTrue(results[-1].success)
        self.assertEqual(results[-1].data["physical_verification"], "NOT_VERIFIED")
        self.assertEqual(manager.speaker_channel_summary()["overall"], "MANUAL VERIFY")
        manager.set_speaker_physical_verification("RIGHT", "NO_SOUND")
        self.assertEqual(manager.speaker_channel_summary()["overall"], "FAIL")


if __name__ == "__main__":
    unittest.main()
