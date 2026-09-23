"""Focused coverage for isolated ODS AUTO suite and inline manual evaluation."""
import asyncio
import pytest
from types import SimpleNamespace
from dataclasses import replace

from PySide6.QtWidgets import QLineEdit, QPushButton, QTableWidget

from desktop_app.ui.wifi_page import WifiPage
from desktop_app.wifi.auto_collectors import command_plan
from desktop_app.wifi.auto_suite import (AutoSuiteBatchRunner, AutoSuiteSetup,
                                          criteria_for_auto, evaluate_auto_case,
                                          execution_safety, missing_prerequisites,
                                          DISRUPTIVE_CONTROL_PATH, READ_ONLY)
from desktop_app.wifi.catalog import (AUTO_SOURCE, AUTO_SHEETS, auto_source_rows,
                                      load_auto_catalog, load_catalog)
from desktop_app.wifi.runtime import WifiRuntime
from tests.test_wifi_module import SharedService, app, result


def ready_setup():
    return AutoSuiteSetup(
        dashboard_jetson_connected=True, shared_ssh_ready=True,
        ssh_transport="Ethernet", control_interface="eno1",
        control_jetson_ip="192.168.9.22", control_client_ip="192.168.9.10",
        jetson_wifi_interface="wlP1p1s0", jetson_ap_ip="192.168.2.22",
        ap_profile="Hotspot", test_ssid_24g="JET-24", test_ssid_5g="JET-5",
        wifi_credential="session-secret", external_ssid_24g="EXT-24",
        external_ssid_5g="EXT-5", external_credential="session-secret",
        local_iperf3_available=True, jetson_iperf3_available=True,
        backup_ethernet_ready=True, backup_ethernet_interface="eno1",
        primary_non_wifi_management=True,
        local_nmcli_available=True, laptop_wifi_connected=True,
        laptop_ssid="JET-24", laptop_wifi_ip="192.168.2.30",
        laptop_active_profile="Laptop valid", saved_wifi_profile="Laptop valid",
        saved_wifi_profile_uuid="11111111-1111-1111-1111-111111111111",
        saved_wifi_profile_available=True, valid_access_source="SAVED NETWORKMANAGER PROFILE",
        laptop_to_dut_wifi=True, dut_to_laptop_wifi=True, full_wifi_qual_path=True,
        allow_disruptive=True,
        nmcli_available=True, iw_available=True, network_manager_ready=True,
        network_manager_state="active", network_path_ready=True,
        wifi_state="connected", current_ssid="JET-24", current_mode="AP",
        current_band="2.4 GHz", current_channel=6, current_frequency_mhz=2437,
        current_channel_width="80 MHz", current_ipv4="192.168.2.22/24",
        current_route="default via 192.168.2.1 dev wlP1p1s0",
        wifi_driver="iwlwifi", wifi_phy="phy0", ap_capable=True,
        band_24g_capable=True, band_5g_capable=True, he_capable=True,
        client_association_ready=True,
        distance_1m_confirmed=True,
        distance_10m_confirmed=True, recorded_distance_1m="1.2 m",
        recorded_distance_10m="10 m", client_wifi_interface="wlan0",
        jetson_user="agx", packet_loss_threshold_percent=1,
        udp_loss_threshold_percent=1, udp_jitter_threshold_ms=20,
        udp_min_receiver_mbps=4.5,
        security_requirement="WPA2/WPA3",
    )


def passing_measurements(case, setup):
    values = {}
    for spec in criteria_for_auto(case, setup):
        if spec.operator in {"capture", "observed", "setup_confirmation"}: continue
        if spec.operator == "valid_channel": value = 11 if case.band == "2.4G" else 44
        elif spec.operator == "frequency_matches_channel": value = 2462 if case.band == "2.4G" else 5220
        elif spec.operator in {"truthy"}: value = True
        elif spec.operator == "falsy": value = False
        elif spec.operator == "zero": value = 0
        elif spec.operator in {"eq", "ieq", "le", "ge"}: value = spec.target
        elif spec.operator == "positive": value = spec.target + 1
        elif spec.operator in {"number", "measured"}: value = 1.0
        elif spec.operator == "valid_ip": value = spec.target if isinstance(spec.target, str) else "192.168.2.30/24"
        else: raise AssertionError(spec.operator)
        values[spec.metric] = value
    return values


def test_auto_ods_imports_exact_four_groups_and_42_rows():
    assert AUTO_SOURCE.is_file()
    rows = auto_source_rows()
    assert set(rows) == set(AUTO_SHEETS)
    assert {name: len(values) for name, values in rows.items()} == {
        "JET_2.4G": 13, "JET_5G": 13, "JET_STA_2.4G": 8, "JET_STA_5G": 8,
    }
    cases = load_auto_catalog()
    assert len(cases) == 42
    assert all(case.mode == "AUTO" and case.suite == "AUTO" for case in cases)
    assert len({case.test_id for case in cases}) == 42


def test_catalog_and_evidence_history_isolation(tmp_path):
    existing, auto = load_catalog("VD"), load_auto_catalog()
    assert len(existing) == 26 and len(auto) == 42
    assert not ({case.test_id for case in existing} & {case.test_id for case in auto})
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    old_attempt = runtime.start("VD", existing[1])
    assert "/VD/existing/" in str(old_attempt.directory)
    runtime.active.status = "COMPLETED"
    runtime.auto_setup = ready_setup()
    new_attempt = runtime.start("VD", auto[0])
    assert "/VD/auto/AP_24G/" in str(new_attempt.directory)
    assert runtime.attempts("VD", existing[1].test_id) == [old_attempt.directory / "summary.json"]
    assert runtime.attempts("VD", auto[0].test_id) == [new_attempt.directory / "summary.json"]


def test_auto_blocked_pass_fail_and_error_engine(tmp_path):
    case = load_auto_catalog()[0]
    blocked_rows, blocked, reason = evaluate_auto_case(case, AutoSuiteSetup(), {})
    assert blocked == "BLOCKED" and blocked_rows and "Missing setup" in reason
    setup = ready_setup()
    measurements = passing_measurements(case, setup)
    rows, status, _ = evaluate_auto_case(case, setup, measurements)
    assert status == "PASS" and all(row["status"] == "PASS" for row in rows if row["required"])
    measurements["Band"] = "5 GHz"
    rows, status, _ = evaluate_auto_case(case, setup, measurements)
    assert status == "FAIL" and next(row for row in rows if row["metric"] == "Band")["status"] == "FAIL"
    _rows, status, _ = evaluate_auto_case(case, setup, {}, "shared SSH failed")
    assert status == "ERROR"


def test_all_42_have_complete_automatic_criteria_and_command_plans():
    for case in load_auto_catalog():
        setup = replace(
            ready_setup(),
            current_band="2.4 GHz" if case.band == "2.4G" else "5 GHz",
            current_ssid="JET-24" if case.band == "2.4G" else "JET-5",
        )
        assert not missing_prerequisites(case, setup), case.test_id
        specs = criteria_for_auto(case, setup)
        assert specs and all(spec.metric and spec.operator for spec in specs)
        rows, status, _reason = evaluate_auto_case(case, setup,
                                                   passing_measurements(case, setup))
        assert status == "PASS", case.test_id
        assert all(row["status"] == ("MEASURED" if row["operator"] == "measured" else "PASS") for row in rows if row["required"])
        plan = command_plan(case, setup)
        assert plan and "JETSON" in {item.side for item in plan}
        if case.wifi_role == "AP":
            assert (any(item.label == "ap_restart_phase" for item in plan)) == case.test_id.endswith("-012")
        else:
            assert not any(item.label == "sta_restore_ap" for item in plan)
            assert any(item.label == "sta_connect" for item in plan) == case.test_id.endswith("-001")


def test_auto_runtime_states_and_batch_runner(tmp_path):
    cases = load_auto_catalog()
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    attempt = runtime.start("VD", cases[0])
    assert attempt is None and runtime.active is None
    assert not list(tmp_path.rglob("attempt_*"))
    runtime.auto_setup = ready_setup()
    attempt = runtime.start("VD", cases[0])
    assert attempt.status == "PREPARING"
    runtime.complete_auto(passing_measurements(cases[0], runtime.auto_setup))
    assert attempt.status == "COMPLETED" and attempt.final_result == "PASS"
    selected = [cases[0], cases[1]]
    batch = AutoSuiteBatchRunner(runtime.auto_setup,
                                lambda case: passing_measurements(case, runtime.auto_setup))
    queue = batch.enqueue(selected)
    assert all(item.status == "WAITING" for item in queue)
    assert [item.status for item in batch.run()] == ["PASS", "PASS"]


def test_per_test_dependency_gating_is_not_global(tmp_path):
    cases = {case.test_id: case for case in load_auto_catalog()}
    setup = replace(ready_setup(), current_band="5 GHz", current_ssid="JET-5",
                    current_channel=36, current_frequency_mhz=5180)
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)

    # Distance metadata belongs only to the range/10 m cases.
    runtime.auto_setup = replace(setup, distance_10m_confirmed=False, recorded_distance_10m="")
    assert runtime.latest_status("VD", "TC-JET-5G-009") == "BLOCKED"
    assert runtime.latest_status("VD", "TC-JET-5G-001") == "READY"
    assert "10 m position not confirmed" in runtime.block_reasons(cases["TC-JET-5G-009"])

    # iperf3 gates throughput/UDP tests, not AP runtime validation.
    runtime.auto_setup = replace(setup, local_iperf3_available=False, jetson_iperf3_available=False)
    for test_id in ("TC-JET-5G-006", "TC-JET-5G-007", "TC-JET-5G-009"):
        assert runtime.latest_status("VD", test_id) == "BLOCKED"
    assert runtime.latest_status("VD", "TC-JET-5G-001") == "READY"

    # External STA configuration cannot leak into AP dependencies.
    runtime.auto_setup = replace(setup, external_ssid_24g="", external_ssid_5g="")
    assert runtime.latest_status("VD", "TC-JET-5G-001") == "READY"
    assert runtime.latest_status("VD", "TC-JET-STA-5G-001") == "BLOCKED"

    # Unrelated missing setup may coexist without leaking into AP runtime gating.
    runtime.auto_setup = replace(
        setup, distance_10m_confirmed=False, recorded_distance_10m="",
        local_iperf3_available=False, jetson_iperf3_available=False,
        external_ssid_24g="", external_ssid_5g="",
        udp_jitter_threshold_ms=None,
    )
    assert runtime.latest_status("VD", "TC-JET-5G-001") == "READY"


def test_detected_resource_change_recomputes_blocked_attempt(tmp_path):
    cases = {case.test_id: case for case in load_auto_catalog()}
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = replace(
        ready_setup(), local_iperf3_available=False,
        jetson_iperf3_available=False, backup_ethernet_ready=False,
        current_band="5 GHz", current_ssid="JET-5",
        current_channel=36, current_frequency_mhz=5180,
    )
    attempt = runtime.start("VD", cases["TC-JET-5G-006"])
    assert attempt is None and runtime.active is None
    assert runtime.latest_status("VD", "TC-JET-5G-006") == "BLOCKED"

    runtime.update_detected_auto_setup(
        local_iperf3_available=True,
        jetson_iperf3_available=True,
        backup_ethernet_ready=True,
    )
    assert runtime.latest_status("VD", "TC-JET-5G-006") == "READY"


def test_5g_readiness_uses_runtime_and_exact_per_tc_requirements(tmp_path):
    cases = {case.test_id: case for case in load_auto_catalog()}
    wifi_control = replace(
        ready_setup(), ssh_transport="Wi-Fi", control_interface="wlP1p1s0",
        control_jetson_ip="192.168.2.22", control_client_ip="192.168.2.104",
        backup_ethernet_ready=False, backup_ethernet_interface="",
        current_band="5 GHz", current_ssid="RD3.02", current_channel=44,
        current_frequency_mhz=5220, test_ssid_5g="",
        local_iperf3_available=False, jetson_iperf3_available=False,
        distance_1m_confirmed=False, recorded_distance_1m="",
        distance_10m_confirmed=False, recorded_distance_10m="",
        wifi_credential="",
    )
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = wifi_control

    # A/B/D/E/G: runtime/PHY inspection works over the active Wi-Fi SSH path.
    for test_id in ("TC-JET-5G-001", "TC-JET-5G-002"):
        assert runtime.latest_status("VD", test_id) == "READY"
        assert not runtime.block_reasons(cases[test_id])
    assert execution_safety(cases["TC-JET-5G-001"]) == READ_ONLY
    assert execution_safety(cases["TC-JET-5G-002"]) == READ_ONLY

    # C/F/H: only the cases consuming the missing resource are blocked.
    assert runtime.latest_status("VD", "TC-JET-5G-006") == "BLOCKED"
    assert runtime.block_reasons(cases["TC-JET-5G-006"]) == [
        "iperf3 local is not ready", "iperf3 Jetson is not ready"]
    assert runtime.latest_status("VD", "TC-JET-5G-009") == "BLOCKED"
    assert "10 m position not confirmed" in runtime.block_reasons(cases["TC-JET-5G-009"])
    assert runtime.latest_status("VD", "TC-JET-5G-011") == "BLOCKED"
    assert any("Active control session uses the same Wi-Fi interface" in reason for reason in runtime.block_reasons(cases["TC-JET-5G-011"]))

    # I: AP recovery would tear down the interface carrying its own SSH.
    assert execution_safety(cases["TC-JET-5G-012"]) == DISRUPTIVE_CONTROL_PATH
    assert runtime.latest_status("VD", "TC-JET-5G-012") == "BLOCKED"
    assert any("Restarting the AP" in reason for reason in runtime.block_reasons(cases["TC-JET-5G-012"]))

    # J: the same recovery is safe when the active shared session is Ethernet.
    runtime.auto_setup = replace(
        wifi_control, ssh_transport="Ethernet", control_interface="eno1",
        backup_ethernet_ready=True, backup_ethernet_interface="eno1",
        wifi_credential="session-secret",
    )
    assert runtime.latest_status("VD", "TC-JET-5G-012") == "READY"


def test_shared_ssh_wifi_discovery_makes_read_only_5g_cases_ready(tmp_path, monkeypatch):
    import desktop_app.wifi.runtime as wifi_runtime
    import desktop_app.wifi.auto_collectors as collectors

    monkeypatch.setattr(wifi_runtime.shutil, "which", lambda _name: None)
    async def unavailable_local(command, timeout):
        return "", "Local tools unavailable in this fixture", 127
    monkeypatch.setattr(collectors, "_local", unavailable_local)
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)

    class WifiControlSSH:
        async def run(self, command, timeout):
            if command.startswith("printf '__WIFI_REMOTE_READY__"):
                return result(command, "__WIFI_REMOTE_READY__\n192.168.2.104 51000 192.168.2.22 22\n")
            if command.startswith("ip route get"):
                return result(command, "192.168.2.104 dev wlP1p1s0 src 192.168.2.22")
            if command.startswith("nmcli -t -f DEVICE"):
                return result(command, "wlP1p1s0:wifi:connected:Hotspot\neno1:ethernet:disconnected:--")
            if command == "ip -4 -br addr":
                return result(command, "wlP1p1s0 UP 192.168.2.22/24\neno1 DOWN")
            if command == "ip route":
                return result(command, "192.168.2.0/24 dev wlP1p1s0 src 192.168.2.22")
            if command.startswith("for c in"):
                return result(command, "nmcli=/usr/bin/nmcli\niw=/usr/sbin/iw")
            if command.startswith("systemctl"):
                return result(command, "active")
            if command == "iw phy":
                return result(command, "Wiphy phy0\n * AP\n 2437 MHz\n 5220 MHz\n HE PHY Capabilities")
            if command.endswith(" info"):
                return result(command, "Interface wlP1p1s0\n\ttype AP\n\tssid RD3.02\n\tchannel 44 (5220 MHz), width: 80 MHz")
            if command.startswith("readlink"):
                return result(command, "/sys/bus/pci/drivers/iwlwifi")
            return result(command, "")

    request_id = runtime.refresh_auto_discovery()
    discovered = asyncio.run(service.operations[-1][1](WifiControlSSH()))
    service.operation_succeeded.emit(request_id, discovered)
    setup = runtime.effective_auto_setup()
    assert setup.shared_ssh_ready and setup.ssh_transport == "Wi-Fi"
    assert setup.control_interface == setup.jetson_wifi_interface == "wlP1p1s0"
    assert setup.current_ssid == "RD3.02" and setup.current_band == "5 GHz"
    assert setup.current_channel == 44 and setup.current_frequency_mhz == 5220
    assert not setup.backup_ethernet_ready and not setup.local_iperf3_available
    assert runtime.latest_status("VD", "TC-JET-5G-001") == "READY"
    assert runtime.latest_status("VD", "TC-JET-5G-002") == "READY"
    assert runtime.latest_status("VD", "TC-JET-5G-006") == "BLOCKED"


def test_pretest_detection_auto_binds_resources_and_refreshes_readiness(tmp_path, monkeypatch):
    import desktop_app.ui.wifi_page as wifi_page
    import desktop_app.wifi.runtime as wifi_runtime
    import desktop_app.wifi.auto_collectors as collectors

    async def local(command, timeout):
        if command.startswith("nmcli -t -f DEVICE"):
            return "enp1s0:ethernet:connected:Wired", "", 0
        if command.startswith("ip route get"):
            return "192.168.9.22 dev enp1s0 src 192.168.9.10", "", 0
        return "", "", 0

    monkeypatch.setattr(collectors, "_local", local)

    monkeypatch.setattr(wifi_page.shutil, "which", lambda name: "/usr/bin/iperf3" if name == "iperf3" else None)
    monkeypatch.setattr(wifi_runtime.shutil, "which", lambda name: "/usr/bin/iperf3" if name == "iperf3" else None)
    app()
    service = SharedService()
    page = WifiPage(service, evidence_root=tmp_path)

    class FakeSSH:
        async def run(self, command, timeout):
            if command.startswith("printf '__WIFI_REMOTE_READY__"):
                return result(command, "__WIFI_REMOTE_READY__\n192.168.9.10 50000 192.168.9.22 22\n")
            if command.startswith("ip route get"):
                return result(command, "192.168.9.10 dev eno1 src 192.168.9.22")
            if command.startswith("nmcli -t"):
                return result(command, "wlP1p1s0:wifi:connected:Hotspot\neno1:ethernet:connected:Wired")
            if command == "ip -4 -br addr":
                return result(command, "wlP1p1s0 UP 192.168.2.22/24\neno1 UP 192.168.9.22/24")
            if command == "ip route":
                return result(command, "default via 192.168.9.1 dev eno1")
            if command.startswith("for c in"):
                return result(command, "nmcli=/usr/bin/nmcli\niw=/usr/sbin/iw\niperf3=/usr/bin/iperf3")
            if command.startswith("systemctl"):
                return result(command, "active")
            if command == "iw phy":
                return result(command, "Wiphy phy0\n * AP\n 2437 MHz\n 5180 MHz\n HE Iftypes")
            if command.endswith(" info"):
                return result(command, "Interface wlP1p1s0\n\ttype AP\n\tssid RD3.02\n\tchannel 44 (5220 MHz), width: 80 MHz")
            if command.endswith(" station dump"):
                return result(command, "Station aa:bb:cc:dd:ee:ff")
            if command.startswith("readlink"):
                return result(command, "/sys/bus/pci/drivers/iwlwifi")
            return result(command, "")

    request_id = page.runtime.refresh_auto_discovery()
    results = asyncio.run(service.operations[-1][1](FakeSSH()))
    service.operation_succeeded.emit(request_id, results)
    detected = page.runtime.effective_auto_setup()
    assert detected.shared_ssh_ready and detected.ssh_transport == "Ethernet"
    assert detected.control_interface == "eno1"
    assert detected.jetson_wifi_interface == "wlP1p1s0"
    assert detected.current_ssid == "RD3.02"
    assert detected.current_band == "5 GHz" and detected.current_channel == 44
    assert detected.local_iperf3_available
    assert detected.jetson_iperf3_available
    assert detected.backup_ethernet_ready
    assert page.pretest.auto_fields["local_iperf3_available"].currentText() == "READY (DETECTED)"
    assert page.pretest.auto_fields["jetson_iperf3_available"].currentText() == "READY (DETECTED)"
    assert page.pretest.auto_fields["backup_ethernet_ready"].currentText() == "READY (DETECTED)"
    page.close()


def test_wifi_primary_control_safe_measurements_and_capability(tmp_path):
    cases = {case.test_id: case for case in load_auto_catalog()}
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = replace(ready_setup(), ssh_transport="Wi-Fi", control_interface="wlP1p1s0",
                                 backup_ethernet_ready=False, backup_ethernet_interface="",
                                 current_band="5 GHz", current_ssid="JET-5")
    for suffix in ("001", "002", "004", "006", "013"):
        test_id = f"TC-JET-5G-{suffix}"
        assert runtime.latest_status("VD", test_id) == "READY"
        backup = next(dep for dep in runtime.dependency_report(cases[test_id]) if dep.key == "backup_ethernet_ready")
        assert not backup.required and not backup.satisfied
        assert backup.requirement == "No" and backup.state == "NOT REQUIRED"
        assert not any(spec.label in {"ap_down", "sta_connect", "sta_restore_ap"}
                       for spec in command_plan(cases[test_id], runtime.auto_setup))
    assert runtime.latest_status("VD", "TC-JET-24G-002") == "READY"
    assert runtime.latest_status("VD", "TC-JET-24G-001") == "BLOCKED"
    runtime.auto_setup.band_24g_capable = False
    assert runtime.latest_status("VD", "TC-JET-24G-002") == "BLOCKED"
    runtime.auto_setup.distance_1m_confirmed = False
    assert runtime.latest_status("VD", "TC-JET-5G-008") == "BLOCKED"
    assert runtime.latest_status("VD", "TC-JET-5G-009") == "READY"


def test_disruptive_control_requires_alternate_or_executable_reconnect(tmp_path):
    case = next(case for case in load_auto_catalog() if case.test_id == "TC-JET-5G-012")
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    runtime.auto_setup = replace(ready_setup(), ssh_transport="Wi-Fi", control_interface="wlP1p1s0",
                                 backup_ethernet_ready=False, current_band="5 GHz")
    assert runtime.latest_status("VD", case.test_id) == "BLOCKED"
    backup = next(dep for dep in runtime.dependency_report(case) if dep.key == "backup_ethernet_ready")
    assert backup.requirement == "Preferred" and backup.state == "NOT READY"
    called = []

    async def reconnect(shared, selected, setup):
        called.append(shared)
        assert selected is case
        return {"measurements": passing_measurements(case, setup), "evidence": {}, "raw": ""}

    runtime.register_reconnect_workflow(case.test_id, reconnect)
    assert runtime.latest_status("VD", case.test_id) == "READY_WITH_RECONNECT"
    assert runtime.latest_status("VD", "TC-JET-5G-011") == "BLOCKED"
    attempt = runtime.start("VD", case)
    shared = object()
    captured = asyncio.run(service.operations[-1][1](shared))
    service.operation_succeeded.emit("wifi-auto:1", captured)
    assert called == [shared] and attempt.final_result == "PASS"
    runtime.auto_setup = replace(ready_setup(), current_band="5 GHz")
    assert runtime.latest_status("VD", "TC-JET-5G-011") == "READY"


def test_sta_post_association_plans_preserve_the_current_control_path(tmp_path):
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = replace(ready_setup(), ssh_transport="Wi-Fi", control_interface="wlP1p1s0",
                                 backup_ethernet_ready=False, current_mode="managed", current_ssid="EXT-24",
                                 external_credential="", ap_profile="")
    for case in load_auto_catalog():
        if case.catalog_group != "STA_24G":
            continue
        number = int(case.test_id.rsplit("-", 1)[1])
        if number == 7:
            assert runtime.latest_status("VD", case.test_id) == "BLOCKED"
        else:
            assert runtime.latest_status("VD", case.test_id) == "READY"
            assert not any("device wifi connect" in spec.command or "connection up" in spec.command
                           for spec in command_plan(case, runtime.auto_setup))
    # Joining TC has setup prerequisites, not a requirement to already be STA.
    runtime.auto_setup = ready_setup()
    assert runtime.auto_setup.current_mode == "AP"
    assert runtime.latest_status("VD", "TC-JET-STA-24G-001") == "READY"


def test_local_wifi_discovery_removes_manual_interface_dependency(tmp_path):
    from desktop_app.wifi.runtime import parse_local_wifi_discovery
    outputs = {"devices": "eno1:ethernet:connected:Wired\nwlo1:wifi:connected:JET-5\nwlxabc:wifi:disconnected:--",
               "iw": "phy#0\n Interface wlo1\nphy#1\n Interface wlxabc",
               "addresses": "wlo1 UP 192.168.2.104/24", "routes": "default via 192.168.2.22 dev wlo1"}
    assert parse_local_wifi_discovery(outputs) == "wlo1"
    assert parse_local_wifi_discovery({"iw": "phy#0\n Interface wlx123"}) == "wlx123"
    assert parse_local_wifi_discovery({"iw": "Interface wlan0\nInterface wlan1"}) == ""
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = replace(ready_setup(), client_wifi_interface="")
    assert runtime.latest_status("VD", "TC-JET-24G-004") == "BLOCKED"
    runtime._local_discovery_finished(outputs)
    assert runtime.latest_status("VD", "TC-JET-24G-004") == "READY"
    dep = next(dep for dep in runtime.dependency_report(load_auto_catalog()[3]) if dep.key == "client_wifi_interface")
    assert dep.current_state == "wlo1" and dep.source == "AUTO DETECTED"


def test_ethernet_presence_and_ip_are_not_verified_control():
    from desktop_app.wifi.runtime import parse_auto_discovery
    results = {"health": result("", "__WIFI_REMOTE_READY__\n192.168.2.104 50000 192.168.2.22 22\n"),
               "devices": result("", "wlP1p1s0:wifi:connected:Hotspot\neno1:ethernet:connected:Wired\neth0:ethernet:disconnected:--"),
               "control_route": result("", "192.168.2.104 dev wlP1p1s0 src 192.168.2.22"),
               "addresses": result("", "wlP1p1s0 UP 192.168.2.22/24\neno1 UP 192.168.9.22/24"),
               "routes": result("", "192.168.9.0/24 dev eno1")}
    snapshot = parse_auto_discovery(results)
    assert snapshot["shared_ssh_ready"] and snapshot["detected_ethernet_interfaces"] == "eno1, eth0"
    assert not snapshot["backup_ethernet_ready"] and snapshot["backup_ethernet_interface"] == ""
    results["ethernet_verified_eno1"] = result("", "192.168.9.22")
    snapshot = parse_auto_discovery(results)
    assert snapshot["backup_ethernet_ready"] and snapshot["backup_ethernet_ip"] == "192.168.9.22"


@pytest.mark.parametrize("local_interface, identity, expected", [
    ("enp0s31f6", "robot-identity", True),
    ("wlo1", "robot-identity", False),
    ("enp0s31f6", "different-robot", False),
])
def test_alternate_discovery_verifies_reverse_ethernet_route_and_identity(
        tmp_path, monkeypatch, local_interface, identity, expected):
    import desktop_app.wifi.auto_collectors as collectors
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    runtime.auto_setup = ready_setup()
    monkeypatch.setattr(runtime, "refresh_local_wifi_discovery", lambda: None)

    async def local(command, timeout):
        if command.startswith("ip route get"):
            return f"192.168.9.22 dev {local_interface} src 192.168.9.10", "", 0
        if command.startswith("nmcli"):
            return "enp0s31f6:ethernet:connected:Wired\nwlo1:wifi:connected:Hotspot", "", 0
        if command.startswith("ssh"):
            return identity, "", 0
        if command.startswith("iw dev") or command == "ip -4 -br addr":
            return "", "", 0
        raise AssertionError(command)

    class Remote:
        config = SimpleNamespace(username="agx", port=22)

        async def run(self, command, timeout):
            if command.startswith("printf"):
                return result(command, "__WIFI_REMOTE_READY__\n192.168.2.104 50000 192.168.2.22 22\n")
            if command.startswith("ip route get"):
                device = "eno1" if "oif" in command else "wlP1p1s0"
                return result(command, f"192.168.2.104 dev {device}")
            if command == "cat /etc/machine-id":
                return result(command, "robot-identity")
            if command.startswith("nmcli -t -f DEVICE"):
                return result(command, "wlP1p1s0:wifi:connected:Hotspot\neno1:ethernet:connected:Wired")
            if command == "ip -4 -br addr":
                return result(command, "wlP1p1s0 UP 192.168.2.22/24\neno1 UP 192.168.9.22/24")
            return result(command, "")

    monkeypatch.setattr(collectors, "_local", local)
    request_id = runtime.refresh_auto_discovery()
    outputs = asyncio.run(service.operations[-1][1](Remote()))
    service.operation_succeeded.emit(request_id, outputs)
    setup = runtime.effective_auto_setup()
    assert setup.ssh_transport == "Wi-Fi" and setup.shared_ssh_ready
    assert setup.control_interface == "wlP1p1s0" and setup.detected_ethernet_interfaces == "eno1"
    assert setup.backup_ethernet_ready is expected
    assert setup.backup_ethernet_ip == ("192.168.9.22" if expected else "")


def test_verified_alternate_uses_the_same_shared_manager(tmp_path):
    case = next(case for case in load_auto_catalog() if case.test_id == "TC-JET-5G-012")
    service = SharedService()
    hosts = []

    async def collector(shared, selected, setup):
        assert shared.config.host == "192.168.9.22"
        return {"measurements": passing_measurements(selected, setup), "evidence": {}, "raw": ""}

    class SharedManager:
        config = SimpleNamespace(host="192.168.2.22")

        async def disconnect(self):
            pass

        async def connect(self):
            hosts.append(self.config.host)

    runtime = WifiRuntime(service, evidence_root=tmp_path, auto_collector=collector)
    runtime.auto_setup = replace(ready_setup(), ssh_transport="Wi-Fi", control_interface="wlP1p1s0",
                                 current_band="5 GHz", backup_ethernet_ip="192.168.9.22")
    runtime.start("VD", case)
    manager = SharedManager()
    asyncio.run(service.operations[-1][1](manager))
    assert hosts == ["192.168.9.22", "192.168.2.22"]
    assert manager.config.host == "192.168.2.22"


def test_sta_filter_counts_reset_and_compact_pretest_layout(tmp_path):
    from PySide6.QtWidgets import QApplication
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    page.resize(1250, 950)
    page.show()
    QApplication.processEvents()
    pretest = page.pretest
    assert pretest.discovery_table is pretest.table
    assert pretest.table.height() <= 530 and pretest.table.minimumHeight() == 0
    assert pretest.setup_scroll.widgetResizable()
    sta = page.vd.auto_pages["STA_24G"]
    assert sta.table.rowCount() == 8 and sta.selection_summary.isHidden()
    sta.filters["Category"].setCurrentText("SSH")
    assert sta.table.rowCount() == 1
    assert "Visible: 1 / Total: 8" in sta.counts.text() and "SSH" in sta.filter_summary.text()
    assert sta.table.item(0, 5).text() == "SSH unavailable"
    assert "Blocking dependencies:" in sta.table.item(0, 5).toolTip()
    sta.table.setCurrentCell(0, 2)
    assert not sta.selection_summary.isHidden()
    sta.reset_button.click()
    assert sta.table.rowCount() == 8 and "Visible: 8 / Total: 8" in sta.counts.text()
    assert page.vd.auto_tabs.tabText(2) == "STA 2.4 GHz (8)"
    page.close()


def test_auto_runtime_reuses_shared_ssh_operation_and_keeps_secrets_session_only(tmp_path):
    service = SharedService()
    case = load_auto_catalog()[0]
    setup = ready_setup()

    async def collector(ssh, selected, configured):
        assert selected is case and configured is setup
        assert ssh.marker == "shared-dashboard-ssh"
        return {"measurements": passing_measurements(case, setup), "evidence": {}, "raw": ""}

    runtime = WifiRuntime(service, evidence_root=tmp_path, auto_collector=collector)
    runtime.auto_setup = setup
    attempt = runtime.start("VD", case)
    assert attempt.status == "PREPARING" and len(service.operations) == 1
    assert service.operations[0][0] == "wifi-auto"
    shared = type("SharedSSH", (), {"marker": "shared-dashboard-ssh"})()
    capture = asyncio.run(service.operations[0][1](shared))
    service.operation_succeeded.emit("wifi-auto:1", capture)
    assert attempt.status == "COMPLETED" and attempt.final_result == "PASS"
    runtime.update_auto_setup(wifi_credential="do-not-persist",
                              external_credential="also-session-only")
    saved = runtime.auto_setup_path.read_text(encoding="utf-8")
    assert "do-not-persist" not in saved and "also-session-only" not in saved


def test_inline_manual_review_is_per_criterion_and_aggregates(tmp_path):
    app()
    service = SharedService()
    page = WifiPage(service, evidence_root=tmp_path)
    vd = page.environments["VD"]
    case = vd.by_id["TC-WIFI-C04"]
    vd._start_case(case)
    service.operation_succeeded.emit("wifi-inspect:1", [result(
        "nmcli connection show Hotspot",
        "802-11-wireless.mode: ap\n802-11-wireless.ssid: RD3.02\n"
        "802-11-wireless-security.key-mgmt: wpa-psk\nipv4.method: shared\n"
        "connection.autoconnect: yes\n")])
    attempt = page.runtime.active
    assert [vd.criteria_table.horizontalHeaderItem(i).text() for i in range(5)] == [
        "Criterion", "Expected", "Actual", "Result", "Action"]
    assert vd.criteria_table.item(2, 1).text() == "NOT CONFIGURED"
    assert isinstance(vd.criteria_table.cellWidget(2, 2), QLineEdit)
    first_actions = vd.criteria_table.cellWidget(2, 4).findChildren(QPushButton)
    second_actions = vd.criteria_table.cellWidget(3, 4).findChildren(QPushButton)
    assert [control.text() for control in first_actions] == ["PASS", "FAIL", "NEEDS REVIEW"]
    first_actions[0].click()
    assert next(row for row in attempt.criteria if row["criterion_id"] == "C04-03")["status"] == "PASS"
    assert next(row for row in attempt.criteria if row["criterion_id"] == "C04-04")["status"] == "MANUAL_REQUIRED"
    # Widgets are rebuilt after a review, so resolve the current FAIL control.
    vd.criteria_table.cellWidget(3, 4).findChildren(QPushButton)[1].click()
    assert attempt.final_result == "FAIL"
    assert next(row for row in attempt.criteria if row["criterion_id"] == "C04-03")["status"] == "PASS"
    page.close()


def test_vd_auto_navigation_counts_and_button_hierarchy(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    assert [page.vd.suite_tabs.tabText(i) for i in range(2)] == ["EXISTING TCs", "AUTO TCs"]
    assert [(page.vd.auto_tabs.tabText(i), page.vd.auto_tabs.widget(i).table.rowCount())
            for i in range(4)] == [("AP 2.4 GHz (13)", 13), ("AP 5 GHz (13)", 13),
                                   ("STA 2.4 GHz (8)", 8), ("STA 5 GHz (8)", 8)]
    page.vd.suite_tabs.setCurrentIndex(1)
    page.vd.auto_tabs.setCurrentIndex(3)
    auto_page = page.vd.auto_pages["STA_5G"]
    auto_page.filters["Status"].setCurrentText("BLOCKED")
    page.tabs.setCurrentIndex(2)
    page.tabs.setCurrentIndex(1)
    assert page.vd.suite_tabs.currentIndex() == 1 and page.vd.auto_tabs.currentIndex() == 3
    assert auto_page.filters["Status"].currentText() == "BLOCKED"
    assert auto_page.run_selected.objectName() == "PrimaryButton"
    assert page.environments["VD"].run_this.objectName() == "PrimaryButton"
    ap_page = page.vd.auto_pages["AP_24G"]
    assert ap_page.table.horizontalHeaderItem(5).text() == "Block Reason"
    ap_page._cell_clicked(0, 4)
    dependency_table = ap_page.detail_view.findChild(QTableWidget, "AutoDependencyTable")
    assert dependency_table is not None
    assert [dependency_table.horizontalHeaderItem(i).text() for i in range(4)] == [
        "Dependency", "Expected / Requirement", "Actual", "State"]
    page.close()
