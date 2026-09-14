import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop_app.stress.baseline import (
    BaselineCapture,
    SystemBaselineCollector,
    build_baseline,
    write_baseline_capture,
)
from desktop_app.stress.collectors import EvidenceManager, ObservationEvidenceCollector
from desktop_app.stress.load_strategy import (
    LoadStrategy,
    LoadStrategyDecision,
    TargetSemantics,
    decide_cpu_strategy,
    target_semantics,
)
from desktop_app.stress.models import (
    EnvironmentStatus,
    EvidenceDefinition,
    ExecutionType,
    RuntimeStatus,
    StressTestDefinition,
)
from desktop_app.stress.runner import StressTestRunner
from desktop_app.stress.session import StressSessionManager
from desktop_app.ui.system_stress_page import SystemStressPage


def process_until(predicate, timeout=5.0):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def raw_snapshot(timestamp, aggregate, cpu0):
    return (
        f"{timestamp}\n"
        f"cpu {aggregate}\n"
        f"cpu0 {cpu0}\n"
        "10:32:54 up 1:19, 1 user, load average: 1.00, 2.00, 3.00\n"
        "Mem: 8589934592 2147483648 0 0 0 6442450944\n"
        "Swap: 1073741824 268435456 805306368\n"
    )


def capture(cpu_percent, *, stress_ng=True):
    data = {
        "start_time": "2026-09-10T10:00:00+00:00",
        "end_time": "2026-09-10T10:00:02+00:00",
        "measured_at": "2026-09-10T10:00:02+00:00",
        "sample_duration_sec": 2,
        "cpu": {
            "avg_percent": cpu_percent,
            "min_percent": cpu_percent,
            "max_percent": cpu_percent,
            "sample_count": 2,
            "per_core_avg": {"cpu0": cpu_percent},
        },
    }
    return BaselineCapture(data, "synthetic baseline\n", stress_ng_available=stress_ng)


def cpu_definition(*, semantics="minimum", duration=1, target=30):
    criterion = "CPU average >= 30%" if semantics == "minimum" else "CPU remains approximately near 30%"
    evidence = (
        EvidenceDefinition("workload", "Workload", "log", "workload", "adaptive workload"),
        EvidenceDefinition("system_metrics", "System Metrics", "log", "system_metrics", "metrics", 0.05),
    )
    return StressTestDefinition(
        test_id="ST-CPU-001",
        group="CPU/GPU/AI Stress",
        test_name="CPU target",
        source_fields={"Acceptance Criteria": criterion},
        duration_text=f"{duration} sec",
        duration_seconds=duration,
        execution_type=ExecutionType.AUTO,
        evidence_plan=evidence,
        workload_program="stress-ng",
        workload_arguments=("--cpu", "0", "--timeout", f"{duration}s", "--metrics-brief"),
        target_cpu_percent=target,
    )


class FixedBaselineCollector:
    def __init__(self, result):
        self.result = result

    def collect_local(self, _should_stop=None):
        return self.result


class BaselineTests(unittest.TestCase):
    def test_multiple_samples_cpu_statistics_per_core_and_memory(self):
        samples = iter(
            [
                raw_snapshot("2026-09-10T10:00:00+00:00", "100 0 50 800 50 0 0 0", "50 0 25 400 25 0 0 0"),
                raw_snapshot("2026-09-10T10:00:01+00:00", "140 0 60 840 60 0 0 0", "70 0 30 420 30 0 0 0"),
                raw_snapshot("2026-09-10T10:00:02+00:00", "150 0 75 900 75 0 0 0", "75 0 37 450 38 0 0 0"),
            ]
        )
        collector = SystemBaselineCollector(
            duration_sec=2,
            sample_interval_sec=1,
            sample_reader=lambda: next(samples),
            sleeper=lambda _seconds: None,
        )
        with patch("desktop_app.stress.baseline.shutil.which", return_value=None):
            result = collector.collect_local()
        self.assertEqual(result.data["cpu"]["sample_count"], 2)
        self.assertAlmostEqual(result.data["cpu"]["avg_percent"], 37.5)
        self.assertAlmostEqual(result.data["cpu"]["min_percent"], 25.0)
        self.assertAlmostEqual(result.data["cpu"]["max_percent"], 50.0)
        self.assertIn("cpu0", result.data["cpu"]["per_core_avg"])
        self.assertAlmostEqual(result.data["memory"]["used_gib"], 2.0)
        self.assertAlmostEqual(result.data["memory"]["usage_percent"], 25.0)
        self.assertAlmostEqual(result.data["memory"]["swap_used_gib"], 0.25)

    def test_baseline_files_are_written_and_malformed_input_has_no_fake_metrics(self):
        malformed = build_baseline(
            "not a proc sample\n",
            start_time="start",
            end_time="end",
            duration_sec=1,
        )
        self.assertNotIn("cpu", malformed)
        self.assertNotIn("memory", malformed)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = capture(30)
            write_baseline_capture(
                item,
                json_path=root / "baseline.json",
                summary_path=root / "baseline_summary.log",
                metrics_path=root / "09_system_metrics_baseline.log",
                tegrastats_path=root / "10_tegrastats_baseline.log",
                kernel_path=root / "11_kernel_baseline.log",
                journal_path=root / "12_journal_baseline.log",
            )
            self.assertEqual(json.loads((root / "baseline.json").read_text())["cpu"]["avg_percent"], 30)
            self.assertTrue((root / "baseline_summary.log").is_file())
            self.assertEqual((root / "09_system_metrics_baseline.log").read_text(), "synthetic baseline\n")


class StrategyTests(unittest.TestCase):
    def test_minimum_target_observe_only_and_below_target_load_assist(self):
        observe = decide_cpu_strategy(target_percent=30, baseline_percent=34, semantics=TargetSemantics.MINIMUM, stress_ng_available=False)
        assist = decide_cpu_strategy(target_percent=30, baseline_percent=12.3, semantics=TargetSemantics.MINIMUM, stress_ng_available=True)
        self.assertEqual(observe.strategy, LoadStrategy.OBSERVE_ONLY)
        self.assertTrue(observe.can_start)
        self.assertEqual(assist.strategy, LoadStrategy.LOAD_ASSIST)
        self.assertAlmostEqual(assist.gap_percent, 17.7)

    def test_approximate_above_target_and_missing_inputs_need_review(self):
        above = decide_cpu_strategy(target_percent=30, baseline_percent=55, semantics=TargetSemantics.APPROXIMATE)
        no_baseline = decide_cpu_strategy(target_percent=30, baseline_percent=None, semantics=TargetSemantics.MINIMUM)
        no_target = decide_cpu_strategy(target_percent=None, baseline_percent=20, semantics=None)
        self.assertEqual(above.strategy, LoadStrategy.NEEDS_REVIEW)
        self.assertEqual(no_baseline.strategy, LoadStrategy.NEEDS_REVIEW)
        self.assertEqual(no_target.strategy, LoadStrategy.NEEDS_REVIEW)
        self.assertIsNone(no_target.target_percent)

    def test_stress_ng_is_conditional_on_strategy(self):
        observe = decide_cpu_strategy(target_percent=30, baseline_percent=34, semantics=TargetSemantics.MINIMUM, stress_ng_available=False)
        assist = decide_cpu_strategy(target_percent=30, baseline_percent=10, semantics=TargetSemantics.MINIMUM, stress_ng_available=False)
        self.assertTrue(observe.can_start)
        self.assertFalse(assist.can_start)

    def test_catalog_has_explicit_target_and_no_blind_cpu_load_argument(self):
        from desktop_app.stress.catalog import StressCatalog

        definition = StressCatalog.load_vd().get("ST-CPU-001")
        self.assertEqual(definition.target_cpu_percent, 30)
        self.assertNotIn("--cpu-load", definition.workload_arguments)
        self.assertEqual(target_semantics(definition), TargetSemantics.APPROXIMATE)


class AdaptiveExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_immediate_baseline_is_authoritative_and_observe_only_never_launches_stress_ng(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = StressSessionManager(root)
            runner = StressTestRunner(manager, baseline_collector=FixedBaselineCollector(capture(34, stress_ng=False)))
            definition = cpu_definition(semantics="minimum")
            self.assertTrue(runner.enqueue([definition], environment_status=EnvironmentStatus.READY))
            # A stale session baseline must not influence the attempt decision.
            (manager.pretest_dir() / "baseline.json").write_text(json.dumps({"cpu": {"avg_percent": 1}}), encoding="utf-8")
            self.assertTrue(process_until(lambda: runner.current_strategy is not None))
            self.assertEqual(runner.current_strategy.strategy, LoadStrategy.OBSERVE_ONLY)
            self.assertTrue((runner.current_paths.attempt_dir / "pre_run_baseline.json").is_file())
            self.assertTrue((runner.current_paths.logs_dir / "pre_run_system_metrics.log").is_file())
            self.assertTrue(runner.start_current())
            workload = runner.evidence_manager.collectors["workload"]
            self.assertIsInstance(workload, ObservationEvidenceCollector)
            self.assertTrue(process_until(lambda: definition.runtime_status == RuntimeStatus.NEEDS_REVIEW, timeout=3))
            result = json.loads((workload.log_path.parent.parent / "result.json").read_text())
            self.assertEqual(result["status"], "NEEDS_REVIEW")
            self.assertEqual(result["load_strategy"], "OBSERVE_ONLY")

    def test_load_assist_missing_stress_ng_blocks_start(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = StressTestRunner(
                StressSessionManager(directory),
                baseline_collector=FixedBaselineCollector(capture(10, stress_ng=False)),
            )
            definition = cpu_definition(semantics="minimum")
            errors = []
            runner.error.connect(errors.append)
            runner.enqueue([definition], environment_status=EnvironmentStatus.READY)
            self.assertTrue(process_until(lambda: runner.current_strategy is not None))
            self.assertEqual(runner.current_strategy.strategy, LoadStrategy.LOAD_ASSIST)
            self.assertFalse(runner.start_current())
            self.assertIn("stress-ng is required", errors[-1])
            runner.stop()

    def test_load_assist_records_adjustment_history_and_separate_stress_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "stress-ng"
            executable.write_text("#!/bin/sh\necho safe-mock-stress-ng\n", encoding="utf-8")
            executable.chmod(0o755)
            definition = cpu_definition(duration=1)
            definition.evidence_plan = (definition.evidence_plan[0],)
            decision = LoadStrategyDecision(
                LoadStrategy.LOAD_ASSIST,
                30,
                29,
                1,
                "test",
                TargetSemantics.MINIMUM,
                True,
            )
            manager = EvidenceManager(definition, root, strategy_decision=decision)
            with patch.dict(os.environ, {"PATH": f"{root}:{os.environ.get('PATH', '')}"}):
                manager.start_all()
                self.assertTrue(process_until(lambda: not manager.collectors["workload"].running, timeout=4))
            workload = (root / manager.records[0].file).read_text(encoding="utf-8")
            self.assertIn("Execution Strategy: LOAD_ASSIST", workload)
            self.assertIn("ADJUSTMENT T+", workload)
            self.assertIn("initial conservative step", workload)
            self.assertIn("safe-mock-stress-ng", (root / "logs" / "stress_ng_output.log").read_text())

    def test_qt_strategy_confirmation_execution_back_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            page = SystemStressPage(
                evidence_root=directory,
                baseline_collector=FixedBaselineCollector(capture(30, stress_ng=False)),
            )
            definition = page.catalog.get("ST-CPU-001")
            page.runner.enqueue([definition], environment_status=EnvironmentStatus.READY)
            self.assertTrue(process_until(lambda: page.runner.current_strategy is not None))
            self.assertEqual(page.runner.current_strategy.strategy, LoadStrategy.OBSERVE_ONLY)
            self.assertIn("OBSERVE ONLY", page.vd_page.confirmation_text.toPlainText())
            self.assertEqual(page.vd_page.confirmation_start_button.text(), "START MONITORING")
            page.vd_page.confirmation_start_button.click()
            self.assertTrue(process_until(lambda: definition.runtime_status == RuntimeStatus.RUNNING))
            self.assertIsInstance(page.runner.evidence_manager.collectors["workload"], ObservationEvidenceCollector)
            self.assertIn("Baseline CPU: 30.0 %", page.vd_page.strategy_summary_label.text())
            self.assertIn("Strategy: OBSERVE ONLY", page.vd_page.strategy_summary_label.text())
            strategy = page.runner.current_strategy
            paths = page.runner.current_paths
            page.vd_page.back_to_list_button.click()
            self.assertEqual(page.vd_page.view_stack.currentIndex(), 0)
            self.assertIs(page.runner.current_strategy, strategy)
            self.assertIs(page.runner.current_paths, paths)
            page.vd_page.view_running_button.click()
            self.assertEqual(page.vd_page.view_stack.currentIndex(), 2)
            page.runner.stop()
            self.assertTrue(process_until(lambda: definition.runtime_status == RuntimeStatus.STOPPED))
            page.shutdown()
            page.close()

    def test_qt_load_assist_without_stress_ng_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            page = SystemStressPage(
                evidence_root=directory,
                baseline_collector=FixedBaselineCollector(capture(10, stress_ng=False)),
            )
            definition = page.catalog.get("ST-CPU-001")
            page.runner.enqueue([definition], environment_status=EnvironmentStatus.READY)
            self.assertTrue(process_until(lambda: page.runner.current_strategy is not None))
            self.assertEqual(page.runner.current_strategy.strategy, LoadStrategy.LOAD_ASSIST)
            self.assertFalse(page.vd_page.confirmation_start_button.isEnabled())
            self.assertIn("stress-ng is required", page.vd_page.confirmation_text.toPlainText())
            page.runner.stop()
            page.shutdown()
            page.close()


if __name__ == "__main__":
    unittest.main()
