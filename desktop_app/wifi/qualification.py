"""Read-only Wi-Fi path/profile discovery shared by readiness and collectors.

Only explicit non-secret NetworkManager properties are requested. A management
route is never substituted for a proven laptop/DUT Wi-Fi qualification route.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass


NONE = "NONE"
RUNTIME_WIFI_STATE = "RUNTIME_WIFI_STATE"
FORWARD_WIFI_PATH = "FORWARD_WIFI_PATH"
BIDIRECTIONAL_WIFI_PATH = "BIDIRECTIONAL_WIFI_PATH"
CONTROL_PATH_RECOVERY = "CONTROL_PATH_RECOVERY"

QUALIFIED = "QUALIFIED"
NOT_REQUIRED = "NOT_REQUIRED"
PARTIALLY_QUALIFIED = "PARTIALLY_QUALIFIED"
FAILED = "FAILED"
ERROR = "ERROR"


@dataclass(frozen=True)
class RouteResult:
    target: str = ""
    dev: str = ""
    src: str = ""
    via: str = ""


@dataclass(frozen=True)
class QualificationResult:
    required_level: str
    client_wifi_if: str = ""
    client_wifi_ip: str = ""
    dut_wifi_if: str = ""
    target_wifi_ip: str = ""
    forward_route_dev: str = ""
    forward_route_src: str = ""
    forward_route_via: str = ""
    forward_wifi_ok: bool = False
    reverse_required: bool = False
    reverse_route_dev: str = ""
    reverse_route_src: str = ""
    reverse_wifi_ok: bool = False
    management_transport: str = "Unknown"
    management_route_dev: str = ""
    alternate_control_ready: bool = False
    auto_recovery_ready: bool = False
    status: str = NOT_REQUIRED
    reason: str = "No Wi-Fi path qualification is required"

    def as_dict(self) -> dict:
        return asdict(self)


def nm_fields(line: str) -> list[str]:
    fields, current, escaped = [], [], False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    return [*fields, "".join(current)]


def route_device(text: str) -> str:
    return parse_route(text).dev


def parse_route(text: str) -> RouteResult:
    """Parse one ``ip route get`` result without consulting default routes."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    target = first.split(maxsplit=1)[0] if first else ""
    field = lambda name: (match.group(1) if (match := re.search(
        rf"(?:^|\s){name}\s+(\S+)", first)) else "")
    return RouteResult(target=target, dev=field("dev"), src=field("src"), via=field("via"))


def qualify_wifi_path(*, required_level: str, client_wifi_if: str = "",
                      client_wifi_ip: str = "", dut_wifi_if: str = "",
                      target_wifi_ip: str = "", forward_output: str = "",
                      forward_command_ok: bool = True, reverse_output: str = "",
                      reverse_command_ok: bool = True,
                      runtime_wifi_ok: bool = True,
                      management_transport: str = "Unknown",
                      management_route_dev: str = "",
                      alternate_control_ready: bool = False,
                      auto_recovery_ready: bool = False) -> QualificationResult:
    """Evaluate only the route level requested by a test.

    The target-specific ``ip route get`` result is authoritative.  Default
    routes, other active interfaces, and the Dashboard SSH transport cannot
    change a valid forward data-path result.
    """
    if required_level not in {NONE, RUNTIME_WIFI_STATE, FORWARD_WIFI_PATH,
                              BIDIRECTIONAL_WIFI_PATH, CONTROL_PATH_RECOVERY}:
        raise ValueError(f"Unknown Wi-Fi qualification level: {required_level}")
    forward = parse_route(forward_output)
    reverse = parse_route(reverse_output)
    forward_ok = bool(client_wifi_if and forward_command_ok and forward.dev == client_wifi_if)
    reverse_required = required_level == BIDIRECTIONAL_WIFI_PATH
    reverse_ok = bool(dut_wifi_if and reverse_command_ok and reverse.dev == dut_wifi_if)
    common = dict(
        required_level=required_level, client_wifi_if=client_wifi_if,
        client_wifi_ip=client_wifi_ip, dut_wifi_if=dut_wifi_if,
        target_wifi_ip=target_wifi_ip, forward_route_dev=forward.dev,
        forward_route_src=forward.src, forward_route_via=forward.via,
        forward_wifi_ok=forward_ok, reverse_required=reverse_required,
        reverse_route_dev=reverse.dev, reverse_route_src=reverse.src,
        reverse_wifi_ok=reverse_ok, management_transport=management_transport,
        management_route_dev=management_route_dev,
        alternate_control_ready=alternate_control_ready,
        auto_recovery_ready=auto_recovery_ready,
    )
    if required_level == NONE:
        return QualificationResult(**common, status=NOT_REQUIRED,
                                   reason="No network-path qualification is required")
    if required_level == RUNTIME_WIFI_STATE:
        return QualificationResult(**common,
            status=QUALIFIED if runtime_wifi_ok else FAILED,
            reason="Runtime Wi-Fi state is available" if runtime_wifi_ok else "Runtime Wi-Fi state is not available")
    if required_level == CONTROL_PATH_RECOVERY:
        ready = alternate_control_ready or auto_recovery_ready
        return QualificationResult(**common, status=QUALIFIED if ready else FAILED,
            reason=("Alternate management control path is ready" if alternate_control_ready else
                    "Tested automatic recovery is ready" if auto_recovery_ready else
                    "No safe alternate control path or automatic recovery is available"))
    if not client_wifi_if or not target_wifi_ip:
        return QualificationResult(**common, status=ERROR,
                                   reason="Wi-Fi interface or target Wi-Fi IP could not be discovered")
    if not forward_command_ok:
        return QualificationResult(**common, status=ERROR,
                                   reason="Target-specific route command could not be executed")
    if not forward_ok:
        return QualificationResult(**common, status=FAILED,
            reason=(f"Route to {target_wifi_ip} uses {forward.dev or 'no interface'} "
                    f"instead of {client_wifi_if}"))
    if reverse_required:
        if not reverse_command_ok:
            return QualificationResult(**common, status=ERROR,
                                       reason="Reverse route command could not be executed")
        if not reverse_ok:
            return QualificationResult(**common, status=FAILED,
                reason=(f"Forward Wi-Fi path is valid; reverse route uses "
                        f"{reverse.dev or 'no interface'} instead of {dut_wifi_if}"))
    return QualificationResult(**common, status=QUALIFIED,
        reason=("Forward and reverse Wi-Fi paths are valid" if reverse_required else
                "Target-specific forward Wi-Fi path is valid"))


def qualification_path(*, interface: str, associated: bool, laptop_ssid: str,
                       dut_ssid: str, dut_interface: str, outbound: str,
                       inbound: str, outbound_ok: bool = True,
                       inbound_ok: bool = True) -> dict:
    result = qualify_wifi_path(
        required_level=BIDIRECTIONAL_WIFI_PATH, client_wifi_if=interface,
        dut_wifi_if=dut_interface, target_wifi_ip=parse_route(outbound).target,
        forward_output=outbound, forward_command_ok=outbound_ok,
        reverse_output=inbound, reverse_command_ok=inbound_ok)
    direct, reverse = result.forward_wifi_ok, result.reverse_wifi_ok
    same_ssid = bool(laptop_ssid and dut_ssid and laptop_ssid == dut_ssid)
    return {"laptop_to_dut_wifi": direct, "dut_to_laptop_wifi": reverse,
            "full_wifi_qual_path": bool(associated and same_ssid and direct and reverse)}


async def discover_laptop_wifi(snapshot: dict, local_run, remote_run=None) -> dict:
    """Discover saved profile metadata and verify both data routes, without PSKs.

    run(command, timeout) returns (stdout, stderr, exit_status). Discovery never
    activates a profile or changes the laptop network.
    """
    q = shlex.quote
    devices, _, device_code = await local_run(
        "nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status", 5)
    iw, _, _ = await local_run("iw dev", 5)
    addresses, _, _ = await local_run("ip -4 -br addr", 5)
    rows = [nm_fields(line) for line in devices.splitlines()]
    wifi = [row for row in rows if len(row) >= 4 and row[1] == "wifi"]
    connected = [row for row in wifi if row[2].startswith("connected")]
    candidates = list(dict.fromkeys([row[0] for row in wifi] +
                                    re.findall(r"^\s*Interface\s+(\S+)", iw, re.M)))
    interface = connected[0][0] if len(connected) == 1 else (
        candidates[0] if len(candidates) == 1 else "")
    # Disambiguate multiple connected adapters using the actual data route.
    target = snapshot.get("jetson_ap_ip", "")
    outbound, _, route_code = await local_run(f"ip route get {q(target)}", 5) if target else ("", "", -1)
    routed = route_device(outbound)
    if any(row[0] == routed for row in connected):
        interface = routed
    state = next((row for row in wifi if row[0] == interface), None)
    active_profile = state[3] if state and state[2].startswith("connected") else ""
    link, _, link_code = await local_run(f"iw dev {q(interface)} link", 5) if interface else ("", "", -1)
    ssid = re.search(r"^\s*SSID:\s*(.+)$", link, re.M)
    laptop_ssid = ssid.group(1).strip() if ssid else ""
    bssid = re.search(r"Connected to\s+([0-9a-f:]{17})", link, re.I)
    frequency = re.search(r"\bfreq:\s*(\d+)", link)
    laptop_frequency = int(frequency.group(1)) if frequency else None
    laptop_channel = (14 if laptop_frequency == 2484 else
                      (laptop_frequency - 2407) // 5 if laptop_frequency and 2412 <= laptop_frequency <= 2472 else
                      (laptop_frequency - 5000) // 5 if laptop_frequency and 5000 <= laptop_frequency < 6000 else None)
    gateway_output, _, gateway_code = await local_run(
        f"nmcli -g IP4.GATEWAY device show {q(interface)}", 5) if interface else ("", "", -1)
    gateway = re.search(r"\b\d+(?:\.\d+){3}\b", gateway_output)
    address_row = next((row for row in addresses.splitlines() if row.split() and row.split()[0] == interface), "")
    ip = re.search(r"\b(\d+(?:\.\d+){3})/\d+", address_row)
    laptop_ip = ip.group(1) if ip else ""
    profiles, _, _ = await local_run("nmcli -t -f UUID,TYPE,NAME connection show", 5)
    matches = []
    for row in (nm_fields(line) for line in profiles.splitlines()):
        if len(row) != 3 or row[1] not in {"802-11-wireless", "wifi"}:
            continue
        saved_ssid, _, code = await local_run(
            f"nmcli -g 802-11-wireless.ssid connection show uuid {q(row[0])}", 5)
        saved_ssid = re.sub(r"\\(.)", r"\1", saved_ssid.strip())
        if code == 0 and snapshot.get("current_ssid") and saved_ssid == snapshot["current_ssid"]:
            matches.append((row[0], row[2]))
    saved = next((row for row in matches if row[1] == active_profile), None) or (matches[0] if matches else ("", ""))
    inbound, _, return_code = await remote_run(f"ip route get {q(laptop_ip)}", 5) if remote_run and laptop_ip else ("", "", -1)
    associated = bool(link_code == 0 and "Connected to" in link)
    path = qualification_path(interface=interface, associated=associated,
                              laptop_ssid=laptop_ssid, dut_ssid=snapshot.get("current_ssid", ""),
                              dut_interface=snapshot.get("jetson_wifi_interface", ""),
                              outbound=outbound, inbound=inbound,
                              outbound_ok=route_code == 0, inbound_ok=return_code == 0)
    control = snapshot.get("control_jetson_ip", "")
    control_route, _, control_code = await local_run(f"ip route get {q(control)}", 5) if control else ("", "", -1)
    control_dev = route_device(control_route) if control_code == 0 else ""
    control_type = next((row[1] for row in rows if len(row) >= 2 and row[0] == control_dev), "")
    control_path = "WIFI_CONTROL" if control_type == "wifi" else "ETHERNET_MGMT" if control_type == "ethernet" else "OTHER_CONTROL"
    if control_path == "WIFI_CONTROL" and control == target:
        control_path = "DUT_WIFI_DIRECT"
    # Ethernet verification also needs the authenticated primary session and a
    # matching remote Ethernet return route; local interface type alone isn't enough.
    primary_mgmt = bool(snapshot.get("shared_ssh_ready") and control_type == "ethernet"
                        and snapshot.get("ssh_transport") == "Ethernet")
    level_result = qualify_wifi_path(
        required_level=FORWARD_WIFI_PATH, client_wifi_if=interface,
        client_wifi_ip=laptop_ip, dut_wifi_if=snapshot.get("jetson_wifi_interface", ""),
        target_wifi_ip=target, forward_output=outbound,
        forward_command_ok=route_code == 0, reverse_output=inbound,
        reverse_command_ok=return_code == 0,
        management_transport=snapshot.get("ssh_transport", "Unknown"),
        management_route_dev=snapshot.get("control_interface", ""),
        alternate_control_ready=primary_mgmt)
    return {**path, "client_wifi_interface": interface, "laptop_wifi_connected": associated,
            "laptop_ssid": laptop_ssid, "laptop_wifi_ip": laptop_ip,
            "laptop_bssid": bssid.group(1) if bssid else "",
            "laptop_channel": laptop_channel,
            "laptop_frequency_mhz": laptop_frequency,
            "laptop_gateway": gateway.group(0) if gateway and gateway_code == 0 else "",
            "laptop_active_profile": active_profile, "saved_wifi_profile_uuid": saved[0],
            "saved_wifi_profile": saved[1], "saved_wifi_profile_available": bool(saved[0]),
            "valid_access_source": "SAVED NETWORKMANAGER PROFILE" if saved[0] else "NOT AVAILABLE",
            "local_nmcli_available": device_code == 0, "control_path": control_path,
            "primary_non_wifi_management": primary_mgmt,
            "forward_route_dev": level_result.forward_route_dev,
            "forward_route_src": level_result.forward_route_src,
            "forward_route_via": level_result.forward_route_via,
            "forward_wifi_ok": level_result.forward_wifi_ok,
            "reverse_route_dev": parse_route(inbound).dev,
            "reverse_route_src": parse_route(inbound).src,
            "reverse_wifi_ok": path["dut_to_laptop_wifi"],
            "management_transport": snapshot.get("ssh_transport", "Unknown"),
            "management_route_dev": snapshot.get("control_interface", ""),
            "alternate_control_ready": primary_mgmt}
