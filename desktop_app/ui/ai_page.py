import html
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from core.testing.definitions import load_definitions
from core.testing.registry import TestRegistry
from desktop_app.workers.ai_test_runner_worker import AiTestRunnerWorker
from devices.ai import AiModule, AiRemoteService, AiRuntimeAdapter, register_ai_handlers
from desktop_app.ui.widgets import Card, StatusChip


class AiPage(QWidget):
    """Phase 8.4A AI automation workspace; all remote work uses Jetson service."""
    def __init__(self, jetson_state, jetson_service, parent=None):
        super().__init__(parent)
        self.jetson_state, self.jetson_service = jetson_state, jetson_service
        self.registry = TestRegistry(); register_ai_handlers(self.registry)
        self.definitions = load_definitions("testcases/ai/definitions/phase8_4a.json", self.registry)
        self.modules, self.environment = (), {}
        self.environment_result_exists = False
        self.statuses, self.results = {}, {}
        self._request_id = None; self._worker = None; self._selected_test_id = None
        self._build_ui(); self._load_tests()
        self.jetson_state.state_changed.connect(self._on_state_changed)
        self.jetson_service.operation_succeeded.connect(self._on_operation_succeeded)
        self.jetson_service.operation_failed.connect(self._on_operation_failed)
        self._on_state_changed(self.jetson_state)

    @staticmethod
    def _table(table):
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)

    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(18, 14, 18, 14); root.setSpacing(10)
        title = QLabel("AI Automation"); title.setObjectName("PageTitle"); root.addWidget(title)
        tabs = QHBoxLayout(); self.monitor_button = QPushButton("MONITOR"); self.tests_button = QPushButton("AUTOMATED TESTS"); self.performance_button = QPushButton("PERFORMANCE")
        for index, button in enumerate((self.monitor_button, self.tests_button, self.performance_button)):
            button.setCheckable(True); button.setObjectName("OutlineButton"); button.clicked.connect(lambda _checked=False, value=index: self._set_tab(value)); tabs.addWidget(button)
        tabs.addStretch(); root.addLayout(tabs)
        self.stack = QStackedWidget(); self.stack.addWidget(self._monitor()); self.stack.addWidget(self._tests()); self.stack.addWidget(self._performance()); root.addWidget(self.stack, 1)
        self._set_tab(0)

    def _monitor(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(10)
        header = Card("AI Status")
        row = QHBoxLayout(); row.addWidget(QLabel("Target AI Module:")); self.target_combo = QComboBox(); self.target_combo.currentIndexChanged.connect(self._target_changed); row.addWidget(self.target_combo, 1)
        self.discover_button = QPushButton("⌕  DISCOVER / REFRESH"); self.discover_button.setObjectName("PrimaryButton"); self.discover_button.clicked.connect(self.discover); row.addWidget(self.discover_button)
        header.body_layout.addLayout(row)
        badges = QHBoxLayout(); self.jetson_chip = StatusChip("Jetson: DISCONNECTED", "idle"); self.runtime_chip = StatusChip("AI Runtime: UNKNOWN", "idle"); self.input_chip = StatusChip("Input: UNKNOWN", "idle"); self.model_chip = StatusChip("Model: UNKNOWN", "idle"); self.inference_chip = StatusChip("Inference: UNKNOWN", "idle")
        for chip in (self.jetson_chip, self.runtime_chip, self.input_chip, self.model_chip, self.inference_chip): badges.addWidget(chip)
        badges.addStretch(); header.body_layout.addLayout(badges); layout.addWidget(header)
        split = QSplitter(Qt.Orientation.Horizontal)
        runtime = Card("Runtime Overview"); self.runtime_table = QTableWidget(8, 2); self.runtime_table.setHorizontalHeaderLabels(["Field", "Value"]); self._table(self.runtime_table)
        for row, name in enumerate(("Runtime", "Framework", "Version", "Model", "Precision", "Execution Device", "Process / Node", "Status")):
            self.runtime_table.setItem(row, 0, QTableWidgetItem(name)); self.runtime_table.setItem(row, 1, QTableWidgetItem("-"))
        runtime.body_layout.addWidget(self.runtime_table); split.addWidget(runtime)
        pipeline = Card("Pipeline Overview"); self.pipeline_text = QTextEdit(); self.pipeline_text.setReadOnly(True); self.pipeline_text.setObjectName("LiveLog"); self.pipeline_text.setPlainText("INPUT  →  AI INFERENCE  →  OUTPUT\nDiscover an AI module to populate this view."); pipeline.body_layout.addWidget(self.pipeline_text); split.addWidget(pipeline)
        split.setSizes([520, 700]); layout.addWidget(split, 1)
        return page

    def _tests(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0)
        controls = Card(); row = QHBoxLayout(); self.ai_summary = QLabel("Total 0 | Selected 0 | PASS 0 | FAIL 0 | BLOCKED 0"); row.addWidget(self.ai_summary); row.addStretch(); self.run_button = QPushButton("▶  RUN SELECTED"); self.run_button.setObjectName("PrimaryButton"); self.run_button.clicked.connect(self.run_selected); self.cancel_button = QPushButton("■  CANCEL"); self.cancel_button.setObjectName("DangerButton"); self.cancel_button.setEnabled(False); self.cancel_button.clicked.connect(self.cancel); row.addWidget(self.run_button); row.addWidget(self.cancel_button); controls.body_layout.addLayout(row); layout.addWidget(controls)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget(); left_layout = QVBoxLayout(left); left_layout.setContentsMargins(0, 0, 0, 0)
        table_card = Card("AI Automated Tests"); self.test_table = QTableWidget(0, 6); self.test_table.setHorizontalHeaderLabels(["Select", "ID", "Test Name", "Target", "Duration", "Status"]); self._table(self.test_table); self.test_table.itemChanged.connect(self._refresh_summary); self.test_table.cellClicked.connect(self._select_test); table_card.body_layout.addWidget(self.test_table); left_layout.addWidget(table_card, 2)
        detail_card = Card("Selected Test Detail"); self.detail_text = QTextEdit(); self.detail_text.setReadOnly(True); self.detail_text.setPlainText("Select an AI test case to view details."); detail_card.body_layout.addWidget(self.detail_text); left_layout.addWidget(detail_card, 1); split.addWidget(left)
        log_card = Card("AI Execution Log"); self.log_text = QTextEdit(); self.log_text.setObjectName("LiveLog"); self.log_text.setReadOnly(True); log_card.body_layout.addWidget(self.log_text); split.addWidget(log_card); split.setSizes([820, 460]); layout.addWidget(split, 1)
        return page

    @staticmethod
    def _performance():
        page = QWidget(); layout = QVBoxLayout(page); layout.addStretch(); label = QLabel("AI performance automation will be implemented in Phase 8.4C."); label.setObjectName("Muted"); label.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(label); layout.addStretch(); return page

    def _set_tab(self, index):
        self.stack.setCurrentIndex(index)
        for current, button in enumerate((self.monitor_button, self.tests_button, self.performance_button)): button.setChecked(current == index)

    def _load_tests(self):
        self.test_table.blockSignals(True); self.test_table.setRowCount(len(self.definitions))
        for row, definition in enumerate(self.definitions):
            self.statuses.setdefault(definition.test_id, "NOT RUN")
            select = QTableWidgetItem(); select.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable); select.setCheckState(Qt.CheckState.Unchecked); self.test_table.setItem(row, 0, select)
            values = (definition.test_id, definition.name, self._target_label(), f"{int(definition.timeout_s)} s", self.statuses[definition.test_id])
            for col, value in enumerate(values, 1): self.test_table.setItem(row, col, QTableWidgetItem(value))
        self.test_table.blockSignals(False); self._refresh_summary()

    def _target_label(self):
        if not self.modules: return "No module discovered"
        return "All AI Modules" if self.target_combo.currentData() is None else self.target_combo.currentText()

    def discover(self):
        if not self.jetson_service.is_connected or self._request_id: return
        self.log("INFO", "AI discovery started."); self.discover_button.setEnabled(False)
        async def operation(ssh): return {"ai_discovery": await AiRemoteService().execute_with_ssh(ssh, "discover", {}, 10)}
        self._request_id = self.jetson_service.submit_operation("ai_discovery", operation)

    def _on_operation_succeeded(self, request_id, payload):
        if request_id != self._request_id: return
        self._request_id = None; self.discover_button.setEnabled(self.jetson_service.is_connected)
        response = (payload or {}).get("ai_discovery") or {}
        # AiRemoteService returns {ok, environment}; retain the inner
        # environment even when its modules list is empty.  The direct form is
        # accepted as well for deterministic service/UI test doubles.
        environment = response.get("environment") if isinstance(response, dict) and isinstance(response.get("environment"), dict) else response
        if not isinstance(environment, dict):
            environment = {}
        self.environment = environment; self.environment_result_exists = bool(environment)
        # The registry is the final selector gate.  A remote discovery result
        # may include candidates for diagnostics, but only confirmed static
        # registrations may appear as normal targets.
        self.modules = AiRuntimeAdapter().discover_modules(environment)
        self.target_combo.blockSignals(True); self.target_combo.clear(); self.target_combo.addItem("All AI Modules", None)
        for module in self.modules: self.target_combo.addItem(module.display_name, module.module_uid)
        self.target_combo.setEnabled(bool(self.modules)); self.target_combo.blockSignals(False)
        for runtime in environment.get("runtime_probe_results") or environment.get("runtimes") or ():
            name = runtime.get("name", "Runtime")
            if runtime.get("available"):
                self.log("INFO", f"AI environment: {name} {runtime.get('version') or 'available'} available.")
            else:
                self.log("INFO", f"AI environment: {name} unavailable.")
        self.log("INFO", f"AI modules: {1 if environment.get('module_running') else 0} active modules discovered ({int(environment.get('module_count') or len(self.modules))} configured/discovered).")
        candidates = environment.get("module_candidates") or ()
        if candidates:
            self.log("WARNING", f"AI modules: {len(candidates)} candidate(s) require explicit registration.")
        self.log("INFO", "AI discovery completed.")
        for warning in environment.get("warnings") or (): self.log("WARNING", warning)
        self._update_monitor(); self._load_tests()

    def _on_operation_failed(self, request_id, error):
        if request_id != self._request_id: return
        self._request_id = None; self.discover_button.setEnabled(self.jetson_service.is_connected); self.log("ERROR", "AI discovery failed: " + str(error))

    def _target_changed(self): self._update_monitor(); self._load_tests()

    def _selected_modules(self):
        uid = self.target_combo.currentData()
        return self.modules if uid is None else tuple(item for item in self.modules if item.module_uid == uid)

    def _update_monitor(self):
        module = self._selected_modules()[0] if len(self._selected_modules()) == 1 else None
        env = self.environment
        self.jetson_chip.set_state("ok" if self.jetson_service.is_connected else "idle", "Jetson: " + ("CONNECTED" if self.jetson_service.is_connected else "DISCONNECTED"))
        self.runtime_chip.set_state("ok" if env.get("runtime_available") else "warning" if self.environment_result_exists else "idle", "AI Runtime: " + ("READY" if env.get("runtime_available") else "NOT AVAILABLE" if self.environment_result_exists else "UNKNOWN"))
        running = bool(module and module.status in {"EXTERNAL", "RUNNING"})
        input_known = bool(module and module.input_topics)
        input_text = "READY" if input_known and running else "CONFIGURED / NOT ACTIVE" if input_known else "NO DATA" if module else "UNKNOWN / NO MODULE"
        self.input_chip.set_state("ok" if input_known and running else "warning" if input_known else "idle", "Input: " + input_text)
        config_available = bool(env.get("configuration_candidates") or env.get("model_candidates"))
        model_loaded = bool(module and module.metadata.get("model_loaded"))
        model_text = "LOADED" if model_loaded else "CONFIGURED" if module and (module.model_name or module.model_path) else "CONFIG CANDIDATES AVAILABLE" if config_available else "UNKNOWN"
        self.model_chip.set_state("ok" if model_loaded else "warning" if module and (module.model_name or module.model_path) or config_available else "idle", "Model: " + model_text)
        self.inference_chip.set_state("ok" if running else "idle", "Inference: " + ("RUNNING" if running else "IDLE / NOT RUNNING"))
        values = (module.runtime if module else env.get("runtime_name", "-"), module.runtime if module else "-", env.get("runtime_version", "-") or "-", (module.model_name or module.model_path) if module else "-", "-", env.get("execution_host", "-") if self.environment_result_exists else "-", (module.ros_node or module.process_name or module.executable) if module else "-", module.status if module else "READY" if env.get("runtime_available") else "UNKNOWN")
        for row, value in enumerate(values): self.runtime_table.item(row, 1).setText(str(value or "-"))
        if module:
            inp = module.input_topics[0] if module.input_topics else None; out = module.output_topics[0] if module.output_topics else None
            prefix = "Active" if running else "Configured"
            self.pipeline_text.setPlainText("%s INPUT\n%s\n%s\n\n→  AI INFERENCE\n%s\n%s\n%s\n\n→  %s OUTPUT\n%s\n%s" % (prefix, inp.name if inp else "UNKNOWN", inp.message_type if inp else "-", module.display_name, module.runtime, "RUNNING" if running else "NOT RUNNING", prefix, out.name if out else "UNKNOWN", out.message_type if out else "-"))
        else:
            candidate_note = "\n%d candidate(s) require explicit registration." % len(env.get("module_candidates") or ()) if env.get("module_candidates") else ""
            self.pipeline_text.setPlainText((("INPUT  →  AI INFERENCE  →  OUTPUT\nRuntime available: %s %s\nNo active AI module discovered." % (env.get("runtime_name", "UNKNOWN"), env.get("runtime_version", ""))).rstrip() + candidate_note) if env.get("runtime_available") else "No AI runtime or module discovery result is available.")

    def _select_test(self, row, _column):
        item = self.test_table.item(row, 1)
        if not item: return
        test_id = item.text(); self._selected_test_id = test_id; definition = next(item for item in self.definitions if item.test_id == test_id); result = self.results.get(test_id)
        lines = [f"Test ID: {definition.test_id}", f"Target: {self._target_label()}", f"Automation Key: {definition.automation_key}", "Parameters: " + " | ".join(f"{key}={value}" for key, value in definition.parameters.items())]
        if result:
            m = result.get("measurements") or {}; lines.append("Latest Result: " + result.get("status", "--"))
            for sub in result.get("sub_results") or (): lines.append(f"{sub.get('display_name')}: " + ", ".join(f"{key}={value}" for key, value in (sub.get("measurements") or {}).items() if key in {"runtime_name", "process_alive", "startup_time_s", "endpoint", "message_type", "sample_count", "observed_rate_hz", "input_count", "output_count", "correlation_result", "cleanup_success"}))
        self.detail_text.setPlainText("\n".join(lines))

    def _refresh_summary(self, *_):
        selected = sum(self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(self.test_table.rowCount()))
        counts = {key: list(self.statuses.values()).count(key) for key in ("PASS", "FAIL", "BLOCKED")}
        self.ai_summary.setText(f"Total {len(self.definitions)} | Selected {selected} | PASS {counts['PASS']} | FAIL {counts['FAIL']} | BLOCKED {counts['BLOCKED']}")
        self.run_button.setEnabled(bool(selected) and bool(self.modules) and not (self._worker and self._worker.isRunning()))

    def run_selected(self):
        selected = [self.test_table.item(row, 1).text() for row in range(self.test_table.rowCount()) if self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked]
        if not selected or not self._selected_modules(): return
        definitions = [item for item in self.definitions if item.test_id in selected]; modules = self._selected_modules()
        worker = AiTestRunnerWorker(definitions, self.registry, self.jetson_service, modules, "ALL_MODULES" if self.target_combo.currentData() is None else "INDIVIDUAL", self)
        worker.test_started.connect(self._test_started); worker.test_finished.connect(self._test_finished); worker.log_event.connect(self.log); worker.suite_finished.connect(self._suite_finished); worker.finished.connect(self._worker_finished)
        self._worker = worker; self.target_combo.setEnabled(False); self.test_table.setEnabled(False); self.run_button.setEnabled(False); self.cancel_button.setEnabled(True); self.log("INFO", f"Captured AI target snapshot with {len(modules)} module(s)."); worker.start()

    def cancel(self):
        if self._worker and self._worker.isRunning(): self.cancel_button.setEnabled(False); self._worker.cancel(); self.log("WARNING", "Cancellation requested; only test-owned AI sessions will be stopped.")

    def _test_started(self, test_id):
        self.statuses[test_id] = "RUNNING"; self._load_tests(); self._refresh_summary()

    def _test_finished(self, test_id, status, result):
        self.statuses[test_id] = status; self.results[test_id] = result; self._load_tests(); self._refresh_summary()
        if self._selected_test_id == test_id:
            row = next(index for index in range(self.test_table.rowCount()) if self.test_table.item(index, 1).text() == test_id); self._select_test(row, 1)

    def _suite_finished(self, summary, root): self.log("INFO", "AI test suite finished: " + ", ".join(f"{key}={value}" for key, value in summary.items()) + f". Evidence: {root}")

    def _worker_finished(self):
        self.target_combo.setEnabled(bool(self.modules)); self.test_table.setEnabled(True); self.cancel_button.setEnabled(False); self._worker = None; self._refresh_summary()

    def _on_state_changed(self, _state):
        connected = self.jetson_service.is_connected; self.discover_button.setEnabled(connected and self._request_id is None); self.jetson_chip.set_state("ok" if connected else "idle", "Jetson: " + ("CONNECTED" if connected else "DISCONNECTED")); self._refresh_summary()

    def log(self, level, message):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S"); color = {"PASS": "#3FB950", "FAIL": "#F85149", "ERROR": "#F85149", "WARNING": "#D29922"}.get(level, "#8B949E")
        self.log_text.append(f'<span style="color:{color}">[{stamp}] [{html.escape(level)}] {html.escape(str(message))}</span>')
