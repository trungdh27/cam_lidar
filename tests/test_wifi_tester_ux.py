"""Tester-facing results, density, evidence, and source-aware RF semantics."""
from dataclasses import replace
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from desktop_app.ui.wifi_page import WifiPage, WifiMonitor, ERROR_COLOR
from desktop_app.wifi.auto_suite import AutoSuiteSetup, criteria_for_auto, evaluate_auto_case
from desktop_app.wifi.auto_collectors import _parse, command_plan
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.expectations import (CRITICAL, SUPPORTING, INFORMATIONAL, Exact, Band,
    ValidChannelForBand, FrequencyMatchesChannel, Minimum, Maximum, OneOf, ConfiguredValue, Informational)
from desktop_app.wifi.runtime import WifiAttempt
from tests.test_wifi_module import SharedService, app
from tests.test_wifi_auto_suite import ready_setup, passing_measurements


@pytest.fixture
def page(tmp_path):
    app()
    widget = WifiPage(SharedService(), evidence_root=tmp_path)
    widget.resize(1200, 900)
    widget.show()
    widget.tabs.setCurrentIndex(1)
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def generic_rf_case(band):
    case = next(c for c in load_auto_catalog() if c.test_id == f"TC-JET-{band}-001")
    return replace(case, expected="AP mode; runtime band; interface UP; valid IP; no regulatory/activation errors")


@pytest.mark.parametrize("band,channel,frequency", [("24G", 11, 2462), ("5G", 44, 5220), ("24G", 14, 2484), ("5G", 149, 5745)])
def test_generic_rf_uses_band_and_consistency_instead_of_defaults(band, channel, frequency):
    case = generic_rf_case(band)
    band_name = "2.4 GHz" if band == "24G" else "5 GHz"
    setup = replace(ready_setup(), current_band=band_name)
    values = passing_measurements(case, setup)
    values.update(Channel=channel, Frequency=frequency)
    rows, status, _ = evaluate_auto_case(case, setup, values)
    assert status == "PASS"
    by_metric = {row["metric"]: row for row in rows}
    assert by_metric["Band"]["importance"] == CRITICAL
    assert by_metric["Band"]["expected_rule"]["kind"] == "band"
    for metric in ("Channel", "Frequency"):
        assert by_metric[metric]["importance"] == SUPPORTING
        assert by_metric[metric]["status"] == "PASS"
    values["Band"] = "5 GHz" if band == "24G" else "2.4 GHz"
    assert evaluate_auto_case(case, setup, values)[1] == "FAIL"


@pytest.mark.parametrize("band,channel,frequency", [("24G", 11, 2462), ("5G", 48, 5240)])
def test_imported_example_rf_values_remain_flexible_for_generic_runtime_tc(band, channel, frequency):
    case = next(c for c in load_auto_catalog() if c.test_id == f"TC-JET-{band}-001")
    setup = replace(ready_setup(), current_band="2.4 GHz" if band == "24G" else "5 GHz")
    values = passing_measurements(case, setup)
    values.update(Channel=channel, Frequency=frequency)
    rows, status, _ = evaluate_auto_case(case, setup, values)
    assert status == "PASS"
    rf = {row["metric"]: row for row in rows}
    assert rf["Channel"]["importance"] == SUPPORTING
    assert rf["Frequency"]["importance"] == SUPPORTING


def test_explicit_project_rf_config_is_enforced_and_can_be_cleared(page):
    case = generic_rf_case("24G")
    unlocked = replace(ready_setup(), channel_24g=11, frequency_24g_mhz=2462)
    unlocked_values = passing_measurements(case, unlocked)
    unlocked_values.update(Channel=6, Frequency=2437)
    assert evaluate_auto_case(case, unlocked, unlocked_values)[1] == "PASS"
    setup = replace(ready_setup(), channel_24g=11, frequency_24g_mhz=2462, rf_project_lock_24g=True)
    values = passing_measurements(case, setup)
    assert evaluate_auto_case(case, setup, values)[1] == "PASS"
    values["Channel"] = 6
    assert evaluate_auto_case(case, setup, values)[1] == "FAIL"
    page.runtime.auto_setup = setup
    page.pretest.auto_fields["channel_24g"].clear()
    page.pretest.auto_fields["frequency_24g_mhz"].clear()
    values = page.pretest._auto_values()
    assert values["channel_24g"] is None and values["frequency_24g_mhz"] is None
    assert AutoSuiteSetup().channel_24g is None and AutoSuiteSetup().channel_5g is None


def test_supporting_rf_problem_is_visible_without_becoming_a_hard_failure():
    case, setup = generic_rf_case("24G"), ready_setup()
    values = passing_measurements(case, setup)
    values.update(Channel=11, Frequency=2437)
    rows, status, _ = evaluate_auto_case(case, setup, values)
    assert status == "PASS"
    assert next(row for row in rows if row["metric"] == "Frequency")["status"] == "FAIL"


def test_regulatory_channel_list_comes_from_phy_evidence():
    case = generic_rf_case("5G")
    setup = replace(ready_setup(), current_band="5 GHz")
    outputs = {"jetson_phy": {"stdout": " * 5180 MHz [36] (20.0 dBm)\n * 5220 MHz [44] (disabled)\n * 5260 MHz [52] (no IR)\n", "exit_status": 0}}
    metrics = _parse(case, setup, outputs)
    assert metrics["Allowed channels"] == [36]
    rule = ValidChannelForBand("5 GHz")
    assert rule.matches(36, metrics) and not rule.matches(44, metrics)
    assert not rule.matches(200)


def test_expected_value_types_have_distinct_semantics():
    assert Exact("AP").matches("AP")
    assert Band("2.4 GHz").matches("2.4 GHz")
    assert Minimum(5, "Mbps").matches(5)
    assert Maximum(100, "ms").matches(99)
    assert OneOf(["WPA2", "WPA3"]).matches("WPA3")
    assert ConfiguredValue("production_ssid").matches("Robot", config={"production_ssid": "Robot"})
    assert Informational().matches(-99)
    assert FrequencyMatchesChannel().matches(2462, {"Channel": 11, "Band": "2.4 GHz"})
    assert not FrequencyMatchesChannel().matches(2437, {"Channel": 11, "Band": "2.4 GHz"})


@pytest.mark.parametrize("token,expected", [("VHT-MCS 9", "Wi-Fi 5 / 802.11ac"),
                                              ("HE-MCS 7", "Wi-Fi 6 / 802.11ax")])
def test_tc002_accepts_wifi5_or_wifi6_runtime(token, expected):
    case = next(c for c in load_auto_catalog() if c.test_id == "TC-JET-5G-002")
    setup = replace(ready_setup(), current_band="5 GHz", current_ssid="RD3.02")
    phy = "Wiphy phy0\nVHT Capabilities" + ("\nHE PHY Capabilities" if token.startswith("HE") else "")
    outputs = {
        "jetson_phy": {"stdout": phy, "exit_status": 0},
        "jetson_station": {"stdout": f"Station aa:bb:cc:dd:ee:ff\n tx bitrate: 433.3 MBit/s {token}", "exit_status": 0},
    }
    metrics = _parse(case, setup, outputs)
    assert metrics["Runtime PHY standard"] == expected
    rows, status, _ = evaluate_auto_case(case, setup, metrics)
    assert status == "PASS"
    assert {row["status"] for row in rows if row["importance"] == CRITICAL} == {"PASS"}


def test_tc002_rejects_runtime_without_vht_or_he_evidence():
    case = next(c for c in load_auto_catalog() if c.test_id == "TC-JET-5G-002")
    setup = replace(ready_setup(), current_band="5 GHz", current_ssid="RD3.02")
    metrics = _parse(case, setup, {
        "jetson_phy": {"stdout": "Wiphy phy0\nVHT Capabilities", "exit_status": 0},
        "jetson_station": {"stdout": "Station aa:bb:cc:dd:ee:ff\n tx bitrate: 54.0 MBit/s", "exit_status": 0},
    })
    rows, status, _ = evaluate_auto_case(case, setup, metrics)
    assert status == "FAIL"
    assert next(row for row in rows if row["metric"] == "Runtime PHY standard")["status"] == "FAIL"


def tc003_setup(**changes):
    return replace(ready_setup(), current_band="5 GHz", current_ssid="RD3.02",
                   test_ssid_5g="", jetson_ap_ip="10.42.0.1",
                   client_wifi_interface="wlx58044f6c4e0e", laptop_wifi_ip="10.42.0.25",
                   full_wifi_qual_path=False, laptop_to_dut_wifi=False,
                   dut_to_laptop_wifi=False, **changes)


def tc003_outputs(route_dev="wlx58044f6c4e0e", ping_code=0, address="10.42.0.25/24"):
    return {
        "jetson_iw_info": {"stdout": "Interface wlP1p1s0\n type AP\n ssid RD3.02\n channel 48 (5240 MHz)", "exit_status": 0},
        "client_iw_link": {"stdout": "Connected to aa:bb:cc:dd:ee:ff\n\tfreq: 5240\n\tSSID: RD3.02", "exit_status": 0},
        "client_ipv4": {"stdout": f"3: wlx: <UP>\n    inet {address} scope global wlx" if address else "3: wlx: <UP>", "exit_status": 0},
        "client_route": {"stdout": f"10.42.0.1 dev {route_dev} src 10.42.0.25", "exit_status": 0},
        "client_ping": {"stdout": "1 packets transmitted, 1 received" if ping_code == 0 else "1 packets transmitted, 0 received", "exit_status": ping_code},
    }


def test_tc003_uses_runtime_target_and_independent_route_criteria():
    case = next(c for c in load_auto_catalog() if c.test_id == "TC-JET-5G-003")
    setup = tc003_setup()
    plan = command_plan(case, setup)
    assert next(spec.command for spec in plan if spec.label == "client_ipv4") == "ip -4 addr show dev wlx58044f6c4e0e"
    assert next(spec.command for spec in plan if spec.label == "client_route") == "ip route get 10.42.0.1"
    assert "ping -I wlx58044f6c4e0e" in next(spec.command for spec in plan if spec.label == "client_ping")
    metrics = _parse(case, setup, tc003_outputs())
    rows, status, _ = evaluate_auto_case(case, setup, metrics)
    by_name = {row["name"]: row for row in rows}
    assert by_name["Association"]["expected"].endswith("RD3.02")
    assert by_name["Association"]["status"] == "PASS"
    assert by_name["Client IPv4"]["actual"] == "10.42.0.25/24"
    assert by_name["Wi-Fi route"]["status"] == "PASS"
    assert metrics["Full Wi-Fi qualification path"] is True and status == "PASS"


def test_tc003_negative_conditions_fail_only_their_criteria():
    case = next(c for c in load_auto_catalog() if c.test_id == "TC-JET-5G-003")
    setup = tc003_setup()
    metrics = _parse(case, setup, tc003_outputs(route_dev="eno1", ping_code=1))
    rows, status, reason = evaluate_auto_case(case, setup, metrics)
    by_name = {row["name"]: row for row in rows}
    assert status == "FAIL" and "Route interface" not in reason
    assert by_name["Association"]["status"] == "PASS"
    assert by_name["Band"]["status"] == "PASS"
    assert by_name["Client IPv4"]["status"] == "PASS"
    assert by_name["Wi-Fi route"]["status"] == "FAIL"
    assert by_name["Reachability"]["status"] == "FAIL"
    assert metrics["Full Wi-Fi qualification path"] is False
    no_ip = _parse(case, setup, tc003_outputs(address=""))
    no_ip_rows, no_ip_status, _ = evaluate_auto_case(case, setup, no_ip)
    assert no_ip_status == "FAIL"
    assert next(row for row in no_ip_rows if row["name"] == "Client IPv4")["status"] == "FAIL"


def test_auto_table_id_name_and_checkbox_have_distinct_navigation(page):
    auto = page.vd.auto_pages["AP_24G"]
    page.vd.suite_tabs.setCurrentIndex(1)
    page.runtime.auto_setup = ready_setup()
    page.runtime.changed.emit()
    row = next(row for row in range(auto.table.rowCount())
               if auto.table.item(row, 1).text() == "TC-JET-24G-001")
    auto.table.cellClicked.emit(row, 1)
    assert auto.stack.currentWidget() is auto.list_view
    assert page.runtime.active is None and not list(page.runtime.evidence_root.rglob("attempt_*"))
    auto.table.cellClicked.emit(row, 3)
    assert auto.stack.currentWidget() is auto.list_view
    auto.table.cellDoubleClicked.emit(row, 1)
    assert auto.stack.currentWidget() is auto.execution_view
    assert auto.execution_status.text() == "READY"
    auto.back_to_list()
    auto.table.cellDoubleClicked.emit(row, 3)
    assert auto.stack.currentWidget() is auto.execution_view
    assert auto._console_case.test_id == "TC-JET-24G-001"
    auto.back_to_list()
    auto.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    auto.table.cellClicked.emit(row, 0)
    assert auto.stack.currentWidget() is auto.list_view
    assert "TC-JET-24G-001" in auto.selected


def test_checkbox_header_and_block_reason_are_tester_friendly(page):
    views = [*page.environments.values(), *page.vd.auto_pages.values()]
    for view in views:
        assert view.table.horizontalHeaderItem(0).text() == ""
        assert view.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable if view.table.rowCount() else True
    view = page.vd.auto_pages["AP_24G"]
    assert view.table.item(0, 5).text() == "SSH unavailable"
    assert view.table.item(0, 5).foreground().color().name().upper() == ERROR_COLOR
    view.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert view.table.item(0, 1).text() in view.selected


def test_dependencies_default_to_relevant_and_expand_in_every_context(page):
    view = page.vd.auto_pages["AP_24G"]
    view.show_detail("TC-JET-24G-001")
    view.table.setCurrentCell(0, 2)
    view._start_case(view.by_id["TC-JET-24G-001"])
    for dependency_view in (view.detail_dependency_view, view.selection_dependency_view):
        table = dependency_view.table
        states = [table.item(r, 3).text() for r in range(table.rowCount())]
        assert "NOT REQUIRED" not in states and "BLOCKING" in states
        assert table.height() <= 260 and table.minimumHeight() <= 260
        assert all(table.item(r, 1).text() not in {"Yes", "No"} for r in range(table.rowCount()))
        assert table.item(states.index("BLOCKING"), 3).foreground().color().name().upper() == ERROR_COLOR
        previous = table.rowCount()
        dependency_view.show_all.setChecked(True)
        assert table.rowCount() > previous
        assert "NOT REQUIRED" in [table.item(r, 3).text() for r in range(table.rowCount())]


def test_blocked_result_has_visible_reason_and_compact_panels(page):
    view = page.vd.auto_pages["AP_24G"]
    page.vd.suite_tabs.setCurrentIndex(1)
    view._start_case(view.by_id["TC-JET-24G-001"])
    QApplication.processEvents()
    assert page.runtime.active is None
    assert view.stack.currentWidget() is view.execution_view
    assert view.execution_status.text() == "ERROR"
    assert view.console_setup.title.property("tone") == "error"
    assert "PREFLIGHT ERROR" in view.console_setup.title.text()
    assert "No manual value entry" in view.console_setup.note.text()
    assert view.console_setup.dependencies.isHidden()
    assert not list(page.runtime.evidence_root.rglob("attempt_*"))
    view._setup_action("setup", "shared_ssh_ready", view.current_case)
    assert page.tabs.currentIndex() == 0


def test_empty_cards_hide_entire_monitor(tmp_path):
    app()
    monitor = WifiMonitor()
    case = load_auto_catalog()[0]
    attempt = WifiAttempt("VD", case, 1, tmp_path)
    monitor.render(attempt)
    assert not monitor.cards and monitor.isHidden()
    attempt.metrics.update(Interface="wlP1p1s0", Mode="AP", Band="2.4 GHz", SSID="Robot")
    monitor.render(attempt)
    assert len(monitor.cards) == 4
    assert monitor.sizeHint().height() < 160
    assert all(card.value.text() != "—" for card in monitor.cards.values())
    monitor.close()


def test_auto_result_measurements_log_modes_search_and_evidence(page):
    view = page.vd.auto_pages["AP_24G"]
    page.vd.suite_tabs.setCurrentIndex(1)
    page.runtime.auto_setup = ready_setup()
    case = view.by_id["TC-JET-24G-001"]
    view._start_case(case)
    raw = "[JETSON] $ iw dev wlP1p1s0 info\nInterface wlP1p1s0\n channel 6 (2437 MHz)\n[exit 0]\n"
    page.runtime.append_raw(raw)
    values = passing_measurements(case, ready_setup())
    values.update(Channel=6, Frequency=2437)
    values["Interface"] = "wlP1p1s0"
    page.runtime.complete_auto(values, evidence={"snapshot.log": {"status": "CAPTURED", "command": "iw dev wlP1p1s0 info"}})
    assert "COMPLETED" in view.execution_status.text()
    assert view.auto_result_label.text() == "Result: PASS"
    assert view.criteria_table.columnCount() == 4
    assert [view.layers.tabText(i) for i in range(view.layers.count())] == ["MEASUREMENTS", "TECHNICAL LOG"]
    view.layers.setCurrentIndex(0)
    metrics = {view.measurements_view.item(r, 0).text(): view.measurements_view.item(r, 1).text() for r in range(view.measurements_view.rowCount())}
    assert metrics["Channel"] == "6" and metrics["Band"] == "2.4 GHz"
    assert "AUTO RESULT" not in metrics and "Passed" not in metrics
    view.layers.setCurrentIndex(1)
    assert view.log_modes.currentIndex() == 0
    assert view.log_modes.tabText(0) == "EVENTS" and view.log_modes.tabText(1) == "FULL OUTPUT"
    assert "Shared SSH connected through Ethernet" in view.events_view.toPlainText()
    assert "Detected interface wlP1p1s0" in view.events_view.toPlainText()
    assert "Test PASS" in view.events_view.toPlainText()
    assert view.raw_input is None and view.import_output is None and view.import_widget is None
    assert view.raw_view.toPlainText() == raw
    view.find_log.setText("Test PASS")
    view._find_log(False)
    assert view.events_view.textCursor().selectedText() == "Test PASS"
    view.log_modes.setCurrentIndex(1)
    view.find_log.setText("channel 6")
    view._find_log(False)
    assert view.raw_view.textCursor().selectedText() == "channel 6"
    assert not view.completed_folder.isHidden() and view.stop_test.isHidden()
    saved = json.loads((page.runtime.active.directory / "result.json").read_text())
    assert saved["events"] == page.runtime.active.events
    assert (page.runtime.active.directory / "raw/commands.log").read_text() == raw


def test_error_result_and_event_use_red_emphasis(page):
    view = page.vd.auto_pages["AP_24G"]
    page.runtime.auto_setup = ready_setup()
    view._start_case(view.by_id["TC-JET-24G-001"])
    page.runtime.complete_auto({}, "Shared SSH command failed")
    assert view.execution_status.text() == "ERROR"
    assert view.execution_status.property("tone") == "error"
    assert view.result_reason_label.property("tone") == "error"
    assert "Shared SSH command failed" in view.events_view.toPlainText()
    cursor = view.events_view.document().find("Test ERROR")
    assert cursor.charFormat().foreground().color().name().upper() == ERROR_COLOR


def test_source_limits_do_not_turn_diagnostics_into_acceptance_thresholds():
    setup = replace(ready_setup(), current_band="5 GHz", distance_10m_confirmed=False)
    cases = {c.test_id: c for c in load_auto_catalog()}
    latency = cases["TC-JET-5G-005"]
    values = passing_measurements(latency, setup)
    values["RTT max"] = 500
    rows, status, _ = evaluate_auto_case(latency, setup, values)
    assert status == "PASS"
    assert next(r for r in rows if r["metric"] == "RTT max")["importance"] == INFORMATIONAL
    values["RTT avg"] = 101
    assert evaluate_auto_case(latency, setup, values)[1] == "FAIL"
    sta = cases["TC-JET-STA-5G-006"]
    values = passing_measurements(sta, setup)
    values.update({"Forward receiver Mbps": 2, "Reverse receiver Mbps": 3})
    assert evaluate_auto_case(sta, setup, values)[1] == "PASS"
    assert evaluate_auto_case(sta, replace(setup, distance_10m_confirmed=True), values)[1] == "FAIL"
    endurance = cases["TC-JET-STA-5G-008"]
    spec = next(s for s in criteria_for_auto(endurance, setup) if s.metric == "Elapsed seconds")
    assert spec.target == 1800
    rf = cases["TC-JET-5G-004"]
    values = passing_measurements(rf, setup)
    values["RSSI"] = -99
    assert evaluate_auto_case(rf, setup, values)[1] == "PASS"
    del values["RSSI"]
    assert evaluate_auto_case(rf, setup, values)[1] == "ERROR"  # Source still requires measurement capture.


def test_audit_covers_every_case_with_individual_requirements():
    from scripts.wifi_criteria_audit import audit_cases
    rows = audit_cases()
    assert len(rows) == 68 and len({row["test_id"] for row in rows}) == 68
    assert sum(row["suite"] == "AUTO" for row in rows) == 42
    assert all(row["critical"] and row["hard_dependencies"] for row in rows)
    assert all(check["name"] != "Source acceptance requirement" for row in rows for check in row["critical"])
    assert next(row for row in rows if row["test_id"] == "TC-JET-24G-001")["critical"] != next(row for row in rows if row["test_id"] == "TC-JET-24G-005")["critical"]


def test_inline_manual_input_survives_clock_refresh_and_import_is_retained(page):
    from PySide6.QtWidgets import QLineEdit
    view = page.environments["VD"]
    view._start_case(view.by_id["TC-WIFI-C04"])
    field = view.criteria_table.cellWidget(2, 2)
    assert isinstance(field, QLineEdit)
    field.setText("Tester measurement")
    view._render_execution()
    assert view.criteria_table.cellWidget(2, 2) is field
    assert field.text() == "Tester measurement"
    assert not view.import_widget.isHidden()
    assert not view.import_output.isHidden()


def test_failed_criterion_actual_and_result_are_red(page):
    view = page.vd.auto_pages["AP_24G"]
    page.runtime.auto_setup = ready_setup()
    case = view.by_id["TC-JET-24G-001"]
    view._start_case(case)
    values = passing_measurements(case, ready_setup())
    values["Mode"] = "managed"
    page.runtime.complete_auto(values)
    assert view.auto_result_label.text() == "Result: FAIL" and view.auto_result_label.property("tone") == "error"
    row = next(r for r in range(view.criteria_table.rowCount()) if view.criteria_table.item(r, 3).text() == "FAIL")
    for col in (2, 3):
        assert view.criteria_table.item(row, col).foreground().color().name().upper() == ERROR_COLOR
    assert "AP mode" in view.result_reason_label.text() and "managed" in view.result_reason_label.text()


def test_no_expected_ssid_records_actual_without_inventing_failure():
    case = generic_rf_case("24G")
    setup = replace(ready_setup(), test_ssid_24g="")
    values = passing_measurements(case, setup)
    values["SSID"] = "Observed SSID"
    rows, status, _ = evaluate_auto_case(case, setup, values)
    assert status == "PASS"
    ssid = next(r for r in rows if r["metric"] == "SSID")
    assert ssid["importance"] == INFORMATIONAL and ssid["status"] == "MEASURED"


def test_empty_history_does_not_show_large_tables_or_output(page):
    view = page.vd.auto_pages["AP_24G"]
    view.show_history()
    assert view.history_table.isHidden() and view.history_details.isHidden()
    assert view.history_empty.text() == "No saved attempts for this suite."


@pytest.mark.parametrize("text", ["Example: channel 6; frequency 2437 MHz", "Default channel 6; frequency 2437 MHz", "channel <EXAMPLE_CHANNEL>; frequency <EXAMPLE_FREQUENCY>"])
def test_rf_examples_and_placeholders_are_not_acceptance_bounds(text):
    case = replace(generic_rf_case("24G"), expected="AP; band 2.4 GHz; " + text)
    setup = ready_setup()
    values = passing_measurements(case, setup)
    values.update(Channel=11, Frequency=2462)
    rows, status, _ = evaluate_auto_case(case, setup, values)
    assert status == "PASS"
    assert all(row["importance"] == SUPPORTING for row in rows if row["metric"] in {"Channel", "Frequency"})


def test_explicit_project_locked_rf_bounds_are_preserved():
    case = generic_rf_case("24G")
    setup = replace(ready_setup(), channel_24g=11, frequency_24g_mhz=2462, rf_project_lock_24g=True)
    values = passing_measurements(case, setup)
    assert values["Channel"] == 11 and values["Frequency"] == 2462
    assert evaluate_auto_case(case, setup, values)[1] == "PASS"
    values["Channel"] = 6
    assert evaluate_auto_case(case, setup, values)[1] == "FAIL"


def test_physical_geometry_is_visible_as_critical_acceptance():
    case = next(c for c in load_auto_catalog() if c.test_id == "TC-JET-24G-009")
    setup = ready_setup()
    rows, status, _ = evaluate_auto_case(case, setup, passing_measurements(case, setup))
    criterion = next(row for row in rows if row["metric"] == "Distance confirmation")
    assert criterion["importance"] == CRITICAL and criterion["expected"] == "10 m confirmed"
    assert criterion["actual"] is True and status == "PASS"
    assert evaluate_auto_case(case, replace(setup, distance_10m_confirmed=False), {})[1] == "BLOCKED"


def test_boolean_measurements_are_readable_for_testers():
    from desktop_app.ui.wifi_page import metric_text
    assert metric_text("SSH reachable", True) == "Yes"
    assert metric_text("Invalid credential rejected", False) == "No"
    assert metric_text("Disconnects", 0) == "0"


def test_saved_result_counts_match_critical_overview(page):
    case = generic_rf_case("24G")
    setup = ready_setup()
    page.runtime.auto_setup = setup
    view = page.vd.auto_pages["AP_24G"]
    view._start_case(case)
    values = passing_measurements(case, setup)
    values.update(Channel=11, Frequency=2437)
    page.runtime.complete_auto(values)
    saved = json.loads((page.runtime.active.directory / "result.json").read_text())
    assert saved["status"] == "PASS"
    assert saved["criteria_summary"]["fail"] == 0
    assert saved["criteria_summary"]["pass"] == 6
    assert saved["criteria_summary"]["supporting_warnings"] == 1
    assert "Failed  0" in view.result_counts.text()


def test_guided_automatic_failure_is_visible_before_manual_completion(page):
    view = page.environments["VD"]
    view._start_case(view.by_id["TC-WIFI-C04"])
    page.runtime.add_metrics({"Hotspot profile": "Present", "Mode": "infrastructure"})
    assert page.runtime.active.status == "RUNNING"
    assert any(row["status"] == "FAIL" for row in page.runtime.active.criteria)
    assert view.auto_result_label.text() == "Result: FAIL"
    assert view.result_reason_label.property("tone") == "error"
    assert "AP profile mode" in view.result_reason_label.text()


def test_historical_manual_criteria_have_no_review_controls(page):
    view = page.environments["VD"]
    view._start_case(view.by_id["TC-WIFI-C04"])
    from tests.test_wifi_module import result
    page.runtime.message.disconnect(view._message)
    page.runtime.services["Jetson"].operation_succeeded.emit("wifi-inspect:1", [result(
        "nmcli connection show Hotspot", "802-11-wireless.mode: ap\n802-11-wireless.ssid: Robot\n")])
    page.runtime.finish("NEEDS REVIEW")
    assert view.criteria_table.columnCount() == 4
    assert view.criteria_table.cellWidget(2, 2) is None
    view._open_attempt(page.runtime.active.directory / "result.json")
    assert view.criteria_table.columnCount() == 4
    assert view.criteria_table.cellWidget(2, 2) is None
    assert view.import_widget.isHidden()
