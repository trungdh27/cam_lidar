"""Source-linked Wi-Fi criteria and conservative automatic decisions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from . import parsers

SUPPORTED_CHECKS = frozenset({"interface", "wifi_type", "nm_state", "kernel", "driver", "firmware",
                              "present", "ping1000", "both_throughput", "cycles", "duration",
                              "min5", "max100", "band_min5", "active", "profile", "role", "ping100",
                              "ap_supported", "both_bands", "profile_exists", "ap_mode"})


@dataclass(frozen=True)
class CriterionSpec:
    criterion_id: str
    name: str
    description: str
    expected: str
    required: bool = True
    source: str = "VD expected result"
    metric: str = ""
    check: str = ""
    collector: str = ""
    parser: str = ""
    importance: str = "CRITICAL"
    expected_rule: dict | None = None


def specs_for(case) -> list[CriterionSpec]:
    number = int(case.test_id.rsplit("C", 1)[1])
    if number == 1:
        return [
            CriterionSpec("C01-01", "Wi-Fi interface exists", case.expected, "configured Wi-Fi interface exists", metric="Interface", check="interface", collector="nmcli/ip", parser="parse_nmcli_device_status/parse_ip_link"),
            CriterionSpec("C01-02", "NetworkManager device type", case.expected, "TYPE=wifi", metric="Device Type", check="wifi_type", collector="nmcli device status/show", parser="parse_nmcli_device_status/parse_nmcli_device_show"),
            CriterionSpec("C01-03", "NetworkManager runtime state", case.expected, "state != unavailable", metric="NetworkManager", check="nm_state", collector="nmcli device status/show", parser="parse_nmcli_device_status/parse_nmcli_device_show"),
            CriterionSpec("C01-04", "Kernel device detection", case.expected, 'no relevant "Device not found" error', metric="Kernel Device Errors", check="kernel", collector="journalctl -k -b", parser="parse_kernel_device_errors"),
            CriterionSpec("C01-05", "Driver detection", case.expected, "driver identified when exposed", metric="Driver/PHY", check="driver", collector="sysfs/ethtool", parser="parse_driver"),
            CriterionSpec("C01-06", "Firmware / hardware recognition", case.expected, "reliable platform evidence", required=False, importance="INFORMATIONAL", expected_rule={"kind": "informational"}, metric="Firmware", check="firmware", collector="ethtool -i", parser="parse_driver"),
        ]
    if number == 3:
        return [CriterionSpec("C03-01", "NetworkManager service", case.expected, "active", metric="NetworkManager", check="active", collector="systemctl is-active NetworkManager", parser="parse_service_active"),
                CriterionSpec("C03-02", "Hotspot profile", case.expected, "Hotspot present", metric="Hotspot profile", check="profile", collector="nmcli connection show", parser="parse_nmcli_profile_list"),
                CriterionSpec("C03-03", "Wi-Fi interface visible to iw", case.expected, "configured Wi-Fi interface visible", metric="Interface", check="interface", collector="iw dev", parser="parse_iw_interface_role"),
                CriterionSpec("C03-04", "Runtime Wi-Fi role", case.expected, "managed or AP", metric="Role", check="role", collector="iw dev", parser="parse_iw_interface_role")]
    if number == 2:
        return [CriterionSpec("C02-01", "AP mode capability", case.expected, "AP supported", metric="AP capability", check="ap_supported", collector="iw list", parser="parse_iw_phy_capabilities"),
                CriterionSpec("C02-02", "Required hardware bands", case.expected, "2.4 GHz and 5 GHz", metric="Supported Bands", check="both_bands", collector="iw list", parser="parse_iw_phy_capabilities"),
                CriterionSpec("C02-03", "AP+STA concurrency", case.expected, "confirm if production requires concurrency", check="manual")]
    if number == 4:
        from .catalog import load_config
        config = load_config()
        expected_ssid = config.get("Robot SSID", "NOT CONFIGURED")
        expected_security = config.get("AP Security Requirement", "NOT CONFIGURED")
        return [CriterionSpec("C04-01", "Hotspot profile exists", case.expected, "Hotspot present", metric="Hotspot profile", check="profile_exists", collector="nmcli connection show", parser="parse_nmcli_profile_show"),
                CriterionSpec("C04-02", "AP profile mode", case.expected, "mode=ap", metric="Mode", check="ap_mode", collector="nmcli connection show", parser="parse_nmcli_profile_show"),
                CriterionSpec("C04-03", "Production SSID", case.expected,
                              f"SSID = {expected_ssid}" if expected_ssid != "NOT CONFIGURED" else "NOT CONFIGURED",
                              metric="SSID", check="manual"),
                CriterionSpec("C04-04", "Security configuration", case.expected,
                              expected_security, metric="Security", check="manual"),
                CriterionSpec("C04-05", "IPv4 sharing", case.expected, "NOT CONFIGURED", metric="IPv4 method", check="manual"),
                CriterionSpec("C04-06", "Autoconnect and permissions", case.expected, "NOT CONFIGURED", metric="Autoconnect", check="manual"),
                CriterionSpec("C04-07", "Band and channel", case.expected, "NOT CONFIGURED", metric="Band", check="manual")]
    # Only requirements with an explicit machine-readable measurement get an
    # automated path. The source's remaining conditions stay visible for review.
    if number == 10:
        return [CriterionSpec("C10-01", "Ping completion", case.expected, "1000 packets transmitted", metric="Packets transmitted", check="ping1000", collector="import ping output", parser="parse_ping"),
                CriterionSpec("C10-02", "Latency and link stability", case.expected, "source/project threshold and no disconnect", check="manual")]
    if number == 12:
        return [CriterionSpec("C12-01", "Throughput measured", case.expected, "upload and download measured", check="both_throughput", collector="import iperf3 JSON", parser="parse_iperf3"),
                CriterionSpec("C12-02", "Throughput and stability", case.expected, "project threshold, no disconnect, ping/SSH responsive", check="manual")]
    if number == 13:
        return [CriterionSpec("C13-01", "Reconnect cycles", case.expected, "all recorded cycles pass", check="cycles", collector="import cycle output", parser="parse_recovery"),
                CriterionSpec("C13-02", "IP, ping and recovery time", case.expected, "source requirement met", check="manual")]
    if number == 16:
        return [CriterionSpec("C16-01", "Endurance duration", case.expected, "2 hours completed", check="duration", collector="runtime clock", parser="elapsed_seconds"),
                CriterionSpec("C16-02", "Link stability", case.expected, "no prolonged disconnect or manual intervention", check="manual")]
    if number == 23:
        return [CriterionSpec("C23-01", "Physical distance", case.expected, "10 m confirmed", check="manual"),
                CriterionSpec("C23-02", "2.4 GHz throughput", case.expected, ">= 5 Mbps over 3 runs", metric="2.4 GHz Average", check="band_min5", collector="import iperf3 JSON/record measurement", parser="parse_iperf3"),
                CriterionSpec("C23-03", "5 GHz throughput", case.expected, ">= 5 Mbps over 3 runs", metric="5 GHz Average", check="band_min5", collector="import iperf3 JSON/record measurement", parser="parse_iperf3"),
                CriterionSpec("C23-04", "Connection stability", case.expected, "no disconnect or abnormal retransmits", check="manual")]
    if number == 26:
        return [CriterionSpec("C26-01", "Ping completion", case.expected, "100 packets transmitted", metric="Packets transmitted", check="ping100", collector="import ping output", parser="parse_ping"),
                CriterionSpec("C26-02", "Average local RTT", case.expected, "<= 100 ms", metric="Average RTT", check="max100", collector="import ping output", parser="parse_ping"),
                CriterionSpec("C26-03", "Timeouts and packet loss", case.expected, "source/project limits met", check="manual")]
    if number == 27:
        return [CriterionSpec("C27-01", "Authentication evidence", case.expected, "valid/invalid credential behavior confirmed", check="manual"),
                CriterionSpec("C27-02", "Production security restored", case.expected, "configuration restored", check="manual"),
                CriterionSpec("C27-03", "WPA2 operation", case.expected, "valid WPA2 client can use the network", check="manual"),
                CriterionSpec("C27-04", "Required WPA3 / SAE operation", case.expected, "WPA3 works when mandatory; otherwise confirm not required", check="manual"),
                CriterionSpec("C27-05", "Rejected client IPv4", case.expected, "invalid credential receives no robot-subnet IP", check="manual"),
                CriterionSpec("C27-06", "Secret exposure", case.expected, "no plaintext PSK in evidence", check="manual")]
    names = {
        5: ("Hotspot activation", "Runtime AP mode", "Profile SSID", "Client SSID discovery"),
        6: ("Authentication", "Client IPv4 / gateway", "AP subnet", "Packet loss", "SSH host identity"),
        7: ("Invalid / missing credential rejection",),
        9: ("Secret exposure", "Allowed services", "Invalid SSH credential rejection"),
        11: ("Configured channel / width / power", "RF stability / configured limits"),
        14: ("AP restoration", "Client recovery / ping", "Ethernet preservation", "Reboot-free recovery"),
        15: ("Three AP autostart cycles", "Automatic DHCP / ping", "Configured boot-ready time"),
        17: ("AP operation", "Ethernet preservation", "Production route preservation"),
        18: ("Production Ethernet reachability", "Wi-Fi SSH", "Production service policy"),
        19: ("Resource health", "Disruptive / repeated errors"),
        20: ("Automatic production readiness", "Client usability", "Production network / services", "Serious errors"),
        21: ("Hotspot activation", "AP mode", "Band", "SSID discovery", "Authentication / DHCP", "Ping / SSH", "Station evidence / stability", "Unexpected disconnects"),
        22: ("AP activation", "Band", "SSID discovery", "Authentication / DHCP", "Ping / SSH", "Station evidence", "Regulatory errors / disconnects"),
        24: ("Wi-Fi 6 capability / runtime HE", "Link stability / fallback"),
        25: ("Factory discovery at >=1 m", "Client association / IPv4", "Ping / SSH", "Prolonged disconnects", "Local RF usability"),
    }
    clauses = [clause.strip().rstrip(".") for clause in case.expected.split(";") if clause.strip()]
    result = []
    for index, clause in enumerate(clauses):
        name = names.get(number, ())[index] if index < len(names.get(number, ())) else clause
        rule = None
        expected = clause
        if name == "Band" and number in {21, 22}:
            expected = "2.4 GHz" if number == 21 else "5 GHz"
            rule = {"kind": "band", "value": expected}
        result.append(CriterionSpec(f"C{number:02d}-{index + 1:02d}", name,
                                    clause, expected, check="manual", expected_rule=rule))
    return result



def evaluate(case, metrics: dict, evidence: dict, interface: str, elapsed_seconds: int = 0,
             manual_reviews: dict[str, dict] | None = None) -> tuple[list[dict], str, str]:
    manual_reviews = manual_reviews or {}
    rows = []
    for spec in specs_for(case):
        actual = metrics.get(spec.metric) if spec.metric else None
        status, reason = "NOT_COLLECTED", "No parsed measurement was collected"
        if spec.check == "manual":
            review = manual_reviews.get(spec.criterion_id, {})
            reviewed = review.get("status")
            if reviewed in {"PASS", "FAIL", "NEEDS REVIEW"}:
                status = reviewed
                reason = review.get("note") or f"Tester marked criterion {reviewed}"
                if review.get("actual", "") != "":
                    actual = review["actual"]
            else:
                status, reason = "MANUAL_REQUIRED", "Inline tester confirmation is required"
        elif spec.check == "interface":
            if actual == interface: status, reason = "PASS", "Interface detected"
            elif actual: status, reason = "FAIL", f"Expected {interface}; detected {actual}"
            elif metrics.get("Interface Missing"): status, reason = "FAIL", "Interface absent from nmcli and ip"
        elif spec.check == "wifi_type" and actual is not None:
            status, reason = ("PASS", "NetworkManager reports TYPE=wifi") if str(actual).lower() == "wifi" else ("FAIL", f"NetworkManager reports TYPE={actual}")
        elif spec.check == "nm_state" and actual is not None:
            state = str(actual).lower()
            status, reason = ("FAIL", 'NetworkManager device state is "unavailable"') if "unavailable" in state else ("PASS", f"NetworkManager reports {actual}")
        elif spec.check == "kernel" and actual is not None:
            status, reason = ("FAIL", f"{actual} relevant Device not found error(s)") if actual else ("PASS", "No relevant Device not found error")
        elif spec.check == "driver" and actual is not None:
            status, reason = "PASS", f"Driver identified: {actual}"
        elif spec.check == "firmware":
            status, reason = ("PASS", "Firmware information exposed") if actual else ("UNKNOWN", "Platform did not expose reliable firmware information")
        elif spec.check == "active" and actual is not None:
            status, reason = ("PASS", "NetworkManager is active") if str(actual).strip() == "active" else ("FAIL", f"NetworkManager is {actual}")
        elif spec.check == "profile" and actual is not None:
            status, reason = ("PASS", "Hotspot profile found") if actual == "Present" else ("FAIL", "Hotspot profile absent")
        elif spec.check == "role" and actual is not None:
            status, reason = ("PASS", f"Runtime role is {actual}") if str(actual).lower() in {"managed", "ap"} else ("FAIL", f"Unexpected runtime role: {actual}")
        elif spec.check == "ap_supported" and actual is not None:
            status, reason = ("PASS", "AP interface mode supported") if actual == "Supported" else ("FAIL", "AP interface mode absent")
        elif spec.check == "both_bands" and actual is not None:
            status, reason = ("PASS", "Both required bands exposed") if {"2.4 GHz", "5 GHz"}.issubset(set(actual)) else ("FAIL", "Required hardware band is missing")
        elif spec.check == "profile_exists" and actual is not None:
            status, reason = ("PASS", "Hotspot profile found") if actual != "Absent" else ("FAIL", "Hotspot profile absent")
        elif spec.check == "ap_mode" and actual is not None:
            status, reason = ("PASS", "Profile mode is AP") if str(actual).lower() == "ap" else ("FAIL", f"Profile mode is {actual}")
        elif spec.check == "present" and actual is not None:
            status, reason = "PASS", "Measurement parsed"
        elif spec.check == "ping1000" and isinstance(actual, (int, float)):
            status, reason = ("PASS", "1000-packet run completed") if actual >= 1000 else ("NOT_COLLECTED", f"Only {actual:g} of 1000 packets transmitted")
        elif spec.check == "ping100" and isinstance(actual, (int, float)):
            status, reason = ("PASS", "100-packet run completed") if actual >= 100 else ("NOT_COLLECTED", f"Only {actual:g} of 100 packets transmitted")
        elif spec.check == "both_throughput":
            if "Upload" in metrics and "Download" in metrics: status, reason = "PASS", "Both directions measured"
        elif spec.check == "cycles":
            done, failed = metrics.get("Completed cycles"), metrics.get("Failed cycles")
            if failed: status, reason = "FAIL", f"{failed} recorded cycle(s) failed"
            elif done: status, reason = "PASS", f"{done} recorded cycle(s) passed"
        elif spec.check == "duration":
            if elapsed_seconds >= 7200: status, reason, actual = "PASS", "2-hour duration completed", elapsed_seconds
            else: actual = elapsed_seconds
        elif spec.check in {"min5", "max100"} and isinstance(actual, (int, float)):
            good = actual >= 5 if spec.check == "min5" else actual <= 100
            status, reason = ("PASS", "Measurement meets threshold") if good else ("FAIL", "Measurement violates threshold")
        elif spec.check == "band_min5" and isinstance(actual, (int, float)):
            band = spec.metric.removesuffix(" Average")
            runs = metrics.get(f"{band} Runs", [])
            if len(runs) >= 3:
                status, reason = ("PASS", "Three-run average meets 5 Mbps") if actual >= 5 else ("FAIL", "Three-run average below 5 Mbps")
            else:
                status, reason = "NOT_COLLECTED", f"Only {len(runs)} of 3 required runs collected"
        refs = [name for name, item in evidence.items() if spec.collector and
                (spec.collector.split("/")[0].split(" ")[0] in item.get("command", "") or
                 spec.collector.startswith("import") and name == "commands.log")]
        if spec.check == "kernel": refs = [name for name, item in evidence.items() if "journalctl -k" in item.get("command", "")]
        if spec.check == "driver": refs = [name for name, item in evidence.items() if "ethtool" in item.get("command", "") or "readlink" in item.get("command", "")]
        rule = spec.expected_rule or {"kind": "source_requirement", "value": spec.expected}
        if spec.check == "max100":
            rule = {"kind": "maximum", "value": 100, "unit": "ms"}
        elif spec.check in {"min5", "band_min5"}:
            rule = {"kind": "minimum", "value": 5, "unit": "Mbps"}
        rows.append({**asdict(spec), "expected_rule": rule, "actual": actual, "status": status,
                     "evidence_reference": refs, "reason": reason})
    required = [row for row in rows if row["required"] and row["importance"] == "CRITICAL"]
    failed = [row for row in required if row["status"] == "FAIL"]
    pending = [row for row in required if row["status"] not in {"PASS", "FAIL"}]
    if failed:
        result = "FAIL"
        reason = "; ".join(f"{row['criterion_id']}: {row['reason']} "
                           f"(expected {row['expected']}; actual {row['actual']}; "
                           f"evidence {', '.join(row['evidence_reference']) or 'none'})" for row in failed)
    elif pending:
        result = "NEEDS REVIEW"
        reason = "; ".join(f"{row['criterion_id']}: {row['reason']}" for row in pending)
    else:
        result = "PASS"
        reason = f"All {len(required)} required criteria passed."
    return rows, result, reason


def coverage_report(cases) -> list[dict]:
    report = []
    for case in cases:
        specs = specs_for(case)
        automated = [s for s in specs if s.check != "manual"]
        manual = [s for s in specs if s.check == "manual"]
        broken = [s.criterion_id for s in automated if not (
            s.collector and s.check in SUPPORTED_CHECKS and s.parser and
            all(name == "elapsed_seconds" or callable(getattr(parsers, name, None))
                for name in s.parser.split("/")))]
        report.append({"test_id": case.test_id, "mode": case.mode, "criteria": len(specs),
                       "automated": len(automated), "manual": len(manual), "unsupported": len(broken),
                       "collectors": sorted({s.collector for s in automated}),
                       "parsers": sorted({s.parser for s in automated}),
                       "evaluator_implemented": not broken,
                       "evaluation_coverage": "full" if not manual and not broken else "partial" if automated else "manual"})
    return report
