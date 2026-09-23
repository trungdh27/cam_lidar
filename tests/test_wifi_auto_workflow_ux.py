"""AUTO tester workflow, preflight isolation, live progress and safe stop."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import shlex

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget, QTableWidget

from desktop_app.ui.wifi_page import WifiPage, ERROR_COLOR
from desktop_app.wifi import auto_collectors as collectors
from desktop_app.wifi.auto_suite import AutoSuiteSetup, readiness_status
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.runtime import WifiRuntime
from tests.test_wifi_auto_suite import ready_setup, passing_measurements
from tests.test_wifi_module import SharedService, app, result


@pytest.fixture
def page(tmp_path):
    app()
    widget = WifiPage(SharedService(), evidence_root=tmp_path)
    widget.resize(1280, 950)
    widget.show()
    widget.tabs.setCurrentIndex(1)
    widget.vd.suite_tabs.setCurrentIndex(1)
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def view(page):
    return page.vd.auto_pages["AP_24G"]


def visible_labels(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel) if label.isVisibleTo(widget))


def test_readiness_is_one_authoritative_table(page):
    pretest = page.pretest
    assert pretest.discovery_table is pretest.table
    assert pretest.findChildren(QTableWidget) == [pretest.table]
    assert [pretest.table.horizontalHeaderItem(i).text() for i in range(3)] == ["Check", "Detected Value", "State"]
    assert "AUTO-DETECTED" not in visible_labels(pretest)
    page.runtime.auto_setup = ready_setup()
    page.runtime.changed.emit()
    assert pretest.table.item(pretest.rows["Current band"], 1).text() == "2.4 GHz"
    page.runtime.update_detected_auto_setup(current_band="5 GHz", current_channel=48)
    assert pretest.table.item(pretest.rows["Current band"], 1).text() == "5 GHz"
    assert pretest.table.item(pretest.rows["Current channel"], 1).text() == "48"
    assert pretest.table.minimumHeight() == 0


@pytest.mark.parametrize("test_id,missing", [
    ("TC-JET-5G-002", {}),
    ("TC-JET-5G-009", {"distance_10m_confirmed": False}),
    ("TC-JET-STA-5G-001", {"external_ssid_5g": "", "external_credential": ""}),
])
def test_pretest_never_exposes_runtime_or_setup_inputs(page, test_id, missing):
    setup = replace(ready_setup(), current_band="5 GHz", current_ssid="JET-5", **missing)
    page.runtime.auto_setup = setup
    case = next(case for case in load_auto_catalog() if case.test_id == test_id)
    page.pretest.set_context(case)
    assert page.pretest.unresolved_setup.isHidden()
    assert page.pretest.unresolved_title.isHidden()
    assert page.pretest.advanced_setup.isHidden()
    for key in ("jetson_wifi_interface", "client_wifi_interface", "jetson_ap_ip", "ap_profile"):
        assert page.pretest.field_rows[key].isHidden() and page.pretest.auto_fields[key].isReadOnly()


def test_discovery_failure_does_not_create_manual_sta_input(page):
    page.runtime.auto_setup = replace(ready_setup(), external_ssid_5g="")
    case = next(case for case in load_auto_catalog() if case.test_id == "TC-JET-STA-5G-001")
    page.pretest.set_context(case)
    page.tabs.setCurrentIndex(0)
    assert page.pretest.unresolved_setup.isHidden()
    assert not page.pretest.auto_fields["external_ssid_5g"].isVisibleTo(page.pretest)


def test_blocked_preflight_never_allocates_attempt_or_execution(page):
    auto = view(page)
    auto._start_case(auto.by_id["TC-JET-24G-001"])
    assert page.runtime.active is None
    assert not list(page.runtime.evidence_root.rglob("attempt_*"))
    assert auto.stack.currentWidget() is auto.execution_view
    assert auto.execution_status.text() == "ERROR"
    labels = visible_labels(auto.execution_view)
    assert "No manual value entry is available" in labels
    assert "Why:" in labels and "How to fix: Connect to Jetson from Dashboard" in labels
    assert "Execution:" not in labels and "Elapsed" not in labels and "Attempt:" not in labels
    assert auto.console_setup.title.property("tone") == "error"
    assert auto.console_setup.dependencies.isHidden()
    auto.console_setup.dependencies.show_all.setChecked(True)
    assert auto.console_setup.dependencies.blockers.isHidden()
    actions = {button.text() for button in auto.console_setup.items.findChildren(QPushButton)}
    assert {"GO TO DASHBOARD", "RECHECK"} <= actions
    assert labels.count("Shared SSH —") == 1
    auto.run_again.click()
    assert not list(page.runtime.evidence_root.rglob("attempt_*"))


def test_blocked_preflight_preserves_previous_real_history(page):
    page.runtime.auto_setup = ready_setup()
    auto = view(page)
    case = auto.by_id["TC-JET-24G-001"]
    auto._start_case(case)
    attempt = page.runtime.active
    page.runtime.complete_auto(passing_measurements(case, ready_setup()))
    page.runtime.auto_setup = AutoSuiteSetup()
    auto._start_case(case)
    assert page.runtime.active is attempt
    assert page.runtime.attempts("VD", case.test_id) == [attempt.directory / "result.json"]
    assert not (attempt.directory.parent / "attempt_002").exists()
    assert auto.stack.currentWidget() is auto.execution_view
    assert auto.execution_status.text() == "ERROR"


def test_setup_actions_confirm_and_navigate(page):
    auto = view(page)
    page.runtime.auto_setup = replace(ready_setup(), distance_10m_confirmed=False)
    case = auto.by_id["TC-JET-24G-009"]
    auto.show_test_console(case.test_id)
    auto.run_again.click()
    assert auto.execution_status.text() == "SETUP REQUIRED"
    confirm = next(button for button in auto.console_setup.items.findChildren(QPushButton)
                   if button.text() == "CONFIRM 10 m POSITION")
    confirm.click()
    QApplication.processEvents()
    assert page.runtime.auto_setup.distance_10m_confirmed
    assert page.runtime.active is not None
    events = []
    page.dashboard_requested.connect(lambda: events.append("dashboard"))
    auto._setup_action("dashboard", "shared_ssh_ready", case)
    assert events == ["dashboard"]


def test_auto_detail_retains_source_without_primary_documentation_tabs(page):
    auto = view(page)
    auto.show_detail("TC-JET-24G-001")
    assert auto.detail_tabs is None and not auto.detail_view.findChildren(QTabWidget)
    assert "Band: 2.4 GHz" in auto.auto_checks.text()
    assert "Channel" not in auto.auto_checks.text() and "Frequency" not in auto.auto_checks.text()
    assert auto.source_details.isHidden()
    assert auto.current_case.procedure in auto.source_details.toPlainText()


def test_ready_start_phase_elapsed_stop_and_completion(page):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    auto.show_detail("TC-JET-24G-001")
    assert auto.run_this.text() == "OPEN TEST CONSOLE"
    assert not list(page.runtime.evidence_root.rglob("attempt_*"))
    auto.run_this.click()
    assert auto.stack.currentWidget() is auto.execution_view
    assert auto.execution_status.text() == "READY"
    assert page.runtime.active is None
    assert not list(page.runtime.evidence_root.rglob("attempt_*"))
    auto.run_again.click()
    attempt = page.runtime.active
    assert attempt.directory.name == "attempt_001"
    assert auto.stack.currentWidget() is auto.execution_view
    assert "PREPARING" in auto.execution_status.text()
    assert "Phase: PREPARING" in auto.execution_phase.text()
    assert "Started:" in auto.execution_elapsed.text() and "Elapsed:" in auto.execution_elapsed.text()
    assert not auto.stop_test.isHidden() and auto.stop_test.isEnabled()
    page.runtime._collector_updated({"attempt": str(attempt.directory), "phase": "MEASURING", "detail": "Upload run 1 / 1", "done": 0, "total": 2})
    assert "RUNNING" in auto.execution_status.text() and "Upload run 1 / 1" in auto.execution_phase.text()
    assert not auto.execution_progress.isHidden()
    attempt.started -= timedelta(seconds=42)
    auto._render_execution()
    assert "00:00:42" in auto.execution_elapsed.text()
    page.runtime.complete_auto(passing_measurements(attempt.case, ready_setup()))
    assert attempt.status == "COMPLETED" and attempt.final_result == "PASS"
    assert "COMPLETED" in auto.execution_status.text() and "PASS" in auto.auto_result_label.text()
    assert "Duration:" in auto.execution_elapsed.text() and "Completed:" in auto.execution_elapsed.text()
    frozen = attempt.elapsed_seconds
    attempt.finished += timedelta(seconds=5)
    assert attempt.elapsed_seconds == frozen + 5
    assert auto.stop_test.isHidden() and not auto.run_again.isHidden() and not auto.completed_folder.isHidden()
    assert json.loads((attempt.directory / "result.json").read_text())["execution_state"] == "COMPLETED"


def test_live_measurements_and_log_receive_resizable_space(page):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    case = auto.by_id["TC-JET-24G-006"]
    auto._start_case(case)
    attempt = page.runtime.active
    page.runtime._collector_updated({"attempt": str(attempt.directory), "phase": "MEASURING", "detail": "Download run 1 / 1", "metrics": {"Forward receiver Mbps": 50, "Reverse receiver Mbps": 60, "RSSI": -61, "Retransmits": 2}})
    QApplication.processEvents()
    assert auto.layers.count() == 2 and auto.layers.currentIndex() == 1
    assert auto.layers.height() >= 220 and auto.top_scroll.height() >= 170
    assert auto.monitor.cards["Forward receiver Mbps"].height() == 88
    assert "RSSI" not in auto.monitor.cards  # supporting data, not a throughput summary card
    old = auto.splitter.sizes()
    auto.splitter.setSizes([160, 450])
    QApplication.processEvents()
    assert auto.splitter.sizes() != old
    auto._render_execution()
    assert auto.splitter.sizes()[1] > auto.splitter.sizes()[0]
    assert auto.log_modes.currentIndex() == 0 and auto.import_widget is None


def test_back_preserves_running_attempt_filters_selection_and_banner(page):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    auto.filters["Category"].setCurrentText("AP Runtime")
    auto.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    selected = set(auto.selected)
    auto._list_scroll = 17
    auto._start_case(auto.by_id["TC-JET-24G-001"])
    attempt = page.runtime.active
    auto._list_scroll = 17
    auto.back_to_list()
    assert page.runtime.active is attempt and attempt.status in {"PREPARING", "RUNNING"}
    assert auto.selected == selected and auto.filters["Category"].currentText() == "AP Runtime"
    assert auto._list_scroll == 17 and not auto.mini.isHidden()
    assert attempt.case.test_id in auto.mini_text.text()
    auto.view_running.click()
    assert page.runtime.active is attempt and auto.stack.currentWidget() is auto.execution_view


def test_refresh_run_and_stop_actions_transition_immediately(page, monkeypatch):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    case = auto.by_id["TC-JET-24G-001"]
    auto.show_test_console(case.test_id)
    calls = []
    monkeypatch.setattr(page.runtime, "refresh_auto_discovery",
                        lambda: calls.append("refresh") or "refresh-request")
    auto.refresh_status.click()
    assert calls == ["refresh"] and page.runtime.active is None
    assert not list(page.runtime.evidence_root.rglob("attempt_*"))

    monkeypatch.setattr(page.runtime, "start", lambda *_args, **_kwargs: None)
    auto.run_again.click()
    assert auto.execution_status.text() == "PREPARING..."
    assert auto.run_again.text() == "PREPARING..." and not auto.run_again.isEnabled()


def test_stop_click_is_debounced_and_back_does_not_cancel(page):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    auto._start_case(auto.by_id["TC-JET-24G-001"])
    attempt = page.runtime.active
    auto.stop_test.click()
    assert attempt.status == "STOPPING"
    assert auto.stop_test.text() == "STOPPING..." and not auto.stop_test.isEnabled()
    auto.back_to_list()
    assert page.runtime.active is attempt and attempt.status == "STOPPING"


def test_stop_waits_for_safe_cleanup_and_preserves_evidence(page, monkeypatch):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    case = auto.by_id["TC-JET-24G-006"]
    commands = []
    async def local(command, timeout, on_output=None):
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: JET-24", "", 0
        if command.startswith("ip route"):
            return "192.168.2.22 dev wlan0 src 192.168.2.30", "", 0
        if command.startswith("iperf3"):
            asyncio.get_running_loop().call_soon(auto.stop_test.click)
            await asyncio.Event().wait()
        return "", "", 0
    class Remote:
        async def run(self, command, timeout):
            commands.append(command)
            if "SERVER_STATE=" in command:
                return result(command, "SERVER_STATE=STARTED_BY_APP\nSERVER_OWNED=YES\nSERVER_PID=123\nSERVER_START=456\nSERVER_DIR=/tmp/cam_lidar_iperf.aBc123\n")
            if command.startswith("ip route get"):
                return result(command, "192.168.2.30 dev wlP1p1s0")
            if command.endswith(" info"):
                return result(command, "Interface wlP1p1s0\n type AP\n ssid JET-24\n channel 6 (2437 MHz)")
            return result(command)
    monkeypatch.setattr(collectors, "_local", local)
    auto._start_case(case)
    attempt = page.runtime.active
    capture = asyncio.run(asyncio.wait_for(page.runtime.services["Jetson"].operations[0][1](Remote()), timeout=3))
    assert capture["stopped"] and any("then kill 123" in command for command in commands)
    assert attempt.status == "RESTORING"
    assert not auto.stop_test.isEnabled()
    page.runtime.services["Jetson"].operation_succeeded.emit("wifi-auto:1", capture)
    assert attempt.status == attempt.final_result == "STOPPED"
    assert auto.execution_status.text() == "STOPPED"
    assert auto.stop_test.isHidden()
    saved = json.loads((attempt.directory / "result.json").read_text())
    assert saved["final_result"] == "STOPPED" and saved["evidence"]
    assert any("iperf_cleanup" in name for name in saved["evidence"])
    raw = (attempt.directory / "raw" / "commands.log").read_text()
    assert raw == capture["raw"] and "Command stopped by user" in raw


def test_local_stop_terminates_owned_process_and_keeps_partial_output(tmp_path):
    async def run():
        ready = asyncio.Event()
        chunks = []
        def output(chunk):
            chunks.append(chunk)
            ready.set()
        marker = tmp_path / "should_not_run"
        task = asyncio.create_task(collectors._local(f"printf 'time=12.4 ms\\n'; sleep 10; touch {shlex.quote(str(marker))}", 20, on_output=output))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(collectors.CommandInterrupted) as stopped:
            await asyncio.wait_for(task, 5)
        assert "time=12.4 ms" in stopped.value.stdout and not marker.exists()
    asyncio.run(run())


@pytest.mark.parametrize("safe,allowed,expected", [(True, True, "READY_WITH_RECONFIG"), (False, True, "BLOCKED"), (True, False, "BLOCKED")])
def test_reconfigure_is_advertised_only_with_verified_restore_control(safe, allowed, expected):
    case = load_auto_catalog()[0]
    setup = replace(ready_setup(), current_band="5 GHz", current_channel=48,
                    primary_non_wifi_management=safe, backup_ethernet_ready=False, allow_disruptive=allowed)
    assert readiness_status(case, setup) == expected
    if expected == "READY_WITH_RECONFIG":
        from desktop_app.wifi.auto_suite import dependencies_for
        assert next(dep for dep in dependencies_for(case, setup) if dep.key == "current_band").state == "RECONFIG REQUIRED"


@pytest.mark.parametrize("cancel", [False, True])
def test_temporary_ap_reconfiguration_restores_original_on_finish_and_stop(monkeypatch, cancel):
    case = load_auto_catalog()[0]
    setup = replace(ready_setup(), current_band="5 GHz", current_channel=48)
    commands, events = [], []
    async def local(command, timeout, on_output=None):
        return "JET-24", "", 0
    class Remote:
        async def run(self, command, timeout):
            commands.append(command)
            if command.endswith(" info"):
                if cancel:
                    raise asyncio.CancelledError()
                return result(command, "Interface wlP1p1s0\n type AP\n ssid JET-24\n channel 6 (2437 MHz)")
            if command.startswith("nmcli -g GENERAL.CONNECTION"):
                return result(command, "Hotspot")
            return result(command)
    monkeypatch.setattr(collectors, "_local", local)
    captured = asyncio.run(collectors.collect_auto_case(Remote(), case, setup, observer=events.append))
    assert any("connection clone Hotspot __cam_lidar_auto_" in command for command in commands)
    assert any("connection up Hotspot" in command for command in commands)
    assert any("connection delete __cam_lidar_auto_" in command for command in commands)
    assert not any("connection modify Hotspot" in command for command in commands)
    assert any(event.get("phase") == "CONFIGURING" for event in events)
    assert any(event.get("phase") == "RESTORING" for event in events)
    assert captured["stopped"] == cancel and captured["error"] is None
    if not cancel:
        assert captured["measurements"]["Band"] == "2.4 GHz"


def test_stop_during_listener_start_still_obtains_identity_and_cleans_it(monkeypatch):
    case = next(case for case in load_auto_catalog() if case.test_id == "TC-JET-24G-006")
    commands = []
    async def local(command, timeout):
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: JET-24", "", 0
        return "192.168.2.22 dev wlan0 src 192.168.2.30", "", 0
    async def run():
        task = None
        class Remote:
            async def run(self, command, timeout):
                commands.append(command)
                if "SERVER_STATE=" in command:
                    asyncio.get_running_loop().call_soon(task.cancel)
                    await asyncio.sleep(.01)
                    return result(command, "SERVER_STATE=STARTED_BY_APP\nSERVER_OWNED=YES\nSERVER_PID=123\nSERVER_START=456\nSERVER_DIR=/tmp/cam_lidar_iperf.aBc123\n")
                if command.startswith("ip route get"):
                    return result(command, "192.168.2.30 dev wlP1p1s0")
                return result(command)
        task = asyncio.create_task(collectors.collect_auto_case(Remote(), case, ready_setup()))
        return await asyncio.wait_for(task, 2)
    monkeypatch.setattr(collectors, "_local", local)
    capture = asyncio.run(run())
    assert capture["stopped"] and capture["error"] is None
    assert any("then kill 123" in command for command in commands)
    assert any("iperf_cleanup" in name for name in capture["evidence"])


def test_pending_live_checks_are_neutral_not_errors(page):
    auto = view(page)
    page.runtime.auto_setup = ready_setup()
    auto._start_case(auto.by_id["TC-JET-24G-006"])
    attempt = page.runtime.active
    page.runtime._collector_updated({"attempt": str(attempt.directory), "metrics": {"Forward receiver Mbps": 50}})
    assert not any(row["status"] == "ERROR" for row in attempt.criteria)
    statuses = [auto.criteria_table.item(row, 3).text() for row in range(auto.criteria_table.rowCount())]
    assert "RUNNING" in statuses and "ERROR" not in statuses
    assert "50 Mbps" in auto.criteria_table.item(0, 2).text()


def test_readiness_removes_obsolete_actions_without_overlapping_state(page):
    pretest = page.pretest
    assert pretest.table.columnCount() == 3
    page.runtime.auto_setup = ready_setup()
    page.runtime.changed.emit()
    assert all(pretest.table.cellWidget(row, col) is None
               for row in range(pretest.table.rowCount()) for col in range(pretest.table.columnCount()))
    assert pretest.table.item(pretest.rows["Dashboard Jetson Connection"], 2).text() == "READY"


def test_stopped_sta_connection_restores_previous_dut_profile():
    case = next(case for case in load_auto_catalog() if case.test_id == "TC-JET-STA-24G-001")
    commands = []
    class Remote:
        async def run(self, command, timeout):
            commands.append(command)
            if command.startswith("nmcli --wait 45 device wifi connect"):
                raise asyncio.CancelledError()
            return result(command, "Hotspot" if command.startswith("nmcli -g GENERAL.CONNECTION") else "")
    capture = asyncio.run(collectors.collect_auto_case(Remote(), case, ready_setup()))
    assert capture["stopped"] and capture["error"] is None
    assert any("connection up Hotspot" in command for command in commands)
    assert any("original_dut_restore" in name for name in capture["evidence"])


def test_reconfigure_detail_exposes_restore_note_and_contextual_authorization(page):
    page.runtime.auto_setup = replace(ready_setup(), current_band="5 GHz")
    auto = view(page)
    auto.show_detail("TC-JET-24G-001")
    assert auto.run_this.text() == "OPEN TEST CONSOLE"
    assert not auto.reconfigure_note.isHidden()
    assert "restore" in auto.reconfigure_note.text()


def test_missing_temporary_auth_profile_is_safe_when_absence_is_proven(monkeypatch):
    from tests.test_wifi_reference_behavior import phase_runner, setup
    commands, base = phase_runner()
    async def run(spec):
        response = await base(spec)
        if spec.label == "temporary_invalid_cleanup":
            response["exit_status"] = 10  # Already deleted after the negative attempt.
        return response
    metrics = asyncio.run(collectors.client_recovery_phase(setup(), run, negative=True))
    assert metrics["Temporary invalid profile removed"] and metrics["Client cleanup restored"]


def test_completion_refresh_uses_shared_discovery_and_preflight_rechecks_before_folder(tmp_path):
    from PySide6.QtCore import Signal
    class Service(SharedService):
        connected = Signal()
    service = Service()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    runtime.auto_setup = ready_setup()
    case = load_auto_catalog()[0]
    assert runtime.start("VD", case) is None
    assert runtime.discovery_pending and not list(tmp_path.rglob("attempt_*"))
    runtime._pending_auto_start = None
    runtime._discovery_request_id = None
    attempt = runtime.start("VD", case, _discovery_refreshed=True)
    assert attempt.directory.exists()
    service.operation_succeeded.emit("wifi-auto:2", {"measurements": passing_measurements(case, ready_setup())})
    assert attempt.status == "COMPLETED" and runtime.discovery_pending
    assert service.operations[-1][0] == "wifi-auto-discovery"


def test_active_banner_from_another_subgroup_restores_the_same_auto_view(page):
    owner = view(page)
    page.runtime.auto_setup = ready_setup()
    owner._start_case(owner.by_id["TC-JET-24G-001"])
    attempt = page.runtime.active
    other = page.vd.auto_pages["AP_5G"]
    page.vd.auto_tabs.setCurrentWidget(other)
    other.view_running.click()
    assert page.vd.auto_tabs.currentWidget() is owner
    assert owner.stack.currentWidget() is owner.execution_view
    assert page.runtime.active is attempt and owner.layers.count() == 2


def test_reconfiguration_authorization_is_contextual_and_can_be_resolved(page):
    page.runtime.auto_setup = replace(ready_setup(), current_band="5 GHz", allow_disruptive=False)
    auto = view(page)
    case = auto.by_id["TC-JET-24G-001"]
    page.pretest.set_context(case)
    assert not page.pretest.field_rows["allow_disruptive"].isHidden()
    page.pretest.auto_fields["allow_disruptive"].setCurrentText("READY")
    auto.show_detail(case.test_id)
    assert auto.run_this.text() == "OPEN TEST CONSOLE"
