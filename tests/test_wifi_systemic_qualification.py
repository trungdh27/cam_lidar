import asyncio
from dataclasses import replace

from core.remote.ssh_manager import CommandResult
from desktop_app.wifi import auto_collectors as collectors
from desktop_app.wifi.auto_collectors import collect_auto_case
from desktop_app.wifi.auto_suite import (
    AutoSuiteSetup, criteria_for_auto, evaluate_auto_case,
    qualification_level_for, readiness_status,
    NONE, RUNTIME_WIFI_STATE, FORWARD_WIFI_PATH, CONTROL_PATH_RECOVERY,
)
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.qualification import (
    BIDIRECTIONAL_WIFI_PATH, FAILED, QUALIFIED, qualify_wifi_path, parse_route,
)


CASES = {case.test_id: case for case in load_auto_catalog()}


def setup(**changes):
    base = AutoSuiteSetup(
        dashboard_jetson_connected=True, shared_ssh_ready=True,
        ssh_transport="Ethernet", management_transport="Ethernet",
        control_interface="mgmt0", management_route_dev="mgmt0",
        control_jetson_ip="172.20.0.2", control_client_ip="172.20.0.3",
        jetson_wifi_interface="dutwifi0", jetson_ap_ip="10.77.0.1",
        ap_profile="runtime-profile", nmcli_available=True, iw_available=True,
        network_manager_ready=True, wifi_phy="phy-runtime", ap_capable=True,
        band_24g_capable=True, band_5g_capable=True,
        current_mode="AP", current_ssid="runtime-ap", current_band="5 GHz",
        current_channel=48, current_frequency_mhz=5240,
        current_ipv4="10.77.0.1/24", client_association_ready=True,
        client_wifi_interface="clientwifi0", laptop_wifi_ip="10.77.0.83",
        laptop_wifi_connected=True, laptop_ssid="runtime-ap",
        local_nmcli_available=True, saved_wifi_profile_available=True,
        saved_wifi_profile_uuid="saved-runtime-uuid", saved_wifi_profile="runtime-profile",
        local_iperf3_available=True, jetson_iperf3_available=True,
        primary_non_wifi_management=True, alternate_control_ready=True,
        allow_disruptive=True, packet_loss_threshold_percent=5,
        udp_loss_threshold_percent=5, udp_jitter_threshold_ms=50,
        udp_min_receiver_mbps=4, security_requirement="WPA2/WPA3",
        distance_1m_confirmed=True, distance_10m_confirmed=True,
    )
    return replace(base, **changes)


def result(command, stdout="", stderr="", code=0):
    return CommandResult(command, stdout, stderr, code)


def test_target_route_is_authoritative_despite_ethernet_default_and_management():
    route = parse_route("10.77.0.1 dev clientwifi0 src 10.77.0.83")
    assert (route.dev, route.src, route.via) == ("clientwifi0", "10.77.0.83", "")
    qualified = qualify_wifi_path(
        required_level=FORWARD_WIFI_PATH, client_wifi_if="clientwifi0",
        client_wifi_ip="10.77.0.83", dut_wifi_if="dutwifi0",
        target_wifi_ip="10.77.0.1",
        forward_output="10.77.0.1 dev clientwifi0 src 10.77.0.83",
        management_transport="Ethernet", management_route_dev="mgmt0",
        alternate_control_ready=True)
    assert qualified.status == QUALIFIED and qualified.forward_wifi_ok


def test_forward_and_bidirectional_levels_are_independent():
    arguments = dict(client_wifi_if="wlo1", dut_wifi_if="wlan-dut",
                     target_wifi_ip="10.88.0.1",
                     forward_output="10.88.0.1 dev wlo1 src 10.88.0.4",
                     reverse_output="10.88.0.4 dev eth0 src 10.88.0.1")
    assert qualify_wifi_path(required_level=FORWARD_WIFI_PATH, **arguments).status == QUALIFIED
    bidirectional = qualify_wifi_path(required_level=BIDIRECTIONAL_WIFI_PATH, **arguments)
    assert bidirectional.status == FAILED and not bidirectional.reverse_wifi_ok


def test_dynamic_interfaces_and_dhcp_addresses_have_no_historical_dependency():
    for interface, address in (("wlan0", "10.42.0.9"), ("wlo1", "172.19.4.23"),
                               ("wlxabcdef", "192.0.2.44")):
        value = qualify_wifi_path(
            required_level=FORWARD_WIFI_PATH, client_wifi_if=interface,
            client_wifi_ip=address, target_wifi_ip="198.51.100.1",
            forward_output=f"198.51.100.1 dev {interface} src {address}")
        assert value.status == QUALIFIED and value.forward_route_src == address


def test_all_auto_cases_have_an_auditable_minimum_qualification_level():
    expected = {NONE, RUNTIME_WIFI_STATE, FORWARD_WIFI_PATH, CONTROL_PATH_RECOVERY}
    mapping = {case.test_id: qualification_level_for(case) for case in CASES.values()}
    assert len(mapping) == 42 and set(mapping.values()) <= expected
    assert mapping["TC-JET-5G-002"] == NONE
    assert mapping["TC-JET-5G-003"] == FORWARD_WIFI_PATH
    assert mapping["TC-JET-5G-011"] == CONTROL_PATH_RECOVERY


def test_expected_value_provenance_is_present_and_specific():
    rows = criteria_for_auto(CASES["TC-JET-5G-003"], setup())
    assert all(row.expected_provenance for row in rows)
    assert next(row for row in rows if row.name == "Association").expected_provenance == "RUNTIME_DISCOVERY"
    rf = criteria_for_auto(CASES["TC-JET-5G-001"], setup())
    assert next(row for row in rf if row.name == "Channel").expected_provenance == "DERIVED_RULE"


class Remote:
    async def run(self, command, timeout=30, **_kwargs):
        if command.startswith("iw dev") and command.endswith(" info"):
            return result(command, "Interface dutwifi0\n type AP\n ssid runtime-ap\n channel 48 (5240 MHz), width: 80 MHz\n")
        if command.startswith("iw dev") and command.endswith(" link"):
            return result(command, "Not connected.\n", code=0)
        if "station dump" in command:
            return result(command, "Station aa:bb:cc:dd:ee:ff\n tx bitrate: 433.3 MBit/s VHT-MCS 9\n")
        if command.startswith("ip -4 addr"):
            return result(command, "inet 10.77.0.1/24 scope global dutwifi0\n")
        if command == "iw phy":
            return result(command, "Wiphy phy-runtime\n VHT Capabilities\n * 5240 MHz [48]\n")
        if command.startswith("nmcli -g GENERAL.STATE"):
            return result(command, "100 (connected)\n")
        if command.startswith("journalctl"):
            if "--after-cursor" in command:
                return result(command, "")
            return result(command, "-- cursor: runtime-cursor\n")
        return result(command, "")


def local_runner(route_interface="clientwifi0", ping_ok=True, secret_ip=None, calls=None):
    async def run(command, timeout, on_output=None):
        if calls is not None:
            calls.append(command)
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: runtime-ap\n freq: 5240\n tx bitrate: 433.3 MBit/s VHT-MCS 9\n", "", 0
        if command.startswith("ip -4 addr"):
            return f"inet {secret_ip or '10.77.0.83'}/24 scope global clientwifi0\n", "", 0
        if command.startswith("ip route get"):
            return f"10.77.0.1 dev {route_interface} src {secret_ip or '10.77.0.83'}\n", "", 0
        if command.startswith("ping "):
            output = ("1000 packets transmitted, 1000 received, 0% packet loss\n"
                      "rtt min/avg/max/mdev = 1.0/2.0/3.0/0.4 ms\n")
            return output, "", 0 if ping_ok else 1
        return "", "", 0
    return run


def test_tc005_executes_ping_and_collects_rtt_on_valid_forward_route(monkeypatch):
    calls = []
    monkeypatch.setattr(collectors, "_local", local_runner(calls=calls))
    captured = asyncio.run(collect_auto_case(Remote(), CASES["TC-JET-5G-005"], setup()))
    assert captured["error"] is None
    assert any(command.startswith("ping -I clientwifi0") for command in calls)
    metrics = captured["measurements"]
    assert metrics["Forward Wi-Fi path"] is True
    assert (metrics["Packets sent"], metrics["Packets received"], metrics["RTT avg"]) == (1000, 1000, 2.0)


def test_tc005_route_failure_marks_ping_metrics_not_measured(monkeypatch):
    calls = []
    monkeypatch.setattr(collectors, "_local", local_runner(route_interface="eth0", calls=calls))
    case = CASES["TC-JET-5G-005"]
    captured = asyncio.run(collect_auto_case(Remote(), case, setup()))
    assert captured["error"] is None and not any(command.startswith("ping ") for command in calls)
    metrics = captured["measurements"]
    rows, outcome, _ = evaluate_auto_case(case, setup(), metrics)
    assert outcome == "FAIL"
    assert next(row for row in rows if row["metric"] == "Forward Wi-Fi path")["status"] == "FAIL"
    assert all(next(row for row in rows if row["metric"] == metric)["status"] == "NOT MEASURED"
               for metric in ("Packets sent", "Packets received", "RTT avg", "Packet loss"))


def test_raw_output_is_parsed_before_redacted_evidence(monkeypatch):
    secret_ip = "10.77.0.83"
    monkeypatch.setattr(collectors, "_local", local_runner(secret_ip=secret_ip))
    state = setup(wifi_credential=secret_ip)
    captured = asyncio.run(collect_auto_case(Remote(), CASES["TC-JET-5G-003"], state))
    assert captured["measurements"]["Client IPv4"] == secret_ip + "/24"
    evidence_text = "\n".join(item["content"] for item in captured["evidence"].values())
    assert secret_ip not in evidence_text and "[REDACTED]" in evidence_text


def test_tc011_management_and_recovery_states_are_separate():
    case = CASES["TC-JET-5G-011"]
    alternate = setup(management_route_dev="mgmt0", control_interface="mgmt0",
                      alternate_control_ready=True, allow_disruptive=True)
    assert readiness_status(case, alternate) == "READY"
    same_wifi = setup(ssh_transport="Wi-Fi", management_transport="Wi-Fi",
                      management_route_dev="dutwifi0", control_interface="dutwifi0",
                      alternate_control_ready=False, primary_non_wifi_management=False,
                      backup_ethernet_ready=False, allow_disruptive=True)
    assert readiness_status(case, same_wifi) == "BLOCKED"
    reconnect = replace(same_wifi, auto_reconnect_cases=(case.test_id,))
    assert readiness_status(case, reconnect) == "READY_WITH_RECONNECT"
    ethernet_exists_only = replace(same_wifi, detected_ethernet_interfaces="eth9")
    assert readiness_status(case, ethernet_exists_only) == "BLOCKED"
