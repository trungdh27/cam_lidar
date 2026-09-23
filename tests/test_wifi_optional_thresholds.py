"""Source acceptance limits are distinct from AUTO execution prerequisites."""
import asyncio
import json
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from desktop_app.ui.wifi_page import WifiPage
from desktop_app.wifi.auto_collectors import collect_auto_case, _udp_stats
from desktop_app.wifi.auto_suite import (INFORMATIONAL, PROJECT_OPTIONAL, SOURCE_REQUIRED,
                                       criteria_for_auto, dependencies_for, evaluate_auto_case,
                                       missing_prerequisites, threshold_configs_for)
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.runtime import WifiRuntime
from tests.test_wifi_auto_suite import passing_measurements, ready_setup
from tests.test_wifi_module import SharedService, app, result


CASES = {case.test_id: case for case in load_auto_catalog()}
UDP = CASES["TC-JET-5G-007"]
UDP_FIELDS = ("udp_loss_threshold_percent", "udp_jitter_threshold_ms", "udp_min_receiver_mbps")


def wifi_setup():
    return replace(ready_setup(), ssh_transport="Wi-Fi", control_interface="wlP1p1s0",
                   backup_ethernet_ready=False, backup_ethernet_interface="",
                   current_band="5 GHz", current_ssid="JET-5", current_channel=36,
                   current_frequency_mhz=5180, packet_loss_threshold_percent=None,
                   udp_loss_threshold_percent=None, udp_jitter_threshold_ms=None,
                   udp_min_receiver_mbps=None)


def test_udp_without_project_thresholds_is_ready_and_requires_qualitative_review(tmp_path):
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = wifi_setup()
    assert runtime.latest_status("VD", UDP.test_id) == "READY"
    assert not missing_prerequisites(UDP, runtime.auto_setup)
    configs = threshold_configs_for(UDP)
    assert all(configs[key].classification == PROJECT_OPTIONAL for key in UDP_FIELDS)
    assert configs["packet_loss_threshold_percent"].classification == INFORMATIONAL
    for dep in runtime.dependency_report(UDP):
        if dep.key in UDP_FIELDS:
            assert not dep.required and not dep.satisfied and dep.state == "NOT REQUIRED"
    measurements = passing_measurements(UDP, runtime.auto_setup)
    rows, status, reason = evaluate_auto_case(UDP, runtime.auto_setup, measurements)
    assert status == "NEEDS REVIEW" and "qualitative" in reason
    assert all(row["status"] == "MEASURED" for row in rows if row["qualitative"])
    assert all(row["status"] == "PASS" for row in rows if row["operator"] != "measured")
    assert not any(row["status"] == "BLOCKED" for row in rows)
    attempt = runtime.start("VD", UDP)
    assert attempt.status == "PREPARING"
    runtime.complete_auto(measurements)
    saved = json.loads((attempt.directory / "result.json").read_text())
    assert saved["final_result"] == "NEEDS REVIEW"
    assert saved["criteria_summary"]["needs_review"] == 3
    assert saved["criteria_summary"]["measured"] == 12


@pytest.mark.parametrize("field, metric, limit, within, exceeds", [
    ("udp_loss_threshold_percent", "UDP loss", 1, 0.5, 2),
    ("udp_loss_threshold_percent", "UDP loss", 0, 0, 0.1),
    ("udp_jitter_threshold_ms", "UDP jitter", 20, 10, 30),
    ("udp_min_receiver_mbps", "UDP 5 receiver Mbps", 4.5, 4.8, 4.0),
])
def test_each_optional_project_limit_enables_pass_fail(field, metric, limit, within, exceeds):
    setup = replace(wifi_setup(), **{field: limit})
    measurements = passing_measurements(UDP, setup)
    measurements[metric] = within
    rows, status, _ = evaluate_auto_case(UDP, setup, measurements)
    criterion = next(row for row in rows if row["metric"] == metric)
    assert criterion["status"] == "PASS" and criterion["configuration_type"] == PROJECT_OPTIONAL
    assert status == "NEEDS REVIEW"  # The other two source limits remain qualitative.
    measurements[metric] = exceeds
    rows, status, _ = evaluate_auto_case(UDP, setup, measurements)
    assert next(row for row in rows if row["metric"] == metric)["status"] == "FAIL"
    assert status == "FAIL"


def test_all_configured_udp_limits_resolve_review_without_scoring_informational_data():
    setup = replace(wifi_setup(), udp_loss_threshold_percent=1, udp_jitter_threshold_ms=20,
                    udp_min_receiver_mbps=4.5)
    rows, status, _ = evaluate_auto_case(UDP, setup, passing_measurements(UDP, setup))
    assert status == "PASS"
    assert all(row["status"] == "MEASURED" for row in rows if row["configuration_type"] == INFORMATIONAL)


def test_udp_gates_only_execution_path_runtime_and_iperf_dependencies():
    setup = replace(wifi_setup(), nmcli_available=False, network_manager_ready=False,
                    iw_available=False, client_association_ready=False)
    assert not missing_prerequisites(UDP, setup)
    assert "current band is not 5 GHz" in missing_prerequisites(UDP, replace(setup, current_band="2.4 GHz"))
    assert "valid network path is not ready" not in missing_prerequisites(UDP, replace(setup, network_path_ready=False))


def test_source_numeric_limits_are_used_directly_and_cannot_be_relaxed_by_project_limits():
    case = replace(UDP, expected="UDP loss ≤1%; UDP jitter ≤20 ms; UDP 5 Mbps receiver ≥4.5 Mbps; link stays connected")
    setup = wifi_setup()
    assert all(threshold_configs_for(case)[key].classification == SOURCE_REQUIRED for key in UDP_FIELDS)
    assert not missing_prerequisites(case, setup)  # Numeric limits are present in source.
    threshold_deps = [dep for dep in dependencies_for(case, setup) if dep.key in UDP_FIELDS]
    assert all(dep.required and dep.satisfied and dep.source == "SOURCE" for dep in threshold_deps)
    rows, status, _ = evaluate_auto_case(case, setup, passing_measurements(case, setup))
    assert status == "PASS"
    assert next(row for row in rows if row["metric"] == "UDP loss")["target"] == 1
    relaxed = replace(setup, udp_loss_threshold_percent=50)
    values = passing_measurements(case, relaxed)
    values["UDP loss"] = 2
    rows, status, _ = evaluate_auto_case(case, relaxed, values)
    assert status == "FAIL"
    assert next(row for row in rows if row["name"] == "UDP loss")["status"] == "FAIL"
    assert next(row for row in rows if row["name"] == "Project UDP loss")["status"] == "PASS"


def test_source_rtt_limit_is_100_ms_with_packet_loss_policy_blank(tmp_path):
    case = CASES["TC-JET-5G-005"]
    setup = wifi_setup()
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = setup
    assert runtime.latest_status("VD", case.test_id) == "READY"
    criterion = next(spec for spec in criteria_for_auto(case, setup) if spec.metric == "RTT avg")
    assert criterion.target == 100 and criterion.operator == "le"
    values = passing_measurements(case, setup)
    values["RTT avg"] = 99
    rows, status, _ = evaluate_auto_case(case, setup, values)
    assert next(row for row in rows if row["metric"] == "RTT avg")["status"] == "PASS"
    assert next(row for row in rows if row["metric"] == "Packet loss")["status"] == "MEASURED"
    assert status == "NEEDS REVIEW"
    values["RTT avg"] = 101
    assert evaluate_auto_case(case, setup, values)[1] == "FAIL"


def test_ap_5g_optional_configuration_does_not_globally_gate_execution(tmp_path):
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = replace(wifi_setup(), recorded_distance_1m="", recorded_distance_10m="",
                                 wifi_credential="", security_requirement="")
    for number in (*range(1, 11), 13):
        assert runtime.latest_status("VD", f"TC-JET-5G-{number:03d}") == "READY"
    assert any("Active control session uses the same Wi-Fi interface" in reason for reason in runtime.block_reasons(CASES["TC-JET-5G-011"]))
    runtime.auto_setup.distance_1m_confirmed = False
    assert runtime.latest_status("VD", "TC-JET-5G-008") == "BLOCKED"
    assert runtime.latest_status("VD", "TC-JET-5G-009") == "READY"
    runtime.auto_setup.distance_10m_confirmed = False
    assert runtime.latest_status("VD", "TC-JET-5G-009") == "BLOCKED"
    runtime.auto_setup.local_iperf3_available = False
    assert runtime.block_reasons(UDP) == ["iperf3 local is not ready"]


def test_optional_thresholds_are_visible_in_tooltip_and_selection_without_block_count(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    page.runtime.auto_setup = wifi_setup()
    page.runtime.changed.emit()
    ap = page.vd.auto_pages["AP_5G"]
    row = next(row for row in range(ap.table.rowCount()) if ap.table.item(row, 1).text() == UDP.test_id)
    assert ap.table.item(row, 4).text() == "READY" and ap.table.item(row, 5).text() == ""
    tooltip = ap.table.item(row, 5).toolTip()
    assert "Blocking dependencies:\nNone" in tooltip and "Optional missing configuration" in tooltip
    assert all(text in tooltip for text in ("Max UDP loss", "Max UDP jitter", "Min UDP receiver"))
    ap.table.setCurrentCell(row, 2)
    assert ap.selection_summary.isHidden()
    ap.show_detail(UDP.test_id)
    assert ap.detail_setup.dependencies.isHidden()
    assert all(ap.selection_dependencies.item(r, 3).text() != "NOT REQUIRED" for r in range(ap.selection_dependencies.rowCount()))
    ap.selection_dependency_view.show_all.setChecked(True)
    optional_rows = [row for row in range(ap.selection_dependencies.rowCount())
                     if ap.selection_dependencies.item(row, 1).text() == "Optional project limit"]
    assert len(optional_rows) == 3
    assert all(ap.selection_dependencies.item(row, 3).text() == "NOT REQUIRED" for row in optional_rows)
    page.runtime.update_auto_setup(persist=False, udp_loss_threshold_percent=0)
    tooltip = ap.table.item(row, 5).toolTip()
    assert "Max UDP loss" not in tooltip and "Max UDP jitter" in tooltip
    page.close()


def udp_report(rate, receiver, lost=0, packets=1000, jitter=0.3):
    return json.dumps({"end": {
        "sum_sent": {"bits_per_second": rate * 1e6, "sender": True},
        "sum_received": {"bits_per_second": receiver * 1e6, "sender": False,
                         "lost_packets": lost, "packets": packets,
                         "lost_percent": 100 * lost / packets, "jitter_ms": jitter},
    }})


@pytest.mark.parametrize("post_station, post_log, expected", [
    ("Station aa:bb:cc:dd:ee:ff", "", "NEEDS REVIEW"),
    ("", "", "FAIL"),
    ("Station aa:bb:cc:dd:ee:ff", "wlP1p1s0: disconnected", "NEEDS REVIEW"),
    ("Station aa:bb:cc:dd:ee:ff", "device (wlP1p1s0): state change: activated -> disconnected", "FAIL"),
    ("Station aa:bb:cc:dd:ee:ff", "wlP1p1s0: Network is unreachable", "NEEDS REVIEW"),
    ("Station aa:bb:cc:dd:ee:ff", "wlP1p1s0: activation failed", "NEEDS REVIEW"),
])
def test_udp_collector_collects_both_rates_and_checks_actual_link_and_new_events(
        monkeypatch, post_station, post_log, expected):
    import desktop_app.wifi.auto_collectors as collectors
    commands = []

    async def local(command, timeout):
        commands.append(command)
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: JET-5\n", "", 0
        if "-b 5M" in command:
            return udp_report(5, 4.8, lost=10), "", 0
        if "-b 10M" in command:
            return udp_report(10, 9.7, lost=20), "", 0
        return "192.168.2.22 dev wlan0 src 192.168.2.104", "", 0

    class SharedSSH:
        async def run(self, command, timeout):
            commands.append(command)
            if "--show-cursor" in command:
                return result(command, "-- cursor: s=abc;i=123\n")
            if "--after-cursor=" in command:
                assert "s=abc;i=123" in command
                if "-o json" in command:
                    from datetime import datetime, timezone
                    return result(command, json.dumps({"MESSAGE": post_log, "__REALTIME_TIMESTAMP": str(int(datetime.now(timezone.utc).timestamp() * 1e6))}) if post_log else "")
                return result(command, post_log)
            if "GENERAL.STATE" in command:
                return result(command, "100 (connected)")
            if command.startswith("journalctl"):
                return result(command, "historic wlP1p1s0: disconnected\nhistoric wlP1p1s0: failed")
            if command.endswith(" info"):
                return result(command, "Interface wlP1p1s0\n type AP\n ssid JET-5\n channel 36 (5180 MHz)")
            if command.endswith(" station dump"):
                return result(command, post_station)
            if command.startswith("ip route get"):
                return result(command, "192.168.2.104 dev wlP1p1s0 src 192.168.2.22")
            if "SERVER_STATE=" in command:
                return result(command, "SERVER_STATE=ALREADY_LISTENING\nSERVER_OWNED=NO\n")
            return result(command, "")

    monkeypatch.setattr(collectors, "_local", local)
    setup = wifi_setup()
    captured = asyncio.run(collect_auto_case(SharedSSH(), UDP, setup))
    values = captured["measurements"]
    assert values["UDP 5 sender Mbps"] == 5 and values["UDP 5 receiver Mbps"] == 4.8
    assert values["UDP 10 sender Mbps"] == 10 and values["UDP 10 receiver Mbps"] == 9.7
    assert values["UDP 5 lost datagrams"] == 10 and values["UDP 10 total datagrams"] == 1000
    assert values["UDP loss"] == 1 and values["UDP 10 loss"] == 2
    assert values["UDP jitter"] == 0.3 and values["UDP 10 jitter"] == 0.3
    assert values["UDP 5 run completed"] and values["UDP 10 run completed"]
    assert evaluate_auto_case(UDP, setup, values)[1] == expected
    if expected == "NEEDS REVIEW":
        assert values["Disconnects"] == 0
        assert values["NetworkManager failures"] == int("failed" in post_log)
        assert values["Network errors"] == int("Network is unreachable" in post_log)
        assert values["UDP 10 link connected"]


def test_legacy_udp_sender_summary_uses_server_receiver_output():
    raw = json.dumps({"end": {"sum": {"bits_per_second": 5e6, "sender": True}},
                      "server_output_text": "[ 5] 0.00-30.00 sec 17.2 MBytes 4.80 Mbits/sec 0.30 ms 10/1000 (1.0%) receiver\n"})
    stats = _udp_stats(raw)
    assert stats["sender Mbps"] == 5 and stats["receiver Mbps"] == 4.8
    assert stats["loss"] == 1 and stats["jitter"] == 0.3 and stats["lost datagrams"] == 10


def test_udp_wifi_control_probe_proves_post_load_connectivity_without_station_data():
    from desktop_app.wifi.auto_collectors import _parse
    outputs = {"iperf_udp_5": {"stdout": udp_report(5, 4.8), "stderr": "", "exit_status": 0},
               "iperf_udp_10": {"stdout": udp_report(10, 9.7), "stderr": "", "exit_status": 0},
               "udp_control_post": {"stdout": "__WIFI_UDP_POST_CONTROL_READY__", "exit_status": 0},
               "udp_nm_log_post": {"stdout": "", "exit_status": 0}}
    values = _parse(UDP, wifi_setup(), outputs)
    assert values["UDP 10 link connected"]
    assert evaluate_auto_case(UDP, wifi_setup(), values)[1] == "NEEDS REVIEW"
    ethernet = replace(wifi_setup(), ssh_transport="Ethernet", control_interface="eno1")
    assert "UDP 10 link connected" not in _parse(UDP, ethernet, outputs)


def test_source_informational_udp_recording_does_not_invent_a_review_requirement():
    case = CASES["TC-JET-STA-5G-006"]
    setup = wifi_setup()
    assert not missing_prerequisites(case, setup)
    rows, status, _ = evaluate_auto_case(case, setup, passing_measurements(case, setup))
    assert status == "PASS"
    assert all(row["status"] == "MEASURED" and not row["qualitative"]
               for row in rows if row["metric"] in {"UDP loss", "UDP jitter"})


def test_auto_queue_continues_after_a_qualitative_result(tmp_path, monkeypatch):
    from tests.test_wifi_batch import Harness
    harness = Harness(tmp_path, monkeypatch, wifi_setup())
    harness.begin([UDP, CASES["TC-JET-5G-006"]])
    state = harness.finish()
    assert [entry.status for entry in state.entries] == ["NEEDS REVIEW", "PASS"]
    assert harness.executed == [UDP.test_id, "TC-JET-5G-006"]
