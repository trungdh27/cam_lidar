import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.ai_page import AiPage


class AiPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = JetsonState(); self.service = JetsonConnectionService(self.state); self.page = AiPage(self.state, self.service)

    def tearDown(self):
        self.service.shutdown(); self.page.deleteLater()

    def test_page_has_monitor_tests_and_performance_placeholder(self):
        self.assertEqual(self.page.stack.count(), 3)
        self.assertEqual(self.page.test_table.rowCount(), 5)
        self.assertEqual([self.page.test_table.item(row, 1).text() for row in range(5)], ["AI-001", "AI-002", "AI-003", "AI-004", "AI-005"])
        self.page.performance_button.click()
        self.assertIn("Phase 8.4C", " ".join(item.text() for item in self.page.stack.currentWidget().findChildren(QLabel)))

    def test_discovery_populates_target_monitor_pipeline_and_detail(self):
        self.page._request_id = "request"
        self.page._on_operation_succeeded("request", {"ai_discovery": {
            "runtime_available": True, "runtime_name": "TensorRT", "runtime_version": "8.6", "ros_available": True, "ai_packages": ["vision_inference"], "model_candidates": ["/models/test.engine"], "candidate_nodes": ["/vision/inference"], "candidate_processes": [], "errors": [], "warnings": [], "execution_host": "Jetson", "gpu_available": True,
            "modules": [{"module_uid": "vision", "display_name": "Vision", "function_type": "generic_inference", "runtime": "TensorRT", "ros_node": "/vision/inference", "model_name": "test.engine", "input_topics": [{"name": "/camera/image", "message_type": "sensor_msgs/msg/Image", "role": "input"}], "output_topics": [{"name": "/vision/output", "message_type": "std_msgs/msg/String", "role": "output"}], "status": "EXTERNAL", "metadata": {"confidence": "CONFIRMED"}}],
        }})
        self.assertEqual(self.page.target_combo.count(), 2)
        self.page.target_combo.setCurrentIndex(1)
        self.assertIn("/camera/image", self.page.pipeline_text.toPlainText())
        self.assertIn("TensorRT", self.page.runtime_table.item(0, 1).text())
        self.page._select_test(2, 1)
        self.assertIn("ai.input", self.page.detail_text.toPlainText())
        self.page.log("INFO", "AI log event")
        self.assertIn("AI log event", self.page.log_text.toPlainText())

    def test_runtime_ready_without_module_shows_idle_no_module_state(self):
        self.page._request_id = "request"
        self.page._on_operation_succeeded("request", {"ai_discovery": {"ok": True, "environment": {
            "runtime_available": True, "runtime_name": "TensorRT", "runtime_version": "10.3.0", "module_count": 0, "module_discovered": False, "module_running": False,
            "modules": [], "configuration_candidates": [{"path": "/opt/ros/share/zed_wrapper/config/object_detection.yaml", "active_module": False}], "model_candidates": ["/opt/ros/share/zed_wrapper/config/object_detection.yaml"], "warnings": [], "errors": [], "execution_host": "Jetson",
        }}})
        self.assertIn("READY", self.page.runtime_chip.text_label.text())
        self.assertIn("NO MODULE", self.page.input_chip.text_label.text())
        self.assertIn("CONFIG CANDIDATES", self.page.model_chip.text_label.text())
        self.assertIn("IDLE / NOT RUNNING", self.page.inference_chip.text_label.text())
        self.assertIn("No active AI module discovered", self.page.pipeline_text.toPlainText())
        self.assertIn("AI discovery completed", self.page.log_text.toPlainText())

    def test_runtime_unavailable_is_rendered_only_after_environment_result(self):
        self.page._request_id = "request"
        self.page._on_operation_succeeded("request", {"ai_discovery": {"ok": True, "environment": {
            "runtime_available": False, "module_count": 0, "modules": [], "warnings": [], "errors": [], "execution_host": "Jetson",
        }}})
        self.assertIn("NOT AVAILABLE", self.page.runtime_chip.text_label.text())

    def test_confirmed_inactive_module_is_selectable_but_never_shown_active(self):
        self.page._request_id = "request"
        self.page._on_operation_succeeded("request", {"ai_discovery": {"ok": True, "environment": {
            "runtime_available": True, "runtime_name": "TensorRT", "runtime_version": "10.3.0", "module_count": 1,
            "module_discovered": True, "module_running": False, "warnings": [], "errors": [], "execution_host": "Jetson",
            "modules": [{"module_uid": "robot:vision", "display_name": "Robot Vision", "runtime": "TensorRT", "status": "CONFIGURED", "model_path": "/models/vision.engine", "input_topics": [{"name": "/camera/image", "message_type": "sensor_msgs/msg/Image", "role": "input"}], "output_topics": [{"name": "/vision/out", "message_type": "std_msgs/msg/String", "role": "output"}], "metadata": {"confidence": "CONFIRMED"}}],
        }}})
        self.page.target_combo.setCurrentIndex(1)
        self.assertIn("CONFIGURED / NOT ACTIVE", self.page.input_chip.text_label.text())
        self.assertIn("Model: CONFIGURED", self.page.model_chip.text_label.text())
        self.assertIn("IDLE / NOT RUNNING", self.page.inference_chip.text_label.text())
        self.assertIn("Configured INPUT", self.page.pipeline_text.toPlainText())
        self.assertIn("NOT RUNNING", self.page.pipeline_text.toPlainText())

    def test_candidates_are_not_selectable_modules(self):
        self.page._request_id = "request"
        self.page._on_operation_succeeded("request", {"ai_discovery": {"ok": True, "environment": {
            "runtime_available": True, "runtime_name": "TensorRT", "runtime_version": "10.3.0", "module_count": 0,
            "module_discovered": False, "module_running": False, "modules": [], "module_candidates": [{"display_name": "YAML only", "confidence": "WEAK_CANDIDATE"}], "warnings": [], "errors": [], "execution_host": "Jetson",
        }}})
        self.assertEqual(self.page.target_combo.count(), 1)
        self.assertIn("candidate(s) require explicit registration", self.page.pipeline_text.toPlainText())
        self.assertIn("candidate(s) require explicit registration", self.page.log_text.toPlainText())


if __name__ == "__main__":
    unittest.main()
