import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from desktop_app.ui.lidar_ros2_page import LidarRos2Page
from devices.livox.testing.ros2_profile import load_lidar_ros2_profile


class _FakeRos2Service(QObject):
    request_succeeded = Signal(str, object)
    request_failed = Signal(str, str)
    request_cancelled = Signal(str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self._next = 0

    def _request(self, name):
        self._next += 1
        request_id = f"{name}-{self._next}"
        self.calls.append((name, request_id))
        return request_id

    def check_environment(self):
        return self._request("environment")

    def refresh_graph(self):
        return self._request("graph")

    def topic_type(self, topic):
        return self._request(f"type:{topic}")

    def topic_info(self, topic):
        return self._request(f"info:{topic}")

    def topic_hz(self, topic):
        return self._request(f"hz:{topic}")

    def topic_echo_once(self, topic):
        return self._request(f"echo:{topic}")


class LidarRos2PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = _FakeRos2Service()
        self.page = LidarRos2Page(load_lidar_ros2_profile(), self.service)

    def test_target_summary_is_profile_driven(self):
        labels = [label.text() for label in self.page.findChildren(type(self.page.environment_status))]
        self.assertIn("192.168.1.21", " ".join(labels))
        self.assertEqual(self.page.environment_status.text(), "NOT CHECKED")

    def test_environment_result_renders_status_nodes_and_topics(self):
        self.page.check_environment()
        request_id = self.page._active_request_id
        self.service.request_succeeded.emit(request_id, {
            "exit_status": 0,
            "stdout": "\n".join((
                "__LIDAR_SECTION__=HOSTNAME", "motion-pc",
                "__LIDAR_SECTION__=ARCH", "x86_64",
                "__LIDAR_SECTION__=KERNEL", "6.8.0",
                "__LIDAR_SECTION__=ADDR", "enp86s0 UP 192.168.1.21/24",
                "__LIDAR_SECTION__=CONTAINER", "true",
                "__LIDAR_SECTION__=NODES", "/livox_lidar_publisher",
                "__LIDAR_SECTION__=TOPICS", "/lidar/points\n/lidar/imu",
            )),
            "stderr": "",
        })
        self.assertEqual(self.page.environment_status.text(), "READY")
        self.assertEqual(self.page.node_list.item(0).text(), "/livox_lidar_publisher")
        self.assertEqual(self.page.topic_list.count(), 2)

    def test_topic_selection_and_type_info_hz_echo_operations(self):
        self.page.topic_list.addItems(["/lidar/points", "/lidar/imu"])
        self.page.topic_list.setCurrentRow(0)
        self.assertEqual(self.page.selected_topic_label.text(), "/lidar/points")

        self.page.topic_type()
        request_id = self.page._active_request_id
        self.service.request_succeeded.emit(request_id, {"exit_status": 0, "stdout": "sensor_msgs/msg/PointCloud2\n", "stderr": ""})
        self.assertIn("PointCloud2", self.page.summary_table.item(1, 1).text())

        self.page.topic_info()
        request_id = self.page._active_request_id
        self.service.request_succeeded.emit(request_id, {"exit_status": 0, "stdout": "Type: sensor_msgs/msg/PointCloud2\nPublisher count: 1\nSubscription count: 2\nReliability: RELIABLE\n", "stderr": ""})
        self.assertEqual(self.page.summary_table.rowCount(), 5)

        self.page.topic_hz()
        request_id = self.page._active_request_id
        self.service.request_succeeded.emit(request_id, {"exit_status": 0, "stdout": "average rate: 10.0\nmin: 0.09s max: 0.11s std dev: 0.002s\n", "stderr": ""})
        self.assertIn("10.0", self.page.summary_table.item(1, 1).text())

        self.page.topic_echo_once()
        request_id = self.page._active_request_id
        self.service.request_succeeded.emit(request_id, {"exit_status": 0, "stdout": "header:\n  frame_id: lidar_link\n", "stderr": ""})
        self.assertIn("lidar_link", self.page.output_text.toPlainText())

    def test_echo_output_is_bounded_and_failure_is_safe(self):
        self.page.topic_list.addItem("/lidar/points")
        self.page.topic_list.setCurrentRow(0)
        self.page.topic_echo_once()
        request_id = self.page._active_request_id
        self.service.request_succeeded.emit(request_id, {"exit_status": 0, "stdout": "x" * 20000, "stderr": "", "output_truncated": False})
        self.assertLessEqual(len(self.page.output_text.toPlainText()), 16020)
        self.page.topic_type()
        request_id = self.page._active_request_id
        self.service.request_failed.emit(request_id, "host unreachable")
        self.assertEqual(self.page.environment_status.text(), "ERROR")
        self.assertIn("host unreachable", self.page.output_text.toPlainText())

    def test_operations_are_serialized_and_use_managed_service(self):
        self.page.topic_list.addItem("/lidar/points")
        self.page.topic_list.setCurrentRow(0)
        self.page.topic_hz()
        self.assertFalse(self.page.hz_button.isEnabled())
        self.assertEqual(self.service.calls[-1][0], "hz:/lidar/points")
        self.service.request_cancelled.emit(self.page._active_request_id)
        self.assertTrue(self.page.hz_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
