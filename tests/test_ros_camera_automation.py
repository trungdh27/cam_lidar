import tempfile
import unittest
import ast
import contextlib
import io
import json
import os
import sys
from enum import Enum
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

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
from devices.camera.ros_automation.models import RosBagSession, RosNodeSession
from devices.camera.ros_automation.jetson_ros_manager import JETSON_ROS_MANAGER
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
        self.last_record_topics = []

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

    def collect_capabilities(
        self, session, spec, topics, warmup_s, timeout_s, sample_count, **_options
    ):
        if callable(self.collection_override):
            return self.collection_override(session, spec, topics, sample_count)
        records = {}
        for index, requirement in enumerate(topics):
            topic = requirement.topic(spec.namespace)
            record = {
                "topic_name": topic,
                "capability": requirement.capability,
                "availability": requirement.availability,
                "exists": True,
                "type": requirement.message_type,
                "type_matches": True,
                "publisher_count": 1,
                "message_received": True,
                "sample_count": sample_count,
                "valid_sample_count": sample_count,
                "invalid_sample_count": 0,
                "message_timeout_count": 0,
                "timestamp_rollback_count": 0,
                "non_finite_value_count": 0,
                "nan_count": 0,
                "inf_count": 0,
                "first_timestamp": 1.0,
                "last_timestamp": 2.0,
                "measurement_duration_s": 1.0,
                "measured_rate_hz": 30.0,
                "frame_id": "camera_color_optical_frame",
            }
            if requirement.message_type == "sensor_msgs/msg/Image":
                record.update(
                    width=spec.requested_profile.width,
                    height=spec.requested_profile.height,
                    encoding=spec.requested_profile.encodings[0],
                )
            elif requirement.message_type == "sensor_msgs/msg/CameraInfo":
                record.update(
                    width=spec.requested_profile.width,
                    height=spec.requested_profile.height,
                    distortion_model="plumb_bob",
                    K=[700.0, 0.0, 960.0, 0.0, 700.0, 600.0, 0.0, 0.0, 1.0],
                    D=[0.0] * 5,
                    R=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    P=[700.0, 0.0, 960.0, 0.0, 0.0, 700.0, 600.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                    fx=700.0, fy=700.0, cx=960.0, cy=600.0,
                )
            elif requirement.capability == "imu":
                record.update(
                    angular_velocity_min=-0.01,
                    angular_velocity_max=0.01,
                    linear_acceleration_min=-9.82,
                    linear_acceleration_max=0.02,
                    orientation_available=True,
                    measured_rate_hz=200.0,
                )
            records[f"{requirement.capability}:{index}"] = record
        return {"topics": records, "collection_duration_s": 1.0}

    def qos_matrix(
        self, session, spec, topics, graph_timeout_s, timeout_s, sample_count
    ):
        records = {}
        for index, requirement in enumerate(topics):
            records[f"{requirement.capability}:{index}"] = {
                "topic": requirement.topic(spec.namespace),
                "publisher_count": 1,
                "publisher_qos": [{"reliability": "BEST_EFFORT", "durability": "VOLATILE", "history": "KEEP_LAST", "depth": 10}],
                "qos_metadata_discovered": True,
                "compatible_subscriber_qos": {"reliability": "BEST_EFFORT", "durability": "VOLATILE", "history": "KEEP_LAST", "depth": 10},
                "compatible_message_count": sample_count,
                "compatible_timeout": False,
                "compatible_result": "PASS",
                "negative_test_attempted": True,
                "incompatible_subscriber_qos": {"reliability": "RELIABLE", "durability": "VOLATILE", "history": "KEEP_LAST", "depth": 10},
                "incompatible_message_count": 0,
                "negative_expected_result": "INCOMPATIBLE_NO_MESSAGES",
                "negative_actual_result": "INCOMPATIBLE_NO_MESSAGES",
                "warnings": [],
            }
        return {"topics": records}

    def bag_preflight(self, setup_files, timeout_s=10):
        return {"available": True, "supports_remap": True, "default_storage": "sqlite3", "errors": []}

    def start_bag_record(self, setup_files, topics, timeout_s=10):
        self.events.append(("record_start", tuple(topics)))
        self.last_record_topics = list(topics)
        return RosBagSession(
            "record-1", "record", True, pid=200, process_group=200,
            bag_path="/tmp/cam_lidar/ros_camera/bag_record-1/bag",
            setup_files=tuple(setup_files), topics=tuple(topics),
        )

    def bag_status(self, session, timeout_s=8):
        return {"process_alive": True, "process_exit_code": None}

    def stop_bag_process(self, session, timeout_s=10):
        self.events.append((session.kind + "_stop", session.session_id))
        return {"stopped": True, "process_exit_code": 0, "session": session.to_dict()}

    def inspect_bag(self, session, timeout_s=10):
        counts = {topic: 10 for topic in self.last_record_topics}
        types = {
            topic: (
                "sensor_msgs/msg/CameraInfo" if topic.endswith("camera_info")
                else "sensor_msgs/msg/Imu" if "/imu" in topic
                else "sensor_msgs/msg/Image"
            ) for topic in self.last_record_topics
        }
        return {
            "bag_path": session.bag_path,
            "bag_directory_exists": True,
            "metadata_exists": True,
            "metadata_readable": True,
            "storage_identifier": "sqlite3",
            "storage_files": [session.bag_path + "/bag_0.db3"],
            "bag_size_bytes": 1024,
            "record_duration_s": 0.01,
            "topic_count": len(counts),
            "recorded_topics": list(counts),
            "message_count_by_topic": counts,
            "message_type_by_topic": types,
        }

    def start_bag_replay(self, record_session, topics, remappings, timeout_s=10):
        self.events.append(("replay_start", tuple(topics)))
        return RosBagSession(
            "replay-1", "replay", True, pid=201, process_group=201,
            bag_path=record_session.bag_path,
            setup_files=tuple(record_session.setup_files), topics=tuple(topics),
            remappings=dict(remappings),
        )

    def collect_topic_names(self, setup_files, topics, timeout_s, sample_count):
        return {"topics": {
            topic: {
                "sample_count": sample_count,
                "valid_sample_count": sample_count,
                "invalid_sample_count": 0,
            } for topic in topics
        }}


def _embedded_qos_probe_source():
    tree = ast.parse(JETSON_ROS_MANAGER)
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "QOS_PROBE"
            for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError("QOS_PROBE was not found in the remote manager")


def _run_embedded_qos_probe(*, publishers_after=3, publish_messages=True, environment=None):
    class ReliabilityPolicy(Enum):
        SYSTEM_DEFAULT = 0
        BEST_EFFORT = 1
        RELIABLE = 2
        UNKNOWN = 3

    class DurabilityPolicy(Enum):
        SYSTEM_DEFAULT = 0
        TRANSIENT_LOCAL = 1
        VOLATILE = 2
        UNKNOWN = 3

    class HistoryPolicy(Enum):
        SYSTEM_DEFAULT = 0
        KEEP_LAST = 1
        KEEP_ALL = 2
        UNKNOWN = 3

    class LivelinessPolicy(Enum):
        AUTOMATIC = 1

    class QoSProfile:
        def __init__(
            self, *, history=HistoryPolicy.KEEP_LAST, depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        ):
            self.history = history
            self.depth = depth
            self.reliability = reliability
            self.durability = durability
            self.liveliness = LivelinessPolicy.AUTOMATIC
            self.deadline = SimpleNamespace(nanoseconds=0)
            self.lifespan = SimpleNamespace(nanoseconds=0)
            self.liveliness_lease_duration = SimpleNamespace(nanoseconds=0)

    endpoint = SimpleNamespace(qos_profile=QoSProfile(
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    ))

    class Node:
        def __init__(self):
            self.spin_count = 0
            self.subscriptions = []

        def get_topic_names_and_types(self):
            if publishers_after is not None and self.spin_count >= publishers_after:
                return [("/camera/image", ["sensor_msgs/msg/Image"])]
            return []

        def get_publishers_info_by_topic(self, _topic):
            if publishers_after is not None and self.spin_count >= publishers_after:
                return [endpoint]
            return []

        def create_subscription(self, _message_class, _topic, callback, qos):
            self.subscriptions.append((callback, qos))
            return callback

        def spin_once(self):
            self.spin_count += 1
            if not publish_messages:
                return
            for callback, qos in self.subscriptions:
                if qos.reliability != ReliabilityPolicy.RELIABLE:
                    callback(object())

        def destroy_node(self):
            pass

    node = Node()
    rclpy = SimpleNamespace(
        init=lambda args=None: None,
        shutdown=lambda: None,
        create_node=lambda _name: node,
        spin_once=lambda active_node, timeout_sec=0: active_node.spin_once(),
    )
    qos_module = SimpleNamespace(
        DurabilityPolicy=DurabilityPolicy,
        HistoryPolicy=HistoryPolicy,
        QoSProfile=QoSProfile,
        ReliabilityPolicy=ReliabilityPolicy,
    )
    utilities = SimpleNamespace(get_message=lambda _name: object)
    request = {
        "topics": [{
            "result_key": "color:0",
            "topic": "/camera/image",
            "candidate_topics": ["/camera/image"],
            "message_type": "sensor_msgs/msg/Image",
        }],
        "graph_timeout_s": 0.1,
        "timeout_s": 0.1,
        "sample_count": 3,
    }
    modules = {
        "rclpy": rclpy,
        "rclpy.qos": qos_module,
        "rosidl_runtime_py": SimpleNamespace(utilities=utilities),
        "rosidl_runtime_py.utilities": utilities,
    }
    output = io.StringIO()
    with patch.dict(sys.modules, modules), patch.object(
        sys, "argv", ["qos_probe", json.dumps(request)]
    ), patch.dict(os.environ, environment or {}, clear=False), contextlib.redirect_stdout(output):
        exec(compile(_embedded_qos_probe_source(), "<qos_probe>", "exec"), {})
    marker = "CAMERA_ROS_QOS_JSON="
    line = next(item for item in reversed(output.getvalue().splitlines()) if item.startswith(marker))
    return json.loads(line[len(marker):]), node


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
        definitions = []
        for path in (
            "testcases/camera/definitions/phase8_3a.json",
            "testcases/camera/definitions/phase8_3b.json",
            "testcases/camera/definitions/phase8_3d_a.json",
        ):
            definitions.extend(load_definitions(path, self.registry))
        self.definitions = {item.test_id: item for item in definitions}
        bag_definition = self.definitions["ROS-008"]
        self.definitions["ROS-008"] = replace(
            bag_definition,
            parameters={**bag_definition.parameters, "record_duration_s": 0.01},
        )
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, test_id, devices, manager, busy_serials=(), cancel_event=None):
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
            cancel_event=cancel_event or Event(),
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

    def _capability_result(self, test_id, capability, **changes):
        def collection(session, spec, topics, sample_count):
            result = _Manager().collect_capabilities(
                session, spec, topics, 0, 0, sample_count
            )
            record = next(
                item for key, item in result["topics"].items()
                if key.split(":", 1)[0] == capability
            )
            record.update(changes)
            return result
        return self._run(test_id, [_zed()], _Manager(collection=collection))

    def test_ros005_valid_image_camera_info_passes(self):
        result = self._run("ROS-005", [_zed()], _Manager())
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertTrue(result.sub_results[0]["rule_results"])

    def test_ros005_width_mismatch_fails(self):
        self.assertEqual(
            self._capability_result("ROS-005", "camera_info", width=1280).status,
            TestStatus.FAIL,
        )

    def test_ros005_height_mismatch_fails(self):
        self.assertEqual(
            self._capability_result("ROS-005", "camera_info", height=720).status,
            TestStatus.FAIL,
        )

    def test_ros005_nonpositive_fx_fails(self):
        self.assertEqual(
            self._capability_result("ROS-005", "camera_info", fx=0.0).status,
            TestStatus.FAIL,
        )

    def test_ros005_nonpositive_fy_fails(self):
        self.assertEqual(
            self._capability_result("ROS-005", "camera_info", fy=-1.0).status,
            TestStatus.FAIL,
        )

    def test_ros005_nan_calibration_fails(self):
        result = self._capability_result(
            "ROS-005", "camera_info", nan_count=1, non_finite_value_count=1
        )
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros005_inf_calibration_fails(self):
        result = self._capability_result(
            "ROS-005", "camera_info", inf_count=1, non_finite_value_count=1
        )
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros005_missing_camera_info_fails(self):
        result = self._capability_result(
            "ROS-005", "camera_info", exists=False, publisher_count=0,
            message_received=False, sample_count=0,
        )
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros005_timestamp_rollback_fails(self):
        self.assertEqual(
            self._capability_result(
                "ROS-005", "camera_info", timestamp_rollback_count=1
            ).status,
            TestStatus.FAIL,
        )

    def test_ros005_interleaved_topic_timestamps_are_independently_monotonic(self):
        interleaved = [
            ("image", 10.50), ("camera_info", 10.47),
            ("image", 10.53), ("camera_info", 10.50),
            ("image", 10.56), ("camera_info", 10.53),
        ]

        def collection(session, spec, topics, sample_count):
            result = _Manager().collect_capabilities(
                session, spec, topics, 0, 0, sample_count
            )
            for capability in ("image", "camera_info"):
                target = "color" if capability == "image" else capability
                record = next(
                    item for key, item in result["topics"].items()
                    if key.split(":", 1)[0] == target
                )
                record["message_timestamps"] = [
                    stamp for source, stamp in interleaved if source == capability
                ]
            return result

        result = self._run("ROS-005", [_zed()], _Manager(collection=collection))
        measurements = result.sub_results[0]["measurements"]
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(measurements["image"]["timestamp_rollback_count"], 0)
        self.assertEqual(measurements["camera_info"]["timestamp_rollback_count"], 0)
        self.assertEqual(measurements["timestamp_rollback_count"], 0)

    def test_ros005_true_per_topic_timestamp_rollback_fails(self):
        def collection(session, spec, topics, sample_count):
            result = _Manager().collect_capabilities(
                session, spec, topics, 0, 0, sample_count
            )
            image = next(
                item for key, item in result["topics"].items()
                if key.startswith("color:")
            )
            image["message_timestamps"] = [10.50, 10.53, 10.40]
            camera_info = next(
                item for key, item in result["topics"].items()
                if key.startswith("camera_info:")
            )
            camera_info["message_timestamps"] = [10.47, 10.50, 10.53]
            return result

        result = self._run("ROS-005", [_zed()], _Manager(collection=collection))
        measurements = result.sub_results[0]["measurements"]
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertEqual(measurements["image"]["timestamp_rollback_count"], 1)
        self.assertEqual(measurements["camera_info"]["timestamp_rollback_count"], 0)
        self.assertEqual(measurements["timestamp_rollback_count"], 1)

    def test_ros005_equal_timestamps_within_topic_are_valid(self):
        def collection(session, spec, topics, sample_count):
            result = _Manager().collect_capabilities(
                session, spec, topics, 0, 0, sample_count
            )
            for key, record in result["topics"].items():
                record["message_timestamps"] = (
                    [10.50, 10.50, 10.53]
                    if key.startswith("color:")
                    else [10.47, 10.47, 10.50]
                )
            return result

        result = self._run("ROS-005", [_zed()], _Manager(collection=collection))
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(
            result.sub_results[0]["measurements"]["timestamp_rollback_count"],
            0,
        )

    def test_ros005_related_frame_ids_pass(self):
        result = self._capability_result(
            "ROS-005", "camera_info", frame_id="camera_color_frame"
        )
        self.assertEqual(result.status, TestStatus.PASS)

    def test_ros006_mandatory_imu_present_and_rate_stored(self):
        result = self._run("ROS-006", [_zed()], _Manager())
        self.assertEqual(result.status, TestStatus.PASS)
        imu = result.sub_results[0]["measurements"]["sensor_topics"][0]
        self.assertEqual(imu["measured_rate_hz"], 200.0)

    def test_ros006_mandatory_topic_missing_fails(self):
        result = self._capability_result(
            "ROS-006", "imu", exists=False, publisher_count=0,
            message_received=False, message_timeout_count=1,
        )
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros006_topic_without_messages_fails(self):
        result = self._capability_result(
            "ROS-006", "imu", message_received=False, sample_count=0,
            message_timeout_count=1,
        )
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_ros006_timestamp_rollback_fails(self):
        self.assertEqual(
            self._capability_result(
                "ROS-006", "imu", timestamp_rollback_count=1
            ).status,
            TestStatus.FAIL,
        )

    def test_ros006_nan_sensor_value_fails(self):
        self.assertEqual(
            self._capability_result(
                "ROS-006", "imu", nan_count=1, non_finite_value_count=1
            ).status,
            TestStatus.FAIL,
        )

    def test_ros006_inf_sensor_value_fails(self):
        self.assertEqual(
            self._capability_result(
                "ROS-006", "imu", inf_count=1, non_finite_value_count=1
            ).status,
            TestStatus.FAIL,
        )

    def test_ros006_optional_temperature_missing_does_not_fail(self):
        result = self._capability_result(
            "ROS-006", "temperature", exists=False, publisher_count=0,
            message_received=False, sample_count=0, message_timeout_count=1,
        )
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(
            result.sub_results[0]["measurements"]["temperature_availability"],
            "OPTIONAL_NOT_AVAILABLE",
        )

    def test_ros007_qos_metadata_and_compatible_messages_pass(self):
        result = self._run("ROS-007", [_zed()], _Manager())
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertTrue(result.sub_results[0]["measurements"]["publisher_qos_discovered"])

    def test_ros007_probe_waits_until_publisher_appears_after_spin_cycles(self):
        payload, node = _run_embedded_qos_probe(
            publishers_after=4, publish_messages=True
        )
        topic = payload["topics"]["color:0"]
        self.assertGreaterEqual(node.spin_count, 4)
        self.assertEqual(topic["publisher_count"], 1)
        self.assertTrue(topic["qos_metadata_discovered"])
        self.assertGreaterEqual(topic["compatible_message_count"], 3)
        self.assertTrue(topic["compatible_sample_target_met"])

    def test_ros007_probe_publisher_never_appears_has_diagnostics(self):
        payload, _node = _run_embedded_qos_probe(
            publishers_after=None, publish_messages=False
        )
        topic = payload["topics"]["color:0"]
        self.assertEqual(topic["publisher_count"], 0)
        self.assertFalse(topic["qos_metadata_discovered"])
        self.assertIsNone(topic["compatible_subscriber_qos"])
        self.assertIn("topic_list", topic["diagnostic_summary"])

    def test_ros007_publisher_never_appears_is_structured_discovery_failure(self):
        manager = _Manager()
        original = manager.qos_matrix

        def qos(*args):
            result = original(*args)
            for item in result["topics"].values():
                item.update(
                    publisher_count=0,
                    publisher_qos=[],
                    qos_metadata_discovered=False,
                    compatible_subscriber_qos=None,
                    compatible_message_count=0,
                    compatible_timeout=True,
                    diagnostic_summary={"reason": "publisher not discovered"},
                )
            return result

        manager.qos_matrix = qos
        result = self._run("ROS-007", [_zed()], manager)
        codes = {
            item["code"] for item in result.sub_results[0]["failure_reasons"]
        }
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertIn("ROS_QOS_DISCOVERY_FAILED", codes)
        self.assertNotIn("ROS_QOS_MESSAGE_TIMEOUT", codes)

    def test_ros007_probe_endpoint_without_messages_times_out(self):
        payload, _node = _run_embedded_qos_probe(
            publishers_after=1, publish_messages=False
        )
        topic = payload["topics"]["color:0"]
        self.assertTrue(topic["qos_metadata_discovered"])
        self.assertEqual(topic["compatible_message_count"], 0)
        self.assertTrue(topic["compatible_timeout"])

    def test_ros007_environment_domain_mismatch_is_structured_not_pass(self):
        payload, _node = _run_embedded_qos_probe(
            publishers_after=None,
            publish_messages=False,
            environment={
                "ROS_DOMAIN_ID": "99",
                "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
                "ROS_LOCALHOST_ONLY": "1",
            },
        )
        topic = payload["topics"]["color:0"]
        self.assertFalse(topic["qos_metadata_discovered"])
        self.assertEqual(topic["probe_environment"]["ROS_DOMAIN_ID"], "99")
        self.assertEqual(topic["probe_environment"]["ROS_LOCALHOST_ONLY"], "1")
        manager = _Manager()
        original = manager.qos_matrix

        def qos(*args):
            result = original(*args)
            result["probe_environment"] = payload["probe_environment"]
            for item in result["topics"].values():
                item.update(topic)
            return result

        manager.qos_matrix = qos
        result = self._run("ROS-007", [_zed()], manager)
        codes = {
            item["code"] for item in result.sub_results[0]["failure_reasons"]
        }
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertIn("ROS_QOS_DISCOVERY_FAILED", codes)

    def test_ros007_compatible_timeout_fails(self):
        manager = _Manager()
        original = manager.qos_matrix
        def qos(*args):
            result = original(*args)
            first = next(iter(result["topics"].values()))
            first.update(compatible_message_count=0, compatible_timeout=True, compatible_result="TIMEOUT")
            return result
        manager.qos_matrix = qos
        self.assertEqual(self._run("ROS-007", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros007_expected_incompatible_no_data_is_pass(self):
        result = self._run("ROS-007", [_zed()], _Manager())
        item = result.sub_results[0]["measurements"]["qos_matrix"][0]
        self.assertEqual(item["incompatible_message_count"], 0)
        self.assertEqual(result.status, TestStatus.PASS)

    def test_ros007_negative_not_constructible_is_not_applicable(self):
        manager = _Manager()
        original = manager.qos_matrix
        def qos(*args):
            result = original(*args)
            for item in result["topics"].values():
                item.update(
                    negative_test_attempted=False,
                    incompatible_subscriber_qos=None,
                    negative_expected_result="NOT_APPLICABLE",
                    negative_actual_result="NOT_APPLICABLE",
                )
            return result
        manager.qos_matrix = qos
        result = self._run("ROS-007", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.PASS)

    def test_ros008_record_metadata_replay_success(self):
        result = self._run("ROS-008", [_zed()], _Manager())
        self.assertEqual(result.status, TestStatus.PASS)
        measurements = result.sub_results[0]["measurements"]
        self.assertTrue(measurements["metadata_valid"])
        self.assertTrue(measurements["mandatory_replay_messages_received"])

    def test_ros008_record_process_failure_fails(self):
        manager = _Manager()
        manager.bag_status = lambda _session, _timeout=8: {"process_alive": False, "process_exit_code": 2, "process_error": "record failed"}
        self.assertEqual(self._run("ROS-008", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros008_metadata_missing_fails(self):
        manager = _Manager()
        original = manager.inspect_bag
        def inspect(session, timeout_s=10):
            result = original(session, timeout_s)
            result.update(metadata_exists=False, metadata_readable=False)
            return result
        manager.inspect_bag = inspect
        self.assertEqual(self._run("ROS-008", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros008_mandatory_topic_missing_or_empty_fails(self):
        manager = _Manager()
        original = manager.inspect_bag
        def inspect(session, timeout_s=10):
            result = original(session, timeout_s)
            first = next(iter(result["message_count_by_topic"]))
            result["message_count_by_topic"][first] = 0
            return result
        manager.inspect_bag = inspect
        self.assertEqual(self._run("ROS-008", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros008_replay_start_failure_fails(self):
        manager = _Manager()
        def fail(*_args):
            from devices.camera.ros_automation.remote import RosRemoteError
            raise RosRemoteError("ROSBAG_REPLAY_FAILED", "play failed")
        manager.start_bag_replay = fail
        self.assertEqual(self._run("ROS-008", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros008_replay_timeout_fails(self):
        manager = _Manager()
        manager.collect_topic_names = lambda _setup, topics, _timeout, _count: {
            "topics": {topic: {"sample_count": 0, "invalid_sample_count": 0} for topic in topics}
        }
        self.assertEqual(self._run("ROS-008", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros008_deserialization_error_fails(self):
        manager = _Manager()
        manager.collect_topic_names = lambda _setup, topics, _timeout, count: {
            "topics": {topic: {"sample_count": count, "invalid_sample_count": 1} for topic in topics}
        }
        self.assertEqual(self._run("ROS-008", [_zed()], manager).status, TestStatus.FAIL)

    def test_ros008_cancel_during_record_cleans_owned_processes(self):
        event = Event()
        manager = _Manager()
        def status(_session, _timeout=8):
            event.set()
            return {"process_alive": True}
        manager.bag_status = status
        result = self._run("ROS-008", [_zed()], manager, cancel_event=event)
        self.assertEqual(result.status, TestStatus.CANCELLED)
        self.assertIn(("record_stop", "record-1"), manager.events)
        self.assertIn(("stop", "58651554"), manager.events)

    def test_ros008_cancel_during_replay_cleans_owned_processes(self):
        event = Event()
        manager = _Manager()
        original = manager.collect_topic_names
        def collect(*args):
            event.set()
            return original(*args)
        manager.collect_topic_names = collect
        result = self._run("ROS-008", [_zed()], manager, cancel_event=event)
        self.assertEqual(result.status, TestStatus.CANCELLED)
        self.assertIn(("replay_stop", "replay-1"), manager.events)

    def test_ros008_external_camera_node_is_never_stopped(self):
        manager = _Manager(owned=False)
        result = self._run("ROS-008", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertNotIn(("stop", "58651554"), manager.events)

    def test_ros008_external_node_without_replay_remap_is_blocked(self):
        manager = _Manager(owned=False)
        manager.bag_preflight = lambda _setup, _timeout=10: {
            "available": True,
            "supports_remap": False,
            "default_storage": "sqlite3",
            "errors": [],
        }
        result = self._run("ROS-008", [_zed()], manager)
        self.assertEqual(result.status, TestStatus.BLOCKED)
        self.assertEqual(
            result.error["code"], "ROSBAG_REPLAY_ISOLATION_UNAVAILABLE"
        )
        self.assertNotIn(("stop", "58651554"), manager.events)

    def test_phase83b_adapters_cover_all_camera_families_without_handler_branches(self):
        registry = RosCameraAdapterRegistry()
        devices = [
            _zed("111"),
            replace(_zed("222"), model="ZED X One 4K", normalized_model="ZED X One 4K", ros_camera_model="zedxone4k"),
            _realsense("333"),
        ]
        for device in devices:
            adapter = registry.resolve(device)
            self.assertTrue(adapter.primary_image_requirement(device))
            self.assertTrue(adapter.camera_info_requirement(device))
            self.assertIsInstance(adapter.sensor_topics(device), tuple)

    def test_ros005_all_mocked_families_execute_sequentially_through_adapters(self):
        devices = [
            _zed("111"),
            replace(
                _zed("222"), model="ZED X One 4K",
                normalized_model="ZED X One 4K", ros_camera_model="zedxone4k",
            ),
            _realsense("333"),
        ]
        manager = _Manager()
        result = self._run("ROS-005", devices, manager)
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(len(result.measurements["device_results"]), 3)
        self.assertEqual(
            manager.events,
            [
                ("start", "111"), ("stop", "111"),
                ("start", "222"), ("stop", "222"),
                ("start", "333"), ("stop", "333"),
            ],
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
