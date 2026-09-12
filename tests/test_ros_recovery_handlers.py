import tempfile
import unittest
from pathlib import Path
from threading import Event

from core.testing import TestContext, TestEvaluator, TestRunner, TestStatus
from core.testing.definitions import load_definitions
from core.testing.registry import TestRegistry
from devices.camera.discovery import normalize_zed_device
from devices.camera.ros_automation import RosCameraAdapterRegistry, register_ros_camera_handlers
from devices.camera.ros_automation.models import RosNodeSession
from devices.camera.ros_automation.recovery_handlers import RosNodeExitRecoveryHandler
from devices.camera.ros_registry import RosCameraDriverRegistry


class RecoveryManager:
    """Deterministic owned-session fake covering the remote safety contract."""

    def __init__(self, *, message_ok=True, leave_node=False, terminate_noop=False, invalid_healthy=False, stop_noop=False, graph_delay_polls=0, graph_delay_after_release=False, external_after_release=False):
        self.sessions = {}
        self.counter = 0
        self.message_ok = message_ok
        self.leave_node = leave_node
        self.terminate_noop = terminate_noop
        self.invalid_healthy = invalid_healthy
        self.stop_noop = stop_noop
        self.graph_delay_polls = graph_delay_polls
        self.graph_delay_after_release = graph_delay_after_release
        self.graph_poll_count = {}
        self.external_after_release = external_after_release
        self.stop_calls = []
        self.terminate_calls = []

    def probe_environment(self, packages, expected_distro="humble"):
        return {
            "ros2_available": True, "ros_distro": expected_distro,
            "ros_environment_loaded": True, "environment_ready": True,
            "setup_files": ["/opt/ros/humble/setup.bash"],
            "packages": {name: {"installed": True} for name in packages},
            "errors": [], "warnings": [],
        }

    def start_node(self, device, spec, setup_files):
        self.counter += 1
        invalid = any("camera_model:=phase83d_invalid_zed_model" == item for item in spec.arguments)
        session_id = f"test-{self.counter}"
        healthy = not invalid or self.invalid_healthy
        self.sessions[session_id] = {
            "alive": healthy, "node": healthy, "metadata": True,
            "invalid": invalid, "expected_node": spec.expected_node,
            "released": False,
        }
        session = RosNodeSession(
            session_id, device.device_uid, spec.driver, spec.namespace,
            spec.expected_node, device.serial, True, pid=100 + self.counter,
            process_group=100 + self.counter, setup_files=tuple(setup_files),
        )
        return session

    def status(self, session):
        state = self.sessions[session.session_id]
        return {
            "process_alive": state["alive"], "node_alive": state["node"],
            "serial_verified": state["alive"], "detected_serial": session.selected_serial,
            "startup_time_s": 0.01, "process_exit_code": 1 if state["invalid"] else None,
        }

    def stop_node(self, session):
        self.stop_calls.append(session.session_id)
        state = self.sessions[session.session_id]
        if not self.stop_noop:
            state.update(alive=False, node=bool(self.leave_node))
        return {"stopped": True, "process_exit_code": 0}

    def terminate_owned_node(self, session, timeout_s=8):
        self.terminate_calls.append(session.session_id)
        state = self.sessions[session.session_id]
        if not self.terminate_noop:
            state.update(alive=False, node=False)
        return {"signal": "SIGTERM", "signal_sent": True, "process_exit_code": -15}

    def release_node(self, session, timeout_s=8):
        state = self.sessions[session.session_id]
        state["metadata"] = False
        state["released"] = True
        if self.graph_delay_after_release:
            self.graph_poll_count[session.session_id] = 0
        return {"released": True}

    def audit_owned(self, setup_files, sessions=(), expected_nodes=(), timeout_s=8, expected_topics=()):
        states = {}
        for session in sessions:
            item = self.sessions[session.session_id]
            states[session.session_id] = {
                "metadata_present": item["metadata"],
                "identity_matches": True,
                "process_alive": item["alive"],
                "remaining_pids": [session.pid] if item["alive"] else [],
            }
        present = []
        for session_id, item in self.sessions.items():
            expected_node = item["expected_node"]
            delayed = False
            delay_applies = not self.graph_delay_after_release or item["released"]
            if not item["node"] and self.graph_delay_polls and delay_applies:
                seen = self.graph_poll_count.get(session_id, 0)
                self.graph_poll_count[session_id] = seen + 1
                delayed = seen < self.graph_delay_polls
            external = self.external_after_release and not item["metadata"]
            if item["node"] or delayed or external:
                present.append(expected_node)
        publisher_counts = {
            topic: int(any(
                self.external_after_release and not item["metadata"]
                for item in self.sessions.values()
            )) for topic in expected_topics
        }
        return {
            "session_states": states,
            "active_owned_process_count": sum(item["alive"] for item in self.sessions.values() if item["metadata"]),
            "owned_runtime_directory_count": sum(item["metadata"] for item in self.sessions.values()),
            "node_names": present,
            "expected_nodes_present": [item for item in present if item in expected_nodes],
            "publisher_counts": publisher_counts,
            "graph_probe_ok": True,
            "graph_probe_method": "rclpy_direct_test_double",
        }

    def collect_capabilities(self, session, spec, topics, warmup_s, timeout_s, sample_count, **_):
        item = self.sessions[session.session_id]
        alive = (item["alive"] or (
            self.external_after_release and not item["metadata"]
        )) and self.message_ok
        records = {}
        for index, requirement in enumerate(topics):
            records[f"{requirement.capability}:{index}"] = {
                "topic_name": requirement.topic(spec.namespace), "capability": requirement.capability,
                "exists": alive, "type_matches": alive, "publisher_count": int(alive),
                "message_received": alive, "sample_count": sample_count if alive else 0,
                "valid_sample_count": sample_count if alive else 0,
                "last_timestamp": 2.0 if alive else None, "measured_rate_hz": 30.0 if alive else 0.0,
            }
        return {"topics": records}


class RosRecoveryHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device = RosCameraDriverRegistry().map_device(
            normalize_zed_device({"model": "ZED X Mini", "serial_number": "test-serial"}),
            {"zed_wrapper": True},
        )
        cls.registry = TestRegistry()
        register_ros_camera_handlers(cls.registry)
        definitions = load_definitions("testcases/camera/definitions/phase8_3d_a.json", cls.registry)
        cls.definitions = {item.test_id: item for item in definitions}

    def run_case(self, test_id, **overrides):
        definition = self.definitions[test_id]
        manager = overrides.pop("_manager_instance", None) or RecoveryManager(
            **overrides.pop("manager", {})
        )
        definition = type(definition)(
            definition.test_id, definition.name, definition.group, definition.automation_key,
            definition.priority, definition.timeout_s, {**definition.parameters, **overrides},
            definition.rules, definition.evidence_metadata,
        )
        with tempfile.TemporaryDirectory() as root:
            context = TestContext(
                services={"remote_client": type("Client", (), {"connected": True})(),
                          "selected_devices": (self.device,),
                          "ros_adapter_registry": RosCameraAdapterRegistry(),
                          "ros_process_manager": manager},
                device={"cameras": [self.device.to_dict()]},
                base_configuration={"target_scope": "INDIVIDUAL", "busy_serials": []},
                result_root=str(Path(root) / test_id),
            )
            return TestRunner(self.registry, TestEvaluator()).run_one(definition, context)

    def test_recovery_cases_pass_with_owned_software_faults(self):
        self.assertEqual(self.run_case("ROS-REC-001", cycle_count=2).status, TestStatus.PASS)
        self.assertEqual(self.run_case("ROS-REC-002").status, TestStatus.PASS)
        self.assertEqual(self.run_case("ROS-REC-005").status, TestStatus.PASS)
        self.assertEqual(self.run_case("ROS-REC-006").status, TestStatus.PASS)
        self.assertEqual(self.run_case("ROS-REC-007", cycle_count=2).status, TestStatus.PASS)

    def test_recovery_cancel_is_structured(self):
        definition = self.definitions["ROS-REC-001"]
        manager = RecoveryManager()
        event = Event(); event.set()
        with tempfile.TemporaryDirectory() as root:
            context = TestContext(
                services={"remote_client": type("Client", (), {"connected": True})(),
                          "selected_devices": (self.device,),
                          "ros_adapter_registry": RosCameraAdapterRegistry(),
                          "ros_process_manager": manager},
                device={"cameras": [self.device.to_dict()]},
                base_configuration={"target_scope": "INDIVIDUAL", "busy_serials": []},
                result_root=str(Path(root) / "cancel"), cancel_event=event,
            )
            result = TestRunner(self.registry, TestEvaluator()).run_one(definition, context)
        self.assertEqual(result.status, TestStatus.CANCELLED)
        self.assertEqual(result.error["code"], "TEST_CANCELLED")

    def test_launch_stop_fails_when_one_cycle_has_no_message(self):
        result = self.run_case(
            "ROS-REC-001", cycle_count=1, manager={"message_ok": False}
        )
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertGreaterEqual(result.measurements["failed_cycle_count"], 1)

    def test_node_exit_fails_when_controlled_exit_is_not_observable(self):
        result = self.run_case(
            "ROS-REC-002", process_exit_timeout_s=0.01,
            node_disappearance_timeout_s=0.01,
            message_timeout_s=0.01,
            manager={"terminate_noop": True},
        )
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertFalse(result.sub_results[0]["measurements"]["process_exit_detected"])

    def test_invalid_launch_that_becomes_healthy_is_a_failure(self):
        result = self.run_case(
            "ROS-REC-006", invalid_launch_timeout_s=0.01,
            manager={"invalid_healthy": True},
        )
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertFalse(result.sub_results[0]["measurements"]["invalid_launch_rejected"])

    def test_cleanup_reports_an_orphan_process(self):
        result = self.run_case(
            "ROS-REC-007", cycle_count=1, process_exit_timeout_s=0.01,
            node_disappearance_timeout_s=0.01, cleanup_timeout_s=0.01,
            manager={"stop_noop": True},
        )
        self.assertIn(result.status, (TestStatus.FAIL, TestStatus.ERROR))
        self.assertGreaterEqual(result.measurements["orphan_process_count"], 1)

    def test_dead_owned_session_reconciles_without_waiting_for_graph_loss(self):
        manager = RecoveryManager()
        session = manager.start_node(
            self.device, RosCameraAdapterRegistry().resolve(self.device).build_launch_spec(self.device), ()
        )
        handler = RosNodeExitRecoveryHandler(); handler._sessions = [session]
        released, details = handler._reconcile_dead_session(manager, session, {
            "process_exit_detected": True,
            "audit": {"session_states": {session.session_id: {
                "process_alive": False, "remaining_pids": [],
            }}},
        })
        self.assertTrue(released)
        self.assertTrue(details["process_dead"])
        self.assertFalse(manager.sessions[session.session_id]["metadata"])

    def test_reconciliation_rejects_live_child_and_external_session(self):
        manager = RecoveryManager()
        session = manager.start_node(
            self.device, RosCameraAdapterRegistry().resolve(self.device).build_launch_spec(self.device), ()
        )
        handler = RosNodeExitRecoveryHandler(); handler._sessions = [session]
        released, details = handler._reconcile_dead_session(manager, session, {
            "process_exit_detected": True,
            "audit": {"session_states": {session.session_id: {
                "process_alive": True, "remaining_pids": [999],
            }}},
        })
        self.assertFalse(released)
        self.assertEqual(details["remaining_pids"], [999])
        external = RosNodeSession("external", session.device_uid, session.driver, session.namespace,
                                  session.expected_node, session.selected_serial, False)
        released, details = handler._reconcile_dead_session(manager, external, {})
        self.assertFalse(released)
        self.assertTrue(details["external"])

    def test_node_exit_waits_for_delayed_direct_graph_convergence(self):
        result = self.run_case(
            "ROS-REC-002", manager={"graph_delay_polls": 2},
        )
        self.assertEqual(result.status, TestStatus.PASS)
        measurements = result.sub_results[0]["measurements"]
        self.assertTrue(measurements["node_loss_detected"])
        self.assertEqual(measurements["graph_probe_method"], "rclpy_direct_test_double")

    def test_node_exit_fails_when_direct_graph_still_reports_target_node(self):
        result = self.run_case(
            "ROS-REC-002", node_disappearance_timeout_s=0.01,
            manager={"graph_delay_polls": 999},
        )
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertFalse(result.sub_results[0]["measurements"]["node_loss_detected"])

    def test_topic_interruption_reconciles_dead_owned_session_before_recovery(self):
        result = self.run_case("ROS-REC-005")
        self.assertEqual(result.status, TestStatus.PASS)
        measurements = result.sub_results[0]["measurements"]
        reconciliation = measurements["dead_session_reconciliation"]
        self.assertTrue(reconciliation["process_dead"])
        self.assertTrue(measurements["dead_session_released"])
        self.assertTrue(measurements["old_node_disappearance_detected"])
        self.assertTrue(measurements["old_publisher_absent"])
        self.assertTrue(measurements["stream_recovered"])
        self.assertGreater(measurements["recovery_message_count"], 0)
        self.assertTrue(measurements["final_cleanup_success"])

    def test_topic_interruption_waits_for_delayed_graph_convergence_after_release(self):
        result = self.run_case(
            "ROS-REC-005",
            manager={"graph_delay_polls": 2, "graph_delay_after_release": True},
        )
        self.assertEqual(result.status, TestStatus.PASS)
        measurements = result.sub_results[0]["measurements"]
        self.assertTrue(measurements["dead_session_released"])
        self.assertTrue(measurements["old_node_disappearance_detected"])
        self.assertGreaterEqual(measurements["stale_graph_wait_s"], 0.15)
        self.assertTrue(measurements["stream_recovered"])

    def test_topic_interruption_never_treats_active_external_publisher_as_stale(self):
        manager = RecoveryManager(external_after_release=True)
        result = self.run_case(
            "ROS-REC-005",
            _manager_instance=manager,
        )
        self.assertEqual(result.status, TestStatus.BLOCKED)
        measurements = result.sub_results[0]["measurements"]
        self.assertTrue(measurements["dead_session_released"])
        self.assertFalse(measurements["old_node_disappearance_detected"])
        self.assertFalse(measurements["recovery_started"])
        # A matching live publisher after release is only inspected; no new
        # recovery process is started and no external session is stopped.
        self.assertEqual(manager.counter, 1)
        self.assertEqual(manager.stop_calls, [])

    def test_topic_interruption_reports_bounded_stale_graph_timeout_with_idempotent_cleanup(self):
        result = self.run_case(
            "ROS-REC-005", node_disappearance_timeout_s=0.01,
            manager={"graph_delay_polls": 999, "graph_delay_after_release": True},
        )
        self.assertEqual(result.status, TestStatus.FAIL)
        measurements = result.sub_results[0]["measurements"]
        self.assertTrue(measurements["dead_session_released"])
        self.assertFalse(measurements["old_node_disappearance_detected"])
        self.assertFalse(measurements["recovery_started"])
        self.assertTrue(measurements["final_cleanup_success"])
        self.assertIn("ROS_STALE_GRAPH_TIMEOUT", {
            item["code"] for item in result.sub_results[0]["failure_reasons"]
        })

    def test_zed_invalid_launch_uses_declared_camera_model_and_unique_namespace(self):
        adapter = RosCameraAdapterRegistry().resolve(self.device)
        normal = adapter.build_launch_spec(self.device)
        invalid, summary = adapter.build_invalid_launch_spec(self.device)
        self.assertEqual(summary["strategy"], "zed_wrapper_invalid_camera_model")
        self.assertIn("camera_model:=phase83d_invalid_zed_model", invalid.arguments)
        self.assertNotIn("phase83d_invalid_argument:=true", invalid.arguments)
        self.assertNotEqual(invalid.expected_node, normal.expected_node)


if __name__ == "__main__":
    unittest.main()
