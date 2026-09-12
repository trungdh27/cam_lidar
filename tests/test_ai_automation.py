import tempfile
import unittest
from pathlib import Path
from threading import Event

from core.testing import TestContext, TestEvaluator, TestRunner, TestStatus
from core.testing.definitions import load_definitions
from core.testing.registry import TestRegistry
from devices.ai import AiEndpoint, AiLaunchSpec, AiModule, AiModuleRegistry, AiRuntimeAdapter, AiSession, register_ai_handlers


class FakeAiManager:
    def __init__(self, *, modules=(), runtime=True, runtime_name="TensorRT", process_alive=True, node_alive=True, input_probe=None, output_probe=None, configuration_candidates=()):
        self.modules = tuple(modules); self.runtime = runtime; self.runtime_name = runtime_name; self.process_alive = process_alive; self.node_alive = node_alive
        self.input_probe, self.output_probe = input_probe, output_probe
        self.started, self.stopped = [], []
        self.configuration_candidates = list(configuration_candidates)

    def discover_environment(self):
        return {
            "runtime_available": self.runtime, "runtime_name": self.runtime_name if self.runtime else "UNKNOWN", "runtime_version": "8.6" if self.runtime else "",
            "runtimes": [{"name": self.runtime_name, "version": "8.6", "available": True}] if self.runtime else [],
            "ros_available": True, "ai_packages": ["vision_inference"], "model_candidates": ["/models/test.engine"],
            "candidate_nodes": [item.ros_node for item in self.modules if item.ros_node], "candidate_processes": [],
            "modules": [item.to_dict() for item in self.modules], "module_count": len(self.modules), "module_discovered": bool(self.modules), "module_running": any(item.status in {"EXTERNAL", "RUNNING"} for item in self.modules), "runtime_probe_results": [{"name": self.runtime_name, "available": self.runtime, "version": "8.6" if self.runtime else "", "python_executable": "/usr/bin/python3", "probe_return_code": 0 if self.runtime else 1, "error": ""}], "configuration_candidates": self.configuration_candidates, "errors": [], "warnings": [], "setup_files": ["/opt/ros/humble/setup.bash"], "execution_host": "Jetson", "execution_user": "robot", "python_executable": "/usr/bin/python3", "python_version": "3.10", "gpu_available": self.runtime,
        }

    def start(self, module, setup_files):
        self.started.append(module.module_uid)
        return AiSession("owned-" + module.module_uid, module.module_uid, pid=101, process_group=101, owned_by_test=True, expected_node=module.launch_spec.expected_node, setup_files=tuple(setup_files))

    def status(self, session):
        return {"process_alive": self.process_alive, "expected_node_alive": self.node_alive, "stderr_summary": ""}

    def stop(self, session):
        self.stopped.append(session.session_id); return {"stopped": True}

    @staticmethod
    def _default_probe(endpoint):
        return {"endpoint_exists": True, "message_type": endpoint.message_type, "type_matches": True, "publisher_count": 1, "sample_count": 2, "valid_sample_count": 2, "observed_rate_hz": 20.0, "first_timestamp": 1.0, "last_timestamp": 1.1, "timestamp_rollback_count": 0, "deserialize_error_count": 0, "structural_error_count": 0, "structurally_valid": True, "image_metadata": {"width": 640, "height": 480, "encoding": "rgb8"}}

    def probe_endpoint(self, session, endpoint, timeout_s, sample_count):
        selected = self.input_probe if endpoint.role == "input" else self.output_probe
        return dict(selected if selected is not None else self._default_probe(endpoint))


class AiAutomationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = TestRegistry(); register_ai_handlers(cls.registry)
        cls.definitions = {item.test_id: item for item in load_definitions("testcases/ai/definitions/phase8_4a.json", cls.registry)}

    @staticmethod
    def module(*, external=False, input=True, output=True, expected_node=""):
        return AiModule(
            "vision-module", "Vision Inference", runtime="TensorRT", package="vision_inference",
            ros_node="/vision/inference" if external else "",
            input_topics=(AiEndpoint("/camera/image", "sensor_msgs/msg/Image", "input"),) if input else (),
            output_topics=(AiEndpoint("/vision/output", "std_msgs/msg/String", "output"),) if output else (),
            model_name="test.engine", model_path="/models/test.engine", status="EXTERNAL" if external else "CONFIGURED",
            launch_spec=None if external else AiLaunchSpec("approved_command", ("vision_infer",), expected_node, True), metadata={"confidence": "CONFIRMED"},
        )

    def run_case(self, test_id, manager, modules=None, **params):
        base = self.definitions[test_id]
        definition = type(base)(base.test_id, base.name, base.group, base.automation_key, base.priority, base.timeout_s, {**base.parameters, **params}, base.rules, base.evidence_metadata)
        with tempfile.TemporaryDirectory() as root:
            context = TestContext(
                services={"remote_client": type("Client", (), {"connected": True})(), "selected_ai_modules": tuple(modules if modules is not None else manager.modules), "ai_runtime_adapter": AiRuntimeAdapter(), "ai_process_manager": manager},
                device={"ai_modules": [item.to_dict() for item in (modules if modules is not None else manager.modules)]}, base_configuration={"target_scope": "INDIVIDUAL"}, result_root=str(Path(root) / test_id),
            )
            return TestRunner(self.registry, TestEvaluator()).run_one(definition, context)

    def test_discovery_no_module_is_structured_blocked(self):
        result = self.run_case("AI-001", FakeAiManager())
        self.assertEqual(result.status, TestStatus.BLOCKED)
        self.assertEqual(result.error["code"], "AI_MODULE_NOT_DISCOVERED")
        self.assertTrue(result.measurements["runtime_available"])
        self.assertEqual(result.measurements["module_count"], 0)
        self.assertFalse(result.measurements["module_running"])

    def test_pytorch_only_runtime_is_available_without_a_module(self):
        manager = FakeAiManager(runtime_name="PyTorch")
        environment = manager.discover_environment()
        self.assertTrue(environment["runtime_available"])
        self.assertEqual(environment["runtime_name"], "PyTorch")
        self.assertEqual(environment["module_count"], 0)

    def test_configuration_candidate_does_not_fabricate_a_running_module(self):
        environment = FakeAiManager(configuration_candidates=[{"path": "/opt/ros/share/zed_wrapper/config/object_detection.yaml", "kind": "ai_configuration_candidate", "active_module": False}]).discover_environment()
        self.assertEqual(environment["module_count"], 0)
        self.assertFalse(environment["module_running"])
        self.assertTrue(environment["configuration_candidates"])

    def test_registry_only_exposes_confirmed_static_modules(self):
        confirmed = self.module()
        weak = AiModule("yaml", "YAML only", model_path="/opt/object_detection.yaml", metadata={"confidence": "WEAK_CANDIDATE"})
        environment = {"modules": [confirmed.to_dict(), weak.to_dict()]}
        modules = AiModuleRegistry().registered_modules(environment)
        self.assertEqual([item.module_uid for item in modules], ["vision-module"])

    def test_configured_endpoints_are_retained_without_runtime_activity(self):
        module = self.module()
        self.assertEqual(module.status, "CONFIGURED")
        self.assertEqual(module.input_topics[0].name, "/camera/image")
        self.assertEqual(module.output_topics[0].name, "/vision/output")

    def test_environment_passes_with_required_runtime_package_and_model(self):
        result = self.run_case("AI-001", FakeAiManager(modules=(self.module(),)))
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertTrue(result.sub_results[0]["measurements"]["model_configuration_found"])

    def test_environment_fails_when_required_runtime_missing(self):
        result = self.run_case("AI-001", FakeAiManager(modules=(self.module(),), runtime=False))
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertFalse(result.sub_results[0]["measurements"]["runtime_available"])

    def test_launch_owned_session_starts_and_cleans_up(self):
        manager = FakeAiManager(modules=(self.module(),)); result = self.run_case("AI-002", manager)
        self.assertEqual(result.status, TestStatus.PASS); self.assertEqual(manager.started, ["vision-module"]); self.assertEqual(manager.stopped, ["owned-vision-module"])

    def test_launch_failure_when_owned_process_exits(self):
        result = self.run_case("AI-002", FakeAiManager(modules=(self.module(),), process_alive=False))
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertFalse(result.sub_results[0]["measurements"]["process_alive"])

    def test_launch_times_out_when_expected_node_never_arrives(self):
        result = self.run_case("AI-002", FakeAiManager(modules=(self.module(expected_node="/vision/inference"),), node_alive=False), startup_timeout_s=0.01)
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_external_module_is_reused_and_never_stopped(self):
        manager = FakeAiManager(modules=(self.module(external=True),)); result = self.run_case("AI-002", manager)
        self.assertEqual(result.status, TestStatus.PASS); self.assertEqual(manager.started, []); self.assertEqual(manager.stopped, [])

    def test_input_validates_messages_and_image_metadata(self):
        result = self.run_case("AI-003", FakeAiManager(modules=(self.module(),)))
        self.assertEqual(result.status, TestStatus.PASS); self.assertEqual(result.sub_results[0]["measurements"]["image_metadata"]["width"], 640)

    def test_input_missing_wrong_type_timeout_and_timestamp_rollback_fail(self):
        self.assertEqual(self.run_case("AI-003", FakeAiManager(modules=(self.module(input=False),))).status, TestStatus.FAIL)
        bad = FakeAiManager._default_probe(AiEndpoint("/camera/image", "sensor_msgs/msg/Image", "input")); bad.update(type_matches=False, sample_count=0, timestamp_rollback_count=1)
        result = self.run_case("AI-003", FakeAiManager(modules=(self.module(),), input_probe=bad))
        self.assertEqual(result.status, TestStatus.FAIL)

    def test_output_valid_empty_payload_passes_but_deserialization_failure_fails(self):
        result = self.run_case("AI-004", FakeAiManager(modules=(self.module(),)))
        self.assertEqual(result.status, TestStatus.PASS)
        self.assertEqual(self.run_case("AI-004", FakeAiManager(modules=(self.module(output=False),))).status, TestStatus.FAIL)
        bad = FakeAiManager._default_probe(AiEndpoint("/vision/output", "std_msgs/msg/String", "output")); bad["deserialize_error_count"] = 1; bad["structurally_valid"] = False
        self.assertEqual(self.run_case("AI-004", FakeAiManager(modules=(self.module(),), output_probe=bad)).status, TestStatus.FAIL)

    def test_multiple_modules_are_processed_sequentially(self):
        second = AiModule("second", "Second", runtime="TensorRT", package="vision_inference", input_topics=(AiEndpoint("/second/input", "std_msgs/msg/String", "input"),), output_topics=(AiEndpoint("/second/output", "std_msgs/msg/String", "output"),), model_name="second.engine", launch_spec=AiLaunchSpec("approved_command", ("second",), "", True), metadata={"confidence": "CONFIRMED"})
        manager = FakeAiManager(modules=(self.module(), second))
        result = self.run_case("AI-003", manager)
        self.assertEqual(result.status, TestStatus.PASS); self.assertEqual(manager.started, ["vision-module", "second"])

    def test_smoke_pipeline_passes_without_unsupported_correlation_and_fails_without_output(self):
        self.assertEqual(self.run_case("AI-005", FakeAiManager(modules=(self.module(),))).status, TestStatus.PASS)
        unsupported = FakeAiManager._default_probe(AiEndpoint("/vision/output", "std_msgs/msg/String", "output")); unsupported.update(first_timestamp=None, last_timestamp=None)
        result = self.run_case("AI-005", FakeAiManager(modules=(self.module(),), output_probe=unsupported))
        self.assertEqual(result.status, TestStatus.PASS); self.assertEqual(result.sub_results[0]["measurements"]["correlation_result"], "NOT_APPLICABLE")
        bad = FakeAiManager._default_probe(AiEndpoint("/vision/output", "std_msgs/msg/String", "output")); bad.update(sample_count=0, valid_sample_count=0, structurally_valid=False)
        self.assertEqual(self.run_case("AI-005", FakeAiManager(modules=(self.module(),), output_probe=bad)).status, TestStatus.FAIL)


if __name__ == "__main__":
    unittest.main()
