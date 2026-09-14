import os
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from unittest.mock import patch

from devices.livox.testing.guided import GuidedDecision, GuidedSubmission
from devices.livox.testing.guided_executor import GuidedWorkflowExecutor
from devices.livox.testing.ros2_executors import Ros2BagExecutor, Ros2EnvironmentExecutor
from devices.livox.testing.ros2_parsers import parse_ros2_hz, parse_sectioned_output
from devices.livox.testing.ros2_profile import LidarRos2ProfileError, LidarRos2TargetProfile, load_lidar_ros2_profile
from devices.livox.testing.test_result import TestStatus
from desktop_app.services.lidar_ros2_service import LidarRos2Service


class _Provider:
    def __init__(self, submission=None):
        self.submission = submission
        self.cancelled = []

    def request(self, request, callback):
        callback(self.submission)

    def cancel(self, test_id):
        self.cancelled.append(test_id)


class _FakeWorker(QObject):
    connected = Signal()
    connection_failed = Signal(str)
    request_succeeded = Signal(str, object)
    request_failed = Signal(str, str)
    request_cancelled = Signal(str)
    stopped = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.running = False
        self.submissions = []
        self.cancellations = []

    def start(self): self.running = True
    def isRunning(self): return self.running
    def submit(self, *args): self.submissions.append(args)
    def cancel(self, request_id): self.cancellations.append(request_id)
    def shutdown(self): self.running = False; self.stopped.emit()
    def wait(self, _ms): return True


class _FakeRosService(QObject):
    request_succeeded = Signal(str, object)
    request_failed = Signal(str, str)
    request_cancelled = Signal(str)

    def __init__(self):
        super().__init__()
        self.command = None

    def execute(self, _name, command, **_kwargs):
        self.command = command
        return "request-1"

    def cancel(self, _request_id): pass


class LidarRos2IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_profile_parses_and_requires_distinct_playback_domain(self):
        profile = load_lidar_ros2_profile()
        self.assertEqual(profile.host, "192.168.1.21")
        self.assertEqual(profile.lidar_interface, "enp86s0")
        self.assertTrue(profile.playback_isolation_configured)
        data = _profile_mapping(profile)
        data["ros"]["isolated_ros_domain_id"] = data["ros"]["production_ros_domain_id"]
        with self.assertRaisesRegex(LidarRos2ProfileError, "must differ"):
            LidarRos2TargetProfile.from_mapping(data)

    def test_ros_parsers_are_structured(self):
        sections = parse_sectioned_output("noise\n__LIDAR_SECTION__=HZ\naverage rate: 10.0\n\tmin: 0.09s max: 0.11s std dev: 0.002s\n")
        self.assertIn("average rate", sections["HZ"])
        metrics = parse_ros2_hz(sections["HZ"])
        self.assertEqual(metrics["mean_hz"], 10.0)
        self.assertEqual(metrics["max_interval_sec"], 0.11)

    def test_bag_executor_never_builds_play_without_isolation(self):
        profile = replace(load_lidar_ros2_profile(), isolated_ros_domain_id=None)
        outcome = Ros2BagExecutor().preflight(profile)
        self.assertEqual(outcome.status, TestStatus.SKIPPED)
        self.assertFalse(outcome.measurements.get("playback_attempted"))
        command = Ros2BagExecutor().build_command(load_lidar_ros2_profile())
        self.assertIn("ROS_DOMAIN_ID=191", command)
        self.assertIn("ros2 topic echo", command)
        self.assertIn("ros2 bag play", command)

    def test_guided_confirm_reject_skip_and_missing_provider(self):
        definition = SimpleNamespace(id="TC-GUIDED", name="Guided", purpose="P", precondition="Pre", procedure="Do", expected_result="Expected", parameters=("x",), timeout_sec=2)
        for decision, expected in ((GuidedDecision.CONFIRM, TestStatus.PASS), (GuidedDecision.REJECT, TestStatus.FAIL), (GuidedDecision.SKIP, TestStatus.SKIPPED)):
            outcomes = []
            executor = GuidedWorkflowExecutor()
            executor.finished.connect(outcomes.append)
            executor.start(definition, SimpleNamespace(guided_provider=_Provider(GuidedSubmission(decision))))
            self.assertEqual(outcomes[0].status, expected)
        outcomes = []
        executor = GuidedWorkflowExecutor()
        executor.finished.connect(outcomes.append)
        executor.start(definition, SimpleNamespace(guided_provider=None))
        self.assertEqual(outcomes[0].status, TestStatus.SKIPPED)

    def test_managed_service_request_lifecycle_and_bounds(self):
        profile = load_lidar_ros2_profile()
        with patch("desktop_app.services.lidar_ros2_service.LidarRos2Worker", _FakeWorker):
            service = LidarRos2Service(profile)
            first = service.execute("one", "true", timeout=2, max_output=100)
            second = service.execute("two", "true", timeout=3, max_output=200)
            self.assertNotEqual(first, second)
            self.assertEqual(len(service._worker.submissions), 2)
            self.assertEqual(service._worker.config.host, profile.host)
            self.assertIsNone(service._worker.config.password)
            service.cancel(first)
            self.assertEqual(service._worker.cancellations, [first])
            with self.assertRaises(ValueError):
                service.execute("bad", "true", max_output=0)
            service.shutdown()
            self.assertFalse(service.active)

    def test_managed_topic_operations_are_bounded_and_use_profile_domain(self):
        profile = load_lidar_ros2_profile()
        with patch("desktop_app.services.lidar_ros2_service.LidarRos2Worker", _FakeWorker):
            service = LidarRos2Service(profile)
            service.topic_hz("/lidar/pointcloud/pointcloud2")
            hz_request = service._worker.submissions[-1]
            self.assertIn("ROS_DOMAIN_ID=0", hz_request[1])
            self.assertIn("timeout 10 ros2 topic hz", hz_request[1])
            service.topic_echo_once("/lidar/pointcloud/pointcloud2")
            echo_request = service._worker.submissions[-1]
            self.assertIn("timeout 10 ros2 topic echo", echo_request[1])
            self.assertEqual(echo_request[3], 16384)
            service.shutdown()

    def test_environment_executor_parses_mocked_remote_result(self):
        profile = load_lidar_ros2_profile()
        service = _FakeRosService()
        definition = SimpleNamespace(id="TC-ENV-001", timeout_sec=30)
        context = SimpleNamespace(ros2_target_profile=profile, lidar_ros2_service=service)
        outcomes = []
        executor = Ros2EnvironmentExecutor()
        executor.finished.connect(outcomes.append)
        executor.start(definition, context)
        self.assertIn("ros2 node list", service.command)
        output = "\n".join((
            "__LIDAR_SECTION__=ARCH", "x86_64",
            "__LIDAR_SECTION__=KERNEL", "6.8.0",
            "__LIDAR_SECTION__=ADDR", "enp86s0 UP 192.168.1.21/24",
            "__LIDAR_SECTION__=CONTAINER", "true",
            "__LIDAR_SECTION__=NODES", "/livox_lidar_publisher",
            "__LIDAR_SECTION__=TOPICS",
            "/lidar/pointcloud/pointcloud2", "/lidar/pointcloud/custom", "/lidar/imu",
        ))
        service.request_succeeded.emit("request-1", {"exit_status": 0, "stdout": output, "stderr": "", "output_truncated": False})
        self.assertEqual(outcomes[0].status, TestStatus.PASS)


def _profile_mapping(profile):
    return {
        "profile_id": profile.profile_id,
        "enabled": profile.enabled,
        "connection": {"host": profile.host, "username": profile.username, "port": profile.port},
        "ros": {"distro": profile.ros_distro, "setup_commands": list(profile.setup_commands), "production_ros_domain_id": profile.production_ros_domain_id, "isolated_ros_domain_id": profile.isolated_ros_domain_id},
        "container": {"name": profile.container_name, "network_mode": profile.container_network_mode},
        "network": {"lidar_interface": profile.lidar_interface, "host_cidr": profile.host_cidr, "lidar_ip": profile.lidar_ip, "udp_ports": list(profile.udp_ports)},
        "nodes": {"lidar_publisher": profile.lidar_publisher, "slam": profile.slam_node},
        "topics": {key: {"name": value.name, "type": value.message_type} for key, value in profile.topics.items()},
        "frames": {"pointcloud": profile.pointcloud_frame, "imu": profile.imu_frame, "robot_base": profile.robot_base_frame},
        "sampling": dict(profile.sampling),
        "acceptance": dict(profile.acceptance),
    }


if __name__ == "__main__":
    unittest.main()
