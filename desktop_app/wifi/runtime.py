"""Wi-Fi attempts and evidence, independent of other device domains."""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from core.remote.ssh_manager import CommandResult

from .catalog import WifiTestCase, load_config
from .auto_suite import (AutoSuiteSetup, criteria_for_auto, dependencies_for,
                         evaluate_auto_case, missing_prerequisites, readiness_status,
                         execution_safety, DISRUPTIVE_CONTROL_PATH,
                         qualification_level_for, qualification_reason_for)
from .auto_suite import sta_association_established, can_auto_configure
from .auto_suite import recovery_phases
from .parsers import (parse_iperf3, parse_ping, parse_recovery, parse_rf, recovery_metrics,
                      parse_nmcli_device_status, parse_nmcli_device_show, parse_driver,
                      parse_kernel_device_errors, parse_ip_link)
from .parsers import (parse_iw_interface_role, parse_nmcli_profile_list, parse_service_active,
                      parse_iw_phy_capabilities, parse_nmcli_profile_show)
from .evaluation import evaluate
from .qualification import discover_laptop_wifi, nm_fields


EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / "evidence" / "wifi"


def _command_output(results: dict, name: str) -> str:
    result = results.get(name)
    return str(getattr(result, "stdout", "") or "")


def _command_ok(results: dict, name: str) -> bool:
    result = results.get(name)
    return bool(result is not None and getattr(result, "exit_status", 1) == 0)


def parse_auto_discovery(results: dict) -> dict[str, object]:
    """Build one runtime snapshot from commands executed by the shared SSH service."""
    health = _command_output(results, "health")
    values: dict[str, object] = {
        "shared_ssh_ready": _command_ok(results, "health") and "__WIFI_REMOTE_READY__" in health,
        "dashboard_jetson_connected": _command_ok(results, "health") and "__WIFI_REMOTE_READY__" in health,
        "nmcli_available": _command_ok(results, "tools") and "nmcli=" in _command_output(results, "tools"),
        "iw_available": _command_ok(results, "tools") and "iw=" in _command_output(results, "tools"),
        "jetson_iperf3_available": _command_ok(results, "tools") and "iperf3=" in _command_output(results, "tools"),
        "network_manager_ready": _command_ok(results, "network_manager") and
                                 _command_output(results, "network_manager").strip() == "active",
        "network_manager_state": _command_output(results, "network_manager").strip(),
        "dut_hostname": _command_output(results, "hostname").strip(),
    }
    connection = re.search(r"(?:^|\n)(\d+(?:\.\d+){3})\s+\d+\s+(\d+(?:\.\d+){3})\s+\d+(?:\n|$)", health)
    client_ip, server_ip = (connection.group(1), connection.group(2)) if connection else ("", "")
    route_back = _command_output(results, "control_route")
    route_if = re.search(r"\bdev\s+(\S+)", route_back)
    route_src = re.search(r"\bsrc\s+(\d+(?:\.\d+){3})", route_back)
    control_interface = route_if.group(1) if route_if else ""
    values.update({
        "control_client_ip": client_ip,
        "control_jetson_ip": server_ip or (route_src.group(1) if route_src else ""),
        "control_interface": control_interface,
    })

    device_rows = []
    for line in _command_output(results, "devices").splitlines():
        parts = nm_fields(line)
        if len(parts) == 4:
            device_rows.append(tuple(parts))
    type_by_interface = {row[0]: row[1].lower() for row in device_rows}
    # SSH's actual local endpoint identifies the DUT control interface even
    # when its return routing is asymmetric or another address is preferred.
    assigned = next((line.split()[0] for line in _command_output(results, "addresses").splitlines()
                     if line.split() and server_ip and re.search(r"(?<![\d.])" + re.escape(server_ip) + r"/\d+", line)), "")
    if assigned:
        control_interface = assigned
        values["control_interface"] = assigned
    control_type = type_by_interface.get(control_interface, "")
    if control_type == "wifi" or control_interface.startswith(("wl", "wlan")):
        transport = "Wi-Fi"
    elif control_type == "ethernet" or control_interface.startswith(("en", "eth")):
        transport = "Ethernet"
    else:
        transport = "Unknown"
    values["ssh_transport"] = transport
    values["management_transport"] = transport
    values["management_route_dev"] = control_interface

    wifi_rows = [row for row in device_rows if row[1].lower() == "wifi"]
    connected_wifi = next((row for row in wifi_rows if row[0] == control_interface), None) or next(
        (row for row in wifi_rows if row[2].lower().startswith("connected")), None)
    wifi_interface = (connected_wifi or (wifi_rows[0] if wifi_rows else ("", "", "", "")))[0]
    if not wifi_interface:
        match = re.search(r"^\s*Interface\s+(\S+)", _command_output(results, "iw_dev"), re.MULTILINE)
        wifi_interface = match.group(1) if match else ""
    values["jetson_wifi_interface"] = wifi_interface
    if connected_wifi:
        values["wifi_state"] = connected_wifi[2]

    iw_info = _command_output(results, "wifi_info")
    iw_link = _command_output(results, "wifi_link")
    station = _command_output(results, "wifi_station")
    mode = re.search(r"^\s*type\s+(\S+)", iw_info, re.MULTILINE)
    if connected_wifi and mode and mode.group(1).lower() == "ap":
        values["ap_profile"] = connected_wifi[3]
    values["dut_active_profile"] = connected_wifi[3] if connected_wifi else ""
    values["dut_gateway"] = _command_output(results, "wifi_gateway").strip()
    ssid = re.search(r"^\s*ssid\s+(.+)$", iw_info, re.MULTILINE | re.I)
    if not ssid:
        ssid = re.search(r"^\s*SSID:\s*(.+)$", iw_link, re.MULTILINE | re.I)
    bssid = re.search(r"Connected to\s+([0-9a-f:]{17})", iw_link, re.I)
    channel = re.search(r"\bchannel\s+(\d+)\s*\((\d+)\s*MHz\)(?:,\s*width:\s*([^\n,]+))?", iw_info, re.I)
    frequency = int(channel.group(2)) if channel else None
    if frequency is None:
        link_frequency = re.search(r"\bfreq:\s*(\d+)", iw_link)
        frequency = int(link_frequency.group(1)) if link_frequency else None
    band = "2.4 GHz" if frequency and 2412 <= frequency <= 2484 else "5 GHz" if frequency and 5000 <= frequency < 6000 else ""
    values.update({
        "current_mode": mode.group(1) if mode else "",
        "current_ssid": ssid.group(1).strip() if ssid else "",
        "current_bssid": bssid.group(1) if bssid else next(iter(re.findall(r"^\s*addr\s+([0-9a-f:]{17})", iw_info, re.M | re.I)), ""),
        "current_channel": int(channel.group(1)) if channel else None,
        "current_frequency_mhz": frequency,
        "current_channel_width": channel.group(3).strip() if channel and channel.group(3) else "",
        "current_band": band,
        "client_association_ready": bool(station.strip() or "Connected to" in iw_link),
    })
    if mode and mode.group(1).lower() in {"managed", "station"} and values["current_ssid"] and band:
        values["external_ssid_24g" if band == "2.4 GHz" else "external_ssid_5g"] = values["current_ssid"]

    address_rows = _command_output(results, "addresses").splitlines()
    wifi_address = next((line for line in address_rows if wifi_interface and line.split(maxsplit=1)[0] == wifi_interface), "")
    ipv4 = re.search(r"\b(\d+(?:\.\d+){3}/\d+)\b", wifi_address)
    values["current_ipv4"] = ipv4.group(1) if ipv4 else ""
    if ipv4:
        values["jetson_ap_ip"] = ipv4.group(1).split("/", 1)[0]
    else:
        values["jetson_ap_ip"] = ""
    route_text = _command_output(results, "routes")
    values["current_route"] = route_text.strip()
    values["network_path_ready"] = bool(_command_ok(results, "control_route") and control_interface)

    driver = _command_output(results, "wifi_driver").strip()
    if not driver:
        driver_match = re.search(r"^driver:\s*(\S+)", _command_output(results, "wifi_ethtool"), re.MULTILINE)
        driver = driver_match.group(1) if driver_match else ""
    phy_text = _command_output(results, "phy")
    phy_name = re.search(r"^Wiphy\s+(\S+)", phy_text, re.MULTILINE)
    values.update({
        "wifi_driver": driver,
        "wifi_phy": phy_name.group(1) if phy_name else "",
        "ap_capable": bool(re.search(r"^\s*\*\s+AP\s*$", phy_text, re.MULTILINE)),
        "band_24g_capable": bool(re.search(r"24\d{2}\s*MHz", phy_text)),
        "band_5g_capable": bool(re.search(r"5\d{3}\s*MHz", phy_text)),
        "he_capable": bool(re.search(r"\bHE (?:Iftypes|MAC|PHY|MCS)|802\.11ax", phy_text, re.I)),
    })
    ethernet_with_ip = []
    for interface, device_type, state, _profile in device_rows:
        if device_type.lower() != "ethernet" or not state.lower().startswith("connected"):
            continue
        row = next((line for line in address_rows if line.split(maxsplit=1)[0] == interface), "")
        if re.search(r"\b\d+(?:\.\d+){3}/\d+\b", row):
            ethernet_with_ip.append(interface)
    # An existing Ethernet control session is proven usable. A merely present
    # Ethernet address is displayed but is not promoted to a control path.
    detected_ethernet = [row[0] for row in device_rows if row[1].lower() == "ethernet"]
    verified = next((interface for interface in ethernet_with_ip
                     if _command_ok(results, f"ethernet_verified_{interface}")), "")
    active_ethernet = transport == "Ethernet" and values["shared_ssh_ready"]
    values["detected_ethernet_interfaces"] = ", ".join(detected_ethernet)
    values["backup_ethernet_ready"] = bool(active_ethernet or verified)
    values["backup_ethernet_interface"] = control_interface if active_ethernet else verified
    values["backup_ethernet_ip"] = values["control_jetson_ip"] if active_ethernet else _command_output(results, f"ethernet_verified_{verified}").strip()
    values["backup_ethernet_reason"] = (
        "Active authenticated Ethernet SSH control" if active_ethernet else
        "Authenticated alternate SSH path verified from laptop" if verified else
        "No verified reachable control path")
    values["alternate_control_ready"] = values["backup_ethernet_ready"]
    if _command_ok(results, "laptop_qualification"):
        laptop = json.loads(_command_output(results, "laptop_qualification"))
        values.update({key: value for key, value in laptop.items() if key in AutoSuiteSetup.__dataclass_fields__})
    return values


def parse_local_wifi_discovery(outputs: dict[str, str]) -> str:
    """Prefer the routed/connected Wi-Fi adapter; never infer one from its name."""
    rows = [line.split(":", 3) for line in outputs.get("devices", "").splitlines()]
    wifi = [row for row in rows if len(row) == 4 and row[1] == "wifi"]
    iw_interfaces = re.findall(r"^\s*Interface\s+(\S+)", outputs.get("iw", ""), re.MULTILINE)
    candidates = list(dict.fromkeys([row[0] for row in wifi] + iw_interfaces))
    routed = re.findall(r"\bdev\s+(\S+)", outputs.get("routes", ""))
    connected = [row[0] for row in wifi if row[2].startswith("connected")]
    return next((name for name in connected if name in routed), None) or (
        connected[0] if connected else candidates[0] if len(candidates) == 1 else "")


def metric_kind(case: WifiTestCase) -> str:
    if case.suite == "AUTO":
        category = case.category.lower()
        if "latency" in category: return "latency"
        if "throughput" in category or category == "udp": return "throughput"
        if "rssi" in category or "rf" in category or "standard" in category or "phy" in category: return "rf"
        if "recovery" in category or "auth" in category: return "recovery"
        if "endurance" in category or "stability" in category: return "endurance"
        return "interface"
    number = int(case.test_id.rsplit("C", 1)[1])
    if number <= 4: return "baseline"
    if number == 5: return "activation"
    if number == 6: return "client"
    if number == 7: return "authentication"
    if number == 9: return "security_exposure"
    if number == 10: return "latency"
    if number == 11: return "rf"
    if number == 12: return "throughput"
    if number == 13: return "recovery"
    if number == 14: return "ap_recovery"
    if number == 15: return "boot"
    if number == 16: return "endurance"
    if number <= 20: return "system"
    if number <= 22: return "band"
    if number == 23: return "band_throughput"
    if number == 24: return "wifi6"
    if number == 25: return "range"
    if number == 26: return "latency"
    if number == 27: return "security_matrix"
    raise ValueError(f"Unknown Wi-Fi test type: {case.test_id}")


METRIC_FIELDS = {
    "baseline": ("Interface", "Driver/PHY", "NetworkManager", "Hotspot profile"),
    "activation": ("AP state", "SSID", "Band", "Activation elapsed"),
    "client": ("Authentication state", "Assigned IPv4", "DHCP state", "Connection elapsed"),
    "security_exposure": ("Security", "Secret exposure", "Service exposure", "Warnings"),
    "ap_recovery": ("Recovery step", "AP state", "Client recovered", "Recovery elapsed"),
    "boot": ("Completed cycles", "Average ready time", "Max ready time", "Failures"),
    "system": ("Wi-Fi AP", "Ethernet route", "NetworkManager", "Errors"),
    "band": ("Band", "Channel", "Frequency", "RSSI"),
    "band_throughput": ("2.4 GHz Average", "5 GHz Average", "Requirement", "Retransmits"),
    "wifi6": ("Jetson HE", "Client HE", "Runtime mode", "HE-MCS"),
    "range": ("Distance", "SSID visible", "RSSI", "Disconnects"),
    "security_matrix": ("WPA2", "WPA3", "Key management", "Production restored"),
    "throughput": ("RSSI", "Upload", "Download", "Retransmits"),
    "latency": ("RTT", "Average RTT", "Jitter", "Packet loss"),
    "rf": ("SSID", "BSSID", "Channel", "RSSI"),
    "dhcp": ("Client IP", "Gateway", "DNS", "DHCP time"),
    "authentication": ("Attempts", "Successful", "Rejected", "Auth time"),
    "recovery": ("Completed cycles", "Successful cycles", "Avg recovery", "Failures"),
    "endurance": ("Elapsed", "Disconnects", "Min RSSI", "Errors"),
    "interface": ("Interface", "State", "Address", "RSSI"),
}


def parse_remote_snapshot(raw: str) -> dict[str, str | float]:
    """Extract presentation metrics; raw output remains untouched on disk."""
    metrics: dict[str, str | float] = {}
    for line in raw.splitlines():
        if line.startswith("GENERAL.DEVICE:"):
            metrics["Interface"] = line.split(":", 1)[1].strip()
        elif line.startswith("GENERAL.STATE:"):
            metrics["State"] = line.split(":", 1)[1].strip()
            metrics["Address state"] = metrics["State"]
        elif line.startswith("GENERAL.CONNECTION:"):
            metrics["SSID"] = line.split(":", 1)[1].strip()
        elif line.startswith("IP4.ADDRESS"):
            metrics["Address"] = line.split(":", 1)[1].strip()
            metrics["Client IP"] = metrics["Address"]
            if "/" in metrics["Address"]:
                metrics["Client IP"], metrics["Prefix"] = metrics["Address"].rsplit("/", 1)
        elif line.startswith("IP4.GATEWAY:"):
            metrics["Gateway"] = line.split(":", 1)[1].strip()
        elif line.startswith("IP4.DNS"):
            metrics["DNS"] = line.split(":", 1)[1].strip()
        elif match := re.search(r"\bsignal:\s*(-?\d+)\s*dBm", line):
            metrics["RSSI"] = float(match.group(1))
        elif match := re.search(r"\bfreq:\s*(\d+)", line):
            metrics["Frequency"] = f"{match.group(1)} MHz"
        elif match := re.search(r"\bSSID:\s*(.+)", line):
            metrics["SSID"] = match.group(1).strip()
    return metrics


@dataclass
class WifiAttempt:
    environment: str
    case: WifiTestCase
    number: int
    directory: Path
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "RUNNING"
    auto_result: str = "NOT EVALUATED"
    final_result: str = "NOT EVALUATED"
    result_reason: str = ""
    criteria: list[dict] = field(default_factory=list)
    evidence: dict[str, dict] = field(default_factory=dict)
    override_by: str | None = None
    override_reason: str | None = None
    override_timestamp: str | None = None
    metrics: dict[str, str | float] = field(default_factory=dict)
    raw_log: str = ""
    comment: str = ""
    trends: dict[str, deque] = field(default_factory=dict)
    manual_reviews: dict[str, dict] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    phase: str = "PREPARING"
    phase_detail: str = "Preparing collector"
    progress_done: int = 0
    progress_total: int = 0
    finished: datetime | None = None

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int(((self.finished or datetime.now(timezone.utc)) - self.started).total_seconds()))

    def add_event(self, level, message, timestamp=None):
        self.events.append({"timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
                            "level": level, "message": message})

    def update_metrics(self, values: dict) -> None:
        for key, label in (("Interface", "Detected interface"), ("Mode", "Wi-Fi mode"),
                           ("Role", "Wi-Fi role"), ("Band", "Current band"), ("SSID", "Current SSID")):
            if key in values and values[key] != self.metrics.get(key):
                self.add_event("INFO", f"{label}: {values[key]}")
        self.metrics.update(values)
        for key, value in values.items():
            if isinstance(value, (float, int)):
                self.trends.setdefault(key, deque(maxlen=120)).append((self.elapsed_seconds, value))


class WifiRuntime(QObject):
    local_discovery_ready = Signal(object)
    changed = Signal()
    message = Signal(str)
    auto_attempt_started = Signal(object)
    preflight_blocked = Signal(object)
    collector_update = Signal(object)
    auto_discovery_completed = Signal(bool, str)
    auto_attempt_completed = Signal(object)
    batch_phase_update = Signal(str)

    def __init__(self, jetson_service, evidence_root: Path = EVIDENCE_ROOT, parent=None,
                 auto_collector=None):
        super().__init__(parent)
        self.services = {"Jetson": jetson_service}
        self.evidence_root = Path(evidence_root)
        self.session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.active: WifiAttempt | None = None

        # Current-session AUTO results are intentionally independent from
        # historical attempt files. Historical result.json/summary.json files
        # remain authoritative for History/Evidence only.
        self._current_session_results: dict[tuple[str, str], str] = {}

        self.auto_setup_path = self.evidence_root / "auto_suite_setup.json"
        self.auto_setup = AutoSuiteSetup.load(self.auto_setup_path)
        self.auto_detected: dict[str, object] = {}
        self._local_discovery_running = False
        self.local_discovery_ready.connect(self._local_discovery_finished)
        self._reconnect_workflows: dict = {}
        self._active_auto_setup: AutoSuiteSetup | None = None
        self.auto_collector = auto_collector
        self._auto_cases_by_id: dict[str, WifiTestCase] | None = None
        self._request_id: str | None = None
        self._discovery_request_id: str | None = None
        self._last_discovery_success = 0.0
        self._pending_auto_start: tuple[str, WifiTestCase] | None = None
        self._sta_join_request_id: str | None = None
        self._collector_task = None
        self._collector_loop = None
        self._stop_requested = threading.Event()
        self.batch_runner = None
        self._batch_setup_overrides = {}
        self._batch_control_lost = threading.Event()
        self._batch_execution_error = ""
        self.collector_update.connect(self._collector_updated)
        self.batch_phase_update.connect(self._batch_phase_changed)
        for service in set(filter(None, self.services.values())):
            service.operation_succeeded.connect(self._operation_succeeded)
            service.operation_failed.connect(self._operation_failed)
            if hasattr(service, "connected"):
                service.connected.connect(lambda *_args: self.refresh_auto_discovery())
            if hasattr(service, "disconnected"):
                service.disconnected.connect(self._shared_ssh_disconnected)

    def _case_root(self, environment: str, case: WifiTestCase | None = None,
                   test_id: str = "") -> Path:
        if case and case.suite == "AUTO" or test_id.startswith("TC-JET-"):
            group = (case.catalog_group if case else
                     ("STA_" if "-STA-" in test_id else "AP_") +
                     ("24G" if "24G" in test_id else "5G"))
            return self.evidence_root / environment / "auto" / group
        return self.evidence_root / environment / "existing"

    def attempts(self, environment: str, test_id: str) -> list[Path]:
        root = self._case_root(environment, test_id=test_id)
        if not root.is_dir():
            return []
        paths = []
        for directory in sorted(root.glob(f"*/{test_id}/attempt_*")):
            result = directory / "result.json"
            summary = directory / "summary.json"
            if result.is_file():
                paths.append(result)
            elif summary.is_file():
                paths.append(summary)
        return paths

    @property
    def capture_pending(self) -> bool:
        return self._request_id is not None

    def _service_connected(self) -> bool:
        service = self.services.get("Jetson")
        state = getattr(service, "is_connected", False) if service else False
        return bool(state() if callable(state) else state)

    @property
    def discovery_pending(self) -> bool:
        return self._discovery_request_id is not None

    def _shared_ssh_disconnected(self, *_args) -> None:
        self._last_discovery_success = 0.0
        self.update_detected_auto_setup(
            dashboard_jetson_connected=False, shared_ssh_ready=False,
            control_interface="", control_jetson_ip="", control_client_ip="",
            ssh_transport="Unknown", network_path_ready=False,
            backup_ethernet_ready=False, backup_ethernet_interface="",
            backup_ethernet_ip="", backup_ethernet_reason="No verified reachable control path",
            nmcli_available=False, iw_available=False,
            network_manager_ready=False, network_manager_state="",
            jetson_iperf3_available=False, jetson_wifi_interface="",
            wifi_state="", current_ssid="", current_bssid="", current_mode="",
            current_band="", current_channel=None, current_frequency_mhz=None,
            current_channel_width="", current_ipv4="", current_route="",
            wifi_driver="", wifi_phy="", ap_capable=False,
            band_24g_capable=False, band_5g_capable=False, he_capable=False,
            client_association_ready=False,
            laptop_to_dut_wifi=False, dut_to_laptop_wifi=False, full_wifi_qual_path=False,
            forward_route_dev="", forward_route_src="", forward_route_via="",
            forward_wifi_ok=False, reverse_route_dev="", reverse_route_src="",
            reverse_wifi_ok=False, management_transport="Unknown",
            management_route_dev="", alternate_control_ready=False,
            auto_recovery_ready=False,
            primary_non_wifi_management=False, control_path="OTHER_CONTROL",
            saved_wifi_profile_available=False, saved_wifi_profile="", saved_wifi_profile_uuid="",
            valid_access_source="NOT AVAILABLE", dut_active_profile="", dut_gateway="",
            dut_hostname="", laptop_bssid="", laptop_channel=None,
            laptop_frequency_mhz=None, laptop_gateway="",
        )

    def refresh_auto_discovery(self) -> str | None:
        """Probe runtime state through the one existing Dashboard SSH session."""
        self.refresh_local_wifi_discovery()
        if self._discovery_request_id:
            return self._discovery_request_id
        service = self.services.get("Jetson")
        self.update_detected_auto_setup(local_iperf3_available=bool(shutil.which("iperf3")))
        if service is None or not self._service_connected():
            self._shared_ssh_disconnected()
            return None

        async def discover(ssh):
            if self.batch and self.batch.running:
                from .batch_network import RecoveringSSH
                ssh = RecoveringSSH(ssh, self, "batch-discovery")
            results = {}

            async def run(name: str, command: str, timeout: int = 15):
                try:
                    results[name] = await ssh.run(command, timeout=timeout)
                except Exception as error:
                    results[name] = CommandResult(command, "", str(error), -1)

            await run("health", "printf '__WIFI_REMOTE_READY__\\n%s\\n' \"$SSH_CONNECTION\"")
            connection = re.search(
                r"(?:^|\n)(\d+(?:\.\d+){3})\s+\d+\s+\d+(?:\.\d+){3}\s+\d+(?:\n|$)",
                _command_output(results, "health"),
            )
            client_ip = connection.group(1) if connection else ""
            await run("control_route", f"ip route get {shlex.quote(client_ip)}" if client_ip else "ip route get 1.1.1.1")
            commands = {
                "devices": "nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status",
                "iw_dev": "iw dev", "addresses": "ip -4 -br addr", "routes": "ip route",
                "active_profiles": "nmcli -t -f NAME,TYPE,DEVICE connection show --active",
                "network_manager": "systemctl is-active NetworkManager",
                "tools": "for c in nmcli iw iperf3; do p=$(command -v \"$c\" 2>/dev/null) && printf '%s=%s\\n' \"$c\" \"$p\"; done; true",
                "phy": "iw phy",
                "hostname": "hostnamectl --static 2>/dev/null || hostname",
            }
            for name, command in commands.items():
                await run(name, command)
            # Presence, link and address alone cannot prove alternate control.
            # Verify both routes and authenticated SSH identity from the laptop.
            from .auto_collectors import _local
            await run("host_identity", "cat /etc/machine-id")
            identity = _command_output(results, "host_identity").strip()
            username = getattr(getattr(ssh, "config", None), "username", self.auto_setup.jetson_user)
            port = getattr(getattr(ssh, "config", None), "port", 22)
            addresses = _command_output(results, "addresses").splitlines()
            for row in _command_output(results, "devices").splitlines():
                fields = row.split(":", 3)
                if len(fields) != 4 or fields[1] != "ethernet" or not fields[2].startswith("connected"):
                    continue
                interface = fields[0]
                address_row = next((line for line in addresses if line.split() and line.split()[0] == interface), "")
                address = re.search(r"\b(\d+(?:\.\d+){3})/\d+", address_row)
                if not address or not client_ip or not username or not identity:
                    continue
                await run(f"ethernet_route_{interface}", f"ip route get {shlex.quote(client_ip)} oif {shlex.quote(interface)}")
                if not _command_ok(results, f"ethernet_route_{interface}"):
                    continue
                route, _, code = await _local(f"ip route get {shlex.quote(address.group(1))}", 5)
                local_device = re.search(r"\bdev\s+(\S+)", route)
                devices, _, _ = await _local("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", 5)
                if code != 0 or not local_device or not any(
                    line.startswith(local_device.group(1) + ":ethernet:connected:") for line in devices.splitlines()
                ):
                    continue
                probe = (f"ssh -o BatchMode=yes -o ConnectTimeout=5 -o ConnectionAttempts=1 -p {int(port)} "
                         f"{shlex.quote(username + '@' + address.group(1))} 'cat /etc/machine-id'")
                host, _, code = await _local(probe, 8)
                if code == 0 and host.strip() == identity:
                    results[f"ethernet_verified_{interface}"] = CommandResult(probe, address.group(1), "", 0)
            wifi_interface = parse_auto_discovery(results)["jetson_wifi_interface"]
            if wifi_interface:
                quoted = shlex.quote(wifi_interface)
                dynamic = {
                    "wifi_device": f"nmcli -g GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION device show {quoted}",
                    "wifi_info": f"iw dev {quoted} info", "wifi_link": f"iw dev {quoted} link",
                    "wifi_station": f"iw dev {quoted} station dump",
                    "wifi_gateway": f"nmcli -g IP4.GATEWAY device show {quoted}",
                    "wifi_driver": f"readlink -f /sys/class/net/{quoted}/device/driver",
                    "wifi_ethtool": f"ethtool -i {quoted} 2>/dev/null",
                }
                for name, command in dynamic.items():
                    await run(name, command)
            snapshot = parse_auto_discovery(results)

            async def remote_path(command, timeout):
                await run("wifi_return_path", command, timeout)
                result = results["wifi_return_path"]
                return result.stdout, result.stderr, result.exit_status

            laptop = await discover_laptop_wifi(snapshot, _local, remote_path)
            # Embed only parsed, non-secret fields in the same atomic discovery
            # snapshot; an independent laptop probe cannot overwrite route proof.
            if (snapshot.get("ssh_transport") == "Ethernet" and not laptop["primary_non_wifi_management"]
                    and not any(key.startswith("ethernet_verified_") for key in results)):
                laptop.update(backup_ethernet_ready=False, backup_ethernet_reason="Primary Ethernet path is not proven from the laptop")
            results["laptop_qualification"] = CommandResult("read-only Wi-Fi qualification discovery",
                                                           json.dumps(laptop), "", 0)
            return results

        request_id = service.submit_operation("wifi-auto-discovery", discover)
        if request_id:
            self._discovery_request_id = request_id
        else:
            self._shared_ssh_disconnected()
        return request_id

    def refresh_local_wifi_discovery(self) -> None:
        """Run local discovery without blocking Qt or depending on robot SSH."""
        if self._local_discovery_running:
            return
        self._local_discovery_running = True
        commands = {"devices": ("nmcli", ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"]),
                    "iw": ("iw", ["dev"]), "addresses": ("ip", ["-4", "-br", "addr"]),
                    "routes": ("ip", ["route"])}
        ready_signal = self.local_discovery_ready

        def inspect():
            outputs = {}
            for key, (program, arguments) in commands.items():
                try:
                    completed = subprocess.run([program, *arguments], capture_output=True,
                                               text=True, timeout=3, check=False)
                    outputs[key] = completed.stdout if completed.returncode == 0 else ""
                except (OSError, subprocess.TimeoutExpired):
                    outputs[key] = ""
            try:
                ready_signal.emit(outputs)
            except RuntimeError:
                pass  # The page/runtime may have been closed during discovery.

        threading.Thread(target=inspect, name="wifi-local-discovery", daemon=True).start()

    def _local_discovery_finished(self, outputs: dict) -> None:
        self._local_discovery_running = False
        interface = parse_local_wifi_discovery(outputs)
        if self.auto_detected.get("full_wifi_qual_path"):
            # The atomic robot/laptop snapshot selected the route's adapter;
            # don't replace it with a less-specific parallel device-only probe.
            return
        if interface or "client_wifi_interface" in self.auto_detected:
            self.update_detected_auto_setup(client_wifi_interface=interface)

    def register_reconnect_workflow(self, test_id: str, collector) -> None:
        """Advertise support only together with a TC-specific executable workflow.

        The workflow receives the existing shared SSH manager and owns safe
        recovery and subsequent evidence collection. No default TC opts in.
        """
        if not callable(collector):
            raise ValueError("Reconnect workflow must be callable")
        self._reconnect_workflows[test_id] = collector
        self.update_detected_auto_setup(auto_reconnect_cases=tuple(self._reconnect_workflows))

    def join_discovered_sta_wifi(self) -> str | None:
        """User-requested preparation only; discovery itself never switches Wi-Fi."""
        if self._sta_join_request_id or self.capture_pending or self.discovery_pending or (self.active and self.active.status == "RUNNING"):
            self.message.emit("Wait for the current Wi-Fi operation before joining the STA network.")
            return None
        setup = self.effective_auto_setup()
        if not self._service_connected() or not (setup.primary_non_wifi_management or setup.backup_ethernet_ready):
            self.message.emit("Laptop STA auto-join requires verified non-Wi-Fi management.")
            return None
        if setup.current_mode.lower() not in {"managed", "station"}:
            self.message.emit("DUT must already be associated to the external STA network.")
            return None

        async def prepare(ssh):
            from .auto_collectors import _local, join_laptop_sta
            async def run(spec):
                stdout, stderr, code = await _local(spec.command, spec.timeout)
                # Preparation is not acceptance evidence. Don't log credentials
                # or nmcli output from a user-input association command.
                return {"stdout": stdout, "stderr": stderr, "exit_status": code}
            if setup.primary_non_wifi_management:
                return await join_laptop_sta(setup, run)
            if not setup.backup_ethernet_ip or not hasattr(ssh, "config"):
                raise RuntimeError("Verified alternate management endpoint unavailable.")
            original_host = ssh.config.host
            try:
                await ssh.disconnect()
                ssh.config.host = setup.backup_ethernet_ip
                await asyncio.wait_for(ssh.connect(), 15)
                return await join_laptop_sta(setup, run)
            finally:
                await ssh.disconnect()
                ssh.config.host = original_host
                await asyncio.wait_for(ssh.connect(), 15)
        self._sta_join_request_id = self.services["Jetson"].submit_operation("wifi-sta-laptop-join", prepare)
        return self._sta_join_request_id

    def _auto_readiness(self, case: WifiTestCase) -> str:
        return readiness_status(case, self.effective_auto_setup())

    @property
    def batch(self):
        return self.batch_runner.state if self.batch_runner else None

    @property
    def wifi_busy(self):
        return bool((self.batch and self.batch.running) or self._pending_auto_start or
                    (self.active and self.active.status in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}))

    def start_batch(self, environment, cases, hooks=None):
        if self.wifi_busy:
            self.message.emit("A Wi-Fi test or batch is already running.")
            return None
        cases = tuple(cases)
        if not cases:
            self.message.emit("Select at least one AUTO test.")
            return None
        from .batch import WifiBatchRunner
        self._batch_control_lost.clear()
        self.batch_runner = WifiBatchRunner(self, environment, cases, hooks)
        self.batch_runner.begin()
        return self.batch_runner.state

    def stop_batch(self):
        if self.batch and self.batch.running:
            self.batch_runner.stop()
        else:
            self.stop_auto()

    def _batch_phase_changed(self, phase):
        if self.batch and self.batch.running:
            self.batch_runner.update_phase(phase)
            self.changed.emit()

    @staticmethod
    def _project_current_status(attempt: WifiAttempt) -> str:
        """Project one live/finished attempt onto the Current Session status."""
        if attempt.status in {"PREPARING", "RUNNING", "STOPPING", "RESTORING"}:
            return attempt.status
        if attempt.status == "ERROR":
            return "ERROR"
        if attempt.status == "STOPPED":
            return "STOPPED"
        if attempt.status == "BLOCKED":
            return "BLOCKED"
        if attempt.status == "COMPLETED":
            if attempt.final_result not in {"", "NOT EVALUATED", None}:
                return attempt.final_result
            return "NEEDS REVIEW"
        if attempt.final_result not in {"", "NOT EVALUATED", None}:
            return attempt.final_result
        return attempt.status or "NOT RUN"

    def current_session_status(self, environment: str, test_id: str) -> str:
        """Return execution/result state for this application validation session.

        This method must never infer Current Session state from historical
        result.json or summary.json files.
        """
        if self.active and (
            self.active.environment,
            self.active.case.test_id,
        ) == (environment, test_id):
            return self._project_current_status(self.active)

        return self._current_session_results.get(
            (environment, test_id),
            "NOT RUN",
        )

    def reset_current_results(
        self,
        environment: str,
        test_ids=None,
    ) -> None:
        """Clear Current Session projection without deleting History/Evidence."""
        selected = set(test_ids) if test_ids is not None else None

        # Do not reset while a test is actively executing.
        busy_states = {"PREPARING", "RUNNING", "STOPPING", "RESTORING"}
        if (
            self.active
            and self.active.environment == environment
            and (selected is None or self.active.case.test_id in selected)
            and self.active.status in busy_states
        ):
            return

        keys = [
            key
            for key in self._current_session_results
            if key[0] == environment
            and (selected is None or key[1] in selected)
        ]

        for key in keys:
            self._current_session_results.pop(key, None)

        # A finished attempt is still referenced by self.active. Since
        # current_session_status() intentionally prioritizes self.active,
        # detach that finished attempt from the Current Session projection.
        # Its result/evidence remains persisted on disk and available in History.
        if (
            self.active
            and self.active.environment == environment
            and (selected is None or self.active.case.test_id in selected)
            and self.active.status not in busy_states
        ):
            self.active = None
            self._active_auto_setup = None

        self.changed.emit()

    def latest_status(self, environment: str, test_id: str) -> str:
        auto_case = None
        if test_id.startswith("TC-JET-"):
            if self._auto_cases_by_id is None:
                from .catalog import load_auto_catalog
                self._auto_cases_by_id = {item.test_id: item for item in load_auto_catalog()}
            auto_case = self._auto_cases_by_id.get(test_id)
        if self.active and (self.active.environment, self.active.case.test_id) == (environment, test_id):
            if self.active.case.suite == "AUTO" and self.active.status == "BLOCKED":
                return self._auto_readiness(self.active.case)
            return self.active.final_result if self.active.status in {"COMPLETED", "ERROR"} else self.active.status
        paths = self.attempts(environment, test_id)
        if not paths:
            if test_id.startswith("TC-JET-"):
                return self._auto_readiness(auto_case) if auto_case else "READY"
            return "NOT RUN"
        if paths[-1].name != "result.json":
            return "NEEDS REVIEW"
        try:
            data = json.loads(paths[-1].read_text(encoding="utf-8"))
            if test_id.startswith("TC-JET-") and data.get("final_result", data.get("status")) == "BLOCKED":
                return self._auto_readiness(auto_case) if auto_case else "READY"
            return data.get("final_result", data["status"])
        except (OSError, ValueError, KeyError):
            return "NEEDS REVIEW"

    def start(self, environment: str, case: WifiTestCase,
              _discovery_refreshed: bool = False, _batch_owned: bool = False) -> WifiAttempt | None:
        if self.batch and self.batch.running and not _batch_owned:
            self.message.emit("The Wi-Fi AUTO batch owns the execution queue.")
            return None
        if self.active and self.active.status in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}:
            self.message.emit("A Wi-Fi test is already running.")
            return None
        if environment not in {"VD", "VMO", "VR"}:
            raise ValueError(environment)
        service = self.services.get("Jetson")
        # Production always refreshes through the shared service immediately
        # before gating. Lightweight test doubles without connection signals
        # retain the synchronous start contract used by unit tests.
        if (case.suite == "AUTO" and not _discovery_refreshed and service is not None
                and hasattr(service, "connected")):
            self._pending_auto_start = (environment, case)
            request_id = self.refresh_auto_discovery()
            if request_id:
                self.changed.emit()
                return None
            self._pending_auto_start = None
        if case.suite == "AUTO" and self.block_reasons(case):
            self.preflight_blocked.emit(case)
            self.changed.emit()
            return None
        root = self._case_root(environment, case)
        prior = root.glob(f"*/{case.test_id}/attempt_*") if root.is_dir() else ()
        number = max((int(p.name.removeprefix("attempt_")) for p in prior
                      if p.is_dir() and p.name.removeprefix("attempt_").isdigit()), default=0) + 1
        directory = root / self.session / case.test_id / f"attempt_{number:03d}"
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "raw").mkdir()
        (directory / "artifacts").mkdir()
        (directory / "test_info.json").write_text(json.dumps({
            "environment": environment, "test_id": case.test_id, "target": case.target,
            "name": case.name, "source": case.source, "attempt": number,
            "suite": case.suite, "band": case.band, "mode": case.wifi_role,
            "started": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.active = WifiAttempt(environment, case, number, directory)
        self._batch_execution_error = ""
        self._stop_requested.clear()
        self.active.add_event("INFO", f"Starting {case.test_id}: {case.name}")
        if case.suite == "AUTO":
            self.active.metrics.update({"Disconnects": 0, "Reconnects": 0, "Disconnect events": []})
            setup = self.effective_auto_setup()
            if case.test_id in self._reconnect_workflows:
                setup = replace(setup, auto_recovery_ready=True)
            self._active_auto_setup = setup
            missing = missing_prerequisites(case, setup, set(self.auto_detected))
            self.active.criteria = [{**asdict(spec), "actual": None,
                                     "status": "BLOCKED" if missing else "WAITING",
                                     "reason": "Missing prerequisite" if missing else "Waiting for collector",
                                     "evidence_reference": []}
                                    for spec in criteria_for_auto(case, setup)]
            if setup.shared_ssh_ready:
                self.active.add_event("INFO", f"Shared SSH connected through {setup.ssh_transport}")
            if setup.jetson_wifi_interface:
                self.active.add_event("INFO", f"Detected interface {setup.jetson_wifi_interface}")
            level = qualification_level_for(case)
            self.active.add_event("INFO", f"Qualification level: {level}")
            self.active.add_event("INFO", qualification_reason_for(case))
            if level == "CONTROL_PATH_RECOVERY":
                self.active.add_event("INFO", f"Management transport: {setup.management_transport or setup.ssh_transport}")
                self.active.add_event("INFO", f"Management interface: {setup.management_route_dev or setup.control_interface}")
                self.active.add_event("INFO", f"DUT test Wi-Fi interface: {setup.jetson_wifi_interface}")
                self.active.add_event("INFO", f"Alternate control path: {'READY' if setup.alternate_control_ready else 'NOT READY'}")
                self.active.add_event("INFO", f"Auto recovery: {'READY' if setup.auto_recovery_ready else 'NOT READY'}")
            for key, label in (("current_mode", "Current Wi-Fi mode"), ("current_band", "Current band"), ("current_ssid", "Current SSID")):
                value = getattr(setup, key)
                if value:
                    self.active.add_event("INFO", f"{label}: {value} (Pre-Test discovery)")
            if case.wifi_role == "AP" and case.test_id.endswith("-003"):
                self.active.add_event("INFO", f"Target Jetson AP SSID: {setup.current_ssid}")
                self.active.add_event("INFO", f"Target Jetson AP IP: {setup.jetson_ap_ip}")
                self.active.add_event("INFO", f"Laptop Wi-Fi interface: {setup.client_wifi_interface}")
            if not missing:
                self.active.status = "PREPARING"
                self.active.auto_result = self.active.final_result = "RUNNING"
                self._save_summary()
                self.changed.emit()
                service = self.services.get("Jetson")
                # The discovery health probe is the AUTO suite's source of
                # truth.  Do not turn a Wi-Fi-backed shared session into an
                # Ethernet-only prerequisite (or consult a stale dashboard
                # label) after that probe has succeeded.
                if service is None or not self.effective_auto_setup().shared_ssh_ready:
                    self.complete_auto({}, "Shared Dashboard SSH command channel unavailable")
                elif self.auto_collector is None:
                    # Production collector is imported lazily so the existing VD
                    # catalog and startup path remain independent.
                    from .auto_collectors import collect_auto_case
                    self.auto_collector = collect_auto_case
                if self.active.status in {"RUNNING", "PREPARING"}:
                    attempt_directory = str(self.active.directory)
                    async def run_collector(ssh):
                        from .auto_collectors import collect_auto_case
                        if _batch_owned:
                            from .batch_network import RecoveringSSH
                            ssh = RecoveringSSH(ssh, self, attempt_directory)
                        if self._stop_requested.is_set():
                            return {"stopped": True, "measurements": {}, "raw": "", "evidence": {}}
                        async def execute():
                            workflow = self._reconnect_workflows.get(case.test_id)
                            if workflow and self._auto_readiness(case) == "READY_WITH_RECONNECT":
                                return await workflow(ssh, case, setup)
                            if self.auto_collector is collect_auto_case:
                                return await self.auto_collector(ssh, case, setup, observer=lambda update:
                                    self.collector_update.emit({**update, "attempt": attempt_directory}),
                                    verify_runtime=_batch_owned)
                            return await self.auto_collector(ssh, case, setup)
                        self._collector_loop = asyncio.get_running_loop()
                        task = self._collector_task = asyncio.create_task(execute())
                        try:
                            return await task
                        except asyncio.CancelledError:
                            return {"stopped": True, "measurements": {}, "raw": "", "evidence": {}}
                        finally:
                            self._collector_task = self._collector_loop = None
                    async def collect(ssh):
                        client_recovery = case.wifi_role == "AP" and case.test_id.endswith(("-011", "-012"))
                        changes_dut = (can_auto_configure(case, setup) or execution_safety(case) == DISRUPTIVE_CONTROL_PATH and not (
                            case.wifi_role == "STA" and case.test_id.endswith("-001") and sta_association_established(case, setup)))
                        needs_alternate = (setup.backup_ethernet_ready and not setup.primary_non_wifi_management and
                                           (client_recovery or changes_dut and setup.control_interface == setup.jetson_wifi_interface))
                        if needs_alternate:
                            if not setup.backup_ethernet_ip or not hasattr(ssh, "config"):
                                raise RuntimeError("Verified alternate SSH endpoint unavailable")
                            original_host = ssh.config.host
                            try:
                                await ssh.disconnect()
                                ssh.config.host = setup.backup_ethernet_ip
                                await asyncio.wait_for(ssh.connect(), timeout=15)
                                return await run_collector(ssh)
                            finally:
                                await ssh.disconnect()
                                ssh.config.host = original_host
                                await asyncio.wait_for(ssh.connect(), timeout=15)
                        return await run_collector(ssh)
                    self._request_id = service.submit_operation("wifi-auto", collect)
                    if self._request_id is None:
                        self.complete_auto({}, "Shared Dashboard SSH operation could not start")
            self.auto_attempt_started.emit(self.active)
            return self.active
        self._evaluate_active()
        if case.test_id == "TC-WIFI-C23":
            self.active.update_metrics({"Requirement": load_config()["Throughput @10m"]})
        elif case.test_id == "TC-WIFI-C26":
            self.active.update_metrics({"Requirement": load_config()["Local RTT Requirement"]})
        duration = re.search(r"\b(\d+)[- ]hour\b", f"{case.name} {case.procedure}", re.IGNORECASE)
        if duration and metric_kind(case) == "endurance":
            seconds = int(duration.group(1)) * 3600
            self.active.update_metrics({"Target seconds": seconds,
                                        "Target duration": f"{seconds // 3600:02d}:00:00"})
        cycles = re.search(r"\brepeat\s+(\d+)\s+cycles\b", case.procedure, re.IGNORECASE)
        if cycles and metric_kind(case) == "recovery":
            self.active.update_metrics({"Total cycles": int(cycles.group(1))})
        self._save_summary()
        self.changed.emit()
        if case.test_id in {"TC-WIFI-C01", "TC-WIFI-C02", "TC-WIFI-C03", "TC-WIFI-C04", "TC-WIFI-C11", "TC-WIFI-C19"}:
            service = self.services.get(case.target)
            if service is None or not service.is_connected:
                if case.mode == "AUTO":
                    self._complete_error(f"{case.target} Dashboard connection unavailable")
                self.message.emit(f"{case.target} Dashboard connection unavailable. Capture manually or review the attempt.")
            else:
                config = load_config()
                interface = config["Wi-Fi interface"]
                profile = config["Hotspot profile"]
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", interface) or (case.test_id == "TC-WIFI-C04" and profile == "NOT CONFIGURED"):
                    if case.mode == "AUTO":
                        self._complete_error("Wi-Fi interface or hotspot profile is NOT CONFIGURED")
                    self.message.emit("Wi-Fi interface or hotspot profile is NOT CONFIGURED.")
                    return self.active
                quoted = shlex.quote(interface)
                profile_quoted = shlex.quote(profile)
                number = int(case.test_id.rsplit("C", 1)[1])
                commands = {
                    1: ("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status",
                        f"nmcli -f GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION device show {quoted}",
                        f"ip -br link show {quoted}", f"iw dev {quoted} info",
                        f"readlink -f /sys/class/net/{quoted}/device/driver",
                        f"ethtool -i {quoted}", "journalctl -k -b --no-pager"),
                    2: ("iw list",),
                    3: ("systemctl is-active NetworkManager", "nmcli connection show", "iw dev"),
                    4: (f"nmcli connection show {profile_quoted}",),
                    11: ("iw dev", f"iw dev {quoted} info", f"iw dev {quoted} link"),
                    19: ("systemctl is-active NetworkManager", "journalctl -u NetworkManager -n 100 --no-pager", "uptime", "free -h"),
                }[number]
                async def inspect(ssh):
                    results = []
                    for command in commands:
                        try:
                            result = await ssh.run(command, timeout=20)
                        except Exception as error:
                            result = CommandResult(command, "", str(error), -1)
                        # Kernel parsing can cover a large boot log; keep it on
                        # the shared connection's worker thread.
                        if command.startswith("journalctl -k") and result.exit_status == 0:
                            result.wifi_parsed = parse_kernel_device_errors(result.stdout, interface)
                        results.append(result)
                    return results
                self._request_id = service.submit_operation("wifi-inspect", inspect)
                if self._request_id is None:
                    if case.mode == "AUTO":
                        self._complete_error(f"{case.target} shared connection became unavailable")
                    self.message.emit(f"{case.target} shared connection became unavailable. Review the attempt manually.")
        return self.active

    def effective_auto_setup(self) -> AutoSuiteSetup:
        values = dict(self.auto_detected)
        values.update(self._batch_setup_overrides)
        values["auto_reconnect_cases"] = tuple(self._reconnect_workflows)
        if values == {"auto_reconnect_cases": ()} and not self.auto_setup.auto_reconnect_cases:
            return self.auto_setup
        return replace(self.auto_setup, **values)

    def dependency_report(self, case: WifiTestCase):
        return dependencies_for(case, self.effective_auto_setup(), set(self.auto_detected))

    def block_reasons(self, case: WifiTestCase) -> list[str]:
        return [item.reason for item in self.dependency_report(case)
                if item.required and not item.satisfied]

    def update_detected_auto_setup(self, **values) -> AutoSuiteSetup:
        for key in values:
            if key not in self.auto_setup.__dataclass_fields__:
                raise ValueError(f"Unknown detected AUTO suite setup field: {key}")
        changed = any(self.auto_detected.get(key) != value for key, value in values.items())
        self.auto_detected.update(values)
        if changed:
            self.changed.emit()
        return self.effective_auto_setup()

    def update_auto_setup(self, persist: bool = True, **values) -> AutoSuiteSetup:
        for key, value in values.items():
            if key not in self.auto_setup.__dataclass_fields__:
                raise ValueError(f"Unknown AUTO suite setup field: {key}")
            setattr(self.auto_setup, key, value)
        if persist:
            self.auto_setup.save(self.auto_setup_path)
        self.changed.emit()
        return self.effective_auto_setup()

    def complete_auto(self, measurements: dict, infrastructure_error: str | None = None,
                      evidence: dict | None = None, stopped: bool = False) -> None:
        if not self.active or self.active.case.suite != "AUTO" or self.active.status not in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}:
            raise ValueError("A running AUTO Wi-Fi attempt is required")
        if evidence:
            for name, item in evidence.items():
                if name not in self.active.evidence:
                    self.active.add_event("COMMAND", item.get("command", name), item.get("timestamp"))
                if item.get("status") == "UNAVAILABLE":
                    self.active.add_event("WARNING", f"Command output unavailable: {item.get('command', name)}", item.get("timestamp"))
            self.active.evidence.update(evidence)
        self.active.update_metrics(measurements)
        if self.batch and self.batch.running:
            self.batch_runner.update_phase("EVALUATING")
            if self._batch_control_lost.is_set():
                infrastructure_error = "Shared control path could not be restored"
            elif self._batch_execution_error:
                infrastructure_error = self._batch_execution_error
        rows, status, reason = evaluate_auto_case(
            self.active.case, self._active_auto_setup or self.effective_auto_setup(),
            measurements, infrastructure_error)
        if stopped or self._stop_requested.is_set():
            status, reason = ("ERROR", infrastructure_error) if infrastructure_error else ("STOPPED", "Stopped by user; collected evidence retained and cleanup completed.")
            if not infrastructure_error:
                for row in rows:
                    if row["status"] == "ERROR":
                        row["status"], row["reason"] = "NOT_COLLECTED", "Not collected: test stopped by user"
        elif status == "BLOCKED":
            status, reason = "ERROR", "Setup became unavailable during execution: " + reason
        for row in rows:
            row.setdefault("evidence_reference", list((evidence or {}).keys()))
        self.active.criteria = rows
        self.active.status = status if status in {"STOPPED", "ERROR"} else "COMPLETED"
        self.active.phase = self.active.status
        self.active.phase_detail = "Stopped by user" if status == "STOPPED" else "Test complete"
        self.active.auto_result = self.active.final_result = status
        self.active.result_reason = reason
        self.active.add_event("ERROR" if status in {"FAIL", "BLOCKED", "ERROR", "MEASUREMENT ERROR"} else "WARNING" if status == "NEEDS REVIEW" else "INFO", f"Test {status}: {reason}")
        self._persist_result()
        self.auto_attempt_completed.emit(self.active)

    def stop_auto(self) -> None:
        if not self.active or self.active.case.suite != "AUTO" or self.active.status not in {"RUNNING", "PREPARING"}:
            return
        self._stop_requested.set()
        self.active.status = "STOPPING"
        self.active.phase, self.active.phase_detail = "STOPPING", "Cancellation requested; cleanup will follow"
        self.active.add_event("WARNING", "Stop requested by user; cleanup/restoration in progress")
        task, loop = self._collector_task, self._collector_loop
        if task is not None and loop is not None:
            loop.call_soon_threadsafe(task.cancel)
        self.changed.emit()

    def _collector_updated(self, update: dict) -> None:
        attempt = self.active
        if not attempt or str(attempt.directory) != update.get("attempt") or attempt.status not in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}:
            return
        phase = update.get("phase")
        if phase:
            if self.batch and self.batch.running:
                self.batch_runner.update_phase(phase)
            attempt.phase = phase
            attempt.phase_detail = update.get("detail", phase.title())
            if phase == "RESTORING":
                attempt.status = "RESTORING"
            elif not self._stop_requested.is_set():
                attempt.status = "PREPARING" if phase == "PREPARING" else "RUNNING"
        if update.get("message"):
            attempt.add_event(update.get("level", "INFO"), update["message"], update.get("timestamp"))
        if "metrics" in update:
            attempt.update_metrics(update["metrics"])
            attempt.criteria = evaluate_auto_case(attempt.case, self._active_auto_setup, attempt.metrics)[0]
            for row in attempt.criteria:
                if row["status"] == "ERROR" and (row["reason"] == "Required collector did not produce a measurement" or row["reason"].startswith("Missing: ")):
                    row["status"], row["actual"], row["reason"] = "WAITING", None, "Measurement pending"
        attempt.progress_done = update.get("done", attempt.progress_done)
        attempt.progress_total = update.get("total", attempt.progress_total)
        if update.get("raw"):
            self.append_raw(update["raw"])
        if update.get("evidence"):
            name, item = update["evidence"]
            item = dict(item)
            content = item.pop("content", "")
            (attempt.directory / "raw" / name).write_text(content, encoding="utf-8")
            attempt.evidence[name] = item
        self._save_summary()
        self.changed.emit()

    def review_criterion(self, criterion_id: str, status: str, actual=None, note: str = "") -> None:
        if status not in {"PASS", "FAIL", "NEEDS REVIEW"}:
            raise ValueError("Invalid manual criterion result")
        if not self.active or self.active.case.suite != "EXISTING" or self.active.status != "RUNNING":
            raise ValueError("A running existing Wi-Fi attempt is required")
        row = next((item for item in self.active.criteria if item["criterion_id"] == criterion_id), None)
        if not row or row["check"] != "manual":
            raise ValueError("Criterion is not manually reviewable")
        self.active.manual_reviews[criterion_id] = {"status": status, "actual": actual, "note": note.strip()}
        self._evaluate_active(final=True)
        self.active.final_result = self.active.auto_result
        required = [item for item in self.active.criteria if item["required"] and item.get("importance", "CRITICAL") == "CRITICAL"]
        if all(item["status"] in {"PASS", "FAIL"} for item in required):
            self.active.status = "COMPLETED"
            self._persist_result()
        else:
            self._save_summary()
            self.changed.emit()

    def add_metrics(self, values: dict) -> None:
        if self.active and self.active.status == "RUNNING":
            self.active.update_metrics(values)
            self._evaluate_active()
            self._save_summary()
            self.changed.emit()

    def append_raw(self, value: str) -> None:
        if self.active:
            with (self.active.directory / "raw" / "commands.log").open("a", encoding="utf-8") as output:
                output.write(value)
            self.active.raw_log = (self.active.raw_log + value)[-262144:]
            self.changed.emit()

    def ingest_output(self, raw: str, direction: str = "Upload", band: str = "2.4 GHz") -> dict:
        """Import a tester-supplied original command output for a guided run."""
        if not self.active or self.active.status != "RUNNING":
            raise ValueError("No running Wi-Fi attempt")
        kind = metric_kind(self.active.case)
        if kind in {"throughput", "band_throughput"}:
            metrics = parse_iperf3(raw, direction)
            if kind == "band_throughput" and metrics:
                if band not in {"2.4 GHz", "5 GHz"}:
                    raise ValueError("Choose a valid Wi-Fi band")
                runs = list(self.active.metrics.get(f"{band} Runs", []))
                runs.append(metrics[f"Average {direction.lower()}"])
                metrics[f"{band} Runs"] = runs
                metrics[f"{band} Run {len(runs)}"] = runs[-1]
                metrics[f"{band} Average"] = round(sum(runs) / len(runs), 3)
                if "Retransmits" in metrics:
                    metrics[f"{band} Retransmits"] = metrics["Retransmits"]
        elif kind == "latency":
            metrics = parse_ping(raw)
        elif kind == "rf":
            metrics = {**parse_remote_snapshot(raw), **parse_rf(raw)}
        elif kind == "recovery":
            metrics = parse_recovery(raw)
            if metrics:
                records = dict(self.active.metrics.get("Cycle records", {}))
                records.update(metrics["Cycle records"])
                metrics = recovery_metrics(records)
        else:
            metrics = parse_remote_snapshot(raw)
        self.append_raw(raw + ("\n" if not raw.endswith("\n") else ""))
        self.active.evidence["commands.log"] = {"command": "tester-imported output", "status": "CAPTURED", "source": "tester import"}
        self.add_metrics(metrics)
        return metrics

    def finish(self, status: str | None = None, comment: str = "", override_by: str | None = None) -> None:
        if not self.active or self.active.status not in {"RUNNING", "COMPLETED", "ERROR"}:
            raise ValueError("An active Wi-Fi attempt is required")
        if status is not None and status not in {"PASS", "FAIL", "NEEDS REVIEW"}:
            raise ValueError("Invalid result")
        if self.capture_pending:
            raise ValueError("Wait for the read-only capture to finish")
        attempt = self.active
        if attempt.status in {"COMPLETED", "ERROR"}:
            if status is None or not comment.strip():
                raise ValueError("An override requires a result and a reason")
            attempt.final_result = status
            attempt.override_by = override_by or "tester"
            attempt.override_reason = comment.strip()
            attempt.override_timestamp = datetime.now(timezone.utc).isoformat()
        else:
            self._evaluate_active(final=True)
            if attempt.case.mode == "AUTO" and status and status != attempt.auto_result:
                if not comment.strip():
                    raise ValueError("An override requires a reason")
                attempt.final_result = status
                attempt.override_by = override_by or "tester"
                attempt.override_reason = comment.strip()
                attempt.override_timestamp = datetime.now(timezone.utc).isoformat()
            elif status and attempt.case.mode != "AUTO":
                unresolved_auto = [row for row in attempt.criteria if row["required"] and row["check"] != "manual" and row["status"] != "PASS"]
                if (attempt.auto_result == "FAIL" and status != "FAIL") or (status == "PASS" and unresolved_auto):
                    if not comment.strip():
                        raise ValueError("Overriding an unresolved automated criterion requires a reason")
                    attempt.override_by = override_by or "tester"
                    attempt.override_reason = comment.strip()
                    attempt.override_timestamp = datetime.now(timezone.utc).isoformat()
                attempt.final_result = status
            else:
                attempt.final_result = attempt.auto_result
            attempt.status = "COMPLETED"
        attempt.comment = comment
        self._persist_result()

    def _persist_result(self) -> None:
        attempt = self.active
        if attempt.status not in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"} and attempt.finished is None:
            attempt.finished = datetime.now(timezone.utc)

        if (
            attempt.case.suite == "AUTO"
            and attempt.status not in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}
        ):
            self._current_session_results[
                (attempt.environment, attempt.case.test_id)
            ] = self._project_current_status(attempt)

        if attempt.status != "RUNNING":
            message = f"Test {attempt.final_result}: {attempt.result_reason}"
            if not attempt.events or attempt.events[-1]["message"] != message:
                attempt.add_event("ERROR" if attempt.status == "ERROR" or attempt.final_result in {"FAIL", "BLOCKED"} else "WARNING" if attempt.final_result == "NEEDS REVIEW" else "INFO", message)
        auto_context = {}
        if attempt.case.suite == "AUTO":
            setup = self._active_auto_setup or self.effective_auto_setup()
            fields = ("shared_ssh_ready", "ssh_transport", "control_path", "control_interface",
                      "control_jetson_ip", "primary_non_wifi_management", "backup_ethernet_ready",
                      "backup_ethernet_interface", "backup_ethernet_reason", "jetson_wifi_interface",
                      "jetson_ap_ip", "current_mode", "current_ssid", "current_bssid", "current_band",
                      "current_channel", "current_frequency_mhz", "dut_gateway", "dut_active_profile",
                      "laptop_active_profile", "laptop_wifi_ip", "laptop_ssid", "client_wifi_interface",
                      "saved_wifi_profile_available", "saved_wifi_profile", "valid_access_source",
                      "laptop_to_dut_wifi", "dut_to_laptop_wifi", "full_wifi_qual_path", "allow_disruptive",
                      "distance_1m_confirmed", "distance_10m_confirmed", "recorded_distance_1m", "recorded_distance_10m")
            auto_context["wifi_runtime"] = {key: getattr(setup, key) for key in fields}
            auto_context["wifi_runtime"]["plaintext_credential_read"] = False
            auto_context["recovery_phases"] = [{"name": phase["name"], "readiness": phase["status"],
                                               "blocking_reasons": phase["reasons"],
                                               "result": attempt.metrics.get(f"Phase {'B' if 'PHASE B' in phase['name'] else 'A'} result", "NOT RUN")}
                                              for phase in recovery_phases(attempt.case, setup)]
        summary = {key: sum(row["status"] == state for row in attempt.criteria
                            if row["required"] and row.get("importance", "CRITICAL") == "CRITICAL")
                   for key, state in (("pass", "PASS"), ("fail", "FAIL"), ("unknown", "UNKNOWN"),
                                      ("manual_required", "MANUAL_REQUIRED"), ("not_collected", "NOT_COLLECTED"))}
        summary["supporting_warnings"] = sum(row.get("importance", "CRITICAL") != "CRITICAL" and row["status"] in {"FAIL", "ERROR"} for row in attempt.criteria)
        summary["optional_unknown"] = sum(not row["required"] and row["status"] in {"UNKNOWN", "NOT_COLLECTED"}
                                          for row in attempt.criteria)
        if attempt.case.suite == "AUTO":
            summary["measured"] = sum(row["status"] == "MEASURED" for row in attempt.criteria)
            summary["needs_review"] = sum(row.get("qualitative", False) or row["status"] == "NEEDS REVIEW"
                                          for row in attempt.criteria)
        (attempt.directory / "result.json").write_text(json.dumps({
            **auto_context,
            "status": attempt.final_result, "execution_state": attempt.status,
            "auto_result": attempt.auto_result, "final_result": attempt.final_result,
            "result_reason": attempt.result_reason, "criteria_summary": summary,
            "criteria": attempt.criteria, "evidence": attempt.evidence, "events": attempt.events,
            "suite": attempt.case.suite, "band": attempt.case.band, "mode": attempt.case.wifi_role,
            "manual_reviews": attempt.manual_reviews,
            "override_by": attempt.override_by, "override_reason": attempt.override_reason,
            "override_timestamp": attempt.override_timestamp,
            "comment": attempt.comment, "started": attempt.started.isoformat(),
            "finished": (attempt.finished or datetime.now(timezone.utc)).isoformat(),
            "phase": attempt.phase, "phase_detail": attempt.phase_detail,
            "duration_seconds": attempt.elapsed_seconds,
            "actual_result": attempt.metrics,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self._save_summary()
        self._request_id = None
        self.changed.emit()

    def _complete_error(self, error: str) -> None:
        if not self.active:
            return
        self._request_id = None
        self._evaluate_active(final=True)
        self.active.status = "ERROR"
        self.active.final_result = self.active.auto_result
        self.active.add_event("ERROR", f"Capture error: {error}")
        self.active.result_reason = f"Capture error: {error}. {self.active.result_reason}"
        self._persist_result()

    def _save_summary(self) -> None:
        if self.active:
            attempt = self.active
            (attempt.directory / "summary.json").write_text(json.dumps({
                "status": attempt.status, "metrics": attempt.metrics,
                "auto_result": attempt.auto_result, "final_result": attempt.final_result,
                "criteria": attempt.criteria, "evidence": attempt.evidence, "events": attempt.events,
                "suite": attempt.case.suite, "band": attempt.case.band, "mode": attempt.case.wifi_role,
                "manual_reviews": attempt.manual_reviews,
                "elapsed_seconds": attempt.elapsed_seconds,
                "started": attempt.started.isoformat(),
                "finished": attempt.finished.isoformat() if attempt.finished else None,
                "phase": attempt.phase, "phase_detail": attempt.phase_detail,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _operation_succeeded(self, request_id: str, results: object) -> None:
        if request_id == self._sta_join_request_id:
            self._sta_join_request_id = None
            self.message.emit("Laptop STA association prepared; refreshing full Wi-Fi qualification path.")
            self.refresh_auto_discovery()
            return
        if request_id == self._discovery_request_id:
            self._discovery_request_id = None
            snapshot = parse_auto_discovery(results if isinstance(results, dict) else {})
            snapshot["local_iperf3_available"] = bool(shutil.which("iperf3"))
            self._last_discovery_success = time.monotonic()
            self.update_detected_auto_setup(**snapshot)
            self.auto_discovery_completed.emit(bool(snapshot.get("shared_ssh_ready")),
                                               "Shared SSH runtime health check failed" if not snapshot.get("shared_ssh_ready") else "")
            pending = self._pending_auto_start
            self._pending_auto_start = None
            if pending:
                self.start(*pending, _discovery_refreshed=True)
            return
        if request_id != self._request_id or not self.active:
            return
        if self.active.case.suite == "AUTO":
            self._request_id = None
            capture = results if isinstance(results, dict) else {}
            raw = str(capture.get("raw", ""))
            if raw and not capture.get("streamed"):
                self.append_raw(raw)
            evidence = capture.get("evidence", {})
            for name, item in evidence.items():
                content = str(item.pop("content", "")) if isinstance(item, dict) else str(item)
                if content:
                    (self.active.directory / "raw" / name).write_text(content, encoding="utf-8")
            self.complete_auto(capture.get("measurements", {}), capture.get("error"), evidence, capture.get("stopped", False))
            if not (self.batch and self.batch.running) and hasattr(self.services.get("Jetson"), "connected"):
                self.refresh_auto_discovery()
            return
        interface = load_config()["Wi-Fi interface"]
        c01 = self.active.case.test_id == "TC-WIFI-C01"
        status_seen = False
        ip_missing = False
        for index, result in enumerate(results):
            raw = f"$ {result.command}\n{result.stdout}{result.stderr}\n[exit {result.exit_status}]\n"
            self.active.add_event("COMMAND", result.command)
            if result.exit_status != 0:
                self.active.add_event("WARNING", f"Command unavailable: {result.command}")
            self.append_raw(raw)
            evidence_name = f"capture_{index + 1:02d}.log"
            (self.active.directory / "raw" / evidence_name).write_text(raw, encoding="utf-8")
            self.active.evidence[evidence_name] = {"command": result.command,
                                                    "status": "CAPTURED" if result.exit_status == 0 else "UNAVAILABLE",
                                                    "source": "remote command", "exit_status": result.exit_status}
            command = result.command
            values = ({} if command.startswith("journalctl -k") else
                      {**parse_remote_snapshot(result.stdout), **parse_rf(result.stdout)})
            number = int(self.active.case.test_id.rsplit("C", 1)[1])
            if c01 and command.startswith("nmcli -t"):
                parsed = parse_nmcli_device_status(result.stdout, interface)
                status_seen = bool(parsed)
                values.update(parsed)
            elif c01 and command.startswith("nmcli -f GENERAL"):
                values.update(parse_nmcli_device_show(result.stdout, interface))
            elif c01 and command.startswith("ip -br"):
                if result.exit_status == 0:
                    values.update(parse_ip_link(result.stdout, interface))
                elif result.exit_status != 0: ip_missing = True
            elif c01 and command.startswith(("readlink", "ethtool")):
                # A sysfs driver is authoritative; ethtool is a fallback.
                parsed = parse_driver(result.stdout, command)
                if "Driver/PHY" in parsed and "Driver/PHY" in self.active.metrics:
                    parsed.pop("Driver/PHY")
                values.update(parsed)
            elif c01 and command.startswith("journalctl -k") and result.exit_status == 0:
                values.update(getattr(result, "wifi_parsed", None) or
                              parse_kernel_device_errors(result.stdout, interface))
            elif number == 2 and command == "iw list":
                if result.exit_status == 0:
                    values.update(parse_iw_phy_capabilities(result.stdout))
            elif number in {3, 19} and command.startswith("systemctl"):
                values.update(parse_service_active(result.stdout))
            elif number == 3 and command.startswith("nmcli connection"):
                if result.exit_status == 0:
                    values.update(parse_nmcli_profile_list(result.stdout, load_config()["Hotspot profile"]))
            elif number == 3 and command == "iw dev":
                values.update(parse_iw_interface_role(result.stdout, interface))
            elif number == 4 and command.startswith("nmcli connection"):
                if result.exit_status == 0:
                    values["Hotspot profile"] = load_config()["Hotspot profile"]
                elif result.exit_status == 10:
                    values["Hotspot profile"] = "Absent"
                if result.exit_status == 0:
                    values.update(parse_nmcli_profile_show(result.stdout))
            elif number == 19 and command.startswith("journalctl"):
                values["Errors"] = sum("error" in line.lower() for line in result.stdout.splitlines())
            self.active.update_metrics(values)
        if c01 and ip_missing and not status_seen and "Interface" not in self.active.metrics:
            self.active.update_metrics({"Interface Missing": True})
        self._request_id = None
        self._evaluate_active()
        self._save_summary()
        if self.active.case.mode == "AUTO":
            self.finish()
        else:
            self.message.emit("Read-only capture finished. Review the remaining criteria.")
        self.changed.emit()

    def _operation_failed(self, request_id: str, error: str) -> None:
        if request_id == self._sta_join_request_id:
            self._sta_join_request_id = None
            self.message.emit(f"Laptop STA preparation failed: {error}")
            self.refresh_auto_discovery()
            return
        if request_id == self._discovery_request_id:
            self._discovery_request_id = None
            self._shared_ssh_disconnected()
            self.auto_discovery_completed.emit(False, "Wi-Fi runtime discovery failed")
            pending = self._pending_auto_start
            self._pending_auto_start = None
            if pending:
                self.start(*pending, _discovery_refreshed=True)
            if not (self.batch and self.batch.running):
                self.message.emit(f"Wi-Fi runtime discovery failed: {error}")
            return
        if request_id == self._request_id:
            if self.active and self.active.case.suite == "AUTO":
                self._request_id = None
                self.complete_auto({}, error)
                if not (self.batch and self.batch.running):
                    self.message.emit(error)
                return
            self.append_raw(f"[capture error] {error}\n")
            self._request_id = None
            if self.active and self.active.case.mode == "AUTO":
                self._complete_error(error)
            else:
                self._evaluate_active()
            self.message.emit(error)

    def _evaluate_active(self, final: bool = False) -> None:
        if self.active:
            attempt = self.active
            criteria, result, reason = evaluate(
                attempt.case, attempt.metrics, attempt.evidence,
                load_config()["Wi-Fi interface"], attempt.elapsed_seconds,
                attempt.manual_reviews)
            attempt.criteria = criteria
            if final:
                attempt.auto_result = result
                attempt.result_reason = reason
