import json
from pathlib import Path
import tempfile
import unittest

from desktop_app.audio.audio_evidence import AudioEvidenceManager, SessionState
from desktop_app.audio.audio_models import AudioDevice, AudioDeviceSnapshot


class AudioEvidenceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = AudioEvidenceManager(Path(self.temp_dir.name) / "evidence")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_session_id_and_directory_creation(self):
        session_id = self.manager.start_session(host="jetson-test")

        self.assertRegex(session_id, r"^AUDIO_\d{8}_\d{6}(_\d{2})?$")
        self.assertTrue(self.manager.evidence_path.is_dir())
        self.assertTrue((self.manager.evidence_path / "session.json").is_file())
        self.assertTrue((self.manager.evidence_path / "session.log").is_file())
        self.assertTrue((self.manager.evidence_path / "operations.jsonl").is_file())
        summary = json.loads((self.manager.evidence_path / "session.json").read_text())
        self.assertEqual(summary["session_status"], "ACTIVE")
        self.assertEqual(summary["host"], "jetson-test")

    def test_baseline_writes_pulse_alsa_and_wav_operation_evidence(self):
        self.manager.start_session(host="jetson-test")
        snapshot = AudioDeviceSnapshot(
            default_sink="alsa_output.usb.test",
            default_source="alsa_input.usb.test",
            sinks=[AudioDevice("alsa_output.usb.test", "USB Speaker")],
            sources=[AudioDevice("alsa_input.usb.test", "USB Mic")],
            alsa_cards=["USB Audio"],
            playback_devices=["card 1: USB"],
            capture_devices=["card 1: USB"],
            raw_info={
                "pactl info": "Default Sink: alsa_output.usb.test",
                "pactl list short sinks": "1 alsa_output.usb.test",
                "pactl list short sources": "2 alsa_input.usb.test",
                "pactl list sinks": "sink details",
                "pactl list sources": "source details",
                "ALSA cards": "USB Audio",
                "ALSA playback devices": "card 1: USB",
                "ALSA capture devices": "card 1: USB",
            },
        )
        self.manager.record_baseline(snapshot)
        self.manager.record_operation(
            "RECORDING_COMPLETED",
            "SUCCESS",
            details={"remote_path": "/home/agx/audio_test_logs/manual/record.wav", "size_bytes": 320044},
        )

        self.assertIn("pactl info", (self.manager.evidence_path / "pulse_audio.txt").read_text())
        self.assertIn("ALSA cards", (self.manager.evidence_path / "alsa_devices.txt").read_text())
        operations = (self.manager.evidence_path / "operations.jsonl").read_text()
        self.assertIn("RECORDING_COMPLETED", operations)
        self.assertIn("/home/agx/audio_test_logs/manual/record.wav", operations)

    def test_notes_volume_routing_and_end_state_are_serialized(self):
        self.manager.start_session(host="jetson-test")
        self.manager.update_state(
            default_output="sink.new",
            default_input="source.new",
            speaker_volume=85,
            speaker_muted=False,
        )
        self.manager.record_operation(
            "SET_VOLUME",
            "SUCCESS",
            details={"previous": 25, "requested": 85, "confirmed": 85, "warning": "High volume"},
        )
        self.manager.record_operation(
            "SET_DEFAULT_OUTPUT", "SUCCESS", details={"previous": "sink.old", "new": "sink.new"}
        )
        self.assertTrue(self.manager.record_note("Microphone capture contains continuous noise."))
        self.assertTrue(self.manager.end_session())
        self.assertEqual(self.manager.state, SessionState.COMPLETED)
        summary = json.loads((self.manager.evidence_path / "session.json").read_text())
        self.assertEqual(summary["session_status"], "COMPLETED")
        self.assertEqual(summary["speaker_volume"], 85)
        self.assertEqual(summary["default_output"], "sink.new")
        self.assertEqual(summary["notes_count"], 1)
        self.assertEqual(summary["result_summary"]["success"], 2)

    def test_disconnect_preserves_interrupted_session_and_redacts_credentials(self):
        self.manager.start_session(
            host="jetson-test",
            jetson_info={"hostname": "jetson", "username": "agx", "password": "secret"},
        )
        self.manager.mark_interrupted("Jetson disconnected during Audio session.")
        self.assertEqual(self.manager.state, SessionState.INTERRUPTED)
        self.assertTrue(self.manager.end_session())
        summary = json.loads((self.manager.evidence_path / "session.json").read_text())
        self.assertEqual(summary["session_status"], "INTERRUPTED")
        self.assertNotIn("password", json.dumps(summary).lower())
        self.assertNotIn("secret", json.dumps(summary).lower())

    def test_session_ids_avoid_same_second_collision(self):
        first = self.manager.start_session()
        self.manager.end_session()
        second = self.manager.start_session()
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith(first.split("_")[0] + "_"))


if __name__ == "__main__":
    unittest.main()
