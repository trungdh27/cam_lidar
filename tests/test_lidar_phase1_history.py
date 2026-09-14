import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop_app.testing.evidence_store import EvidenceStore
from desktop_app.testing.test_case import TestCaseDefinition as LegacyDefinition
from desktop_app.testing.test_execution_service import (
    TestExecutionService as LegacyExecutionService,
)
from desktop_app.testing.test_result import TestResult as LegacyResult
from desktop_app.ui.lidar_history_dialog import LidarHistoryDialog
from devices.livox.testing import LidarEvidenceStore, LidarHistoryStore
from devices.livox.testing.test_case import (
    AutomationLevel,
    TestCaseDefinition,
)
from devices.livox.testing.test_execution_service import TestExecutionService
from devices.livox.testing.test_executor import BaseTestExecutor
from devices.livox.testing.test_registry import TestRegistry
from devices.livox.testing.test_result import (
    TestOutcome,
    TestResult,
    TestStatus,
)


class _PassExecutor(BaseTestExecutor):
    def start(self, definition, context):
        self.finish(TestOutcome(TestStatus.PASS, "Measured result passed."))


class LidarPhase1HistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.evidence_root = Path(self.temp_dir.name) / "evidence"
        self.store = LidarEvidenceStore(self.evidence_root)
        self.history = LidarHistoryStore(self.evidence_root / "lidar")

    def test_public_imports_and_compatibility_identity(self):
        self.assertIs(EvidenceStore, LidarEvidenceStore)
        self.assertIs(LegacyDefinition, TestCaseDefinition)
        self.assertIs(LegacyResult, TestResult)
        self.assertIs(LegacyExecutionService, TestExecutionService)

    def test_evidence_rejects_other_domains_and_uses_lidar_path(self):
        with self.assertRaises(ValueError):
            self.store.begin_session("camera")
        session_id = self.store.begin_session("LiDAR")
        result = TestResult("LID-UNIT-001")
        result.start()
        result.complete(
            TestOutcome(TestStatus.PASS, "Passed", {"point_rate": 1}),
            0.1,
        )
        definition = SimpleNamespace(id="LID-UNIT-001", name="Unit")
        context = SimpleNamespace(
            discovery_result={"model": "MID360S", "serial": "SERIAL"},
            selected_model="MID360S",
        )
        self.store.write_result(
            session_id,
            "LiDAR",
            definition,
            context,
            result,
            ["PASS  measured"],
        )
        test_dir = (
            self.evidence_root / "lidar" / session_id / "LID-UNIT-001"
        )
        self.assertTrue((test_dir / "result.json").is_file())
        self.assertTrue((test_dir / "metrics.json").is_file())
        self.assertTrue((test_dir / "log.txt").is_file())

    def test_session_manifest_creation_finalization_and_secret_filtering(self):
        session_id = self.store.begin_session("lidar")
        self.store.initialize_session(
            session_id,
            {
                "selected_model": "MID360S",
                "test_ids": ["LID-UNIT-001"],
                "password": "do-not-write-password",
                "access_token": "do-not-write-token",
                "private_key": "do-not-write-key",
            },
        )
        manifest = self.store.finalize_session(
            session_id,
            counts={"PASS": 1},
            stream_was_running_before_tests=True,
            runner_started_stream=False,
            cancelled=False,
            duration_sec=0.25,
            detected_model="MID360S",
            serial="SERIAL",
        )
        raw = (
            self.evidence_root / "lidar" / session_id / "session.json"
        ).read_text(encoding="utf-8")
        self.assertEqual(manifest["domain"], "lidar")
        self.assertEqual(manifest["status"], "PASS")
        self.assertIsNotNone(manifest["finished_at"])
        self.assertEqual(manifest["counts"], {"PASS": 1})
        self.assertNotIn("do-not-write", raw)
        self.assertNotIn("password", raw)
        self.assertNotIn("access_token", raw)
        self.assertNotIn("private_key", raw)

    def test_evidence_merges_safe_guided_references_without_copying_payload(self):
        session_id = self.store.begin_session("lidar")
        definition = TestCaseDefinition(
            id="TC-GUIDED-001", device="lidar", name="Guided", group="Guided",
            description="guided", automation_level=AutomationLevel.GUIDED,
            priority="P4", timeout_sec=10, executor=_PassExecutor,
        )
        result = TestResult(definition.id)
        result.start()
        result.complete(TestOutcome(TestStatus.PASS, "Confirmed", {
            "evidence_references": ["/data/large.bag", "token=secret"]
        }), 0.1)
        context = SimpleNamespace(discovery_result={}, selected_model="MID360S")
        self.store.write_result(session_id, "lidar", definition, context, result, [])
        self.assertIn("/data/large.bag", result.evidence)
        self.assertNotIn("token=secret", result.evidence)
        raw = (self.evidence_root / "lidar" / session_id / definition.id / "metrics.json").read_text(encoding="utf-8")
        self.assertNotIn("token=secret", raw)
        self.assertFalse((self.evidence_root / "lidar" / session_id / definition.id / "large.bag").exists())

    def test_execution_service_creates_and_finalizes_manifest(self):
        definition = TestCaseDefinition(
            id="LID-UNIT-001",
            device="lidar",
            name="Unit pass",
            group="Unit",
            description="Runner manifest integration",
            automation_level=AutomationLevel.AUTO,
            priority="P0",
            timeout_sec=1.0,
            executor=_PassExecutor,
        )
        registry = TestRegistry()
        registry.register(definition)
        service = TestExecutionService(registry, evidence_store=self.store)
        context = SimpleNamespace(
            device="LiDAR",
            stream_process_active=False,
            discovery_result={"model": "MID360S", "serial": "SERIAL"},
            selected_model="MID360S",
            lidar_ip="192.168.1.162",
            jetson_state=SimpleNamespace(host="192.168.9.169"),
            network_profile=SimpleNamespace(
                jetson=SimpleNamespace(interface="lidar0")
            ),
        )
        self.assertTrue(service.start([definition.id], context))
        self._wait(lambda: not service.running)
        manifest = self.history.load_session(service.session_id)
        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["test_ids"], [definition.id])
        self.assertEqual(manifest["counts"], {"PASS": 1})
        self.assertEqual(manifest["runner_started_stream"], False)

    def test_session_status_precedence_preserves_non_pass_outcomes(self):
        cases = (
            ({"ERROR": 1, "FAIL": 1}, False, "ERROR"),
            ({"FAIL": 1, "CANCELLED": 1}, True, "FAIL"),
            ({"CANCELLED": 1}, True, "CANCELLED"),
            ({"PASS": 1, "SKIPPED": 1}, False, "COMPLETED"),
        )
        for counts, cancelled, expected in cases:
            with self.subTest(expected=expected):
                session_id = self.store.begin_session("lidar")
                manifest = self.store.finalize_session(
                    session_id,
                    counts=counts,
                    stream_was_running_before_tests=False,
                    runner_started_stream=False,
                    cancelled=cancelled,
                    duration_sec=0.1,
                )
                self.assertEqual(manifest["status"], expected)

    def test_history_lists_newest_first_and_loads_results(self):
        older = self._write_session("20250101T000000Z-old", "2025-01-01T00:00:00+00:00")
        newer = self._write_session("20260101T000000Z-new", "2026-01-01T00:00:00+00:00")
        sessions = self.history.list_sessions()
        self.assertEqual(
            [session["session_id"] for session in sessions],
            [newer, older],
        )
        loaded = self.history.load_session(newer)
        result = self.history.load_result(newer, "LID-UNIT-001")
        self.assertEqual(loaded["model"], "MID360S")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.history.list_results(newer)[0]["test_id"], "LID-UNIT-001")

    def test_corrupt_and_legacy_sessions_are_tolerated(self):
        corrupt_dir = self.evidence_root / "lidar" / "corrupt-session"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "session.json").write_text("{bad json", encoding="utf-8")

        legacy = "20240101T000000Z-legacy"
        self._write_result(legacy, include_manifest=False)
        sessions = {
            item["session_id"]: item for item in self.history.list_sessions()
        }
        self.assertEqual(sessions["corrupt-session"]["manifest_state"], "CORRUPT")
        self.assertEqual(sessions[legacy]["manifest_state"], "LEGACY")
        self.assertEqual(sessions[legacy]["counts"], {"PASS": 1})
        self.assertEqual(sessions[legacy]["detected_model"], "MID360S")

    def test_history_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            self.history.load_session("../camera")
        session_id = self._write_session(
            "safe-session",
            "2026-01-01T00:00:00+00:00",
        )
        with self.assertRaises(ValueError):
            self.history.load_result(session_id, "../../secret")

    def test_lidar_history_dialog_opens_offscreen(self):
        self._write_session(
            "dialog-session",
            "2026-01-01T00:00:00+00:00",
        )
        dialog = LidarHistoryDialog(self.history)
        try:
            dialog.show()
            self.app.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.windowTitle(), "LiDAR Test History")
            self.assertEqual(dialog.session_table.rowCount(), 1)
            self.assertEqual(dialog.result_table.rowCount(), 1)
            self.assertIn("LID-UNIT-001", dialog.detail_view.toPlainText())
        finally:
            dialog.close()

    def _write_session(self, session_id: str, started_at: str) -> str:
        self._write_result(session_id, include_manifest=False)
        session_dir = self.evidence_root / "lidar" / session_id
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "domain": "lidar",
                    "session_id": session_id,
                    "status": "PASS",
                    "model": "MID360S",
                    "serial": "SERIAL",
                    "started_at": started_at,
                    "duration_sec": 0.1,
                    "counts": {"PASS": 1},
                }
            ),
            encoding="utf-8",
        )
        return session_id

    def _write_result(self, session_id: str, *, include_manifest: bool) -> None:
        test_dir = self.evidence_root / "lidar" / session_id / "LID-UNIT-001"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "result.json").write_text(
            json.dumps(
                {
                    "test_id": "LID-UNIT-001",
                    "status": "PASS",
                    "actual_result": "Passed",
                    "duration_sec": 0.1,
                    "started_at": "2024-01-01T00:00:00+00:00",
                    "model": "MID360S",
                    "serial": "SERIAL",
                    "measurements": {"point_rate": 1},
                    "evidence": ["evidence/lidar/result.json"],
                }
            ),
            encoding="utf-8",
        )
        if include_manifest:
            (self.evidence_root / "lidar" / session_id / "session.json").write_text(
                "{}",
                encoding="utf-8",
            )

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for LiDAR execution service")


if __name__ == "__main__":
    unittest.main()
