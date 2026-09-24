"""Coordinated local + shared-SSH collectors for the ODS AUTO catalog."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .auto_suite import (AutoSuiteSetup, missing_prerequisites, sta_association_established,
                         can_auto_configure, target_ap_ssid, qualification_level_for)
from .catalog import WifiTestCase
from .parsers import parse_iperf3, parse_ping, parse_rf
from .qualification import (qualification_path, qualify_wifi_path, parse_route,
                            FORWARD_WIFI_PATH, BIDIRECTIONAL_WIFI_PATH,
                            CONTROL_PATH_RECOVERY, QUALIFIED)
from .disconnects import disconnect_transitions
from .ssh_validation import validate_wifi_ssh


@dataclass(frozen=True)
class CommandSpec:
    label: str
    side: str
    command: str
    timeout: int = 30


def _q(value) -> str:
    return shlex.quote(str(value))


def command_plan(case: WifiTestCase, setup: AutoSuiteSetup) -> list[CommandSpec]:
    """Build a serial plan for the source stage, preserving established STA links."""
    interface, client = _q(setup.jetson_wifi_interface), _q(setup.client_wifi_interface)
    target = _q(setup.jetson_ap_ip) if case.wifi_role == "AP" else "__DUT_WIFI_IP__"
    bind_ip = _q(setup.laptop_wifi_ip)
    profile = _q(setup.ap_profile)
    number = int(case.test_id.rsplit("-", 1)[1])
    qualification_level = qualification_level_for(case)
    external = setup.external_ssid_24g if case.band == "2.4G" else setup.external_ssid_5g
    plan = [
        CommandSpec("jetson_iw_info", "JETSON", f"iw dev {interface} info"),
        CommandSpec("jetson_iw_link", "JETSON", f"iw dev {interface} link"),
        CommandSpec("jetson_ip", "JETSON", f"ip -4 addr show {interface}"),
        CommandSpec("jetson_route", "JETSON", "ip route"),
        CommandSpec("jetson_wifi_gateway", "JETSON", f"nmcli -g IP4.GATEWAY device show {interface}"),
        CommandSpec("jetson_phy", "JETSON", "iw phy"),
        CommandSpec("jetson_station", "JETSON", f"iw dev {interface} station dump"),
        CommandSpec("jetson_nm_state", "JETSON", f"nmcli -g GENERAL.STATE device show {interface}"),
        CommandSpec("attempt_nm_cursor", "JETSON", "journalctl -u NetworkManager -n 0 --show-cursor --no-pager"),
        CommandSpec("jetson_nm_log", "JETSON", "__ATTEMPT_NM_LOG__"),
    ]
    if case.wifi_role == "AP":
        plan.append(CommandSpec("ap_profile", "JETSON", f"nmcli -f connection.id,802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.band,802-11-wireless.channel,802-11-wireless-security.key-mgmt,802-11-wireless-security.proto,802-11-wireless-security.pmf connection show {profile}"))
    elif number == 1 and not sta_association_established(case, setup):
        # Backend-supplied production configuration remains supported, but the
        # normal tester console never exposes a credential field.
        credential = _q(setup.external_credential)
        plan.insert(0, CommandSpec("sta_connect", "JETSON",
                    f"nmcli --wait 45 device wifi connect {_q(external)} password {credential} ifname {interface}", 60))
    if case.wifi_role == "AP" and number in {3, 4, 5, 6, 7, 8, 9, 11, 12, 13}:
        plan.append(CommandSpec("client_iw_link", "LOCAL", f"iw dev {client} link"))
    if qualification_level in {FORWARD_WIFI_PATH, BIDIRECTIONAL_WIFI_PATH}:
        plan.append(CommandSpec("client_ipv4", "LOCAL", f"ip -4 addr show dev {client}"))
    if case.wifi_role == "AP" and number in {1, 3, 8, 10, 11}:
        plan.append(CommandSpec("client_scan", "LOCAL", "nmcli -t -f IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY device wifi list"))
    if qualification_level in {FORWARD_WIFI_PATH, BIDIRECTIONAL_WIFI_PATH}:
        if not any(item.label == "client_iw_link" for item in plan):
            plan.append(CommandSpec("client_iw_link", "LOCAL", f"iw dev {client} link"))
        plan.append(CommandSpec("client_route", "LOCAL", f"ip route get {target}"))
        if qualification_level == BIDIRECTIONAL_WIFI_PATH:
            plan.append(CommandSpec("dut_return_route", "JETSON", f"ip route get {_q(setup.laptop_wifi_ip)}"))
    if number in ({3, 5, 8, 11, 12, 13} if case.wifi_role == "AP" else {2, 5, 8}):
        count = 1000 if case.wifi_role == "AP" and number == 5 else 100
        plan.append(CommandSpec("client_ping", "LOCAL", f"ping -I {client} -c {count} {target}", 240))
    if case.wifi_role == "STA" and number == 3:
        plan.append(CommandSpec("rf_samples", "JETSON",
                    f"for i in 1 2 3 4 5 6 7 8 9 10; do echo SAMPLE:$i; iw dev {interface} link; sleep 1; done", 30))
    if number in ({6, 9} if case.wifi_role == "AP" else {6}):
        duration = 60 if number == 9 else 30
        plan += [
            CommandSpec("iperf_server", "JETSON", "__ENSURE_IPERF_LISTENER__", 15),
            CommandSpec("iperf_forward", "LOCAL", f"iperf3 -J -B {bind_ip} -c {target} -t {duration} -O 5", duration + 20),
            CommandSpec("iperf_reverse", "LOCAL", f"iperf3 -J -B {bind_ip} -c {target} -t {duration} -O 5 -R", duration + 20),
        ]
    if number in ({7} if case.wifi_role == "AP" else {6}):
        if not any(item.label == "iperf_server" for item in plan):
            plan.append(CommandSpec("iperf_server", "JETSON", "__ENSURE_IPERF_LISTENER__", 15))
        plan += [CommandSpec("udp_nm_cursor", "JETSON", "journalctl -u NetworkManager -n 0 --show-cursor --no-pager"),
                 CommandSpec("iperf_udp_5", "LOCAL", f"iperf3 -J --get-server-output -B {bind_ip} -c {target} -u -b 5M -t 30", 50),
                 CommandSpec("iperf_udp_10", "LOCAL", f"iperf3 -J --get-server-output -B {bind_ip} -c {target} -u -b 10M -t 30", 50),
                 CommandSpec("udp_control_post", "JETSON", "printf '__WIFI_UDP_POST_CONTROL_READY__\\n'"),
                 CommandSpec("udp_wifi_info_post", "JETSON", f"iw dev {interface} info"),
                 CommandSpec("udp_wifi_link_post", "JETSON", f"iw dev {interface} link"),
                 CommandSpec("udp_wifi_station_post", "JETSON", f"iw dev {interface} station dump"),
                 CommandSpec("udp_nm_log_post", "JETSON", "__UDP_POST_NM_LOG__")]
    if number in ({8} if case.wifi_role == "AP" else {2, 8}):
        plan.append(CommandSpec("ssh_probe", "LOCAL",
                    "__WIFI_SSH_VALIDATION__", 25))
    if case.wifi_role == "AP" and number in {11, 12}:
        plan.append(CommandSpec("client_recovery_phase", "LOCAL", "__CLIENT_RECOVERY_PHASE__"))
        if number == 12:
            plan.append(CommandSpec("ap_restart_phase", "JETSON", "__AP_RESTART_PHASE__"))
    if case.wifi_role == "STA" and number == 7:
        temporary = _q(f"wifi-auto-invalid-{case.band.lower()}")
        invalid_psk = _q("wifi-auto-intentionally-invalid-credential")
        invalid = (f"nmcli connection delete {temporary} >/dev/null 2>&1 || true; "
                   f"nmcli connection add type wifi ifname {interface} con-name {temporary} "
                   f"ssid {_q(external)} 802-11-wireless-security.key-mgmt wpa-psk "
                   f"802-11-wireless-security.psk {invalid_psk} connection.autoconnect no >/dev/null && "
                   f"nmcli --wait 20 connection up {temporary}")
        valid = (f"nmcli --wait 45 connection up {_q(setup.dut_active_profile)} ifname {interface}"
                 if setup.dut_active_profile else
                 f"nmcli --wait 45 device wifi connect {_q(external)} password {_q(setup.external_credential)} ifname {interface}")
        plan += [CommandSpec("invalid_credential", "JETSON", invalid, 30),
                 CommandSpec("valid_credential", "JETSON", valid, 60),
                 CommandSpec("invalid_profile_cleanup", "JETSON",
                             f"nmcli connection delete {temporary}", 15),
                 CommandSpec("sta_security_scan", "JETSON",
                             "nmcli -t -f IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY device wifi list")]
    if (case.wifi_role == "AP" and number == 13) or (case.wifi_role == "STA" and number == 8):
        plan.append(CommandSpec("endurance_ping", "LOCAL",
                    f"timeout --signal=INT 7200 ping -I {client} -D -i 0.2 {target}", 7220))
        plan.append(CommandSpec("post_nm_log", "JETSON",
                    "__ATTEMPT_NM_LOG__"))
    if any(spec.label == "client_route" for spec in plan) and not (case.wifi_role == "AP" and number in {11, 12}):
        plan += [CommandSpec("client_iw_link_post", "LOCAL", f"iw dev {client} link"),
                 CommandSpec("client_route_post", "LOCAL", f"ip route get {target}")]
        if qualification_level == BIDIRECTIONAL_WIFI_PATH:
            plan.append(CommandSpec("dut_return_route_post", "JETSON", f"ip route get {_q(setup.laptop_wifi_ip)}"))
    plan.append(CommandSpec("attempt_nm_log_post", "JETSON", "__ATTEMPT_NM_LOG__"))
    return plan


class CommandInterrupted(Exception):
    def __init__(self, stdout, stderr):
        self.stdout, self.stderr = stdout, stderr


async def _local(command: str, timeout: int, on_output=None):
    process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    chunks = {"stdout": [], "stderr": []}
    async def read(stream, name):
        while chunk := await stream.read(4096):
            chunks[name].append(chunk)
            if on_output and name == "stdout":
                on_output(chunk.decode(errors="replace"))
    readers = asyncio.gather(read(process.stdout, "stdout"), read(process.stderr, "stderr"))
    try:
        await asyncio.wait_for(asyncio.shield(readers), timeout=timeout)
        await process.wait()
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await readers
        await process.wait()
        return b"".join(chunks["stdout"]).decode(errors="replace"), f"timeout after {timeout}s", -1
    except asyncio.CancelledError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Keep STOP responsive even if a grandchild inherited a pipe and
        # delays EOF. Preserve whatever output was already collected.
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), 1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(readers), 1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            readers.cancel()
        raise CommandInterrupted(b"".join(chunks["stdout"]).decode(errors="replace"), b"".join(chunks["stderr"]).decode(errors="replace"))
    stdout, stderr = b"".join(chunks["stdout"]), b"".join(chunks["stderr"])
    return stdout.decode(errors="replace"), stderr.decode(errors="replace"), process.returncode


def _safe_command(spec: CommandSpec) -> str:
    # Parse quoting before redaction; a password containing spaces must not leak
    # its trailing words to evidence. Saved PSKs are never queried at all.
    if re.search(r"\b(?:password|wifi-sec\.psk)\s", spec.command):
        words = shlex.split(spec.command)
        for index, word in enumerate(words[:-1]):
            if word in {"password", "wifi-sec.psk"}:
                words[index + 1] = "[REDACTED]"
        return shlex.join(words)
    return spec.command


IPERF_START = """command -v iperf3 >/dev/null || { echo SERVER_STATE=DUT_IPERF3_MISSING; exit 1; }
command -v ss >/dev/null || { echo SERVER_STATE=START_FAILED; exit 1; }
if ss -lntH 'sport = :5201' | grep -q .; then
  echo SERVER_STATE=ALREADY_LISTENING; echo SERVER_OWNED=NO; exit 0
fi
TASK_DIR=$(mktemp -d /tmp/cam_lidar_iperf.XXXXXX) || exit 1
nohup iperf3 -s -p 5201 >"$TASK_DIR/server.log" 2>&1 </dev/null &
TASK_PID=$!
echo SERVER_OWNED=YES; echo SERVER_PID=$TASK_PID; echo SERVER_DIR=$TASK_DIR
echo SERVER_START=$(awk '{print $22}' /proc/$TASK_PID/stat)
sleep 2
if kill -0 "$TASK_PID" 2>/dev/null && ss -lntH 'sport = :5201' | grep -q .; then
  echo SERVER_STATE=STARTED_BY_APP
else
  echo SERVER_STATE=START_FAILED; exit 1
fi"""


def iperf_cleanup_command(output: str) -> str | None:
    """Only the PID/creation identity of our own listener is eligible for kill."""
    values = dict(re.findall(r"^(SERVER_\w+)=(.*)$", output, re.M))
    pid, started, directory = (values.get(key, "") for key in ("SERVER_PID", "SERVER_START", "SERVER_DIR"))
    if (values.get("SERVER_OWNED") != "YES"
            or not re.fullmatch(r"/tmp/cam_lidar_iperf\.[A-Za-z0-9]+", directory)):
        return None
    kill = (f"if [ \"$(awk '{{print $22}}' /proc/{pid}/stat 2>/dev/null)\" = {_q(started)} ] "
            f"&& tr '\\0' ' ' </proc/{pid}/cmdline | grep -q 'iperf3 -s -p 5201'; "
            f"then kill {pid}; fi; ") if pid.isdigit() and started.isdigit() else ""
    return kill + f"rm -f {_q(directory + '/server.log')}; rmdir {_q(directory)}"


def authentication_rejected(stdout: str, stderr: str, code: int) -> bool:
    """A timeout, permission error, missing interface or AP isn't rejection proof."""
    return code != 0 and bool(re.search(
        r"(?:secrets were required|no secrets were provided|wrong (?:password|key)|"
        r"authentication (?:failed|rejected)|4-way handshake failed|pre-shared key.*incorrect|"
        r"reason[:= ]+no-secrets|reason[:= ]+wrong-key)", stdout + "\n" + stderr, re.I))


async def client_recovery_phase(setup: AutoSuiteSetup, run, *, negative: bool, result_metrics=None) -> dict:
    """Laptop-only recovery with guaranteed saved-profile restore/temporary cleanup.

    This built-in workflow requires verified non-Wi-Fi management. Local-only
    recovery is deliberately NOT advertised; a registered, tested TC workflow
    must implement that stronger strategy using the existing shared manager.
    """
    if not setup.allow_disruptive or not (setup.alternate_control_ready or setup.primary_non_wifi_management or setup.backup_ethernet_ready):
        raise RuntimeError("Active control session uses the same Wi-Fi interface that this test will intentionally disconnect, and automatic recovery is not available.")
    if not setup.saved_wifi_profile_available or not setup.saved_wifi_profile_uuid:
        raise RuntimeError("Saved valid Wi-Fi profile was not found.")
    profile = f"uuid {_q(setup.saved_wifi_profile_uuid)}"
    interface, target = _q(setup.client_wifi_interface), _q(setup.jetson_ap_ip)
    temporary = "__cam_lidar_negative_" + uuid.uuid4().hex
    metrics, cycles = result_metrics if result_metrics is not None else {}, []

    async def verify(label):
        link = await run(CommandSpec(label + "_link", "LOCAL", f"iw dev {interface} link"))
        address = await run(CommandSpec(label + "_ipv4", "LOCAL", f"ip -4 -br addr show dev {interface}"))
        route = await run(CommandSpec(label + "_route", "LOCAL", f"ip route get {target}"))
        ping = await run(CommandSpec(label + "_ping", "LOCAL", f"ping -I {interface} -c 5 -W 2 {target}", 15))
        ssid = re.search(r"^\s*SSID:\s*(.+)$", link["stdout"], re.M)
        ip = re.search(r"\b(\d+(?:\.\d+){3})/\d+", address["stdout"])
        path = qualify_wifi_path(required_level=FORWARD_WIFI_PATH,
                                 client_wifi_if=setup.client_wifi_interface,
                                 client_wifi_ip=ip.group(1) if ip else "",
                                 dut_wifi_if=setup.jetson_wifi_interface,
                                 target_wifi_ip=setup.jetson_ap_ip,
                                 forward_output=route["stdout"],
                                 forward_command_ok=route["exit_status"] == 0)
        if ip:
            metrics["Client IPv4"] = ip.group(1)
        associated = bool(link["exit_status"] == 0 and "Connected to" in link["stdout"]
                          and ssid and ssid.group(1).strip() == setup.current_ssid)
        metrics["Forward Wi-Fi path"] = path.forward_wifi_ok
        metrics["Full Wi-Fi qualification path"] = bool(associated and path.forward_wifi_ok and ping["exit_status"] == 0)
        if route["exit_status"] == 0 and not path.forward_wifi_ok:
            metrics["Stale routes"] = metrics.get("Stale routes", 0) + 1
        return bool(associated and path.forward_wifi_ok and ip and address["exit_status"] == 0 and ping["exit_status"] == 0)

    async def restore(label):
        radio = await run(CommandSpec(label + "_radio_on", "LOCAL", "nmcli radio wifi on"))
        up = await run(CommandSpec(label + "_profile_up", "LOCAL", f"nmcli --wait 45 connection up {profile} ifname {interface}", 55))
        proven = await verify(label)
        return radio["exit_status"] == 0 and up["exit_status"] == 0 and proven

    try:
        metrics["Valid credential connects"] = await restore("valid_profile_initial")
        if not metrics["Valid credential connects"]:
            return {**metrics, "Valid profile restored": False, "Reconnect successes": 0, "Client recovery cycles": []}
        if negative:
            security = await run(CommandSpec("valid_profile_security", "LOCAL",
                                            f"nmcli -g 802-11-wireless-security.key-mgmt connection show {profile}"))
            key_mgmt = security["stdout"].strip()
            if security["exit_status"] != 0 or key_mgmt not in {"sae", "wpa-psk"}:
                raise RuntimeError("Saved profile security is unavailable/unsupported for an isolated wrong-password test.")
            added = await run(CommandSpec("temporary_invalid_add", "LOCAL",
                                         f"nmcli connection add type wifi ifname {interface} con-name {_q(temporary)} ssid {_q(setup.current_ssid)} connection.autoconnect no"))
            if added["exit_status"] != 0:
                raise RuntimeError("Temporary invalid Wi-Fi profile could not be created.")
            modified = await run(CommandSpec("temporary_invalid_configure", "LOCAL",
                                            f"nmcli connection modify {_q(temporary)} wifi-sec.key-mgmt {_q(key_mgmt)} wifi-sec.psk WRONG_PASSWORD_TEST_12345678"))
            if modified["exit_status"] != 0:
                raise RuntimeError("Temporary invalid Wi-Fi profile could not be configured.")
            down = await run(CommandSpec("valid_profile_down", "LOCAL", f"nmcli connection down {profile}"))
            if down["exit_status"] != 0:
                raise RuntimeError("Valid client profile could not be safely disconnected for authentication testing.")
            rejected = await run(CommandSpec("invalid_credential", "LOCAL", f"nmcli --wait 25 connection up {_q(temporary)} ifname {interface}", 35))
            metrics["Invalid credential rejected"] = authentication_rejected(rejected["stdout"], rejected["stderr"], rejected["exit_status"])
            await run(CommandSpec("temporary_invalid_delete", "LOCAL", f"nmcli connection delete {_q(temporary)}"))
        metrics["Valid profile restored"] = await restore("valid_profile_restore")
        for cycle in range(1, setup.recovery_cycles + 1):
            off = await run(CommandSpec(f"recovery_cycle_{cycle}_off", "LOCAL", "nmcli radio wifi off"))
            await asyncio.sleep(setup.recovery_off_seconds)
            passed = await restore(f"recovery_cycle_{cycle}")
            cycles.append({"cycle": cycle, "status": "PASS" if off["exit_status"] == 0 and passed else "FAIL"})
            metrics[f"Recovery cycle {cycle}"] = off["exit_status"] == 0 and passed
        metrics["Reconnect successes"] = sum(item["status"] == "PASS" for item in cycles)
        metrics["Client recovery cycles"] = cycles
        metrics["Services recovered"] = bool(metrics["Valid profile restored"] and metrics["Reconnect successes"] == setup.recovery_cycles)
        # No duplicate/stale route is inferred from reconnect success. Preserve
        # actual route evidence and verify the chosen data interface each cycle.
        metrics.setdefault("Stale routes", 0)
        return metrics
    finally:
        async def cleanup():
            if negative:
                await run(CommandSpec("temporary_invalid_cleanup", "LOCAL", f"nmcli connection delete {_q(temporary)}"))
                profiles = await run(CommandSpec("temporary_invalid_absence", "LOCAL", "nmcli -t -f NAME connection show"))
                metrics["Temporary invalid profile removed"] = profiles["exit_status"] == 0 and temporary not in profiles["stdout"].splitlines()
            restored = await restore("client_cleanup_restore")
            metrics["Client cleanup restored"] = restored
            if not restored:
                metrics["Valid profile restored"] = False
                metrics["Services recovered"] = False
        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise


async def ap_restart_phase(setup: AutoSuiteSetup, run) -> dict:
    if not setup.allow_disruptive or setup.current_mode.lower() != "ap" or not setup.ap_profile:
        raise RuntimeError("AP restart requires explicit authorization, AP mode and a discovered AP profile.")
    if not (setup.alternate_control_ready or setup.primary_non_wifi_management or setup.backup_ethernet_ready):
        raise RuntimeError("AP profile restart would interrupt SSH; no verified non-Wi-Fi management or Jetson-side self-recovery mechanism is available.")
    profile = _q(setup.ap_profile)
    down = None
    try:
        down = await run(CommandSpec("ap_down", "JETSON", f"sudo -n nmcli connection down {profile}"))
        if down["exit_status"] == 0:
            await asyncio.sleep(10)
    finally:
        up_task = asyncio.create_task(run(CommandSpec("ap_up", "JETSON", f"sudo -n nmcli --wait 45 connection up {profile}", 60)))
        try:
            up = await asyncio.shield(up_task)
        except asyncio.CancelledError:
            await up_task
            raise
    runtime = await run(CommandSpec("ap_runtime_post", "JETSON", f"iw dev {_q(setup.jetson_wifi_interface)} info"))
    active = await run(CommandSpec("ap_active_profile_post", "JETSON", f"nmcli -g GENERAL.CONNECTION device show {_q(setup.jetson_wifi_interface)}"))
    restore = await run(CommandSpec("ap_client_restore", "LOCAL", f"nmcli --wait 45 connection up uuid {_q(setup.saved_wifi_profile_uuid)} ifname {_q(setup.client_wifi_interface)}", 55))
    route = await run(CommandSpec("ap_route_post", "LOCAL", f"ip route get {_q(setup.jetson_ap_ip)}"))
    ping = await run(CommandSpec("ap_ping_post", "LOCAL", f"ping -I {_q(setup.client_wifi_interface)} -c 5 -W 2 {_q(setup.jetson_ap_ip)}", 15))
    link = await run(CommandSpec("ap_link_post", "LOCAL", f"iw dev {_q(setup.client_wifi_interface)} link"))
    address = await run(CommandSpec("ap_ipv4_post", "LOCAL", f"ip -4 -br addr show dev {_q(setup.client_wifi_interface)}"))
    ssid = re.search(r"^\s*SSID:\s*(.+)$", link["stdout"], re.M)
    path = qualify_wifi_path(required_level=FORWARD_WIFI_PATH,
                             client_wifi_if=setup.client_wifi_interface,
                             dut_wifi_if=setup.jetson_wifi_interface,
                             target_wifi_ip=setup.jetson_ap_ip,
                             forward_output=route["stdout"],
                             forward_command_ok=route["exit_status"] == 0)
    ap_ssid = re.search(r"^\s*ssid\s+(.+)$", runtime["stdout"], re.M)
    ap_frequency = re.search(r"\bchannel\s+\d+\s*\((\d+)\s*MHz\)", runtime["stdout"])
    recovered = bool(down and down["exit_status"] == 0 and up["exit_status"] == 0 and runtime["exit_status"] == 0
                     and re.search(r"^\s*type\s+AP\s*$", runtime["stdout"], re.M | re.I)
                     and ap_ssid and ap_ssid.group(1).strip() == setup.current_ssid
                     and ap_frequency and ("2.4 GHz" if int(ap_frequency.group(1)) < 2500 else "5 GHz") == setup.current_band
                     and active["exit_status"] == 0 and active["stdout"].strip() == setup.ap_profile)
    services = bool(recovered and restore["exit_status"] == 0 and ping["exit_status"] == 0 and path.forward_wifi_ok
                    and address["exit_status"] == 0 and re.search(r"\b\d+(?:\.\d+){3}/\d+", address["stdout"]))
    return {"AP recovered": recovered, "Services recovered": services, "Reboot required": False,
            "Phase B result": "PASS" if recovered and services else "FAIL"}


async def join_laptop_sta(setup: AutoSuiteSetup, run) -> dict:
    """Explicit STA preparation: saved profile first, verified management only."""
    if setup.current_mode.lower() not in {"managed", "station"} or not setup.current_ssid:
        raise RuntimeError("DUT is not associated to a known external STA SSID.")
    if not (setup.primary_non_wifi_management or setup.backup_ethernet_ready):
        raise RuntimeError("Laptop STA auto-join requires verified non-Wi-Fi management.")
    if not setup.client_wifi_interface or not setup.local_nmcli_available:
        raise RuntimeError("Laptop Wi-Fi / NetworkManager is unavailable.")
    if setup.laptop_wifi_connected and setup.laptop_ssid == setup.current_ssid:
        return {"status": "ALREADY_CONNECTED", "credential_source": setup.valid_access_source}
    if setup.saved_wifi_profile_available and setup.saved_wifi_profile_uuid:
        command = f"nmcli --wait 45 connection up uuid {_q(setup.saved_wifi_profile_uuid)} ifname {_q(setup.client_wifi_interface)}"
        source = "SAVED NETWORKMANAGER PROFILE"
    elif setup.external_credential:
        command = f"nmcli --wait 45 device wifi connect {_q(setup.current_ssid)} password {_q(setup.external_credential)} ifname {_q(setup.client_wifi_interface)}"
        source = "USER INPUT (hidden)"
    else:
        raise RuntimeError("No saved laptop profile matches the DUT STA SSID; configure an external STA credential or save a profile.")
    success = False
    try:
        activated = await run(CommandSpec("sta_laptop_join", "LOCAL", command, 55))
        link = await run(CommandSpec("sta_laptop_join_link", "LOCAL", f"iw dev {_q(setup.client_wifi_interface)} link"))
        ssid = re.search(r"^\s*SSID:\s*(.+)$", link["stdout"], re.M)
        success = bool(activated["exit_status"] == 0 and link["exit_status"] == 0 and "Connected to" in link["stdout"]
                       and ssid and ssid.group(1).strip() == setup.current_ssid)
        if not success:
            raise RuntimeError("Laptop STA join failed; the previous client profile will be restored.")
        return {"status": "JOINED", "credential_source": source}
    finally:
        if not success and setup.laptop_active_profile:
            task = asyncio.create_task(run(CommandSpec("sta_laptop_restore", "LOCAL",
                f"nmcli --wait 45 connection up {_q(setup.laptop_active_profile)} ifname {_q(setup.client_wifi_interface)}", 55)))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise


def _udp_stats(raw: str) -> dict:
    """Read sender and receiver reports separately; never infer loss from bitrate."""
    try:
        report = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(report, dict):
        return {}
    if report.get("error"):
        return {"run completed": False}
    records = {"sender": [], "receiver": []}
    for side, payload in (("client", report), ("server", report.get("server_output_json", {}))):
        if not isinstance(payload, dict) or not isinstance(payload.get("end"), dict):
            continue
        end = payload["end"]
        for key, role in (("sum_sent", "sender"), ("sum_received", "receiver"),
                          ("sum", "receiver" if side == "server" else "sender")):
            summary = end.get(key)
            if not isinstance(summary, dict):
                continue
            if key == "sum" and isinstance(summary.get("sender"), bool):
                role = "sender" if summary["sender"] else "receiver"
            records[role].append(summary)
    stats = {}
    for role in ("sender", "receiver"):
        for summary in records[role]:
            rate = summary.get("bits_per_second")
            if isinstance(rate, (int, float)):
                stats.setdefault(f"{role} Mbps", rate / 1_000_000)
            if role == "receiver":
                for field, metric in (("lost_packets", "lost datagrams"), ("packets", "total datagrams"),
                                      ("lost_percent", "loss"), ("jitter_ms", "jitter")):
                    value = summary.get(field)
                    if isinstance(value, (int, float)):
                        stats.setdefault(metric, value)
    # Older servers return their report as text. Its explicitly reported UDP
    # receiver summary supplies the actual receive rate and datagram counters.
    server_text = report.get("server_output_text", "")
    receiver_lines = [line for line in server_text.splitlines() if "receiver" in line] if isinstance(server_text, str) else []
    summary_pattern = r"([\d.]+)\s*([KMG]?)bits/sec\s+([\d.]+)\s*ms\s+(\d+)\s*/\s*(\d+)\s*\(([\d.]+)%\)"
    matches = re.findall(summary_pattern, "\n".join(receiver_lines) or (server_text if isinstance(server_text, str) else ""))
    if matches:
        rate, scale, jitter, lost, total, loss = matches[-1]
        for key, value in {"receiver Mbps": float(rate) * {"": 1e-6, "K": 1e-3, "M": 1, "G": 1e3}[scale],
                           "jitter": float(jitter), "lost datagrams": int(lost),
                           "total datagrams": int(total), "loss": float(loss)}.items():
            stats.setdefault(key, value)
    if stats:
        stats["run completed"] = True
    return stats


def _parse(case: WifiTestCase, setup: AutoSuiteSetup, outputs: dict[str, dict]) -> dict:
    text = "\n".join(item["stdout"] for item in outputs.values())
    by = lambda label: outputs.get(label, {}).get("stdout", "")
    ok = lambda label: outputs.get(label, {}).get("exit_status") == 0
    number = int(case.test_id.rsplit("-", 1)[1])
    metrics = {
        key: value for key, value in {
            "Interface": setup.jetson_wifi_interface,
            "Driver": setup.wifi_driver, "PHY": setup.wifi_phy, "BSSID": setup.current_bssid,
            "Mode": setup.current_mode,
            "SSID": setup.current_ssid,
            "Band": setup.current_band,
            "Channel": setup.current_channel,
            "Frequency": setup.current_frequency_mhz,
            "AP IPv4" if case.wifi_role == "AP" else "DUT Wi-Fi IP": setup.current_ipv4,
        }.items() if value not in (None, "")
    }
    if (case.wifi_role == "AP" and number in {5, 6, 7, 9}) or (
            case.wifi_role == "STA" and number in {2, 5, 6}):
        metrics["Target"] = setup.jetson_ap_ip or setup.current_ipv4
    if (case.wifi_role == "AP" and number in {6, 9}) or (case.wifi_role == "STA" and number == 6):
        metrics["Protocol"] = "TCP / UDP" if case.wifi_role == "STA" else "TCP"
    elif case.wifi_role == "AP" and number == 7:
        metrics["Protocol"] = "UDP"
    if case.wifi_role == "AP" and number in {11, 12}:
        metrics["Original state"] = f"{setup.current_mode or 'Wi-Fi'} / {setup.current_band or case.band}"
        metrics["Recovery method"] = "Saved NetworkManager profile" + (" + AP restart" if number == 12 else "")
    rf = parse_rf(by("jetson_iw_info") + "\n" + by("jetson_iw_link") + "\n" + by("client_iw_link"))
    metrics.update(rf)
    allowed = re.findall(r"^\s*\*\s*(\d+)\s*MHz\s*\[(\d+)\]([^\n]*)", by("jetson_phy"), re.MULTILINE)
    if allowed:
        metrics["Allowed channels"] = [int(channel) for frequency, channel, flags in allowed
                                       if "disabled" not in flags.lower() and
                                       (case.wifi_role != "AP" or "no ir" not in flags.lower()) and
                                       (int(frequency) < 2500 if case.band == "2.4G" else 4900 <= int(frequency) < 5900)]
    iw_info = by("jetson_iw_info")
    if profile_ssid := re.search(r"802-11-wireless\.ssid:\s*(\S.*)", by("ap_profile")):
        metrics["SSID"] = profile_ssid.group(1).strip()
    elif iw_ssid := re.search(r"^\s*ssid\s+(\S.*)$", iw_info, re.MULTILINE | re.I):
        metrics["SSID"] = iw_ssid.group(1).strip()
    channel_info = re.search(r"\bchannel\s+(\d+)\s*\((\d+)\s*MHz\)", iw_info, re.I)
    if channel_info:
        metrics["Channel"] = int(channel_info.group(1))
        metrics["Frequency"] = int(channel_info.group(2))
        metrics["Band"] = "2.4 GHz" if int(channel_info.group(2)) < 2500 else "5 GHz"
    mode = re.search(r"^\s*type\s+(\S+)", by("jetson_iw_info"), re.MULTILINE)
    if mode: metrics["Mode"] = mode.group(1)
    if "state UP" in by("jetson_ip") or re.search(r"<[^>]*\bUP\b", by("jetson_ip")):
        metrics["Interface state"] = "UP"
    address = re.search(r"\binet\s+(\d+(?:\.\d+){3}(?:/\d+)?)", by("jetson_ip"))
    if address:
        metrics["AP IPv4" if case.wifi_role == "AP" else "DUT Wi-Fi IP"] = address.group(1)
    gateway = re.search(r"\b(\d+(?:\.\d+){3})\b", by("jetson_wifi_gateway"))
    if not gateway:
        gateway = re.search(r"\bdefault via (\d+(?:\.\d+){3})\s+dev\s+" + re.escape(setup.jetson_wifi_interface) + r"(?:\s|$)", by("jetson_route"))
    if gateway:
        metrics["Gateway"] = gateway.group(1)
    metrics["Wi-Fi route"] = setup.jetson_wifi_interface in by("jetson_route")
    route_output = by("client_route_post") or by("client_route")
    parsed_route = parse_route(route_output)
    route_label = "client_route_post" if "client_route_post" in outputs else "client_route"
    if parsed_route.dev:
        metrics["Route interface"] = parsed_route.dev
        metrics["Forward route dev"] = parsed_route.dev
        metrics["Forward route src"] = parsed_route.src
        metrics["Forward route via"] = parsed_route.via
    elif route_label in outputs and outputs[route_label].get("exit_status") not in {-1, 126, 127}:
        metrics["Route interface"] = ""
    client_link = by("client_iw_link_post") or by("client_iw_link")
    link = client_link + by("jetson_iw_link")
    metrics["Associated"] = "Connected to" in link
    if "client_iw_link_post" in outputs or "client_iw_link" in outputs:
        client_ssid = re.search(r"^\s*SSID:\s*(.+)$", client_link, re.M)
        metrics["Client SSID"] = client_ssid.group(1).strip() if "Connected to" in client_link and client_ssid else ""
    ping_label = "client_ping_post" if "client_ping_post" in outputs else "client_ping"
    if ping_label in outputs and outputs[ping_label].get("exit_status") not in {-1, 126, 127}:
        metrics["Ping reachable"] = ok(ping_label)
    if "ssh_probe" in outputs:
        metrics.update(outputs["ssh_probe"].get("measurements", {}))
    metrics["SSID visible"] = setup.test_ssid_24g in by("client_scan") or setup.test_ssid_5g in by("client_scan")
    phy = by("jetson_phy")
    has_he_capability = bool(re.search(r"\bHE (?:Iftypes|MAC|PHY|MCS)|802\.11ax", phy, re.I))
    has_vht_capability = bool(re.search(r"\bVHT (?:Capabilities|RX|TX|MCS)|802\.11ac", phy, re.I))
    metrics["HE capability"] = has_he_capability
    if "jetson_phy" in outputs and ok("jetson_phy"):
        metrics["PHY standard capability"] = ("Wi-Fi 5 + Wi-Fi 6" if has_he_capability and has_vht_capability else
                                                "Wi-Fi 6 / 802.11ax" if has_he_capability else
                                                "Wi-Fi 5 / 802.11ac" if has_vht_capability else "Neither VHT nor HE")
    runtime_text = by("jetson_station") + "\n" + client_link
    runtime_he = bool(re.search(r"\bHE[- ]?(?:MCS|NSS)|802\.11ax", runtime_text, re.I))
    runtime_vht = bool(re.search(r"\bVHT[- ]?(?:MCS|NSS)|802\.11ac", runtime_text, re.I))
    metrics["Runtime HE"] = runtime_he
    if any(label in outputs and ok(label) for label in ("jetson_station", "client_iw_link")):
        metrics["Runtime PHY standard"] = ("Wi-Fi 6 / 802.11ax" if runtime_he else
                                            "Wi-Fi 5 / 802.11ac" if runtime_vht else "Neither VHT nor HE")
    metrics["PHY capability"] = bool(by("jetson_phy").strip())
    metrics["PHY bitrate collected"] = bool(re.search(r"(?:tx|rx) bitrate:", text, re.I))
    if "rf_samples" in outputs:
        samples = [float(value) for value in re.findall(r"signal:\s*(-?[\d.]+)\s*dBm", by("rf_samples"), re.I)]
        metrics["RF samples"] = len(samples)
        if samples:
            metrics.update({"RSSI min": min(samples), "RSSI average": sum(samples) / len(samples),
                            "RSSI max": max(samples)})
        expected_ssid = setup.external_ssid_24g if case.band == "2.4G" else setup.external_ssid_5g
        rf_sample_blocks = re.split(r"^SAMPLE:\d+\s*$", by("rf_samples"), flags=re.M)[1:]
        baseline_bssid = re.search(r"Connected to\s+(\S+)", by("jetson_iw_link"))
        metrics["RF identity valid"] = bool(rf_sample_blocks and baseline_bssid and all(
            re.search(r"^\s*SSID:\s*" + re.escape(expected_ssid) + r"\s*$", block, re.M)
            and re.search(r"Connected to\s+" + re.escape(baseline_bssid.group(1)), block, re.I)
            for block in rf_sample_blocks))
    for key, pattern in (("TX bitrate", r"tx bitrate:\s*([\d.]+)"), ("RX bitrate", r"rx bitrate:\s*([\d.]+)")):
        match = re.search(pattern, text, re.I)
        if match: metrics[key] = float(match.group(1))
    client_address = re.search(r"\binet\s+(\d+(?:\.\d+){3}(?:/\d+)?)", by("client_ipv4"))
    station_ip = re.search(r"\bsrc\s+(\d+(?:\.\d+){3})", route_output)
    if "client_ipv4" in outputs and ok("client_ipv4"):
        metrics["Client IPv4"] = client_address.group(1) if client_address else ""
    elif station_ip:
        metrics["Client IPv4"] = station_ip.group(1)
    qualification_level = qualification_level_for(case)
    if route_label in outputs:
        qualification = qualify_wifi_path(
            required_level=qualification_level,
            client_wifi_if=setup.client_wifi_interface,
            client_wifi_ip=str(metrics.get("Client IPv4", "")).split("/", 1)[0],
            dut_wifi_if=setup.jetson_wifi_interface,
            target_wifi_ip=setup.jetson_ap_ip if case.wifi_role == "AP" else str(metrics.get("DUT Wi-Fi IP", "")).split("/", 1)[0],
            forward_output=route_output,
            forward_command_ok=outputs[route_label].get("exit_status") == 0,
            reverse_output=by("dut_return_route_post") or by("dut_return_route"),
            reverse_command_ok=(ok("dut_return_route_post") if "dut_return_route_post" in outputs else ok("dut_return_route")),
            management_transport=setup.management_transport or setup.ssh_transport,
            management_route_dev=setup.management_route_dev or setup.control_interface,
            alternate_control_ready=setup.alternate_control_ready,
            auto_recovery_ready=setup.auto_recovery_ready)
        metrics.update({"Qualification level": qualification.required_level,
                        "Qualification status": qualification.status,
                        "Qualification reason": qualification.reason,
                        "Forward Wi-Fi path": qualification.forward_wifi_ok,
                        "Reverse Wi-Fi path": qualification.reverse_wifi_ok,
                        "Reverse required": qualification.reverse_required})
    if case.wifi_role == "AP" and case.test_id.endswith("-003"):
        target_ssid = target_ap_ssid(case, setup)
        metrics.update({"Target AP SSID": target_ssid, "DUT AP IP": setup.jetson_ap_ip,
                        "Laptop Wi-Fi interface": setup.client_wifi_interface,
                        "Route command": f"ip route get {setup.jetson_ap_ip}",
                        "Ping interface": setup.client_wifi_interface})
        metrics["Full Wi-Fi qualification path"] = bool(
            metrics.get("Client SSID") == target_ssid and metrics.get("Client IPv4")
            and metrics.get("Forward Wi-Fi path") is True
            and metrics.get("Ping reachable") is True)
    nm_log = by("attempt_nm_log_post") or by("post_nm_log") or by("jetson_nm_log")
    nm_log = "\n".join(line for line in nm_log.splitlines()
                       if re.search(r"device\s+\(" + re.escape(setup.jetson_wifi_interface) + r"\)", line))
    metrics["Regulatory errors"] = sum(bool(re.search(r"regulatory|activation failed", line, re.I))
                                        for line in nm_log.splitlines())
    metrics["NetworkManager failures"] = sum("failed" in line.lower() for line in nm_log.splitlines())
    metrics["Driver resets"] = sum(bool(re.search(r"driver.*reset|firmware.*crash", line, re.I))
                                    for line in nm_log.splitlines())
    cursor_valid = bool(re.search(r"^-- cursor:\s*\S+", by("attempt_nm_cursor"), re.M)) and ok("attempt_nm_cursor")
    if cursor_valid and (ok("attempt_nm_log_post") or ok("jetson_nm_log")):
        connected = "Connected to" in by("jetson_iw_link") or bool(re.search(r"100\s*\(connected\)", by("jetson_nm_state"), re.I))
        events, reconnects = disconnect_transitions(nm_log, setup.jetson_wifi_interface, connected=connected)
        metrics.update({"Disconnects": len(events), "Reconnects": reconnects, "Disconnect events": events})
    metrics["Service failures"] = sum(bool(re.search(r"service.*fail|failed unit", line, re.I))
                                      for line in nm_log.splitlines())
    ping = parse_ping(by("client_ping"))
    aliases = {"Packets transmitted": "Packets sent", "Packets received": "Packets received",
               "Avg RTT": "RTT avg", "Min RTT": "RTT min", "Max RTT": "RTT max", "Jitter": "RTT mdev",
               "Packet loss": "Packet loss"}
    metrics.update({aliases[key]: value for key, value in ping.items() if key in aliases})
    for label, name in (("iperf_forward", "Forward receiver Mbps"), ("iperf_reverse", "Reverse receiver Mbps")):
        parsed = parse_iperf3(by(label), "Upload")
        if "Receiver Mbps" in parsed: metrics[name] = parsed["Receiver Mbps"]
        elif "Average upload" in parsed: metrics[name] = parsed["Average upload"]
        if "Retransmits" in parsed: metrics["Retransmits"] = parsed["Retransmits"]
    for rate in (5, 10):
        label = f"iperf_udp_{rate}"
        if label not in outputs:
            continue
        stats = _udp_stats(by(label))
        completed = stats.pop("run completed", None)
        if not ok(label) or completed is not None:
            metrics[f"UDP {rate} run completed"] = ok(label) and bool(completed)
        metrics.update({f"UDP {rate} {key}": value for key, value in stats.items()})
        if rate == 5:
            metrics.update({f"UDP {key}": stats[key] for key in ("loss", "jitter") if key in stats})
    if "iperf_udp_10" in outputs:
        # Objective link/log checks use evidence after the load and only new NM
        # events. Historic disconnects from before the test are not failures.
        post_mode = re.search(r"^\s*type\s+(\S+)", by("udp_wifi_info_post"), re.MULTILINE)
        if ok("udp_wifi_info_post") and post_mode:
            if post_mode.group(1).lower() == "ap" and ok("udp_wifi_station_post"):
                metrics["UDP 10 link connected"] = bool(re.search(r"^Station\s+", by("udp_wifi_station_post"), re.MULTILINE))
            elif post_mode.group(1).lower() in {"managed", "station"} and ok("udp_wifi_link_post"):
                metrics["UDP 10 link connected"] = "Connected to" in by("udp_wifi_link_post")
        if (setup.ssh_transport == "Wi-Fi" and setup.control_interface == setup.jetson_wifi_interface
                and ok("udp_control_post") and "__WIFI_UDP_POST_CONTROL_READY__" in by("udp_control_post")):
            # A successful post-load command on this same Wi-Fi SSH path also
            # proves the link is usable when a driver omits station telemetry.
            metrics["UDP 10 link connected"] = True
        for key in ("NetworkManager failures",):
            metrics.pop(key, None)
        if ok("udp_nm_log_post"):
            lines = [line for line in by("udp_nm_log_post").splitlines()
                     if setup.jetson_wifi_interface in line or re.search(r"NetworkManager.*(?:failed|error)", line, re.I)]
            connected = "Connected to" in by("jetson_iw_link") or case.wifi_role == "AP"
            events, reconnects = disconnect_transitions(by("udp_nm_log_post"), setup.jetson_wifi_interface, connected=connected)
            if "attempt_nm_log_post" not in outputs:
                metrics.update({"Disconnects": len(events), "Reconnects": reconnects, "Disconnect events": events})
            metrics["NetworkManager failures"] = sum(bool(re.search(r"\bfailed\b|\berror\b", line, re.I)) for line in lines)
            diagnostic = "\n".join(outputs[label].get("stdout", "") + outputs[label].get("stderr", "")
                                   for label in ("iperf_udp_5", "iperf_udp_10")) + "\n" + "\n".join(lines)
            metrics["Network errors"] = len(re.findall(r"Network is unreachable", diagnostic, re.I))
    security = (by("client_scan") + "\n" + by("sta_security_scan") + "\n" + by("ap_profile")).upper()
    security_tokens = [token for token in ("WPA3", "SAE", "WPA2", "WPA1", "WEP", "OPEN") if token in security]
    if security_tokens:
        metrics["Security"] = " / ".join(security_tokens)
    key_management = re.search(r"(?:key-mgmt|KEY_MGMT)\s*[:=]\s*([^\n]+)", security, re.I)
    pmf = re.search(r"(?:\.pmf|PMF)\s*[:=]\s*([^\n]+)", security, re.I)
    if key_management:
        metrics["Key management"] = key_management.group(1).strip()
    if pmf:
        metrics["PMF"] = pmf.group(1).strip()
    metrics["WPA2 runtime"] = "WPA2" in security
    metrics["SAE capability"] = "SAE" in by("jetson_phy").upper()
    metrics["Insecure modes"] = sum(token in security for token in ("WEP", "WPA1"))
    metrics["WPA3 connected"] = "SAE" in security or "WPA3" in security
    metrics["Security compliant"] = bool(("WPA2" in security or "WPA3" in security or "SAE" in security)
                                          and not any(token in security for token in ("WEP", "WPA1")))
    metrics["Valid credential connects"] = ok("valid_credential") or ok("sta_connect") or metrics.get("Associated", False)
    invalid = outputs.get("invalid_credential", {})
    metrics["Invalid credential rejected"] = bool(invalid and authentication_rejected(
        invalid.get("stdout", ""), invalid.get("stderr", ""), invalid.get("exit_status", -1)))
    recovery = re.findall(r"Cycle\s+\d+\s+(PASS|FAIL)", by("client_reconnect_cycles"), re.I)
    if recovery:
        metrics["Reconnect successes"] = sum(value.upper() == "PASS" for value in recovery)
        metrics["Stale routes"] = 0
        metrics["AP recovered"] = ok("ap_up") if "ap_up" in outputs else True
        metrics["Services recovered"] = metrics["Reconnect successes"] == len(recovery)
        metrics["Reboot required"] = False
    if "endurance_ping" in outputs:
        metrics["Elapsed seconds"] = int(outputs["endurance_ping"].get("elapsed", 0))
        metrics["Manual recoveries"] = 0
        metrics["SSH drops"] = 0 if ok("ssh_probe") else 1
    return metrics


async def collect_auto_case(ssh, case: WifiTestCase, setup: AutoSuiteSetup, *, observer=None,
                            verify_runtime=False) -> dict:
    """Run serial evidence collection while retaining the shared SSH control path."""
    outputs, raw_parts, evidence = {}, [], {}
    attempt_started = datetime.now(timezone.utc)
    phase_metrics, error = {}, None
    stopped = False
    cleanup_failures = []
    plan = command_plan(case, setup)
    measurements_done = 0
    measurement_labels = {spec.label for spec in plan if spec.label.startswith(("iperf_forward", "iperf_reverse", "iperf_udp", "client_ping", "rf_samples", "endurance_ping"))}
    qualification_level = qualification_level_for(case)
    measurement_blocked = ""
    temporary_ap = "__cam_lidar_auto_" + uuid.uuid4().hex if can_auto_configure(case, setup) else None
    last_phase = None

    def notify(**update):
        nonlocal last_phase
        if update.get("phase"):
            current = (update["phase"], update.get("detail"))
            if last_phase == current:
                update.pop("message", None)
            last_phase = current
        if observer:
            observer(update)

    def phase_for(label):
        if "cleanup" in label or label == "ap_up" or label.startswith("original_"):
            return "RESTORING", "Restoring original Wi-Fi state / cleaning up app-owned processes"
        if label.startswith("recovery_cycle_"):
            cycle = re.search(r"recovery_cycle_(\d+)", label).group(1)
            return "RECOVERING", f"Reconnect cycle {cycle} / {setup.recovery_cycles}"
        if label.startswith(("sta_connect", "valid_profile", "valid_credential", "invalid_credential", "ap_client")):
            return "CONNECTING", "Connecting / validating client authentication"
        if label.startswith(("temporary_invalid", "temporary_ap", "ap_down")):
            return "CONFIGURING", "Configuring test state"
        if label.endswith("_post") or label == "post_nm_log":
            return "FINALIZING", "Verifying final Wi-Fi routes and services"
        details = {"iperf_forward": "Upload run 1 / 1", "iperf_reverse": "Download run 1 / 1",
                   "iperf_udp_5": "UDP offered load 5 Mbps", "iperf_udp_10": "UDP offered load 10 Mbps",
                   "client_ping": "Measuring round-trip time and packet loss", "rf_samples": "Sampling RF values",
                   "endurance_ping": "Endurance measurement / 02:00:00"}
        return ("MEASURING", details[label]) if label in details else ("PREPARING", "Inspecting Wi-Fi runtime state")

    async def capture(spec):
        nonlocal measurements_done
        command = spec.command
        phase, detail = phase_for(spec.label)
        notify(phase=phase, detail=detail, message=detail,
               done=measurements_done, total=len(measurement_labels))
        notify(level="COMMAND", message=_safe_command(spec))
        if command == "__UDP_POST_NM_LOG__":
            cursor = re.search(r"^-- cursor:\s*(\S+)", outputs.get("udp_nm_cursor", {}).get("stdout", ""), re.MULTILINE)
            # Fail the evidence check explicitly if no baseline cursor exists;
            # replaying the whole boot log would mix old and current failures.
            command = (f"journalctl -u NetworkManager --after-cursor={_q(cursor.group(1))} --no-pager"
                       if cursor else "printf 'NetworkManager baseline cursor unavailable\\n' >&2; false")
        if command == "__ATTEMPT_NM_LOG__":
            cursor = re.search(r"^-- cursor:\s*(\S+)", outputs.get("attempt_nm_cursor", {}).get("stdout", ""), re.MULTILINE)
            command = (f"journalctl -u NetworkManager --after-cursor={_q(cursor.group(1))} --no-pager -o json"
                       if cursor else "printf 'NetworkManager attempt cursor unavailable\\n' >&2; false")
        ssh_measurements = None
        if command == "__WIFI_SSH_VALIDATION__":
            address = re.search(r"\binet\s+(\d+(?:\.\d+){3})", outputs.get("jetson_ip", {}).get("stdout", ""))
            target = setup.jetson_ap_ip if case.wifi_role == "AP" else address.group(1) if address else None
            if target:
                ssh_measurements = await validate_wifi_ssh(ssh, setup, target, local=_local, notify=notify)
            else:
                ssh_measurements = {"SSH criterion errors": {"SSH authentication": {"status": "ERROR", "reason": "SSH CONFIG ERROR: DUT Wi-Fi IP was not discovered"}}}
            command = "Wi-Fi SSH qualification (application authentication; secrets omitted)"
        if command == "__ENSURE_IPERF_LISTENER__":
            command = IPERF_START
        if "__DUT_WIFI_IP__" in command:
            address = re.search(r"\binet\s+(\d+(?:\.\d+){3})", outputs.get("jetson_ip", {}).get("stdout", ""))
            if address:
                command = command.replace("__DUT_WIFI_IP__", shlex.quote(address.group(1)))
        started = time.monotonic()
        interrupted = False
        ping_buffer, rtt_samples = "", []
        def live_output(chunk):
            nonlocal ping_buffer
            if spec.label not in {"client_ping", "endurance_ping"}:
                return
            ping_buffer += chunk
            lines = ping_buffer.split("\n")
            ping_buffer = lines.pop()
            for line in lines:
                sample = re.search(r"\btime[=<]([\d.]+)\s*ms", line)
                if sample:
                    rtt_samples.append(float(sample.group(1)))
                    notify(metrics={"Current RTT": rtt_samples[-1], "RTT avg": sum(rtt_samples) / len(rtt_samples),
                                    "RTT min": min(rtt_samples), "RTT max": max(rtt_samples)})
        if ssh_measurements is not None:
            stdout, stderr, code = json.dumps(ssh_measurements), "", 0
        elif spec.side == "JETSON":
            try:
                if spec.label == "iperf_server":
                    # Obtain ownership identity before honoring STOP, so even a
                    # newly launched detached listener can be cleaned up safely.
                    listener_task = asyncio.create_task(ssh.run(command, timeout=spec.timeout))
                    try:
                        result = await asyncio.shield(listener_task)
                    except asyncio.CancelledError:
                        result = await listener_task
                        interrupted = True
                else:
                    retry_options = ({"_batch_retry_safe": True} if
                                     hasattr(ssh, "batch_control_lost") and spec.label in {"rf_samples", "iperf_cleanup"}
                                     else {})
                    result = await ssh.run(command, timeout=spec.timeout, **retry_options)
                stdout, stderr, code = result.stdout, result.stderr, result.exit_status
            except Exception as error:
                stdout, stderr, code = "", str(error), -1
            except asyncio.CancelledError:
                stdout, stderr, code, interrupted = "", "Command stopped by user", 130, True
        else:
            if "__DUT_WIFI_IP__" in command:
                stdout, stderr, code = "", "DUT Wi-Fi IP was not discovered", -1
            else:
                try:
                    stdout, stderr, code = await _local(command, spec.timeout, on_output=live_output) if observer else await _local(command, spec.timeout)
                except CommandInterrupted as cancelled:
                    stdout, stderr, code, interrupted = cancelled.stdout, cancelled.stderr + "\nCommand stopped by user", 130, True
                except Exception as exception:
                    stdout, stderr, code = "", str(exception), -1
                except asyncio.CancelledError as cancelled:
                    stdout, stderr, code, interrupted = getattr(cancelled, "stdout", ""), getattr(cancelled, "stderr", "") + "\nCommand stopped by user", 130, True
        elapsed = time.monotonic() - started
        # Runtime parsers consume the untouched command output below.  Only the
        # display/evidence copies are redacted; replacing fields before parsing
        # can corrupt SSIDs, addresses, routes, hostnames, and interface names.
        display_stdout, display_stderr = stdout, stderr
        for secret in (setup.wifi_credential, setup.external_credential, getattr(getattr(ssh, "config", None), "password", None)):
            if secret:
                display_stdout = display_stdout.replace(secret, "[REDACTED]")
                display_stderr = display_stderr.replace(secret, "[REDACTED]")
        safe = _safe_command(CommandSpec(spec.label, spec.side, command, spec.timeout))
        captured = f"[{spec.side}] $ {safe}\n{display_stdout}{display_stderr}\n[exit {code}]\n"
        name = f"capture_{len(evidence) + 1:02d}_{spec.label}.log"
        outputs[spec.label] = {"stdout": stdout, "stderr": stderr, "exit_status": code,
                               "elapsed": elapsed}
        if ssh_measurements is not None:
            outputs[spec.label]["measurements"] = ssh_measurements
        if spec.label == "jetson_nm_state":
            phase_metrics["Disconnect baseline"] = {
                "timestamp": datetime.now(timezone.utc).isoformat(), "interface": setup.jetson_wifi_interface,
                "SSID": parse_rf(outputs.get("jetson_iw_link", {}).get("stdout", "")).get("SSID"),
                "BSSID": parse_rf(outputs.get("jetson_iw_link", {}).get("stdout", "")).get("BSSID"),
                "association_state": "CONNECTED" if "Connected to" in outputs.get("jetson_iw_link", {}).get("stdout", "") else "DISCONNECTED",
                "NetworkManager state": stdout.strip(), "IPv4": outputs.get("jetson_ip", {}).get("stdout", ""),
                "route": outputs.get("jetson_route", {}).get("stdout", "")}
        raw_parts.append(captured)
        expected_timeout = spec.label == "endurance_ping" and code in {124, 130}
        evidence[name] = {"command": safe, "side": spec.side,
                          "status": "CAPTURED" if code == 0 or expected_timeout else "UNAVAILABLE",
                          "exit_status": code, "content": captured, "timestamp": datetime.now(timezone.utc).isoformat()}
        if spec.label in measurement_labels and not interrupted:
            measurements_done += 1
        metrics = _parse(case, setup, outputs)
        metrics.update(phase_metrics)
        # Partial collection must not convert unexecuted commands into failures.
        pending_groups = {
            "jetson_route": ("Wi-Fi route",), "jetson_iw_link": ("Associated",),
            "client_ping": ("Ping reachable",), "ssh_probe": ("SSH reachable",),
            "client_scan": ("SSID visible",), "jetson_phy": ("HE capability", "PHY standard capability", "PHY capability", "SAE capability"),
            "jetson_station": ("Runtime HE", "Runtime PHY standard", "PHY bitrate collected"),
            "jetson_nm_log": ("Regulatory errors", "NetworkManager failures", "Driver resets", "Disconnects", "Service failures"),
            "ap_profile": ("WPA2 runtime", "Insecure modes", "WPA3 connected", "Security compliant"),
            "valid_credential": ("Valid credential connects",), "invalid_credential": ("Invalid credential rejected",),
        }
        for label, keys in pending_groups.items():
            if label not in outputs:
                for key in keys:
                    if key not in phase_metrics:
                        metrics.pop(key, None)
        if can_auto_configure(case, setup) and "jetson_iw_info" not in outputs:
            for key in ("Band", "Channel", "Frequency", "Mode", "SSID"):
                metrics.pop(key, None)
        if phase == "RESTORING" and code != 0 and not interrupted and spec.label != "temporary_invalid_cleanup":
            cleanup_failures.append(spec.label)
        if spec.label == "endurance_ping":
            metrics["Target seconds"] = 7200
        if spec.label in {"iperf_forward", "iperf_reverse"}:
            try:
                report = json.loads(stdout)
                direction = "upload" if spec.label == "iperf_forward" else "download"
                intervals = [item["sum"]["bits_per_second"] / 1e6 for item in report.get("intervals", []) if "sum" in item and "bits_per_second" in item["sum"]]
                if intervals:
                    metrics.update({f"Current {direction}": intervals[-1], f"Min {direction}": min(intervals), f"Max {direction}": max(intervals)})
                    phase_metrics.update({key: metrics[key] for key in (f"Current {direction}", f"Min {direction}", f"Max {direction}")})
                    for value in intervals:
                        notify(metrics={"Forward receiver Mbps" if direction == "upload" else "Reverse receiver Mbps": value})
            except (ValueError, TypeError, KeyError):
                pass
        completed_message = f"{detail} completed"
        if spec.label not in measurement_labels and spec.label not in {"sta_connect", "invalid_credential", "valid_credential", "ap_up", "temporary_ap_activate"}:
            completed_message = ""
        event_level = "WARNING" if interrupted or code != 0 and not expected_timeout else "INFO"
        if case.wifi_role == "AP" and case.test_id.endswith("-003"):
            if spec.label == "client_iw_link":
                completed_message = (f"Laptop associated SSID: {metrics.get('Client SSID')}" if metrics.get("Client SSID")
                                     else "Laptop is not associated with the target Jetson AP SSID")
                event_level = "INFO" if metrics.get("Client SSID") else "FAIL"
            elif spec.label == "client_ipv4":
                completed_message = (f"Client IPv4: {metrics.get('Client IPv4')}" if metrics.get("Client IPv4")
                                     else f"Laptop has no IPv4 on {setup.client_wifi_interface}")
                event_level = "INFO" if metrics.get("Client IPv4") else "ERROR"
            elif spec.label in {"client_route", "client_route_post"}:
                route_dev = metrics.get("Route interface")
                completed_message = (f"Route to {setup.jetson_ap_ip} uses {route_dev}" if route_dev else
                                     f"No route to {setup.jetson_ap_ip} was found")
                event_level = "INFO" if route_dev == setup.client_wifi_interface else "FAIL"
            elif spec.label == "client_ping":
                completed_message = "Wi-Fi reachability PASS" if metrics.get("Ping reachable") else "Wi-Fi reachability FAIL"
                event_level = "INFO" if metrics.get("Ping reachable") else "FAIL"
        rate_key = "Forward receiver Mbps" if spec.label == "iperf_forward" else "Reverse receiver Mbps" if spec.label == "iperf_reverse" else None
        if rate_key and isinstance(metrics.get(rate_key), (int, float)):
            completed_message += f": {metrics[rate_key]:g} Mbps"
        notify(metrics=metrics, raw=captured, evidence=(name, evidence[name]),
               done=measurements_done, total=len(measurement_labels),
               level=event_level,
               message=(completed_message if (code == 0 or expected_timeout) and not interrupted else f"{spec.label.replace('_', ' ')}: {'stopped' if interrupted else 'command unavailable'}"))
        if interrupted:
            raise asyncio.CancelledError
        return outputs[spec.label]

    async def verify_required_state():
        info = await capture(CommandSpec("measurement_runtime_info", "JETSON",
                             f"iw dev {_q(setup.jetson_wifi_interface)} info"))
        role = re.search(r"^\s*type\s+(\S+)", info["stdout"], re.M)
        frequency = re.search(r"channel\s+\d+\s+\((\d+)\s+MHz\)", info["stdout"])
        expected_role = "ap" if case.wifi_role == "AP" else "managed"
        valid_band = bool(frequency and (2400 <= int(frequency.group(1)) < 2500 if case.band == "2.4G"
                                        else 4900 <= int(frequency.group(1)) < 5900))
        if info["exit_status"] or not role or role.group(1).lower() != expected_role or not valid_band:
            raise RuntimeError("Required interface mode/band could not be verified immediately before measurement")
        if case.wifi_role == "STA":
            link = await capture(CommandSpec("measurement_runtime_link", "JETSON",
                                 f"iw dev {_q(setup.jetson_wifi_interface)} link"))
            if link["exit_status"] or "Connected to" not in link["stdout"]:
                raise RuntimeError("Required STA association no longer exists before measurement")
        if "jetson_ip" in outputs and any(spec.label == "dut_return_route" for spec in plan):
            client = await capture(CommandSpec("measurement_client_link", "LOCAL",
                                   f"iw dev {_q(setup.client_wifi_interface)} link"))
            address = re.search(r"\binet\s+(\d+(?:\.\d+){3})", outputs.get("jetson_ip", {}).get("stdout", ""))
            target = setup.jetson_ap_ip if case.wifi_role == "AP" else address.group(1) if address else ""
            if not target:
                raise RuntimeError("DUT Wi-Fi address unavailable before measurement")
            outbound = await capture(CommandSpec("measurement_client_route", "LOCAL", f"ip route get {_q(target)}"))
            inbound = await capture(CommandSpec("measurement_dut_route", "JETSON", f"ip route get {_q(setup.laptop_wifi_ip)}"))
            ssid = re.search(r"^\s*SSID:\s*(.+)$", client["stdout"], re.M)
            path = qualification_path(interface=setup.client_wifi_interface,
                                      associated=client["exit_status"] == 0 and "Connected to" in client["stdout"],
                                      laptop_ssid=ssid.group(1).strip() if ssid else "", dut_ssid=setup.current_ssid,
                                      dut_interface=setup.jetson_wifi_interface,
                                      outbound=outbound["stdout"], inbound=inbound["stdout"],
                                      outbound_ok=outbound["exit_status"] == 0, inbound_ok=inbound["exit_status"] == 0)
            if not path["full_wifi_qual_path"] and not (case.wifi_role == "AP" and case.test_id.endswith("-003")):
                raise RuntimeError("Required Wi-Fi route no longer exists immediately before measurement")

    try:
        missing = missing_prerequisites(case, setup)
        if missing:
            return {"measurements": {}, "evidence": {}, "raw": "", "error": "; ".join(missing)}
        if temporary_ap:
            original, temporary = _q(setup.ap_profile), _q(temporary_ap)
            channel = setup.channel_24g if case.band == "2.4G" else setup.channel_5g
            ssid = setup.test_ssid_24g if case.band == "2.4G" else setup.test_ssid_5g
            configured = [CommandSpec("temporary_ap_clone", "JETSON", f"sudo -n nmcli connection clone {original} {temporary}"),
                          CommandSpec("temporary_ap_configure", "JETSON", f"sudo -n nmcli connection modify {temporary} connection.autoconnect no 802-11-wireless.band {'bg' if case.band == '2.4G' else 'a'} 802-11-wireless.channel {channel or 0}" + (f" 802-11-wireless.ssid {_q(ssid)}" if ssid else "")),
                          CommandSpec("temporary_ap_activate", "JETSON", f"sudo -n nmcli --wait 45 connection up {temporary}", 60)]
            for command in configured:
                if (await capture(command))["exit_status"] != 0:
                    raise RuntimeError("Temporary Wi-Fi configuration failed; restoring original profile.")
        for spec in plan:
            if getattr(ssh, "batch_control_lost", False):
                raise ConnectionError("Shared control path could not be restored")
            if getattr(ssh, "batch_execution_error", ""):
                raise RuntimeError(ssh.batch_execution_error)
            if verify_runtime and (spec.label == "jetson_iw_info" or spec.label in measurement_labels or spec.label == "ssh_probe"):
                await verify_required_state()
            if measurement_blocked and spec.label in measurement_labels:
                skipped = {
                    "client_ping": {"Ping reachable", "Packets sent", "Packets received", "Packet loss", "RTT min", "RTT avg", "RTT max", "RTT mdev"},
                    "iperf_forward": {"Forward receiver Mbps", "Retransmits"},
                    "iperf_reverse": {"Reverse receiver Mbps", "Retransmits"},
                    "iperf_udp_5": {"UDP 5 receiver Mbps", "UDP 5 run completed", "UDP loss", "UDP jitter"},
                    "iperf_udp_10": {"UDP 10 receiver Mbps", "UDP 10 run completed", "UDP 10 link connected"},
                    "endurance_ping": {"Ping reachable", "Packets sent", "Packets received", "Packet loss", "RTT min", "RTT avg", "RTT max", "RTT mdev", "Elapsed seconds"},
                }.get(spec.label, set())
                phase_metrics.setdefault("Not measured metrics", [])
                phase_metrics["Not measured metrics"] = sorted(set(phase_metrics["Not measured metrics"]) | skipped)
                phase_metrics["Measurement blocked reason"] = measurement_blocked
                notify(phase="MEASURING", detail="Measurement not started",
                       level="WARNING", message=f"NOT MEASURED: {measurement_blocked}",
                       metrics=dict(phase_metrics), done=measurements_done,
                       total=len(measurement_labels))
                continue
            if temporary_ap and spec.label == "ap_profile":
                spec = CommandSpec(spec.label, spec.side, spec.command.replace(f"connection show {_q(setup.ap_profile)}", f"connection show {_q(temporary_ap)}"), spec.timeout)
            if spec.command == "__CLIENT_RECOVERY_PHASE__":
                recovered = await client_recovery_phase(setup, capture, negative=case.test_id.endswith("-011"), result_metrics=phase_metrics)
                phase_metrics.update(recovered)
                phase_a_pass = recovered.get("Services recovered") and recovered.get("Valid credential connects") and (
                    not case.test_id.endswith("-011") or recovered.get("Invalid credential rejected") and recovered.get("Temporary invalid profile removed"))
                phase_metrics["Phase A result"] = "PASS" if phase_a_pass else "FAIL"
                continue
            if spec.command == "__AP_RESTART_PHASE__":
                if phase_metrics.get("Phase A result") != "PASS":
                    phase_metrics.update({"Phase B result": "NOT RUN — client recovery failed", "AP recovered": False})
                    continue
                phase_metrics.update(await ap_restart_phase(setup, capture))
                continue
            result = await capture(spec)
            if spec.label in {"client_route", "client_route_post"} and qualification_level in {FORWARD_WIFI_PATH, BIDIRECTIONAL_WIFI_PATH}:
                target = setup.jetson_ap_ip if case.wifi_role == "AP" else re.search(
                    r"\binet\s+(\d+(?:\.\d+){3})", outputs.get("jetson_ip", {}).get("stdout", ""))
                target = target if isinstance(target, str) else target.group(1) if target else ""
                qualified = qualify_wifi_path(
                    required_level=FORWARD_WIFI_PATH,
                    client_wifi_if=setup.client_wifi_interface,
                    client_wifi_ip=setup.laptop_wifi_ip,
                    dut_wifi_if=setup.jetson_wifi_interface,
                    target_wifi_ip=target,
                    forward_output=result["stdout"],
                    forward_command_ok=result["exit_status"] == 0,
                    management_transport=setup.management_transport or setup.ssh_transport,
                    management_route_dev=setup.management_route_dev or setup.control_interface,
                    alternate_control_ready=setup.alternate_control_ready,
                    auto_recovery_ready=setup.auto_recovery_ready)
                phase_metrics.update({"Qualification level": qualification_level,
                                      "Qualification status": qualified.status,
                                      "Qualification reason": qualified.reason,
                                      "Forward Wi-Fi path": qualified.forward_wifi_ok,
                                      "Forward route dev": qualified.forward_route_dev,
                                      "Forward route src": qualified.forward_route_src,
                                      "Forward route via": qualified.forward_route_via})
                notify(level="INFO", message=f"Qualification level: {qualification_level}")
                notify(level="INFO", message=f"Client Wi-Fi interface: {setup.client_wifi_interface}")
                notify(level="INFO", message=f"Client Wi-Fi IPv4: {setup.laptop_wifi_ip or qualified.forward_route_src}")
                notify(level="INFO", message=f"DUT Wi-Fi interface: {setup.jetson_wifi_interface}")
                notify(level="INFO", message=f"Target Wi-Fi IP: {target}")
                notify(level="INFO", message=f"Target route: dev={qualified.forward_route_dev or 'none'} src={qualified.forward_route_src or 'none'}" +
                       (f" via={qualified.forward_route_via}" if qualified.forward_route_via else ""))
                notify(level="INFO" if qualified.forward_wifi_ok else "FAIL",
                       message=f"Forward Wi-Fi path: {'PASS' if qualified.forward_wifi_ok else 'FAIL'}")
                if qualified.status == "ERROR":
                    phase_metrics.setdefault("Criterion measurement errors", {})["Forward Wi-Fi path"] = qualified.reason
                if (not qualified.forward_wifi_ok and not (case.wifi_role == "AP" and case.test_id.endswith("-003"))
                        and spec.label == "client_route"):
                    measurement_blocked = qualified.reason
            if spec.label in {"dut_return_route", "dut_return_route_post"}:
                post = "_post" if spec.label.endswith("_post") else ""
                link = outputs.get("client_iw_link" + post, {})
                ssid = re.search(r"^\s*SSID:\s*(.+)$", link.get("stdout", ""), re.M)
                path = qualification_path(interface=setup.client_wifi_interface,
                                          associated=link.get("exit_status") == 0 and "Connected to" in link.get("stdout", ""),
                                          laptop_ssid=ssid.group(1).strip() if ssid else "",
                                          dut_ssid=setup.current_ssid, dut_interface=setup.jetson_wifi_interface,
                                          outbound=outputs["client_route" + post]["stdout"], inbound=outputs["dut_return_route" + post]["stdout"],
                                          outbound_ok=outputs["client_route" + post]["exit_status"] == 0,
                                          inbound_ok=outputs["dut_return_route" + post]["exit_status"] == 0)
                phase_metrics.update({"Reverse route via Wi-Fi": path["dut_to_laptop_wifi"]})
                phase_metrics.update({"Reverse Wi-Fi path": path["dut_to_laptop_wifi"],
                                      "Reverse route dev": path.get("reverse_route_dev", "")})
                notify(level="INFO", message=f"Reverse route: dev={path.get('reverse_route_dev') or 'none'}")
                notify(level="INFO" if path["dut_to_laptop_wifi"] else "FAIL",
                       message=f"Reverse Wi-Fi path: {'PASS' if path['dut_to_laptop_wifi'] else 'FAIL'}")
            if spec.label == "iperf_server" and outputs[spec.label]["exit_status"] != 0:
                raise RuntimeError("DUT iperf3 listener could not be made ready.")
    except asyncio.CancelledError:
        stopped = True
        notify(phase="RESTORING", detail="Stop requested; restoring original state", level="WARNING", message="Measurements stopped by user")
    except Exception as exception:
        error = str(exception)
    finally:
        notify(phase="CLEANUP", detail="Cleaning up test-owned resources", message="Cleaning up test-owned resources")
        if stopped and case.wifi_role == "STA" and case.test_id.endswith(("-001", "-007")):
            original_profile = setup.dut_active_profile or (setup.ap_profile if setup.current_mode.lower() == "ap" else "")
            if original_profile:
                async def restore_sta():
                    restored = await capture(CommandSpec("original_dut_restore", "JETSON", f"sudo -n nmcli --wait 45 connection up {_q(original_profile)}", 60))
                    active = await capture(CommandSpec("original_dut_verify", "JETSON", f"nmcli -g GENERAL.CONNECTION device show {_q(setup.jetson_wifi_interface)}"))
                    return restored["exit_status"] == 0 and active["exit_status"] == 0 and active["stdout"].strip() == original_profile
                restore_task = asyncio.create_task(restore_sta())
                try:
                    restored = await asyncio.shield(restore_task)
                except asyncio.CancelledError:
                    restored = await restore_task
                if not restored:
                    error = "Original DUT Wi-Fi profile could not be restored after stopping."
            else:
                error = "Original DUT profile was not detected; restoration requires operator review."
        if temporary_ap:
            async def restore_ap():
                restored = await capture(CommandSpec("original_ap_restore", "JETSON", f"sudo -n nmcli --wait 45 connection up {_q(setup.ap_profile)}", 60))
                if restored["exit_status"] != 0:
                    return "Original AP profile could not be restored."
                # Capture identity/runtime evidence of restoration as well.
                active = await capture(CommandSpec("original_ap_verify", "JETSON", f"nmcli -g GENERAL.CONNECTION device show {_q(setup.jetson_wifi_interface)}"))
                removed = await capture(CommandSpec("temporary_ap_cleanup", "JETSON", f"sudo -n nmcli connection delete {_q(temporary_ap)}"))
                if active["exit_status"] != 0 or active["stdout"].strip() != setup.ap_profile or removed["exit_status"] != 0:
                    return "Original AP restoration/temporary profile cleanup could not be verified."
                return None
            restore_task = asyncio.create_task(restore_ap())
            try:
                error = await asyncio.shield(restore_task) or error
            except asyncio.CancelledError:
                error = await restore_task or error
                stopped = True
        cleanup = iperf_cleanup_command(outputs.get("iperf_server", {}).get("stdout", ""))
        if cleanup:
            notify(phase="RESTORING", detail="Stopping app-owned iperf listener", message="Cleaning up app-owned iperf listener")
            task = asyncio.create_task(capture(CommandSpec("iperf_cleanup", "JETSON", cleanup, 15)))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
    measurements = _parse(case, setup, outputs)
    measurements.update(phase_metrics)
    measurements["Attempt started at"] = attempt_started.isoformat()
    measurements["Attempt completed at"] = datetime.now(timezone.utc).isoformat()
    measurements["Disconnect window seconds"] = (datetime.now(timezone.utc) - attempt_started).total_seconds()
    cursor_ok = outputs.get("attempt_nm_cursor", {}).get("exit_status") == 0 and re.search(
        r"^-- cursor:\s*\S+", outputs.get("attempt_nm_cursor", {}).get("stdout", ""), re.M)
    if not cursor_ok or outputs.get("attempt_nm_log_post", {}).get("exit_status") != 0:
        measurements["Disconnect collector error"] = "Current-attempt NetworkManager event evidence unavailable"
    elif "Disconnect baseline" not in measurements or not (
        outputs.get("jetson_nm_state", {}).get("exit_status") == 0 and re.search(r"\d+\s*\(", outputs.get("jetson_nm_state", {}).get("stdout", ""))
        or outputs.get("jetson_iw_link", {}).get("exit_status") == 0 and re.search(r"Connected to|Not connected", outputs.get("jetson_iw_link", {}).get("stdout", ""))):
        measurements["Disconnect collector error"] = "Initial Wi-Fi association baseline unavailable"
    else:
        for line in outputs["attempt_nm_log_post"]["stdout"].splitlines():
            try:
                record = json.loads(line)
                if not isinstance(record.get("MESSAGE"), str) or not str(record.get("__REALTIME_TIMESTAMP", "")).isdigit():
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                measurements["Disconnect collector error"] = "Current-attempt NetworkManager event data is malformed"
                break
    if server := outputs.get("iperf_server"):
        state = re.search(r"^SERVER_STATE=(.+)$", server["stdout"], re.M)
        measurements["iperf server state"] = state.group(1) if state else "START_FAILED"
    unavailable = [name for name, item in evidence.items() if item["status"] == "UNAVAILABLE"]
    # Missing individual commands are handled by criterion evaluation. A wholly
    # failed capture is an infrastructure error rather than an acceptance FAIL.
    error = error or ("All AUTO collectors failed" if not stopped and unavailable and len(unavailable) == len(evidence) else None)
    if cleanup and outputs.get("iperf_cleanup", {}).get("exit_status") != 0:
        error = "App-owned iperf cleanup failed; inspect Technical Log before another run."
    if measurements.get("Client cleanup restored") is False:
        error = "Original client Wi-Fi profile could not be restored."
    if cleanup_failures:
        error = error or "Cleanup/restoration failed: " + ", ".join(cleanup_failures)
    notify(phase="FINALIZING", detail="Saving evidence and evaluating critical checks", message="Finalizing test evidence")
    return {"measurements": measurements, "evidence": evidence,
            "raw": "".join(raw_parts), "error": error, "stopped": stopped, "streamed": observer is not None}
