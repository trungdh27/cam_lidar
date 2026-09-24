"""V5.5-derived qualification, saved-profile, ownership and recovery safety tests."""
import asyncio
import json
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QLabel, QTableWidget

import desktop_app.wifi.auto_collectors as collectors
from desktop_app.wifi.auto_collectors import (CommandSpec, authentication_rejected,
                                             client_recovery_phase, collect_auto_case,
                                             iperf_cleanup_command, _safe_command, join_laptop_sta,
                                             ap_restart_phase, command_plan)
from desktop_app.wifi.auto_suite import (AutoSuiteSetup, dependencies_for, evaluate_auto_case,
                                        missing_prerequisites, readiness_status, recovery_phases)
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.qualification import discover_laptop_wifi, qualification_path, nm_fields
from desktop_app.wifi.runtime import WifiRuntime
from desktop_app.ui.wifi_page import WifiPage
from tests.test_wifi_auto_suite import ready_setup, passing_measurements
from tests.test_wifi_module import SharedService, app, result

CASES = {case.test_id: case for case in load_auto_catalog()}
C11, C12, UDP = (CASES[f"TC-JET-5G-{number:03}"] for number in (11, 12, 7))


def setup(**changes):
    values = dict(current_band="5 GHz", current_ssid="RD3.02", laptop_ssid="RD3.02",
                  test_ssid_5g="RD3.02", saved_wifi_profile="RD3.02",
                  laptop_active_profile="RD3.02", wifi_credential="", recovery_off_seconds=0)
    values.update(changes)
    return replace(ready_setup(), **values)


def discovery_runners(*, profiles=True, ethernet=False):
    commands = []
    async def local(command, timeout):
        commands.append(command)
        if command == "nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status":
            return "wlo1:wifi:connected:RD3.02\nenp1s0:ethernet:connected:Wired", "", 0
        if command == "iw dev":
            return "Interface wlo1", "", 0
        if command == "ip -4 -br addr":
            return "wlo1 UP 192.168.2.30/24", "", 0
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: RD3.02", "", 0
        if command.startswith("ip route get"):
            return f"192.168.2.22 dev {'enp1s0' if ethernet else 'wlo1'} src 192.168.2.30", "", 0
        if command == "nmcli -t -f UUID,TYPE,NAME connection show":
            return ("saved-uuid:802-11-wireless:RD3.02" if profiles else ""), "", 0
        if command.startswith("nmcli -g 802-11-wireless.ssid"):
            return "RD3.02", "", 0
        if command.startswith("nmcli -g IP4.GATEWAY"):
            return "192.168.2.1", "", 0
        raise AssertionError(command)
    async def remote(command, timeout):
        commands.append(command)
        return "192.168.2.30 dev wlP1p1s0 src 192.168.2.22", "", 0
    return commands, local, remote


def test_saved_profile_matches_ssid_without_password_read():
    commands, local, remote = discovery_runners()
    values = asyncio.run(discover_laptop_wifi(vars(setup(control_jetson_ip="192.168.2.22")), local, remote))
    assert values["saved_wifi_profile_available"] and values["saved_wifi_profile"] == "RD3.02"
    assert values["valid_access_source"] == "SAVED NETWORKMANAGER PROFILE"
    assert values["full_wifi_qual_path"] and values["control_path"] == "DUT_WIFI_DIRECT"
    assert not any("psk" in command or "show-secrets" in command for command in commands)
    discovered = replace(setup(), **values)
    assert readiness_status(C11, discovered) == "READY"
    access = next(dep for dep in dependencies_for(C11, discovered) if dep.key == "valid_access_capability")
    assert access.state == "READY" and "Saved NetworkManager" in access.current_state


def test_missing_saved_profile_requires_profile_setup_not_plaintext_secret():
    commands, local, remote = discovery_runners(profiles=False)
    values = asyncio.run(discover_laptop_wifi(vars(setup()), local, remote))
    missing = missing_prerequisites(C11, replace(setup(), **values))
    assert any("Saved valid Wi-Fi profile was not found" in reason for reason in missing)
    assert not any("credential not configured" in reason for reason in missing)


@pytest.mark.parametrize("outbound,inbound,ssid,associated,expected", [
    ("dev wlo1", "dev wlP1p1s0", "RD3.02", True, True),
    ("dev eno1", "dev wlP1p1s0", "RD3.02", True, False),
    ("dev wlo1", "dev eno1", "RD3.02", True, False),
    ("dev wlo1", "dev wlP1p1s0", "Other", True, False),
    ("dev wlo1", "dev wlP1p1s0", "RD3.02", False, False),
    ("", "", "RD3.02", True, False),
])
def test_full_path_needs_association_ssid_and_both_wifi_routes(outbound, inbound, ssid, associated, expected):
    values = qualification_path(interface="wlo1", dut_interface="wlP1p1s0", associated=associated,
                                laptop_ssid=ssid, dut_ssid="RD3.02", outbound=outbound, inbound=inbound)
    assert values["full_wifi_qual_path"] is expected


def test_ethernet_control_is_not_wifi_qualification_evidence():
    commands, local, remote = discovery_runners(ethernet=True)
    values = asyncio.run(discover_laptop_wifi(vars(setup()), local, remote))
    assert not values["full_wifi_qual_path"] and not values["laptop_to_dut_wifi"]
    assert values["control_path"] == "ETHERNET_MGMT" and values["primary_non_wifi_management"]
    for case in (UDP, CASES["TC-JET-5G-005"], CASES["TC-JET-STA-5G-002"]):
        assert readiness_status(case, replace(setup(), **values)) == "READY"
    assert readiness_status(CASES["TC-JET-5G-002"], replace(setup(), **values)) == "READY"


def test_saved_profile_does_not_bypass_recovery_safety():
    state = setup(ssh_transport="Wi-Fi", control_interface="wlP1p1s0", backup_ethernet_ready=False)
    assert readiness_status(C11, state) == "BLOCKED"
    assert not any("credential" in reason.lower() for reason in missing_prerequisites(C11, state))
    assert any("Active control session uses the same Wi-Fi interface" in reason for reason in missing_prerequisites(C11, state))
    phases = recovery_phases(C12, state)
    assert [phase["status"] for phase in phases] == ["BLOCKED", "BLOCKED"]
    assert any("Restarting the AP" in reason for reason in phases[1]["reasons"])
    for number in (1, 2, 4, 5, 6, 7):
        assert readiness_status(CASES[f"TC-JET-5G-{number:03}"], state) == "READY"


def test_disruptive_authorization_and_profile_required_per_phase():
    phases = recovery_phases(C12, setup(ap_profile=""))
    assert [phase["status"] for phase in phases] == ["READY", "BLOCKED"]
    assert "profile" in phases[1]["reasons"][0]
    assert readiness_status(C11, setup(allow_disruptive=False)) == "BLOCKED"
    assert readiness_status(CASES["TC-JET-5G-001"], setup(allow_disruptive=False)) == "READY"


def phase_runner(*, fail_cycle=None, rejection=True, fail_add=False, restore=False):
    commands = []
    async def run(spec):
        commands.append(spec)
        stdout, stderr, code = "", "", 0
        if spec.label == "valid_profile_security":
            stdout = "wpa-psk"
        elif spec.label == "temporary_invalid_add" and fail_add:
            code, stderr = 1, "Not authorized"
        elif spec.label == "invalid_credential":
            code, stderr = 4, "Authentication failed: secrets were required" if rejection else "No Wi-Fi device found"
        elif spec.label.endswith("_link"):
            stdout = "Connected to aa:bb:cc:dd:ee:ff\n SSID: RD3.02"
        elif spec.label.endswith("_ipv4"):
            stdout = "wlan0 UP 192.168.2.30/24"
        elif spec.label.endswith("_route"):
            stdout = "192.168.2.22 dev wlan0 src 192.168.2.30"
        elif spec.label.endswith("_return"):
            stdout = "192.168.2.30 dev wlP1p1s0 src 192.168.2.22"
        if fail_cycle and spec.label == f"recovery_cycle_{fail_cycle}_ping":
            code = 1
        if restore and spec.label == "valid_profile_restore_profile_up":
            code = 1
        return {"stdout": stdout, "stderr": stderr, "exit_status": code}
    return commands, run


def test_auth_isolated_temp_profile_restore_and_five_independent_cycles():
    commands, run = phase_runner()
    state = setup()
    values = asyncio.run(client_recovery_phase(state, run, negative=True))
    assert values["Invalid credential rejected"] and values["Valid profile restored"]
    assert values["Reconnect successes"] == 5 and len(values["Client recovery cycles"]) == 5
    assert all(values[f"Recovery cycle {cycle}"] for cycle in range(1, 6))
    assert not any(spec.command.startswith("nmcli connection modify uuid") for spec in commands)
    modifies = [spec.command for spec in commands if "connection modify" in spec.command]
    assert len(modifies) == 1 and "__cam_lidar_negative_" in modifies[0]
    assert any(spec.label == "client_cleanup_restore_link" for spec in commands)
    assert any(spec.label == "temporary_invalid_cleanup" for spec in commands)
    rows, outcome, _ = evaluate_auto_case(C11, state, values)
    assert outcome == "PASS"
    assert sum(row["status"] == "PASS" for row in rows if row["name"].startswith("Phase A")) == 5


@pytest.mark.parametrize("fail_cycle,rejection,restore", [(3, True, False), (None, False, False), (None, True, True)])
def test_cycle_failure_unknown_auth_error_and_restore_failure_never_pass(fail_cycle, rejection, restore):
    commands, run = phase_runner(fail_cycle=fail_cycle, rejection=rejection, restore=restore)
    values = asyncio.run(client_recovery_phase(setup(), run, negative=True))
    assert evaluate_auto_case(C11, setup(), values)[1] == "FAIL"
    if fail_cycle:
        assert values["Reconnect successes"] == 4 and not values["Recovery cycle 3"]


def test_restore_and_cleanup_even_when_temporary_creation_fails():
    commands, run = phase_runner(fail_add=True)
    with pytest.raises(RuntimeError, match="could not be created"):
        asyncio.run(client_recovery_phase(setup(), run, negative=True))
    assert any(spec.label == "temporary_invalid_cleanup" for spec in commands)
    assert any(spec.label == "client_cleanup_restore_profile_up" for spec in commands)
    assert not any(spec.label == "valid_profile_down" for spec in commands)


def test_failed_final_restore_cannot_report_auth_pass():
    commands, base = phase_runner()
    async def run(spec):
        value = await base(spec)
        if spec.label == "client_cleanup_restore_profile_up":
            value["exit_status"] = 1
        return value
    values = asyncio.run(client_recovery_phase(setup(), run, negative=True))
    assert not values["Client cleanup restored"] and not values["Valid profile restored"]
    assert evaluate_auto_case(C11, setup(), values)[1] == "FAIL"


@pytest.mark.parametrize("error,expected", [("Authentication failed", True), ("wrong key", True),
    ("timeout after 30s", False), ("Not authorized", False), ("No Wi-Fi device found", False),
    ("Connection not found", False)])
def test_only_real_authentication_rejection_counts(error, expected):
    assert authentication_rejected("", error, 4) is expected
    assert not authentication_rejected("", error, 0)


@pytest.mark.parametrize("owned,cleanup", [(False, False), (True, True)])
def test_iperf_listener_ownership_collects_and_cleans_only_app_owned(monkeypatch, owned, cleanup):
    commands = []
    async def local(command, timeout):
        commands.append(command)
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: RD3.02", "", 0
        return "192.168.2.22 dev wlan0 src 192.168.2.30", "", 0
    class Remote:
        async def run(self, command, timeout):
            commands.append(command)
            if "SERVER_STATE=" in command:
                text = "SERVER_STATE=STARTED_BY_APP\nSERVER_OWNED=YES\nSERVER_PID=123\nSERVER_START=456\nSERVER_DIR=/tmp/cam_lidar_iperf.aBc123\n" if owned else "SERVER_STATE=ALREADY_LISTENING\nSERVER_OWNED=NO\n"
                return result(command, text)
            if command.startswith("ip route get"):
                return result(command, "192.168.2.30 dev wlP1p1s0")
            return result(command)
    monkeypatch.setattr(collectors, "_local", local)
    captured = asyncio.run(collect_auto_case(Remote(), UDP, setup()))
    assert any("then kill 123" in command for command in commands) is cleanup
    assert captured["measurements"]["iperf server state"] == ("STARTED_BY_APP" if owned else "ALREADY_LISTENING")
    assert not any("pkill" in command or "killall" in command for command in commands)


def test_listener_cleanup_rejects_unowned_or_unvalidated_targets():
    assert iperf_cleanup_command("SERVER_OWNED=NO\nSERVER_PID=123\n") is None
    assert iperf_cleanup_command("SERVER_OWNED=YES\nSERVER_PID=123\nSERVER_START=456\nSERVER_DIR=/tmp\n") is None


def test_qualification_changes_abort_before_iperf_even_with_old_ready_snapshot(monkeypatch):
    commands = []
    async def local(command, timeout):
        commands.append(command)
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: RD3.02", "", 0
        return "192.168.2.22 dev eno1", "", 0
    class Remote:
        async def run(self, command, timeout):
            return result(command, "192.168.2.30 dev wlP1p1s0")
    monkeypatch.setattr(collectors, "_local", local)
    captured = asyncio.run(collect_auto_case(Remote(), UDP, setup()))
    assert captured["error"] is None
    assert not any(command.startswith("iperf3") for command in commands)
    assert captured["measurements"]["Forward Wi-Fi path"] is False
    assert "UDP 5 receiver Mbps" in captured["measurements"]["Not measured metrics"]


def test_nm_profile_metadata_escaped_colons_and_secret_redaction():
    assert nm_fields(r"id:wifi:Name\: with colon") == ["id", "wifi", "Name: with colon"]
    safe = _safe_command(CommandSpec("connect", "LOCAL", "nmcli device wifi connect test password 'a sensitive password' ifname wlo1"))
    assert "sensitive" not in safe and "[REDACTED]" in safe


def test_c11_c12_detail_exposes_phases_and_exact_safety_reasons(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    page.runtime.auto_setup = setup(ssh_transport="Wi-Fi", control_interface="wlP1p1s0", backup_ethernet_ready=False)
    auto = page.vd.auto_pages["AP_5G"]
    for case in (C11, C12):
        auto.show_detail(case.test_id)
        labels = "\n".join(label.text() for label in auto.detail_view.findChildren(QLabel)
                           if label.isVisibleTo(auto.detail_view))
        assert "How to fix:" in labels and "safe" in labels
        assert auto.detail_tabs is None
        assert "This test has not started." in labels
        assert "valid Wi-Fi credential not configured" not in labels
        if case is C12:
            assert "Phase A — Client recovery strategy" in labels and "Phase B — AP restart strategy" in labels
            assert "backup Ethernet" in labels
            assert "AUTH / DHCP" not in labels  # No leftover C11 widgets after navigation.
    page.close()


def test_client_cleanup_survives_cancellation():
    commands, base = phase_runner()
    async def cancelled(spec):
        if spec.label == "invalid_credential":
            raise asyncio.CancelledError()
        return await base(spec)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client_recovery_phase(setup(), cancelled, negative=True))
    assert any(spec.label == "temporary_invalid_cleanup" for spec in commands)
    assert any(spec.label == "client_cleanup_restore_profile_up" for spec in commands)


def test_ap_restart_always_attempts_up_even_on_down_failure(monkeypatch):
    commands = []
    async def run(spec):
        commands.append(spec)
        text = ""
        if spec.label == "ap_runtime_post":
            text = "Interface wlP1p1s0\n type AP"
        return {"stdout": text, "stderr": "", "exit_status": 1 if spec.label == "ap_down" else 0}
    values = asyncio.run(ap_restart_phase(setup(), run))
    assert any(spec.label == "ap_up" for spec in commands)
    assert values["Phase B result"] == "FAIL"
    commands.clear()
    with pytest.raises(RuntimeError, match="no verified non-Wi-Fi management"):
        asyncio.run(ap_restart_phase(setup(backup_ethernet_ready=False, primary_non_wifi_management=False), run))
    assert commands == []


def test_c12_runs_client_phase_before_ap_restart_and_verifies_both(monkeypatch):
    commands = []
    async def local(command, timeout):
        commands.append(command)
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: RD3.02", "", 0
        if command.startswith("ip -4"):
            return "wlan0 UP 192.168.2.30/24", "", 0
        if command.startswith("ip route get"):
            return "192.168.2.22 dev wlan0 src 192.168.2.30", "", 0
        return "", "", 0
    class Remote:
        async def run(self, command, timeout):
            commands.append(command)
            text = ""
            if command.endswith(" info"):
                text = "Interface wlP1p1s0\n type AP\n ssid RD3.02\n channel 36 (5180 MHz)"
            elif command.startswith("nmcli -g GENERAL.CONNECTION"):
                text = "Hotspot"
            elif command.startswith("ip route get"):
                text = "192.168.2.30 dev wlP1p1s0"
            return result(command, text)
    async def sleep(seconds):
        pass
    monkeypatch.setattr(collectors, "_local", local)
    monkeypatch.setattr(collectors.asyncio, "sleep", sleep)
    captured = asyncio.run(collect_auto_case(Remote(), C12, setup()))
    assert captured["error"] is None
    values = captured["measurements"]
    assert values["Phase A result"] == "PASS" and values["Phase B result"] == "PASS"
    assert evaluate_auto_case(C12, setup(), values)[1] == "PASS"
    down = next(index for index, command in enumerate(commands) if "sudo -n nmcli connection down" in command)
    assert commands[:down].count("nmcli radio wifi off") == 5
    assert not any("WRONG_PASSWORD" in command for command in commands)


def test_sta_laptop_join_uses_saved_profile_and_requires_verified_management():
    commands = []
    async def run(spec):
        commands.append(spec)
        return {"stdout": "Connected to aa:bb:cc:dd:ee:ff\n SSID: RD3.02" if spec.label.endswith("link") else "",
                "stderr": "", "exit_status": 0}
    state = setup(current_mode="managed", laptop_ssid="Other", external_credential="")
    values = asyncio.run(join_laptop_sta(state, run))
    assert values["status"] == "JOINED" and values["credential_source"] == "SAVED NETWORKMANAGER PROFILE"
    assert "connection up uuid" in commands[0].command and "password" not in commands[0].command
    commands.clear()
    with pytest.raises(RuntimeError, match="verified non-Wi-Fi management"):
        asyncio.run(join_laptop_sta(replace(state, ssh_transport="Wi-Fi", backup_ethernet_ready=False), run))
    assert commands == []


def test_sta_laptop_join_failure_restores_previous_profile():
    commands = []
    async def run(spec):
        commands.append(spec)
        return {"stdout": "", "stderr": "Permission denied", "exit_status": 1}
    with pytest.raises(RuntimeError, match="previous client profile"):
        asyncio.run(join_laptop_sta(setup(current_mode="managed", laptop_ssid="Other"), run))
    assert commands[-1].label == "sta_laptop_restore"


def test_sta_existing_association_needs_no_plaintext_or_disruptive_transition():
    case = CASES["TC-JET-STA-5G-001"]
    state = setup(current_mode="managed", external_ssid_5g="RD3.02", external_credential="",
                  ssh_transport="Wi-Fi", control_interface="wlP1p1s0", backup_ethernet_ready=False)
    assert readiness_status(case, state) == "READY"
    assert not any(spec.label == "sta_connect" for spec in command_plan(case, state))


def test_runtime_persists_phase_context_but_never_passwords(tmp_path):
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    runtime.auto_setup = setup(wifi_credential="SECRET_NEVER_PERSIST", external_credential="ALSO_SECRET")
    attempt = runtime.start("VD", C12)
    metrics = {**passing_measurements(C12, runtime.auto_setup), "Phase A result": "PASS", "Phase B result": "PASS"}
    runtime.complete_auto(metrics)
    raw = (attempt.directory / "result.json").read_text()
    data = json.loads(raw)
    assert data["wifi_runtime"]["plaintext_credential_read"] is False
    assert data["wifi_runtime"]["saved_wifi_profile_available"]
    assert [phase["result"] for phase in data["recovery_phases"]] == ["PASS", "PASS"]
    assert "SECRET_NEVER_PERSIST" not in raw and "ALSO_SECRET" not in raw
