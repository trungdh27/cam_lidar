import time
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QObject, Signal

from desktop_app.audio.audio_automation import make_action_result
from desktop_app.audio.audio_evidence import AudioEvidenceManager
from desktop_app.audio.audio_runtime import (
    AudioIterationRunner,
    AudioRuntimeMonitor,
    calculate_cpu_percent,
    classify_kernel_event,
    parse_loadavg,
    parse_meminfo,
    parse_proc_stat,
)


class _Signal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)


class _Service(QObject):
    def __init__(self):
        super().__init__()
        self.operation_succeeded = _Signal()
        self.operation_failed = _Signal()
        self.is_connected = True

    def submit_operation(self, _name, _operation):
        return "audio_runtime_monitor:1"


class AudioRuntimeParsingTests(unittest.TestCase):
    def test_cpu_delta_and_malformed_data(self):
        previous = parse_proc_stat("cpu 100 0 50 50 0 0 0 0 0 0\n")
        current = parse_proc_stat("cpu 200 0 100 100 0 0 0 0 0 0\n")
        self.assertEqual(previous, (200, 50))
        self.assertAlmostEqual(calculate_cpu_percent(previous, current), 75.0, places=3)
        self.assertIsNone(parse_proc_stat("not cpu data\n"))
        self.assertIsNone(calculate_cpu_percent(previous, previous))

    def test_meminfo_and_loadavg(self):
        memory = parse_meminfo("MemTotal:       102400 kB\nMemAvailable:    76800 kB\n")
        self.assertAlmostEqual(memory["total_mb"], 100.0)
        self.assertAlmostEqual(memory["used_mb"], 25.0)
        self.assertAlmostEqual(memory["used_percent"], 25.0)
        self.assertEqual(parse_loadavg("1.2 0.8 0.4 2/100 123\n"), (1.2, 0.8, 0.4))
        self.assertIsNone(parse_loadavg("bad"))

    def test_kernel_classification(self):
        self.assertEqual(classify_kernel_event("usb 1-3: USB disconnect"), "USB_DISCONNECT")
        self.assertEqual(classify_kernel_event("usb 1-3: reset high-speed USB device"), "USB_RESET")
        self.assertEqual(classify_kernel_event("xrun!!! underrun"), "XRUN")
        self.assertEqual(classify_kernel_event("ALSA I/O error"), "IO_ERROR")
        self.assertIsNone(classify_kernel_event("random kernel message"))


class AudioRuntimeMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.evidence = AudioEvidenceManager(Path(self.temp_dir.name) / "evidence")
        self.evidence.start_automation_session(host="robot")
        self.service = _Service()
        self.monitor = AudioRuntimeMonitor(
            self.service,
            self.evidence,
            process_state_provider=lambda: {"capture_running": True, "playback_running": False},
        )
        self.monitor._active = True
        self.monitor._started_monotonic = time.monotonic()
        self.monitor._started_at = "2026-09-22T15:00:00+07:00"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _snapshot(*, present=True, kernel=""):
        return {
            "proc_stat": {"stdout": "cpu 200 0 100 100 0 0 0 0 0 0\n"},
            "meminfo": {"stdout": "MemTotal: 102400 kB\nMemAvailable: 76800 kB\n"},
            "loadavg": {"stdout": "1.0 0.5 0.2 1/100 10\n"},
            "usb": {"stdout": "ID 2886:001a reSpeaker" if present else ""},
            "alsa": {"stdout": " 2 [Array]: USB-Audio - reSpeaker XVF3800 4-Mic Array\n" if present else " 0 [PCH]: HDA-Intel\n"},
            "pulse_sources": {"stdout": "1\talsa_input.usb-respeaker\tmodule\n" if present else ""},
            "pulse_sinks": {"stdout": "2\talsa_output.usb-respeaker\tmodule\n" if present else ""},
            "kernel": {"stdout": kernel},
        }

    def test_device_transitions_and_runtime_csv(self):
        self.monitor._consume_snapshot(self._snapshot())
        self.monitor._consume_snapshot(self._snapshot(present=False))
        self.monitor._consume_snapshot(self._snapshot(present=True))
        self.assertEqual(self.monitor._disconnect_count, 1)
        self.assertEqual(self.monitor._reconnect_count, 1)
        self.assertEqual(self.monitor._audio_error_count, 2)
        self.assertEqual(len(self.monitor._samples), 3)
        csv_path = self.evidence.monitor_path / "runtime.csv"
        rows = csv_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 4)
        self.assertIn("cpu_percent", rows[0])
        self.assertTrue(self.monitor.stop())
        summary = self.monitor.last_summary
        self.assertEqual(summary["events"]["disconnect"], 1)
        self.assertTrue((self.evidence.monitor_path / "runtime_summary.json").is_file())

    def test_kernel_line_is_not_counted_twice(self):
        line = "usb 1-3: USB disconnect"
        self.monitor._consume_kernel_events(f"{line}\n{line}\n")
        self.monitor._consume_kernel_events(f"{line}\n")
        self.assertEqual(self.monitor._disconnect_count, 1)
        self.assertEqual(len(self.monitor._events), 1)


class AudioIterationRunnerTests(unittest.TestCase):
    def test_three_passes_are_counted_and_saved(self):
        temp_dir = tempfile.TemporaryDirectory()
        evidence = AudioEvidenceManager(Path(temp_dir.name) / "evidence")
        evidence.start_automation_session(host="robot")
        runner = AudioIterationRunner(evidence_manager=evidence)
        self.assertTrue(
            runner.start(
                action="capture",
                iterations=3,
                interval_sec=0,
                start_action=lambda _iteration: True,
            )
        )
        for _ in range(3):
            runner.handle_action_result(
                make_action_result(
                    action="capture",
                    success=True,
                    message="capture complete",
                    started_at=datetime.now(),
                    data={"output_file": "capture.wav"},
                )
            )
        self.assertFalse(runner.active)
        campaign = next((evidence.reliability_path).glob("campaign_*.json"))
        self.assertTrue(campaign.is_file())
        self.assertEqual(len(list(evidence.reliability_path.glob("campaign_001_iteration_*.json"))), 3)
        temp_dir.cleanup()

    def test_failed_iteration_does_not_stop_default_campaign(self):
        runner = AudioIterationRunner()
        self.assertTrue(
            runner.start(
                action="capture",
                iterations=3,
                interval_sec=0,
                start_action=lambda _iteration: True,
            )
        )
        for success in (True, False, True):
            runner.handle_action_result(
                make_action_result(
                    action="capture",
                    success=success,
                    message="ok" if success else "capture failed",
                    started_at=datetime.now(),
                )
            )
        self.assertFalse(runner.active)
        self.assertEqual(sum(item.status == "PASS" for item in runner._results), 2)
        self.assertEqual(sum(item.status == "FAIL" for item in runner._results), 1)


if __name__ == "__main__":
    unittest.main()
