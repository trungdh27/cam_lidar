import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from core.testing import TestContext, TestEvaluator, TestRunner, TestStatus
from core.testing.definitions import load_definitions
from core.testing.registry import TestRegistry
from desktop_app.workers.ros_camera_test_runner_worker import (
    RosCameraTestRunnerWorker,
)
from devices.camera.discovery import normalize_realsense_device, normalize_zed_device
from devices.camera.ros_automation import (
    RealSenseRosAdapter,
    RosCameraAdapterRegistry,
    ZedRosAdapter,
    register_ros_camera_handlers,
)
from devices.camera.ros_automation.models import RosNodeSession
from devices.camera.ros_automation.registry import (
    UnsupportedRosCameraAdapterError,
)
from devices.camera.ros_registry import RosCameraDriverRegistry


class _Signals(QObject):
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, str)

    def __init__(self, connected=True):
        super().__init__()
        self.is_connected = connected


class _Client:
    connected = True


class _Manager:
    def __init__(self, *, owned=True, collection=None, installed=None):
        self.owned = owned
        self.collection_override = collection
        self.installed = installed or {
            "zed_wrapper": True,
            "realsense2_camera": True,
        }
        self.required_calls = []
        self.events = []

    def probe_environment(self, packages, expected_distro="humble"):
        self.required_calls.append(tuple(packages))
        return {
            "ros2_available": True,
            "ros_distro": expected_distro,
            "ros_environment_loaded": True,
            "environment_ready": True,
            "workspace_setup": "/work/install/setup.bash",
            "setup_files": [
                f"/opt/ros/{expected_distro}/setup.bash",
                "/work/install/setup.bash",
            ],
            "packages": {
                package: {
                    "installed": self.installed.get(package, False),
                    "prefix": f"/work/install/{package}"
                    if self.installed.get(package, False) else None,
                    "launch_files": [],
                }
                for package in packages
            },
            "warnings": [],
            "errors": [],
        }

    def start_node(self, device, spec, setup_files):
        self.events.append(("start", device.serial))
        return RosNodeSession(
            session_id="owned-" + device.serial if self.owned else "external",
            device_uid=device.device_uid,
            driver=spec.driver,
            namespace=spec.namespace,
            expected_node=spec.expected_node,
            selected_serial=device.serial,
            owned_by_test=self.owned,
            pid=123 if self.owned else None,
            process_group=123 if self.owned else None,
            log_path="/tmp/launch.log" if self.owned else None,
            setup_files=tuple(setup_files),
        )

    def status(self, session):
        return {
            "process_alive": True,
            "node_alive": True,
            "node_names": [session.expected_node],
            "detected_serial": session.selected_serial,
            "serial_verified": True,
            "stderr_summary": (
                "Serial Number -> " + session.selected_serial
            ),
        }

    def stop_node(self, session):
        self.events.append(("stop", session.selected_serial))
        return {"stopped": True}

    def collect(self, session, spec, topics, warmup_s, timeout_s, sample_count):
        if callable(self.collection_override):
            return self.collection_override(session, spec, topics, sample_count)
        if self.collection_override is not None:
            return self.collection_override
        records = {}
        for requirement in topics:
            topic = requirement.topic(spec.namespace)
            record = {
                "exists": True,
                "type": requirement.message_type,
                "type_matches": True,
                "publisher_count": 1,
                "message_received": True,
                "sample_count": sample_count,
                "valid_sample_count": sample_count,
                "invalid_sample_count": 0,
                "timestamp_rollback_count": 0,
                "first_timestamp": 1.0,
                "last_timestamp": 4.4,
                "duration_s": 3.4,
                "calculated_fps": 29.1,
                "host_receive_fps": 29.0,
            }
            if requirement.message_type == "sensor_msgs/msg/Image":
                profile = spec.requested_profile
                record.update(
                    width=profile.width,
                    height=profile.height,
                    encoding=profile.encodings[0],
                    step=profile.width * 4,
                    frame_id="camera_optical_frame",
                )
            records[topic] = record
        return {"topics": records, "collection_duration_s": 3.5}


def _zed(serial="58651554"):
    raw = normalize_zed_device({
        "model": "ZED X Mini",
        "serial_number": serial,
        "device_path": "/dev/i2c-9",
        "interface": "GMSL",
        "api": "Camera",
    })
    return RosCameraDriverRegistry().map_device(raw, {"zed_wrapper": True})


def _realsense(serial="222", usb="2.1"):
    raw = normalize_realsense_device({
        "model": "Intel RealSense D435i",
        "serial": serial,
        "physical_port": "2-1",
        "usb_type_descriptor": usb,
        "backend": "pyrealsense2",
        "profile_status": "AVAILABLE",
        "stream_profiles": [
            {
                "stream": "color",
                "format": "rgb8",
                "width": 640,
                "height": 480,
                "fps": 30,
            }
        ],
    })
    return RosCameraDriverRegistry().map_device(
        raw, {"realsense2_camera": True}
    )


class RosCameraAdapterTests(unittest.TestCase):
    def test_registry_resolves_zed_wrapper(self):
        self.assertIsInstance(
            RosCameraAdapterRegistry().resolve(_zed()), ZedRosAdapter
        )

    def test_registry_resolves_realsense(self):
        self.assertIsInstance(
            RosCameraAdapterRegistry().resolve(_realsense()),
            RealSenseRosAdapter,
        )

    def test_registry_rejects_unknown_driver(self):
        device = SimpleNamespace(ros_driver="future_camera_driver")
        with self.assertRaises(UnsupportedRosCameraAdapterError):
            RosCameraAdapterRegistry().resolve(device)

    def test_zed_launch_uses_serial_model_and_namespace(self):
        device = _zed()
        spec = ZedRosAdapter().build_launch_spec(device)
        self.assertIn("serial_number:=58651554", spec.arguments)
        self.assertIn("camera_model:=zedxm", spec.arguments)
        self.assertEqual(spec.namespace, device.ros_namespace_hint)
        self.assertEqual(spec.expected_node, "/cameras/zedxm_58651554")
        self.assertIn("namespace:=cameras", spec.arguments)

    def test_realsense_usb2_uses_inventory_supported_profile(self):
        spec = RealSenseRosAdapter().build_launch_spec(_realsense())
        self.assertIn("serial_no:=_222", spec.arguments)
        self.assertEqual(spec.requested_profile.width, 640)
        self.assertEqual(spec.requested_profile.height, 480)
        self.assertEqual(spec.requested_profile.fps, 30)
        self.assertEqual(
            spec.requested_profile.source, "inventory_reported_usb2_profile"
        )


class RosCameraHandlerTests(unittest.TestCase):
    def setUp(self):
        self.registry = TestRegistry()
        register_ros_camera_handlers(self.registry)
        definitions = load_definitions(
            "testcases/camera/definitions/phase8_3a.json", self.registry
        )
        self.definitions = {item.test_id: item for item in definitions}
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, test_id, devices, manager, busy_serials=()):
        context = TestContext(
            services={
                "remote_client": _Client(),
                "selected_devices": tuple(devices),
                "ros_adapter_registry": RosCameraAdapterRegistry(),
                "ros_process_manager": manager,
            },
            device={
                "target_scope": "ALL_CAMERAS" if len(devices) > 1 else "INDIVIDUAL",
                "cameras": [item.to_dict() for item in devices],
            },
            base_configuration={
                "target_scope": "ALL_CAMERAS" if len(devices) > 1 else "INDIVIDUAL",
                "busy_serials": list(busy_serials),
            },
            result_root=str(Path(self.temporary.name) / test_id.lower()),
        )
        return TestRunner(self.registry, TestEvaluator()).run_one(
            self.definitions[test_id], context
        )

    def test_ros001_package_requirement_from_selected_inventory(self):
        manager = _Manager()
        result = self._run("ROS-001", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(manager.required_calls, [("zed_wrapper",)])
        self.assertEqual(result.measurements["required_driver_count"], 1)

    def test_ros001_all_cameras_uses_driver_union(self):
        manager = _Manager()
        result = self._run("ROS-001", [_zed(), _realsense()], manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(
            manager.required_calls,
            [("realsense2_camera", "zed_wrapper")],
        )

    def test_unsupported_adapter_is_blocked(self):
        device = replace(_zed("1"), ros_driver="unknown")
        result = self._run("ROS-001", [device], _Manager())
        self.assertEqual(result.status, TestStatus.BLOCKED)
        self.assertEqual(result.error["code"], "ROS_ADAPTER_UNSUPPORTED")

    def test_camera_busy_is_blocked(self):
        result = self._run(
            "ROS-002", [_zed()], _Manager(), busy_serials=("58651554",)
        )
        self.assertEqual(result.status, TestStatus.BLOCKED)
        self.assertEqual(result.error["code"], "CAMERA_BUSY")

    def test_external_node_is_not_stopped(self):
        manager = _Manager(owned=False)
        result = self._run("ROS-002", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(manager.events, [("start", "58651554")])
        self.assertFalse(result.sub_results[0]["measurements"]["owned_by_test"])

    def test_test_owned_node_cleanup_occurs(self):
        manager = _Manager(owned=True)
        result = self._run("ROS-002", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(
            manager.events,
            [("start", "58651554"), ("stop", "58651554")],
        )

    def test_failed_per_device_cleanup_is_retried_by_runner_cleanup(self):
        class RetryCleanupManager(_Manager):
            def __init__(self):
                super().__init__(owned=True)
                self.stop_attempts = 0

            def stop_node(self, session):
                self.stop_attempts += 1
                self.events.append(("stop", session.selected_serial))
                return {"stopped": self.stop_attempts > 1, "remaining_pids": [456]}

        manager = RetryCleanupManager()
        result = self._run("ROS-002", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertEqual(manager.stop_attempts, 2)

    def test_ros003_missing_mandatory_topic_fails(self):
        def collection(_session, spec, topics, _sample_count):
            result = _Manager().collect(_session, spec, topics, 0, 0, 1)
            missing = topics[0].topic(spec.namespace)
            result["topics"][missing].update(
                exists=False,
                type=None,
                type_matches=False,
                publisher_count=0,
                message_received=False,
            )
            return result

        result = self._run("ROS-003", [_zed()], _Manager(collection=collection))
        self.assertEqual(result.status, TestStatus.FAIL)
        codes = {item["code"] for item in result.sub_results[0]["failure_reasons"]}
        self.assertIn("ROS_TOPIC_MISSING", codes)

    def test_ros003_topic_without_message_fails(self):
        def collection(_session, spec, topics, _sample_count):
            result = _Manager().collect(_session, spec, topics, 0, 0, 1)
            topic = topics[0].topic(spec.namespace)
            result["topics"][topic]["message_received"] = False
            return result

        result = self._run("ROS-003", [_zed()], _Manager(collection=collection))
        self.assertEqual(result.status, TestStatus.FAIL)
        codes = {item["code"] for item in result.sub_results[0]["failure_reasons"]}
        self.assertIn("ROS_TOPIC_TIMEOUT", codes)

    def _profile_result(self, **changes):
        def collection(session, spec, topics, sample_count):
            result = _Manager().collect(session, spec, topics, 0, 0, sample_count)
            result["topics"][spec.primary_image_topic].update(changes)
            return result

        return self._run("ROS-004", [_zed()], _Manager(collection=collection))

    def test_ros004_width_mismatch_fails(self):
        result = self._profile_result(width=1280)
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros004_height_mismatch_fails(self):
        result = self._profile_result(height=720)
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros004_low_fps_fails(self):
        result = self._profile_result(calculated_fps=20.0)
        self.assertEqual(result.status, TestStatus.FAIL)
        codes = {item["code"] for item in result.sub_results[0]["failure_reasons"]}
        self.assertIn("ROS_FPS_BELOW_THRESHOLD", codes)

    def test_ros004_valid_profile_passes(self):
        result = self._profile_result()
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertTrue(result.sub_results[0]["rule_results"])

    def test_all_cameras_returns_sequential_per_device_results(self):
        manager = _Manager()
        result = self._run("ROS-002", [_zed("111"), _zed("222")], manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(len(result.sub_results), 2)
        self.assertEqual(
            manager.events,
            [("start", "111"), ("stop", "111"), ("start", "222"), ("stop", "222")],
        )


class RosTargetSnapshotTests(unittest.TestCase):
    def test_worker_captures_stable_target_tuple(self):
        selected = [_zed("111")]
        worker = RosCameraTestRunnerWorker(
            [], TestRegistry(), _Signals(), selected, "INDIVIDUAL"
        )
        selected.append(_zed("222"))
        self.assertEqual([item.serial for item in worker.devices], ["111"])


if __name__ == "__main__":
    unittest.main()
