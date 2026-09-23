import os
from datetime import datetime
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from desktop_app.audio.audio_automation import AudioLogEvent, make_action_result
from desktop_app.audio.audio_manager import AudioManager
from desktop_app.ui.audio_automated_page import AudioAutomatedPage


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
        self.state = None
        self.operation_succeeded = _Signal()
        self.operation_failed = _Signal()
        self.remote_process_started = _Signal()
        self.remote_process_output = _Signal()
        self.remote_process_finished = _Signal()
        self.remote_process_failed = _Signal()
        self.disconnected = _Signal()

    def submit_operation(self, _name, _operation):
        return "test:1"


class AudioAutomatedPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = _Service()
        self.manager = AudioManager(self.service)
        self.page = AudioAutomatedPage(self.manager, self.service, lambda: None)

    def tearDown(self):
        self.page.deleteLater()
        QApplication.processEvents()

    @staticmethod
    def _analysis_result(**overrides):
        data = {
            "path": "/tmp/capture_001.wav",
            "duration_sec": 10.001,
            "sample_rate_hz": 16000,
            "channels": 2,
            "bit_depth": 16,
            "frame_count": 160016,
            "channels_data": [
                {
                    "channel": 1,
                    "peak": 0.0251,
                    "rms": 0.00474,
                    "peak_dbfs": -32.0,
                    "rms_dbfs": -46.5,
                    "clipping_count": 0,
                    "clipping_ratio": 0.0,
                    "zero_signal": False,
                },
                {
                    "channel": 2,
                    "peak": 0.0576,
                    "rms": 0.0141,
                    "peak_dbfs": -24.8,
                    "rms_dbfs": -37.0,
                    "clipping_count": 0,
                    "clipping_ratio": 0.0,
                    "zero_signal": False,
                },
            ],
            "channel_delta_db": 9.5,
            "clipping_detected": False,
        }
        data.update(overrides)
        return make_action_result(
            action="analyze_wav",
            success=True,
            message="Analysis complete",
            started_at=datetime.now(),
            data=data,
        )

    def test_analysis_is_primary_and_runtime_is_compact(self):
        self.assertEqual(self.page.analysis_card.findChild(QLabel, "CardTitle").text(), "AUDIO ANALYSE")
        self.assertEqual(self.page.analysis_card.property("semantic"), "primary")
        self.assertEqual(self.page.log_text.objectName(), "AudioExecutionLog")
        self.assertIsNotNone(self.page._log_highlighter)
        self.assertNotIn("cpu", self.page._monitor_labels)
        self.assertNotIn("ram", self.page._monitor_labels)
        self.assertIn("pcm1", self.page._runtime_audio_labels)

    def test_available_metrics_populate_grouped_analysis_without_rebuilding_card(self):
        card = self.page.analysis_card
        self.page._update_analysis(self._analysis_result())
        self.assertIs(card, self.page.analysis_card)
        visible_groups = [frame for frame, _grid in self.page._analysis_groups.values() if not frame.isHidden()]
        self.assertEqual(len(visible_groups), 4)
        all_text = "\n".join(label.text() for label in self.page.findChildren(QLabel))
        self.assertIn("CH1 RMS dBFS", all_text)
        self.assertIn("-46.50 dBFS", all_text)
        self.assertIn("Channel Delta", all_text)
        self.assertEqual(self.page.critical_card.property("semantic"), "pass")

    def test_unavailable_channel_metric_is_hidden(self):
        result = self._analysis_result(
            channels=1,
            channels_data=[
                {
                    "channel": 1,
                    "peak": 0.1,
                    "rms": 0.05,
                    "rms_dbfs": -26.0,
                    "clipping_count": 0,
                    "clipping_ratio": 0.0,
                    "zero_signal": False,
                }
            ],
            channel_delta_db=None,
        )
        self.page._update_analysis(result)
        all_text = "\n".join(label.text() for label in self.page.findChildren(QLabel))
        self.assertNotIn("Channel Delta", all_text)
        self.assertIn("CH1 RMS dBFS", all_text)

    def test_execution_log_appends_incrementally_and_keeps_analysis_widget(self):
        self.page._update_analysis(self._analysis_result())
        card = self.page.analysis_card
        self.page._on_log_event(AudioLogEvent(datetime.now(), "AUDIO", "INFO", "Preparing audio device"))
        self.page._on_log_event(AudioLogEvent(datetime.now(), "ANALYSIS", "PASS", "Analysis completed"))
        lines = self.page.log_text.toPlainText().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Preparing audio device", lines[0])
        self.assertIn("[ANALYSIS] [PASS]", lines[1])
        self.assertIs(card, self.page.analysis_card)

    def test_section_states_are_textual_and_semantically_bordered(self):
        self.assertEqual(self.page.analysis_card.property("semantic"), "primary")
        self.assertEqual(self.page.critical_card.property("semantic"), "neutral")
        self.page._update_analysis(self._analysis_result())
        self.assertEqual(self.page.critical_card.property("semantic"), "pass")
        self.assertEqual(self.page.test_status_chip.text_label.text(), "IDLE")


if __name__ == "__main__":
    unittest.main()
