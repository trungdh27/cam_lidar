import json
import tempfile
import time
import unittest
from pathlib import Path

from core.testing.definitions import load_definitions
from core.testing.errors import DefinitionValidationError, TestCancelledError
from core.testing.evaluator import TestEvaluator
from core.testing.models import TestCaseDefinition, TestContext, TestStatus
from core.testing.registry import TestRegistry
from core.testing.runner import TestRunner
from devices.camera.testing.handlers import normalize_error


class Handler:
    def __init__(self, measurements): self.measurements = measurements; self.cleaned = False
    def validate(self, context, definition): context.checkpoint(time.monotonic())
    def setup(self, context, definition): pass
    def execute(self, context, definition): return self.measurements, {}, []
    def cleanup(self, context, definition): self.cleaned = True


class CoreTestingTests(unittest.TestCase):
    def test_status_values(self):
        self.assertEqual(
            {x.value for x in TestStatus},
            {"NOT_RUN", "RUNNING", "PASS", "FAIL", "ERROR", "BLOCKED", "SKIPPED", "CANCELLED"},
        )

    def test_evaluator_operators_and_boolean(self):
        evaluator = TestEvaluator()
        rules = [
            {"metric": "a", "operator": "==", "expected": True},
            {"metric": "b", "operator": ">=", "expected": 3},
            {"metric": "c", "operator": "<=", "expected": 5},
        ]
        self.assertTrue(all(x["passed"] for x in evaluator.evaluate({"a": True, "b": 3, "c": 4}, rules)))

    def test_registry_known_and_unknown(self):
        registry = TestRegistry(); handler = Handler({})
        registry.register("known", handler); self.assertIs(registry.resolve("known"), handler)
        with self.assertRaises(Exception): registry.resolve("unknown")

    def test_pass_and_fail_results_and_cleanup(self):
        for actual, expected_status in ((2, TestStatus.PASS), (0, TestStatus.FAIL)):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as root:
                registry = TestRegistry(); handler = Handler({"count": actual}); registry.register("test", handler)
                definition = TestCaseDefinition("T", "Test", "Core", "test", "P0", 2, {},
                                                ({"metric": "count", "operator": ">=", "expected": 1},))
                context = TestContext({}, {}, {}, root)
                result = TestRunner(registry, TestEvaluator()).run_one(definition, context)
                self.assertEqual(result.status, expected_status); self.assertTrue(handler.cleaned)
                self.assertTrue((Path(root) / "T" / "result.json").exists())

    def test_cancelled_context(self):
        context = TestContext({}, {}, {}, "."); context.cancel_event.set()
        with self.assertRaises(TestCancelledError): context.checkpoint(time.monotonic())

    def test_cooperative_timeout(self):
        class TimeoutHandler(Handler):
            def execute(self, context, definition):
                time.sleep(0.02)
                context.checkpoint(time.monotonic())
        with tempfile.TemporaryDirectory() as root:
            registry = TestRegistry(); handler = TimeoutHandler({}); registry.register("timeout", handler)
            definition = TestCaseDefinition("TO", "Timeout", "Core", "timeout", "P0", 0.001)
            result = TestRunner(registry, TestEvaluator()).run_one(
                definition, TestContext({}, {}, {}, root)
            )
            self.assertEqual(result.status, TestStatus.ERROR)
            self.assertEqual(result.error["code"], "TEST_TIMEOUT")
            self.assertTrue(handler.cleaned)

    def test_definition_validation_and_rf_data(self):
        registry = TestRegistry()
        for key in ("camera.basic_grab", "camera.smoke_4k", "camera.resolution_fps", "camera.invalid_config"):
            registry.register(key, object())
        definitions = load_definitions("testcases/camera/definitions/phase8_1a.json", registry)
        self.assertEqual(len(definitions), 7)
        rf = [x for x in definitions if x.test_id.startswith("RF-")]
        self.assertEqual({x.automation_key for x in rf}, {"camera.resolution_fps"})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.json"; path.write_text(json.dumps([{"id": "bad"}]))
            with self.assertRaises(DefinitionValidationError): load_definitions(path, registry)

    def test_camera_error_normalization(self):
        self.assertEqual(normalize_error("Unsupported FPS: 60"), "INVALID_FPS")
        self.assertEqual(normalize_error("resolution not available"), "INVALID_RESOLUTION")
        self.assertEqual(normalize_error("camera already in use"), "CAMERA_BUSY")


if __name__ == "__main__": unittest.main()
