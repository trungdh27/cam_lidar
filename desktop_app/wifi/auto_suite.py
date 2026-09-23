"""Source-backed AUTO Wi-Fi suite readiness, criteria and batch execution."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .catalog import WifiTestCase, load_config
from .expectations import (CRITICAL, SUPPORTING, ExpectedValue, Exact, Band,
                           ValidChannelForBand, FrequencyMatchesChannel, Minimum, Maximum,
                           OneOf, ConfiguredValue, Informational)
from .qualification import (NONE, RUNTIME_WIFI_STATE, FORWARD_WIFI_PATH,
                            BIDIRECTIONAL_WIFI_PATH, CONTROL_PATH_RECOVERY)


DETECTED_ONLY_FIELDS = frozenset({
    "dashboard_jetson_connected", "shared_ssh_ready", "ssh_transport",
    "control_interface", "control_jetson_ip", "control_client_ip",
    "local_iperf3_available", "jetson_iperf3_available",
    "backup_ethernet_ready", "backup_ethernet_interface",
    "nmcli_available", "iw_available", "network_manager_ready",
    "network_manager_state", "network_path_ready",
    "wifi_state", "current_ssid", "current_bssid", "current_mode",
    "current_band", "current_channel", "current_frequency_mhz",
    "current_channel_width", "current_ipv4", "current_route",
    "wifi_driver", "wifi_phy", "ap_capable", "band_24g_capable",
    "band_5g_capable", "he_capable", "client_association_ready",
    "detected_ethernet_interfaces", "backup_ethernet_reason", "backup_ethernet_ip",
    "auto_reconnect_cases", "client_wifi_interface",
    "control_path", "primary_non_wifi_management", "laptop_wifi_connected",
    "laptop_ssid", "laptop_wifi_ip", "laptop_active_profile", "saved_wifi_profile_uuid",
    "saved_wifi_profile", "saved_wifi_profile_available", "valid_access_source",
    "local_nmcli_available", "laptop_to_dut_wifi", "dut_to_laptop_wifi",
    "full_wifi_qual_path", "dut_active_profile", "dut_gateway",
    "dut_hostname", "laptop_bssid", "laptop_channel",
    "laptop_frequency_mhz", "laptop_gateway",
    "forward_route_dev", "forward_route_src", "forward_route_via",
    "forward_wifi_ok", "reverse_route_dev", "reverse_route_src",
    "reverse_wifi_ok", "management_transport", "management_route_dev",
    "alternate_control_ready", "auto_recovery_ready",
})


@dataclass
class AutoSuiteSetup:
    dashboard_jetson_connected: bool = False
    shared_ssh_ready: bool = False
    ssh_transport: str = "Unknown"
    control_interface: str = ""
    control_jetson_ip: str = ""
    control_client_ip: str = ""
    jetson_wifi_interface: str = ""
    jetson_ap_ip: str = ""
    ap_profile: str = ""
    channel_24g: int | None = None
    frequency_24g_mhz: int | None = None
    rf_project_lock_24g: bool = False
    channel_5g: int | None = None
    frequency_5g_mhz: int | None = None
    rf_project_lock_5g: bool = False
    test_ssid_24g: str = ""
    test_ssid_5g: str = ""
    wifi_credential: str = ""
    external_ssid_24g: str = ""
    external_ssid_5g: str = ""
    external_credential: str = ""
    local_iperf3_available: bool = False
    jetson_iperf3_available: bool = False
    backup_ethernet_ready: bool = False
    backup_ethernet_interface: str = ""
    detected_ethernet_interfaces: str = ""
    backup_ethernet_ip: str = ""
    backup_ethernet_reason: str = "No verified reachable control path"
    auto_reconnect_cases: tuple[str, ...] = ()
    control_path: str = "OTHER_CONTROL"
    primary_non_wifi_management: bool = False
    laptop_wifi_connected: bool = False
    laptop_ssid: str = ""
    laptop_wifi_ip: str = ""
    laptop_active_profile: str = ""
    saved_wifi_profile_uuid: str = ""
    saved_wifi_profile: str = ""
    saved_wifi_profile_available: bool = False
    valid_access_source: str = "NOT AVAILABLE"
    local_nmcli_available: bool = False
    laptop_to_dut_wifi: bool = False
    dut_to_laptop_wifi: bool = False
    full_wifi_qual_path: bool = False
    forward_route_dev: str = ""
    forward_route_src: str = ""
    forward_route_via: str = ""
    forward_wifi_ok: bool = False
    reverse_route_dev: str = ""
    reverse_route_src: str = ""
    reverse_wifi_ok: bool = False
    management_transport: str = "Unknown"
    management_route_dev: str = ""
    alternate_control_ready: bool = False
    auto_recovery_ready: bool = False
    dut_active_profile: str = ""
    dut_gateway: str = ""
    dut_hostname: str = ""
    laptop_bssid: str = ""
    laptop_channel: int | None = None
    laptop_frequency_mhz: int | None = None
    laptop_gateway: str = ""
    allow_disruptive: bool = False
    recovery_cycles: int = 5
    recovery_off_seconds: int = 30
    nmcli_available: bool = False
    iw_available: bool = False
    network_manager_ready: bool = False
    network_manager_state: str = ""
    network_path_ready: bool = False
    wifi_state: str = ""
    current_ssid: str = ""
    current_bssid: str = ""
    current_mode: str = ""
    current_band: str = ""
    current_channel: int | None = None
    current_frequency_mhz: int | None = None
    current_channel_width: str = ""
    current_ipv4: str = ""
    current_route: str = ""
    wifi_driver: str = ""
    wifi_phy: str = ""
    ap_capable: bool = False
    band_24g_capable: bool = False
    band_5g_capable: bool = False
    he_capable: bool = False
    client_association_ready: bool = False
    distance_1m_confirmed: bool = False
    distance_10m_confirmed: bool = False
    recorded_distance_1m: str = ""
    recorded_distance_10m: str = ""
    client_wifi_interface: str = ""
    jetson_user: str = ""
    packet_loss_threshold_percent: float | None = None
    udp_loss_threshold_percent: float | None = None
    udp_jitter_threshold_ms: float | None = None
    udp_min_receiver_mbps: float | None = None
    security_requirement: str = ""

    def __post_init__(self):
        if self.ssh_transport != "Ethernet":
            self.primary_non_wifi_management = False

    @classmethod
    def from_project_config(cls) -> "AutoSuiteSetup":
        config = load_config()
        value = lambda key: "" if config.get(key, "NOT CONFIGURED") == "NOT CONFIGURED" else config.get(key, "")
        return cls(
            jetson_wifi_interface=value("Wi-Fi interface"), jetson_ap_ip=value("Jetson Wi-Fi IP"),
            ap_profile=value("Hotspot profile"), test_ssid_24g=value("Robot SSID"),
            test_ssid_5g=value("Robot SSID"), jetson_user=value("Jetson user"),
            security_requirement=value("Security Requirement"),
        )

    def public_dict(self) -> dict:
        values = asdict(self)
        defaults = type(self)()
        for key in DETECTED_ONLY_FIELDS:
            values[key] = getattr(defaults, key)
        for key in ("wifi_credential", "external_credential"):
            values[key] = "CONFIGURED" if values[key] else "NOT CONFIGURED"
        return values

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        values = asdict(self)
        defaults = type(self)()
        for key in DETECTED_ONLY_FIELDS:
            values[key] = getattr(defaults, key)
        # Credentials are session-only. Persisting them in the evidence tree would
        # turn every run archive into a plaintext secret store.
        values["wifi_credential_configured"] = bool(values.pop("wifi_credential"))
        values["external_credential_configured"] = bool(values.pop("external_credential"))
        path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> "AutoSuiteSetup":
        if not path.is_file():
            return cls.from_project_config()
        data = json.loads(path.read_text(encoding="utf-8"))
        defaults = cls()
        for key in DETECTED_ONLY_FIELDS:
            data[key] = getattr(defaults, key)
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})


SOURCE_REQUIRED = "SOURCE_REQUIRED"
PROJECT_OPTIONAL = "PROJECT_OPTIONAL"
INFORMATIONAL = "INFORMATIONAL"


@dataclass(frozen=True)
class ThresholdConfig:
    key: str
    label: str
    classification: str
    source_value: float | None = None


def threshold_configs_for(case: WifiTestCase) -> dict[str, ThresholdConfig]:
    """Only an explicit numeric acceptance bound in Expected Result is mandatory.

    Offered load (5/10 Mbps), 'near target', 'low', and '0% ideal' are
    characterization guidance, not numeric acceptance limits.
    """
    number = int(case.test_id.rsplit("-", 1)[1])
    ap = case.wifi_role == "AP"
    udp = (ap and number == 7) or (not ap and number == 6)
    definitions = (
        ("packet_loss_threshold_percent", "Max packet loss (%)", number == 5,
         r"packet\s+loss\s*(?:<=|≤)\s*(\d+(?:\.\d+)?)\s*%"),
        ("udp_loss_threshold_percent", "Max UDP loss (%)", udp,
         r"(?:UDP\s+)?loss\s*(?:<=|≤)\s*(\d+(?:\.\d+)?)\s*%"),
        ("udp_jitter_threshold_ms", "Max UDP jitter (ms)", udp,
         r"(?:UDP\s+)?jitter\s*(?:<=|≤)\s*(\d+(?:\.\d+)?)\s*ms"),
        ("udp_min_receiver_mbps", "Min UDP receiver (Mbps)", ap and number == 7,
         r"(?:UDP\s+(?:5\s*Mbps\s+)?)?receiver(?:\s+(?:throughput|bitrate))?\s*(?:>=|≥)\s*(\d+(?:\.\d+)?)\s*Mbps"),
    )
    configs = {}
    for key, label, applicable, pattern in definitions:
        match = re.search(pattern, case.expected, re.I) if applicable else None
        classification = SOURCE_REQUIRED if match else PROJECT_OPTIONAL if applicable else INFORMATIONAL
        configs[key] = ThresholdConfig(key, label, classification, float(match.group(1)) if match else None)
    return configs


@dataclass(frozen=True)
class AutoCriterion:
    criterion_id: str
    name: str
    expected: str
    metric: str
    operator: str = "truthy"
    target: object = True
    required: bool = True
    configuration_type: str = SOURCE_REQUIRED
    qualitative: bool = False
    importance: str = CRITICAL
    expected_rule: ExpectedValue | None = None
    source: str = "AUTO source expected result"
    expected_provenance: str = "SOURCE_REQUIREMENT"


@dataclass(frozen=True)
class AutoDependency:
    key: str
    label: str
    current_state: str
    action: str
    satisfied: bool
    reason: str
    source: str
    required: bool = True
    preferred: bool = False
    configuration_type: str | None = None

    @property
    def state(self) -> str:
        if self.required:
            if self.key == "current_band" and self.satisfied and self.current_state != self.expected_requirement:
                return "RECONFIG REQUIRED"
            return "READY" if self.satisfied else "BLOCKING"
        if self.preferred:
            return "READY" if self.satisfied else "NOT READY"
        if self.configuration_type == PROJECT_OPTIONAL and self.satisfied:
            return "READY"
        if self.key == "valid_access_capability":
            return "READY" if self.satisfied else "NOT READY"
        return "NOT REQUIRED"

    @property
    def expected_requirement(self) -> str:
        if self.key == "current_band":
            return self.reason.removeprefix("current band is not ")
        if self.key in {"distance_1m_confirmed", "distance_10m_confirmed"}:
            return "Physical position confirmed"
        if self.key in {"wifi_credential", "external_credential"}:
            return "Configured credential (hidden)"
        if self.configuration_type == PROJECT_OPTIONAL:
            return "Optional project limit"
        if self.configuration_type == INFORMATIONAL:
            return "Evidence only"
        if self.key in {"network_manager_ready", "local_nmcli_available"}:
            return "Active / available"
        return "Available" if self.required else "Preferred" if self.preferred else "Not required"

    @property
    def requirement(self) -> str:
        if self.configuration_type:
            return self.configuration_type
        return "Yes" if self.required else "Preferred" if self.preferred else "No"


READ_ONLY = "READ_ONLY"
SAFE_MEASUREMENT = "SAFE_MEASUREMENT"
SAFE_ACTIVE = SAFE_MEASUREMENT
DISRUPTIVE_CONTROL_PATH = "DISRUPTIVE_CONTROL_PATH"
SOURCE_REQUIREMENT = "SOURCE_REQUIREMENT"
PROJECT_CONFIG = "PROJECT_CONFIG"
DERIVED_RULE = "DERIVED_RULE"
RUNTIME_DISCOVERY = "RUNTIME_DISCOVERY"


def qualification_level_for(case: WifiTestCase) -> str:
    """Minimum runtime qualification needed by each AUTO test family."""
    number = int(case.test_id.rsplit("-", 1)[1])
    if case.wifi_role == "AP":
        if number == 2:
            return NONE
        if number in {1, 4, 10}:
            return RUNTIME_WIFI_STATE
        if number in {11, 12}:
            return CONTROL_PATH_RECOVERY
        return FORWARD_WIFI_PATH
    if number in {1, 3, 4}:
        return RUNTIME_WIFI_STATE
    if number == 7:
        return CONTROL_PATH_RECOVERY
    return FORWARD_WIFI_PATH


def qualification_reason_for(case: WifiTestCase) -> str:
    level = qualification_level_for(case)
    return {
        NONE: "PHY capability does not require a client data route",
        RUNTIME_WIFI_STATE: "The test inspects current Wi-Fi runtime state only",
        FORWARD_WIFI_PATH: "The measurement must target the DUT through the client Wi-Fi interface",
        BIDIRECTIONAL_WIFI_PATH: "The source requires independently proven forward and reverse Wi-Fi routes",
        CONTROL_PATH_RECOVERY: "The test intentionally disrupts Wi-Fi and needs a safe management or recovery path",
    }[level]


def sta_association_established(case: WifiTestCase, setup: AutoSuiteSetup) -> bool:
    expected = setup.external_ssid_24g if case.band == "2.4G" else setup.external_ssid_5g
    band = "2.4 GHz" if case.band == "2.4G" else "5 GHz"
    return bool(case.wifi_role == "STA" and setup.current_mode.lower() in {"managed", "station"}
                and setup.current_ssid and setup.current_ssid == expected
                and setup.current_band == band and setup.current_ipv4)


def execution_safety(case: WifiTestCase) -> str:
    """Classify control-path risk from the source procedure, not UI state."""
    number = int(case.test_id.rsplit("-", 1)[1])
    if (case.wifi_role == "STA" and number in {1, 7}) or (case.wifi_role == "AP" and number in {11, 12}):
        return DISRUPTIVE_CONTROL_PATH
    if number in {1, 2, 4, 10}:
        return READ_ONLY
    return SAFE_ACTIVE


def _id(case: WifiTestCase, index: int) -> str:
    return f"{case.test_id}-{index:02d}"


def target_ap_ssid(case: WifiTestCase, setup: AutoSuiteSetup) -> str:
    """Return the running DUT AP identity used by client qualification tests."""
    if case.wifi_role == "AP" and setup.current_mode.lower() == "ap" and setup.current_ssid:
        return setup.current_ssid
    return setup.test_ssid_24g if case.band == "2.4G" else setup.test_ssid_5g


def _criteria(case: WifiTestCase, setup: AutoSuiteSetup) -> list[tuple[str, str, str, str, object]]:
    """Map every source acceptance result to objective runtime measurements."""
    number = int(case.test_id.rsplit("-", 1)[1])
    band = case.band
    configured_ssid = setup.test_ssid_24g if band == "2.4G" else setup.test_ssid_5g
    ssid = target_ap_ssid(case, setup) if number == 3 and case.wifi_role == "AP" else configured_ssid
    external_ssid = setup.external_ssid_24g if band == "2.4G" else setup.external_ssid_5g
    band_name = "2.4 GHz" if band == "2.4G" else "5 GHz"
    if case.wifi_role == "AP":
        definitions = {
            1: [("AP mode", "AP", "Mode", "ieq", "AP"), ("SSID", f"SSID = {ssid or 'NOT CONFIGURED'}", "SSID", "eq", ssid),
                ("Band", band_name, "Band", "eq", band_name), ("Channel", f"Valid {band_name} channel", "Channel", "valid_channel", band_name),
                ("Frequency", "Consistent with selected channel / band", "Frequency", "frequency_matches_channel", None), ("Interface", "UP", "Interface state", "ieq", "UP"),
                ("AP IPv4", "valid IPv4", "AP IPv4", "valid_ip", True),
                ("Regulatory/activation errors", "none", "Regulatory errors", "zero", 0)],
            2: [("PHY capability", "Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE", "PHY standard capability", "truthy", True),
                ("Runtime PHY", "Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE", "Runtime PHY standard", "truthy", True)],
            3: [("Association", f"client associated with target Jetson AP SSID {ssid or '(runtime-discovered)'}", "Client SSID", "eq", ssid),
                ("Band", band_name, "Band", "eq", band_name), ("Client IPv4", "valid IPv4", "Client IPv4", "valid_ip", True),
                ("Wi-Fi route", f"route to DUT_AP_IP uses {setup.client_wifi_interface or 'CLIENT_WIFI_IF'}", "Route interface", "eq", setup.client_wifi_interface),
                ("Reachability", f"DUT_AP_IP reachable through {setup.client_wifi_interface or 'CLIENT_WIFI_IF'}", "Ping reachable", "truthy", True)],
            4: [("RSSI", "collected", "RSSI", "number", None), ("TX PHY bitrate", "collected", "TX bitrate", "positive", 0),
                ("RX PHY bitrate", "collected", "RX bitrate", "positive", 0), ("Disconnects", "0", "Disconnects", "zero", 0)],
            5: [("Wi-Fi route", "route to TARGET_WIFI_IP uses CLIENT_WIFI_IF", "Forward Wi-Fi path", "truthy", True),
                ("Packets sent", "collected", "Packets sent", "positive", 0), ("Packets received", "collected", "Packets received", "positive", 0),
                ("Average RTT", "<= 100 ms", "RTT avg", "le", 100), ("Packet loss", f"<= {setup.packet_loss_threshold_percent}%", "Packet loss", "le", setup.packet_loss_threshold_percent),
                ("RTT min", "collected", "RTT min", "number", None), ("RTT max", "recorded (no acceptance threshold)", "RTT max", "number", None),
                ("RTT mdev", "collected", "RTT mdev", "number", None), ("Disconnects", "0", "Disconnects", "zero", 0)],
            6: [("Forward receiver throughput", ">= 5 Mbps", "Forward receiver Mbps", "ge", 5),
                ("Reverse receiver throughput", ">= 5 Mbps", "Reverse receiver Mbps", "ge", 5),
                ("Retransmits", "collected", "Retransmits", "number", None), ("Disconnects", "0", "Disconnects", "zero", 0)],
            7: [("UDP 5 Mbps receiver", f">= {setup.udp_min_receiver_mbps} Mbps", "UDP 5 receiver Mbps", "ge", setup.udp_min_receiver_mbps),
                ("UDP loss", f"<= {setup.udp_loss_threshold_percent}%", "UDP loss", "le", setup.udp_loss_threshold_percent),
                ("UDP jitter", f"<= {setup.udp_jitter_threshold_ms} ms", "UDP jitter", "le", setup.udp_jitter_threshold_ms),
                ("10 Mbps link", "link remains connected", "UDP 10 link connected", "truthy", True),
                ("UDP 5 Mbps run", "completed", "UDP 5 run completed", "truthy", True),
                ("UDP 10 Mbps run", "completed", "UDP 10 run completed", "truthy", True),
                ("Network errors", "no Network is unreachable", "Network errors", "zero", 0),
                ("Unexpected disconnects", "none", "Disconnects", "zero", 0),
                ("NetworkManager errors", "none", "NetworkManager failures", "zero", 0)],
            8: [("SSID discovery", "visible at confirmed >1 m", "SSID visible", "truthy", True),
                ("Association", "connected", "Associated", "truthy", True), ("IPv4/route", "valid", "Client IPv4", "valid_ip", True),
                ("Ping", "succeeds", "Ping reachable", "truthy", True), ("SSH", "succeeds", "SSH reachable", "truthy", True),
                ("Disconnects", "0", "Disconnects", "zero", 0)],
            9: [("Forward receiver throughput", ">= 5 Mbps at 10 m", "Forward receiver Mbps", "ge", 5),
                ("Reverse receiver throughput", ">= 5 Mbps at 10 m", "Reverse receiver Mbps", "ge", 5),
                ("Disconnects", "0", "Disconnects", "zero", 0)],
            10: [("WPA2 runtime", "enabled", "WPA2 runtime", "truthy", True), ("SAE capability", "available for WPA3", "SAE capability", "truthy", True),
                 ("WPA3 activation", "successful", "WPA3 connected", "truthy", True), ("Legacy/open security", "none", "Insecure modes", "zero", 0)],
            11: [("Valid credential", "connects", "Valid credential connects", "truthy", True),
                 ("Invalid credential", "rejected", "Invalid credential rejected", "truthy", True),
                 ("Temporary profile cleanup", "deleted", "Temporary invalid profile removed", "truthy", True),
                 ("Valid profile restoration", "association, IPv4, route, reachability", "Valid profile restored", "truthy", True),
                 ("DHCP", "valid IPv4", "Client IPv4", "valid_ip", True), ("Reconnect cycles", f"{setup.recovery_cycles}/{setup.recovery_cycles}", "Reconnect successes", "eq", setup.recovery_cycles),
                 ("Duplicate/stale IP", "none", "Stale routes", "zero", 0)],
            12: [("Phase A — Client recovery", f"{setup.recovery_cycles}/{setup.recovery_cycles}", "Reconnect successes", "eq", setup.recovery_cycles), ("Phase B — AP recovery", "restored", "AP recovered", "truthy", True),
                 ("DHCP/route/service", "restored", "Services recovered", "truthy", True), ("Reboot required", "no", "Reboot required", "falsy", False)],
            13: [("Endurance duration", ">= 7200 s", "Elapsed seconds", "ge", 7200), ("Prolonged SSID loss", "none", "Disconnects", "zero", 0),
                 ("Manual recovery", "none", "Manual recoveries", "zero", 0), ("NetworkManager failures", "none", "NetworkManager failures", "zero", 0),
                 ("Driver resets", "none", "Driver resets", "zero", 0), ("Service failures", "none", "Service failures", "zero", 0)],
        }
    else:
        definitions = {
            1: [("SSID", f"SSID = {external_ssid or 'NOT CONFIGURED'}", "SSID", "eq", external_ssid),
                ("Band", band_name, "Band", "eq", band_name), ("Station mode", "managed", "Mode", "ieq", "managed"),
                ("IPv4", "valid", "DUT Wi-Fi IP", "valid_ip", True), ("Gateway", "valid", "Gateway", "valid_ip", True),
                ("Wi-Fi route", "present", "Wi-Fi route", "truthy", True)],
            2: [("Ping", "success through Wi-Fi", "Ping reachable", "truthy", True),
                ("Wi-Fi route", "uses client Wi-Fi interface", "Route interface", "eq", setup.client_wifi_interface),
                ("TCP port 22", "reachable (application SSH port)", "TCP22 reachable", "truthy", True),
                ("SSH authentication", "login succeeds", "SSH authentication", "truthy", True),
                ("Remote command execution", "WIFI_SSH_OK returned", "Remote command execution", "truthy", True),
                ("Host identity", "expected Jetson host", "Host identity valid", "truthy", True)],
            3: [("SSID/BSSID", "external AP matches", "RF identity valid", "truthy", True),
                ("Frequency", band_name, "Band", "eq", band_name), ("RSSI samples", "10 samples", "RF samples", "ge", 10),
                ("TX/RX bitrate", "collected", "PHY bitrate collected", "truthy", True), ("Disconnects", "0", "Disconnects", "zero", 0)],
            4: [("Capability", "recorded", "PHY capability", "truthy", True),
                ("Wi-Fi 6 evidence", "HE/802.11ax", "Runtime HE", "truthy", True)],
            5: [("Wi-Fi route", "route to TARGET_WIFI_IP uses CLIENT_WIFI_IF", "Forward Wi-Fi path", "truthy", True),
                ("Client↔DUT reachability", "reachable", "Ping reachable", "truthy", True),
                ("Average RTT", "<= 100 ms", "RTT avg", "le", 100),
                ("Packet loss", f"<= {setup.packet_loss_threshold_percent}%", "Packet loss", "le", setup.packet_loss_threshold_percent),
                ("Disconnects", "0", "Disconnects", "zero", 0)],
            6: [("Forward receiver throughput", ">= 5 Mbps", "Forward receiver Mbps", "ge", 5),
                ("Reverse receiver throughput", ">= 5 Mbps", "Reverse receiver Mbps", "ge", 5),
                ("UDP loss", f"<= {setup.udp_loss_threshold_percent}%", "UDP loss", "le", setup.udp_loss_threshold_percent),
                ("UDP jitter", f"<= {setup.udp_jitter_threshold_ms} ms", "UDP jitter", "le", setup.udp_jitter_threshold_ms),
                ("Network errors", "none", "Network errors", "zero", 0),
                ("Disconnects", "none", "Disconnects", "zero", 0)],
            7: [("Runtime security", "WPA2 or WPA3; not open/WEP/WPA1-only", "Security compliant", "truthy", True),
                ("Insecure modes", "none", "Insecure modes", "zero", 0), ("Invalid credential", "rejected", "Invalid credential rejected", "truthy", True),
                ("Valid reconnect", "successful", "Valid credential connects", "truthy", True)],
            8: [("Endurance duration", ">= 7200 s", "Elapsed seconds", "ge", 7200), ("Prolonged Wi-Fi loss", "none", "Disconnects", "zero", 0),
                ("SSH drops", "none", "SSH drops", "zero", 0), ("IP/route", "remains valid", "Wi-Fi route", "truthy", True),
                ("Driver/NM resets", "none", "Driver resets", "zero", 0), ("Manual recovery", "none", "Manual recoveries", "zero", 0)],
        }
    return definitions[number]


def explicit_rf_value(source: str, metric: str):
    """Read literal acceptance clauses while excluding labelled example groups."""
    example_group = False
    prefix = r"\s*(?:(?:must|shall)\s+(?:be|equal)\s*)?(?:=|:)?\s*"
    pattern = r"channel" + prefix + r"(\d+)" if metric == "Channel" else r"frequency" + prefix + r"(\d+)\s*MHz"
    for clause in source.split(";"):
        if re.search(r"example|default|placeholder|e\.g\.|ví dụ|mặc định|<[^>]+>", clause, re.I):
            example_group = True
            continue
        if re.search(r"required|mandatory|must|bắt buộc", clause, re.I):
            example_group = False
        if example_group:
            continue
        match = re.fullmatch(r"\s*(?:required\s+)?" + pattern + r"\s*(?:required|mandatory|\(required\))?\s*[.]?", clause, re.I)
        if match:
            return int(match.group(1))
    return None


def criteria_for_auto(case: WifiTestCase, setup: AutoSuiteSetup) -> list[AutoCriterion]:
    configured_ssid = setup.test_ssid_24g if case.band == "2.4G" else setup.test_ssid_5g
    configs = threshold_configs_for(case)
    threshold_metrics = {
        "Packet loss": ("packet_loss_threshold_percent", "not abnormal (source is qualitative)", "%"),
        "UDP loss": ("udp_loss_threshold_percent", "low / 0% ideal (source is qualitative)", "%"),
        "UDP jitter": ("udp_jitter_threshold_ms", "low (source is qualitative)", "ms"),
        "UDP 5 receiver Mbps": ("udp_min_receiver_mbps", "near 5 Mbps target (source is qualitative)", "Mbps"),
    }
    specs = []
    for name, expected, metric, operator, target in _criteria(case, setup):
        configuration_type, qualitative = SOURCE_REQUIRED, False
        importance, source = CRITICAL, "AUTO source expected result"
        provenance = SOURCE_REQUIREMENT
        rule = None
        # Telemetry alone never establishes a source acceptance requirement.
        stability = bool(re.search(r"(?:không|no|without)[^;.\n]*(?:disconnect|mất (?:link|SSID|Wi-Fi))|không reconnect", case.expected, re.I))
        if metric == "Disconnects":
            name = "Disconnects during test"
            if not stability:
                importance, source = SUPPORTING, "Attempt-local diagnostic; no source no-disconnect requirement"
            elif re.search(r"kéo dài|prolonged", case.expected, re.I) and not re.search(r"(?:không|no|without)\s+disconnect", case.expected, re.I):
                name = "Prolonged Wi-Fi loss"
                expected = "No prolonged loss; transient events require source review"
                source = "Source qualitative prolonged-loss requirement; no invented duration limit"
        if metric in {"NetworkManager failures", "Driver resets", "Service failures"}:
            source_tokens = {"NetworkManager failures": r"NetworkManager[^;.\n]*(?:fail|reset)|driver/NetworkManager",
                             "Driver resets": r"driver[^;.\n]*reset", "Service failures": r"service[^;.\n]*fail"}
            if not re.search(source_tokens[metric], case.expected, re.I):
                importance, source = SUPPORTING, "Diagnostic metric; not a source acceptance requirement"
        if metric == "Network errors" and "Network is unreachable" not in case.expected:
            importance, source = SUPPORTING, "Diagnostic metric; source requires link usability instead"
        if metric in {"Channel", "Frequency"} and case.wifi_role == "AP" and case.test_id.endswith("-001"):
            # These are generic runtime/band tests. Imported example/default RF
            # values remain source text, but become exact acceptance only when a
            # project configuration explicitly locks the value.
            config_key = ("channel_24g" if case.band == "2.4G" else "channel_5g") if metric == "Channel" else ("frequency_24g_mhz" if case.band == "2.4G" else "frequency_5g_mhz")
            configured = getattr(setup, config_key)
            project_locked = setup.rf_project_lock_24g if case.band == "2.4G" else setup.rf_project_lock_5g
            if project_locked and configured is not None:
                target, operator = configured, "eq"
                expected = f"{configured}{' MHz' if metric == 'Frequency' else ''} (project locked)"
                rule, source = ConfiguredValue(config_key), "Explicit project-locked RF configuration"
                provenance = PROJECT_CONFIG
            else:
                importance = SUPPORTING
                source = "Derived RF consistency; no exact source/config value"
                rule = ValidChannelForBand(target) if metric == "Channel" else FrequencyMatchesChannel()
                provenance = DERIVED_RULE
        if metric in {"PHY standard capability", "Runtime PHY standard"}:
            rule = OneOf((True, "Wi-Fi 5 / 802.11ac", "Wi-Fi 6 / 802.11ax", "Wi-Fi 5 + Wi-Fi 6"))
            source = "Wi-Fi 5/6 acceptance policy"
            provenance = DERIVED_RULE
        if metric in {"Client SSID", "Route interface", "Forward Wi-Fi path", "Ping reachable"} and case.wifi_role == "AP":
            provenance = RUNTIME_DISCOVERY
        if metric == "SSID":
            key = ("test_ssid_24g" if case.band == "2.4G" else "test_ssid_5g") if case.wifi_role == "AP" else ("external_ssid_24g" if case.band == "2.4G" else "external_ssid_5g")
            rule = ConfiguredValue(key) if target else Informational()
            source = "Project configured SSID" if target else "Observed runtime SSID"
            provenance = PROJECT_CONFIG if target else RUNTIME_DISCOVERY
            if not target:
                importance = INFORMATIONAL
                expected = "Observed runtime SSID (no expected SSID configured)"
        if metric == "Band":
            rule = Band(target)
        if case.wifi_role == "STA" and case.test_id.endswith("-006") and metric in {"Forward receiver Mbps", "Reverse receiver Mbps"} and not setup.distance_10m_confirmed:
            operator, target, expected = "number", None, "Recorded; 5 Mbps applies only to confirmed 10 m requirement"
        if case.wifi_role == "STA" and case.test_id.endswith("-008") and metric == "Elapsed seconds":
            target, expected = 1800, ">= 1800 s (source minimum 30 minutes; 2 hours optional)"
        if metric in {"RSSI", "TX bitrate", "RX bitrate", "RTT min", "RTT max", "RTT mdev", "Retransmits"}:
            if metric in {"TX bitrate", "RX bitrate"}:
                operator, target = "number", None
            importance = INFORMATIONAL
            source = "Observed measurement; no source acceptance threshold"
        required = not (case.wifi_role == "AP" and name == "SSID" and not configured_ssid)
        if metric in threshold_metrics:
            key, qualitative_expected, unit = threshold_metrics[metric]
            config = configs[key]
            configuration_type = config.classification
            project_value = getattr(setup, key)
            target = config.source_value if config.source_value is not None else project_value
            if target is None:
                # STA UDP source asks to record loss/jitter, with no 'low'
                # acceptance requirement. Recording alone needs no review.
                qualitative = case.wifi_role == "AP" or metric == "Packet loss"
                operator = "measured"
                expected = qualitative_expected if qualitative else "recorded; no source numeric limit"
            else:
                expected = f"{'<=' if operator == 'le' else '>='} {target:g} {unit} ({'source' if config.source_value is not None else 'project'})"
            # A project policy cannot relax an explicit source acceptance bound.
            if config.source_value is not None and project_value is not None and project_value != config.source_value:
                specs.append(AutoCriterion(_id(case, len(specs) + 1), f"Project {name}",
                                           f"{'<=' if operator == 'le' else '>='} {project_value:g} {unit} (project)",
                                           metric, operator, project_value, True, PROJECT_OPTIONAL,
                                           expected_provenance=PROJECT_CONFIG))
        if operator == "measured" and not qualitative:
            importance = INFORMATIONAL
        if rule is None:
            rule = Minimum(target) if operator == "ge" else Maximum(target) if operator == "le" else Exact(target) if operator == "eq" else Informational() if importance == INFORMATIONAL else None
        ssh_ids = {"Route interface": "route", "TCP22 reachable": "tcp22", "SSH authentication": "ssh_auth",
                   "Remote command execution": "remote_command", "Host identity valid": "host_identity"}
        criterion_id = (f"{case.test_id}-{ssh_ids[metric]}" if case.wifi_role == "STA" and case.test_id.endswith("-002") and metric in ssh_ids
                        else _id(case, len(specs) + 1))
        specs.append(AutoCriterion(criterion_id, name, expected, metric, operator, target,
                                   required, configuration_type, qualitative, importance, rule, source,
                                   provenance))
    number = int(case.test_id.rsplit("-", 1)[1])
    distance_key = "distance_1m_confirmed" if case.wifi_role == "AP" and number == 8 else "distance_10m_confirmed" if (case.wifi_role == "AP" and number == 9 or case.wifi_role == "STA" and number == 6 and setup.distance_10m_confirmed) else None
    if distance_key:
        specs.append(AutoCriterion(_id(case, len(specs) + 1), "Physical distance",
                                   ">=1 m confirmed" if distance_key == "distance_1m_confirmed" else "10 m confirmed",
                                   "Distance confirmation", "setup_confirmation", distance_key,
                                   expected_rule=Exact(True), source="Source physical setup / per-TC confirmation",
                                   expected_provenance=SOURCE_REQUIREMENT))
    if case.wifi_role == "AP" and case.test_id.endswith("-007"):
        for rate in (5, 10):
            for suffix in ("sender Mbps", "lost datagrams", "total datagrams"):
                metric = f"UDP {rate} {suffix}"
                specs.append(AutoCriterion(_id(case, len(specs) + 1), metric, "recorded; no source numeric limit",
                                           metric, "measured", None, True, INFORMATIONAL, False, INFORMATIONAL, Informational(),
                                           expected_provenance=SOURCE_REQUIREMENT))
        for suffix in ("receiver Mbps", "loss", "jitter"):
            metric = f"UDP 10 {suffix}"
            specs.append(AutoCriterion(_id(case, len(specs) + 1), metric, "recorded; no source numeric limit",
                                       metric, "measured", None, True, INFORMATIONAL, False, INFORMATIONAL, Informational(),
                                       expected_provenance=SOURCE_REQUIREMENT))
    if case.wifi_role == "AP" and case.test_id.endswith(("-011", "-012")):
        for cycle in range(1, setup.recovery_cycles + 1):
            metric = f"Recovery cycle {cycle}"
            specs.append(AutoCriterion(_id(case, len(specs) + 1), f"Phase A — {metric}",
                                       "association, IPv4, Wi-Fi route and DUT reachability restored",
                                       metric, "truthy", True))
    if case.wifi_role == "AP" and case.test_id.endswith("-001") or case.wifi_role == "STA" and case.test_id.endswith("-003"):
        for metric in ("BSSID", "Driver", "PHY", "Tx power", "Width"):
            specs.append(AutoCriterion(_id(case, len(specs) + 1), metric, "Observed runtime value",
                                       metric, "observed", None, False, INFORMATIONAL, False,
                                       INFORMATIONAL, Informational(), "Runtime evidence/context",
                                       RUNTIME_DISCOVERY))
    informational_capture = [spec.metric for spec in specs if spec.required and spec.importance == INFORMATIONAL]
    if informational_capture:
        specs.append(AutoCriterion(_id(case, len(specs) + 1), "Required measurement capture",
                                   "Source-requested measurements recorded", "Measurement capture", "capture", tuple(informational_capture)))
    return specs


def dependencies_for(case: WifiTestCase, setup: AutoSuiteSetup,
                     detected_keys: set[str] | None = None) -> list[AutoDependency]:
    """Return only the prerequisites used by this individual test case."""
    detected_keys = detected_keys or set()
    number = int(case.test_id.rsplit("-", 1)[1])
    ap = case.wifi_role == "AP"
    configs = threshold_configs_for(case)
    iperf_required = number in ({6, 7, 9} if ap else {6})
    distance_1m_required = ap and number == 8
    distance_10m_required = ap and number == 9
    # C003 inspects the already-associated client; C011/C012 perform auth/reconnect.
    client_recovery = ap and number in {11, 12}
    credential_required = False  # Saved NM profiles are valid access capability.
    association_required = ap and number in {4, 5, 6, 8, 9, 11, 12, 13}
    udp_characterization = ap and number == 7
    qualification_level = qualification_level_for(case)
    network_required = qualification_level in {FORWARD_WIFI_PATH, BIDIRECTIONAL_WIFI_PATH}
    client_if_required = network_required or ap and number in {3, 4, 11, 12}
    band_required = ap and number in {1, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13}
    current_ssid_required = ap and number in {1, 3, 8, 10, 11, 12, 13}
    phy_required = (ap and number in {2, 4}) or (not ap and number == 4)
    disruptive = execution_safety(case) == DISRUPTIVE_CONTROL_PATH
    if not ap and number == 1 and sta_association_established(case, setup):
        disruptive = False  # Already joined: validate without reconfiguring DUT.
    self_disruption = disruptive and bool(setup.control_interface) and setup.control_interface == setup.jetson_wifi_interface
    reconnect = case.test_id in setup.auto_reconnect_cases
    verified_management = (setup.alternate_control_ready or setup.primary_non_wifi_management
                           or setup.backup_ethernet_ready)
    safe_control = not disruptive or (
        bool(setup.control_interface) and setup.control_interface != setup.jetson_wifi_interface
    ) or verified_management or reconnect
    if client_recovery:
        safe_control = verified_management or reconnect
    ssid_key = "external_ssid_24g" if case.band == "2.4G" else "external_ssid_5g"
    target_band = "2.4 GHz" if case.band == "2.4G" else "5 GHz"
    required: list[tuple[str, str, object, bool, str, str, str | None]] = [
        ("shared_ssh_ready", "Shared SSH", setup.shared_ssh_ready, True,
         "Reconnect from Dashboard and refresh", "shared SSH command execution is not ready", None),
        ("control_interface", "Control interface", setup.control_interface, True,
         "Refresh shared SSH transport discovery", "SSH control interface was not detected", None),
        ("jetson_wifi_interface", "Wi-Fi interface", setup.jetson_wifi_interface, True,
         "Refresh Jetson Wi-Fi discovery", "Jetson Wi-Fi interface was not detected", None),
        ("ap_profile", "AP profile", setup.ap_profile, ap and number == 12,
         "Discover or configure the AP profile used for recovery",
         "AP recovery profile was not detected or configured", None),
        ("nmcli_available", "nmcli", setup.nmcli_available, number not in {2, 4} and not udp_characterization,
         "Install nmcli or repair NetworkManager", "nmcli is not available on Jetson", None),
        ("network_manager_ready", "NetworkManager", setup.network_manager_ready,
         number not in {2, 4} and not udp_characterization, "Restore NetworkManager service",
         "NetworkManager is not active", None),
        ("iw_available", "iw", setup.iw_available, not udp_characterization,
         "Install iw on Jetson", "iw is not available on Jetson", None),
        ("wifi_phy", "Wi-Fi PHY", setup.wifi_phy, phy_required,
         "Refresh PHY discovery", "Wi-Fi PHY was not detected", None),
        ("band_24g_capable" if case.band == "2.4G" else "band_5g_capable", "Target band capability",
         setup.band_24g_capable if case.band == "2.4G" else setup.band_5g_capable,
         phy_required, "Refresh PHY capability discovery", f"PHY does not report {target_band} support", None),
        ("current_mode", "Current Wi-Fi mode", setup.current_mode, ap and number == 1,
         "Refresh runtime interface discovery", "current Wi-Fi mode was not detected", None),
        ("current_ssid", "Current SSID", setup.current_ssid, current_ssid_required,
         "Refresh runtime SSID discovery", "current SSID was not detected", None),
        ("sta_association_runtime", "DUT STA association",
         sta_association_established(case, setup) or bool(
             (setup.external_ssid_24g if case.band == "2.4G" else setup.external_ssid_5g)
             and setup.external_credential),
         not ap and number in {1, 7}, "Refresh the existing saved NetworkManager association",
         "the DUT is not associated to a detected saved Wi-Fi profile on the target band", "DERIVED"),
        ("current_band", "Current band", setup.current_band,
         band_required,
         f"Activate or discover the {target_band} runtime", f"current band is not {target_band}", "BAND_MATCH"),
        ("qualification_level", "Qualification level", qualification_level, False,
         "Runtime policy selected automatically", qualification_reason_for(case), "DERIVED"),
        ("client_association_ready", "Required Wi-Fi association", setup.client_association_ready,
         association_required, "Associate the test client to the running AP",
         "required Wi-Fi association is not ready", None),
        ("client_wifi_interface", "Laptop Wi-Fi interface", setup.client_wifi_interface,
         client_if_required, "Refresh local laptop Wi-Fi discovery",
         "Laptop Wi-Fi interface is not configured", None),
        ("jetson_ap_ip", "Jetson AP IP", setup.jetson_ap_ip, network_required or ap and number == 3,
         "Refresh or configure the Jetson AP IP", "Jetson AP IP was not detected", None),
        ("local_iperf3_available", "iperf3 local", setup.local_iperf3_available, iperf_required,
         "Install local iperf3", "iperf3 local is not ready", None),
        ("jetson_iperf3_available", "iperf3 Jetson", setup.jetson_iperf3_available, iperf_required,
         "Install Jetson iperf3", "iperf3 Jetson is not ready", None),
        ("backup_ethernet_ready", "Backup Ethernet", setup.backup_ethernet_ready, False,
         "Provide a reachable alternate Ethernet control path",
         "current SSH uses the target Wi-Fi interface and no alternate control path is ready", None),
        ("auto_reconnect", "Auto reconnect", reconnect,
         self_disruption and not setup.backup_ethernet_ready and not client_recovery,
         "Register a tested reconnect workflow for this TC",
         "Test would interrupt the active Wi-Fi SSH control path and no alternate or automatic reconnect control path is available.", "DERIVED"),
        ("control_path_safety", "Self-disruption safety", safe_control, disruptive and not client_recovery,
         "Use Ethernet control or implement a recovery workflow",
         "test would disrupt its own SSH control path", "DERIVED"),
        ("distance_1m_confirmed", "Position confirmed >=1 m", setup.distance_1m_confirmed,
         distance_1m_required, "Confirm the physical >=1 m position", ">=1 m position not confirmed", None),
        ("recorded_distance_1m", "Recorded >=1 m distance", setup.recorded_distance_1m,
         False, "Record the physical distance as evidence", ">=1 m distance not recorded", None),
        ("distance_10m_confirmed", "Position confirmed 10 m", setup.distance_10m_confirmed,
         distance_10m_required, "Confirm the physical 10 m position", "10 m position not confirmed", None),
        ("recorded_distance_10m", "Recorded 10 m distance", setup.recorded_distance_10m,
         False, "Record the physical distance as evidence", "10 m distance not recorded", None),
        ("wifi_credential", "Valid Wi-Fi credential", setup.wifi_credential, credential_required,
         "Securely provide a valid Wi-Fi credential", "valid Wi-Fi credential not configured", None),
        (ssid_key, f"External {case.band} STA SSID", getattr(setup, ssid_key), not ap,
         "Configure the external STA SSID", f"external {case.band} STA SSID is not configured", None),
        # Normal execution never asks a tester for a plaintext credential.
        # Association tests inspect the discovered active connection; security
        # tests use a temporary invalid profile and restore the detected valid
        # NetworkManager profile.
        ("external_credential", "External STA credential", setup.external_credential,
         False, "Use the detected saved NetworkManager profile", "external STA credential not configured", None),
        ("dut_active_profile", "DUT saved Wi-Fi profile", setup.dut_active_profile or setup.external_credential,
         not ap and number == 7, "Refresh the active NetworkManager profile",
         "a reusable active DUT Wi-Fi profile was not detected", None),
        ("packet_loss_threshold_percent", "Packet-loss threshold", setup.packet_loss_threshold_percent,
         number == 5, "Configure the project packet-loss threshold", "packet-loss threshold not configured", None),
        ("udp_loss_threshold_percent", "UDP-loss threshold", setup.udp_loss_threshold_percent,
         number in ({7} if ap else {6}), "Configure the project UDP-loss threshold", "UDP-loss threshold not configured", None),
        ("udp_jitter_threshold_ms", "UDP-jitter threshold", setup.udp_jitter_threshold_ms,
         number in ({7} if ap else {6}), "Configure the project UDP-jitter threshold", "UDP-jitter threshold not configured", None),
        ("udp_min_receiver_mbps", "UDP receiver threshold", setup.udp_min_receiver_mbps,
         ap and number == 7, "Configure minimum UDP receiver throughput", "UDP receiver threshold not configured", None),
        ("security_requirement", "Security requirement", setup.security_requirement,
         False, "Load an explicit project security policy when one exists",
         "no explicit project security policy is configured", None),
    ]
    if client_recovery:
        required += [
            ("allow_disruptive", "Disruptive execution authorized", setup.allow_disruptive, True,
             "Explicitly allow disruptive Wi-Fi execution", "Disruptive Wi-Fi execution has not been explicitly authorized.", None),
            ("local_nmcli_available", "Laptop NetworkManager", setup.local_nmcli_available, True,
             "Install/enable local NetworkManager", "Laptop nmcli is unavailable for client recovery.", None),
            ("saved_wifi_profile_available", "Saved valid NM profile", setup.saved_wifi_profile_available, True,
             "Save and connect a valid laptop NetworkManager profile matching the DUT SSID",
             "Saved valid Wi-Fi profile was not found. Configure/connect a valid laptop profile first.", None),
            ("valid_access_capability", "Valid Wi-Fi access capability", setup.saved_wifi_profile_available, False,
             "Use a saved valid NetworkManager profile; plaintext PSK is not needed",
             "Valid Wi-Fi access requires a reusable saved laptop NetworkManager profile.", "DERIVED"),
            ("recovery_configuration", "Recovery cycles / OFF duration", 5 <= setup.recovery_cycles <= 100 and 0 <= setup.recovery_off_seconds <= 300, True,
             "Configure 5–100 reconnect cycles (source minimum: 5) and a 0–300 second OFF interval",
             "Recovery cycle configuration is outside supported bounds.", "DERIVED"),
            ("client_recovery_control", "Phase A — Client recovery strategy", verified_management or reconnect, True,
                 "Use verified non-Wi-Fi management or register a tested local reconnect workflow",
             "Active control session uses the same Wi-Fi interface that this test will intentionally disconnect, and automatic recovery is not available.", "DERIVED"),
            ("ap_runtime", "DUT AP mode", setup.current_mode.lower() == "ap", True,
             "Run AP recovery only in AP mode", "Auth/recovery requires the DUT to be in AP mode.", "DERIVED"),
        ]
        if number == 12:
            required.append(("ap_restart_control", "Phase B — AP restart strategy", verified_management or reconnect, True,
                             "Use verified alternate management or a tested Jetson-side self-recovery workflow",
                             f"Restarting the AP would interrupt the current SSH control path over {setup.control_interface or 'the target Wi-Fi interface'} and no verified alternate management or self-recovery path is available.", "DERIVED"))
    dependencies = []
    for key, label, value, is_required, action, reason, source_override in required:
        configuration_type = None
        if key in configs:
            config = configs[key]
            label, configuration_type = config.label, config.classification
            is_required = config.classification == SOURCE_REQUIRED
            if config.source_value is not None:
                value, source_override = config.source_value, "SOURCE"
            elif config.classification == PROJECT_OPTIONAL:
                action = "Optional project limit; blank does not block execution. Qualitative acceptance needs review."
        elif key in {"recorded_distance_1m", "recorded_distance_10m"} or (ap and key == "security_requirement"):
            configuration_type = INFORMATIONAL
        if source_override == "BAND_MATCH":
            satisfied = value == target_band or can_auto_configure(case, setup)
        else:
            satisfied = value if isinstance(value, bool) else value not in (None, "", "NOT CONFIGURED")
        if key in {"wifi_credential", "external_credential"}:
            state = "CONFIGURED (hidden)" if satisfied else "NOT CONFIGURED"
        elif isinstance(value, bool):
            state = "READY" if value else "NOT READY"
        else:
            state = str(value) if value not in (None, "", "NOT CONFIGURED") else "NOT DETECTED"
        if key == "shared_ssh_ready" and satisfied:
            state = f"{setup.ssh_transport} connected"
        elif key == "auto_reconnect":
            state = "Supported for this TC" if value else "Not implemented"
        elif key == "saved_wifi_profile_available":
            state = setup.saved_wifi_profile or "Not found"
        elif key == "valid_access_capability":
            state = "Saved NetworkManager profile (no password read)" if value else "No reusable saved profile"
        elif key == "client_recovery_control":
            state = "Verified alternate management path" if verified_management else "Tested automatic recovery" if reconnect else "No safe recovery strategy"
        elif key == "qualification_level":
            state = str(value)
        elif key == "ap_restart_control":
            state = "Verified non-Wi-Fi management" if verified_management else "Tested TC-specific self-recovery workflow" if reconnect else "No alternate management / self-recovery"
        dependencies.append(AutoDependency(
            key, label, state, action, satisfied, reason,
            source_override or ("AUTO DETECTED" if key in detected_keys else "USER / CONFIG"),
            is_required,
            key == "backup_ethernet_ready" and self_disruption,
            configuration_type,
        ))
    return dependencies


def recovery_phases(case: WifiTestCase, setup: AutoSuiteSetup) -> list[dict]:
    """Visible, independent safety gates for laptop recovery and DUT AP restart."""
    if case.wifi_role != "AP" or not case.test_id.endswith(("-011", "-012")):
        return []
    dependencies = dependencies_for(case, setup)
    common = {"shared_ssh_ready", "allow_disruptive", "local_nmcli_available", "ap_runtime"}
    definitions = [("AUTH / DHCP" if case.test_id.endswith("-011") else "PHASE A — CLIENT RECOVERY",
                    common | {"client_wifi_interface", "current_ssid", "current_band", "jetson_ap_ip",
                              "saved_wifi_profile_available", "client_recovery_control"})]
    if case.test_id.endswith("-012"):
        definitions.append(("PHASE B — AP PROFILE RESTART", common | {"ap_profile", "ap_restart_control"}))
    phases = []
    for name, keys in definitions:
        selected = [dep for dep in dependencies if dep.key in keys]
        reasons = [dep.reason for dep in selected if dep.required and not dep.satisfied]
        status = "BLOCKED" if reasons else "READY_WITH_RECONNECT" if case.test_id in setup.auto_reconnect_cases and not (
            setup.alternate_control_ready or setup.backup_ethernet_ready or setup.primary_non_wifi_management) else "READY"
        phases.append({"name": name, "status": status, "reasons": reasons, "dependencies": selected})
    return phases


def readiness_status(case: WifiTestCase, setup: AutoSuiteSetup) -> str:
    if missing_prerequisites(case, setup):
        return "BLOCKED"
    if can_auto_configure(case, setup):
        return "READY_WITH_RECONFIG"
    self_disruption = (execution_safety(case) == DISRUPTIVE_CONTROL_PATH
                       and setup.control_interface == setup.jetson_wifi_interface)
    client_recovery = case.wifi_role == "AP" and case.test_id.endswith(("-011", "-012"))
    if (self_disruption or client_recovery) and not (setup.alternate_control_ready or setup.backup_ethernet_ready or setup.primary_non_wifi_management) and case.test_id in setup.auto_reconnect_cases:
        return "READY_WITH_RECONNECT"
    return "READY"


def can_auto_configure(case: WifiTestCase, setup: AutoSuiteSetup) -> bool:
    """Only AP runtime inspection can change bands without client/data rerouting.

    Traffic/recovery cases keep their existing association and route gates.
    Never advertise automatic configuration on an unverified control path.
    """
    band = "2.4 GHz" if case.band == "2.4G" else "5 GHz"
    capable = setup.band_24g_capable if case.band == "2.4G" else setup.band_5g_capable
    return bool(case.wifi_role == "AP" and case.test_id.endswith("-001")
                and setup.current_band and setup.current_band != band
                and setup.current_mode.lower() == "ap" and setup.ap_profile
                and setup.allow_disruptive and capable and setup.nmcli_available
                and (setup.primary_non_wifi_management or setup.backup_ethernet_ready))


def missing_prerequisites(case: WifiTestCase, setup: AutoSuiteSetup,
                          detected_keys: set[str] | None = None) -> list[str]:
    return [item.reason for item in dependencies_for(case, setup, detected_keys)
            if item.required and not item.satisfied]


def _evaluate(operator: str, actual, target) -> bool:
    if operator == "truthy": return bool(actual)
    if operator == "falsy": return not bool(actual)
    if operator == "zero": return actual == 0
    if operator == "eq": return actual == target
    if operator == "ieq": return str(actual).lower() == str(target).lower()
    if operator == "ge": return isinstance(actual, (int, float)) and actual >= target
    if operator == "le": return isinstance(actual, (int, float)) and target is not None and actual <= target
    if operator == "positive": return isinstance(actual, (int, float)) and actual > target
    if operator == "number": return isinstance(actual, (int, float))
    if operator == "valid_ip":
        try:
            ipaddress.ip_interface(str(actual))
            return not target or target is True or ipaddress.ip_address(str(actual).split("/", 1)[0]) == ipaddress.ip_address(str(target).split("/", 1)[0])
        except ValueError:
            return False
    raise ValueError(f"Unsupported AUTO evaluator: {operator}")


def evaluate_auto_case(case: WifiTestCase, setup: AutoSuiteSetup, measurements: dict,
                       infrastructure_error: str | None = None) -> tuple[list[dict], str, str]:
    missing = missing_prerequisites(case, setup)
    specs = criteria_for_auto(case, setup)
    if missing:
        rows = [{**asdict(spec), "actual": None, "status": "BLOCKED", "reason": "Missing prerequisite"}
                for spec in specs]
        return rows, "BLOCKED", "Missing setup: " + ", ".join(missing)
    if infrastructure_error:
        rows = [{**asdict(spec), "actual": measurements.get(spec.metric), "status": "ERROR",
                 "reason": infrastructure_error} for spec in specs]
        return rows, "ERROR", infrastructure_error
    rows = []
    from .disconnects import disconnect_measurement_error
    measurement_error = measurements.get("Disconnect collector error") or disconnect_measurement_error(measurements)
    ssh_errors = measurements.get("SSH criterion errors", {})
    criterion_errors = measurements.get("Criterion measurement errors", {})
    not_measured = set(measurements.get("Not measured metrics", ()))
    for spec in specs:
        actual = measurements.get(spec.metric)
        if spec.metric in criterion_errors:
            status, reason = "ERROR", criterion_errors[spec.metric]
        elif spec.metric == "Disconnects" and measurement_error:
            status, reason = "MEASUREMENT ERROR", measurement_error
        elif spec.metric in ssh_errors:
            status, reason = ssh_errors[spec.metric]["status"], ssh_errors[spec.metric]["reason"]
        elif spec.metric in {"SSH authentication", "Remote command execution", "Host identity valid"} and ssh_errors:
            status, reason = "NOT_COLLECTED", "Dependent SSH check not run: " + "; ".join(error["reason"] for error in ssh_errors.values())
        elif spec.metric == "Disconnects" and spec.source.startswith("Source qualitative prolonged-loss") and isinstance(actual, int) and actual > 0:
            status, reason = "NEEDS REVIEW", "Transient disconnects observed; source requires no prolonged loss but defines no duration limit. Review timestamped event evidence."
        elif spec.operator == "setup_confirmation":
            actual = bool(getattr(setup, spec.target))
            status, reason = ("PASS", "Physical position confirmed in Pre-Test") if actual else ("FAIL", "Physical position not confirmed")
        elif spec.operator == "capture":
            blocked_capture = [name for name in spec.target if name in not_measured]
            absent = [name for name in spec.target if name not in not_measured
                      and not isinstance(measurements.get(name), (int, float))]
            if absent:
                actual = "Missing: " + ", ".join(absent)
                status, reason = "ERROR", actual
            elif blocked_capture:
                actual = "Not measured: " + ", ".join(blocked_capture)
                status, reason = "NOT MEASURED", measurements.get(
                    "Measurement blocked reason", actual)
            else:
                actual = reason = "All requested measurements recorded"
                status = "PASS"
        elif not spec.required:
            status, reason = ("MEASURED", "Observed runtime value; no expected configuration") if actual is not None else ("NOT_COLLECTED", "No optional runtime measurement")
        elif spec.metric in not_measured:
            status, reason = "NOT MEASURED", measurements.get(
                "Measurement blocked reason", "Required measurement did not execute")
        elif spec.metric not in measurements:
            status, reason = "ERROR", "Required collector did not produce a measurement"
        elif spec.operator == "measured":
            if not isinstance(actual, (int, float)):
                status, reason = "ERROR", "Collector did not produce a numeric measurement"
            else:
                status = "MEASURED"
                reason = "Source acceptance is qualitative; no numeric limit is configured" if spec.qualitative else "Recorded without an acceptance threshold"
        else:
            passed = spec.expected_rule.matches(actual, measurements, asdict(setup)) if spec.expected_rule and spec.expected_rule.kind != "informational" else _evaluate(spec.operator, actual, spec.target)
            status, reason = ("PASS", "Criterion met") if passed else ("FAIL", f"{spec.name}: expected {spec.expected}; measured {actual}")
        rows.append({**asdict(spec), "actual": actual, "status": status, "reason": reason})
    required_rows = [row for row in rows if row["required"] and row["importance"] == CRITICAL]
    if measurement_error and any(row["metric"] == "Disconnects" for row in required_rows):
        return rows, "MEASUREMENT ERROR", measurement_error
    if any(row["status"] == "ERROR" for row in required_rows):
        return rows, "ERROR", "; ".join(row["reason"] for row in required_rows if row["status"] == "ERROR")
    failed = [row for row in required_rows if row["status"] == "FAIL"]
    if failed:
        return rows, "FAIL", "; ".join(row["reason"] for row in failed)
    unmeasured = [row for row in required_rows if row["status"] == "NOT MEASURED"]
    if unmeasured:
        return rows, "FAIL", "Required measurement not executed: " + ", ".join(row["name"] for row in unmeasured)
    if any(row["qualitative"] or row["status"] == "NEEDS REVIEW" for row in required_rows):
        return rows, "NEEDS REVIEW", "Review qualitative source acceptance: " + ", ".join(row["name"] for row in required_rows if row["qualitative"] or row["status"] == "NEEDS REVIEW")
    passed = sum(row["status"] == "PASS" for row in required_rows)
    measured = sum(row["status"] == "MEASURED" for row in rows)
    return rows, "PASS", f"All {passed} critical source/project criteria passed" + (f"; {measured} measurements recorded" if measured else "")


@dataclass
class QueueItem:
    case: WifiTestCase
    status: str = "WAITING"
    reason: str = ""
    result: dict = field(default_factory=dict)


class AutoSuiteBatchRunner:
    """Sequential, dependency-aware runner; exactly one case executes at a time."""
    def __init__(self, setup: AutoSuiteSetup, executor: Callable[[WifiTestCase], dict],
                 refresh: Callable[[], AutoSuiteSetup] | None = None):
        self.setup, self.executor = setup, executor
        self.refresh = refresh
        self.queue: list[QueueItem] = []
        self.running = False

    def enqueue(self, cases: Iterable[WifiTestCase]) -> list[QueueItem]:
        if self.running:
            raise RuntimeError("A Wi-Fi batch is already running")
        self.queue = [QueueItem(case) for case in cases]
        return self.queue

    def run(self) -> list[QueueItem]:
        if self.running:
            raise RuntimeError("A Wi-Fi batch is already running")
        self.running = True
        try:
            if self.refresh:
                self.setup = self.refresh()
            eligible = []
            for item in self.queue:
                if readiness_status(item.case, self.setup) in {"READY", "READY_WITH_RECONFIG"}:
                    eligible.append(item)
                else:
                    item.status = "SKIPPED"
                    item.reason = "; ".join(missing_prerequisites(item.case, self.setup)) or readiness_status(item.case, self.setup)
            for item in tuple(eligible):
                if self.refresh:
                    self.setup = self.refresh()
                if readiness_status(item.case, self.setup) not in {"READY", "READY_WITH_RECONFIG"}:
                    item.status = "SKIPPED"
                    item.reason = "; ".join(missing_prerequisites(item.case, self.setup)) or readiness_status(item.case, self.setup)
                    continue
                item.status = "RUNNING"
                try:
                    measurements = self.executor(item.case)
                    rows, status, reason = evaluate_auto_case(item.case, self.setup, measurements)
                except Exception:  # infrastructure failures never poison other tests
                    rows, status, reason = evaluate_auto_case(item.case, self.setup, {}, "Execution infrastructure failed")
                item.status, item.reason = status, reason
                item.result = {"criteria": rows, "status": status}
        finally:
            self.running = False
        return self.queue
