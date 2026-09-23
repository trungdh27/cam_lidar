"""Batch-owned network state and bounded recovery on the existing SSH manager."""
from __future__ import annotations

import asyncio
import re
import shlex
import uuid

from .auto_suite import (can_auto_configure, execution_safety, DISRUPTIVE_CONTROL_PATH,
                         sta_association_established)


def _transport_failure(error):
    return (isinstance(error, (ConnectionError, OSError)) or
            type(error).__name__ in {"ConnectionLost", "DisconnectError", "ChannelOpenError"} or
            isinstance(error, RuntimeError) and str(error) == "SSH connection is not established")


class BatchNetwork:
    def __init__(self, runtime):
        self.runtime = runtime
        self.original = {}
        self.clone = ""
        self.original_control_host = None
        self.alternate_control_host = ""

    async def snapshot(self, ssh):
        from .auto_collectors import _local
        setup = self.runtime.effective_auto_setup()
        self.original = {"interface": setup.jetson_wifi_interface,
                         "client_interface": setup.client_wifi_interface,
                         "ssid": setup.current_ssid, "mode": setup.current_mode,
                         "bssid": setup.current_bssid, "association": setup.wifi_state,
                         "network_manager_state": setup.network_manager_state,
                         "client_ssid": setup.laptop_ssid, "client_ipv4": setup.laptop_wifi_ip,
                         "band": setup.current_band, "ipv4": setup.current_ipv4,
                         "route": setup.current_route, "dut_uuid": "", "client_uuid": "",
                         "nmcli": setup.nmcli_available, "local_nmcli": setup.local_nmcli_available}
        for side, interface in (("dut", setup.jetson_wifi_interface), ("client", setup.client_wifi_interface)):
            available = setup.nmcli_available if side == "dut" else setup.local_nmcli_available
            if not interface or not available:
                continue
            command = "nmcli -g GENERAL.CON-UUID device show " + shlex.quote(interface)
            if side == "dut":
                result = await ssh.run(command, timeout=15)
                output, code = result.stdout, result.exit_status
            else:
                output, _, code = await _local(command, 15)
            if code != 0:
                self.original[side + "_snapshot_error"] = "Could not capture original " + side + " Wi-Fi profile"
                self.original["nmcli" if side == "dut" else "local_nmcli"] = False
                continue
            self.original[side + "_uuid"] = output.strip() if output.strip() != "--" else ""
        return self.original

    def preflight_reason(self, case, setup):
        changes_dut = (can_auto_configure(case, setup) or
                       execution_safety(case) == DISRUPTIVE_CONTROL_PATH and not
                       (case.wifi_role == "STA" and case.test_id.endswith("-001") and
                        sta_association_established(case, setup)))
        changes_client = case.wifi_role == "AP" and case.test_id.endswith(("-011", "-012"))
        if changes_dut and self.original.get("dut_snapshot_error"):
            return self.original["dut_snapshot_error"] + "; disruptive test cannot safely restore original state"
        if changes_client and self.original.get("client_snapshot_error"):
            return self.original["client_snapshot_error"] + "; client recovery cannot safely restore original state"
        return ""

    async def prepare(self, ssh, state):
        setup = self.runtime.effective_auto_setup()
        disruptive = any(execution_safety(entry.case) == DISRUPTIVE_CONTROL_PATH or
                         (entry.case.wifi_role == "AP" and entry.case.test_id.endswith(("-011", "-012")))
                         for entry in state.queue if entry.status == "WAITING")
        if disruptive and not setup.primary_non_wifi_management and setup.backup_ethernet_ready:
            if not setup.backup_ethernet_ip or not hasattr(ssh, "config"):
                raise RuntimeError("Verified alternate control configuration unavailable")
            self.original_control_host = ssh.config.host
            self.alternate_control_host = setup.backup_ethernet_ip
            await ssh.disconnect()
            ssh.config.host = self.alternate_control_host
            await asyncio.wait_for(ssh.connect(), 15)
        configurable = next((entry.case for entry in state.queue if entry.status == "WAITING" and can_auto_configure(entry.case, setup)), None)
        # Never move the Dashboard control path merely to optimize batching.
        # Existing per-test alternate-control preparation handles other cases.
        if not configurable or not setup.primary_non_wifi_management:
            return
        if not self.original.get("nmcli") or "dut_uuid" not in self.original:
            raise RuntimeError("Original network snapshot unavailable")
        self.clone = "__cam_lidar_batch_" + uuid.uuid4().hex
        quote = shlex.quote
        band = "bg" if configurable.band == "2.4G" else "a"
        channel = setup.channel_24g if band == "bg" else setup.channel_5g
        ssid = setup.test_ssid_24g if band == "bg" else setup.test_ssid_5g
        commands = [f"sudo -n nmcli connection clone {quote(setup.ap_profile)} {quote(self.clone)}",
                    f"sudo -n nmcli connection modify {quote(self.clone)} connection.autoconnect no 802-11-wireless.band {band} 802-11-wireless.channel {channel or 0}" +
                    (f" 802-11-wireless.ssid {quote(ssid)}" if ssid else ""),
                    f"sudo -n nmcli --wait 45 connection up {quote(self.clone)} ifname {quote(setup.jetson_wifi_interface)}"]
        for command in commands:
            result = await ssh.run(command, timeout=60)
            if result.exit_status:
                raise RuntimeError("Batch target configuration failed")
        self.runtime._batch_setup_overrides["ap_profile"] = self.clone

    async def restore(self, ssh, original):
        from .auto_collectors import _local
        original = original or self.original  # partial preparation still has an owned snapshot
        errors = []
        if self.alternate_control_host and not ssh.connected:
            try:
                ssh.config.host = self.alternate_control_host
                await asyncio.wait_for(ssh.connect(), 15)
            except Exception:
                errors.append("Alternate control could not be restored for network cleanup")
        for side in ("dut", "client"):
            interface = original.get("interface" if side == "dut" else "client_interface", "")
            available = original.get("nmcli" if side == "dut" else "local_nmcli", False)
            if not interface or not available:
                continue
            quote = shlex.quote
            query = "nmcli -g GENERAL.CON-UUID device show " + quote(interface)
            async def run(command):
                if side == "dut":
                    result = await ssh.run(command, timeout=60)
                    return result.stdout, result.exit_status
                out, _, code = await _local(command, 60)
                return out, code
            try:
                current, code = await run(query)
                if code:
                    raise RuntimeError("Profile query failed")
                current = current.strip() if current.strip() != "--" else ""
                expected = original.get(side + "_uuid", "")
                if current != expected:
                    command = (f"nmcli --wait 45 connection up uuid {quote(expected)} ifname {quote(interface)}"
                               if expected else "nmcli device disconnect " + quote(interface))
                    _, code = await run(("sudo -n " if side == "dut" else "") + command)
                    actual, verify_code = await run(query)
                    actual = actual.strip() if actual.strip() != "--" else ""
                    if code or verify_code or actual != expected:
                        raise RuntimeError("Profile restoration did not verify")
            except Exception:
                errors.append(side + " original Wi-Fi state could not be restored")
        if self.clone:
            try:
                result = await ssh.run("sudo -n nmcli connection delete " + shlex.quote(self.clone), timeout=20)
                if result.exit_status:
                    errors.append("Owned batch profile cleanup failed")
            except Exception:
                errors.append("Owned batch profile cleanup failed")
        if self.original_control_host is not None:
            try:
                await ssh.disconnect()
                ssh.config.host = self.original_control_host
                await asyncio.wait_for(ssh.connect(), 15)
            except Exception:
                # Configuration is restored even if the original endpoint is unavailable.
                errors.append("Original shared SSH endpoint could not be restored")
        if errors:
            raise RuntimeError("; ".join(errors))


class RecoveringSSH:
    """Retry read-only commands after reconnecting the same manager, same attempt."""
    def __init__(self, ssh, runtime, directory):
        self.ssh, self.runtime, self.directory = ssh, runtime, directory

    def __getattr__(self, name):
        return getattr(self.ssh, name)

    @property
    def batch_control_lost(self):
        return self.runtime._batch_control_lost.is_set()

    @property
    def batch_execution_error(self):
        return self.runtime._batch_execution_error

    async def run(self, command, _batch_retry_safe=False, **kwargs):
        if self.runtime._batch_control_lost.is_set():
            raise ConnectionError("Shared control path could not be restored")
        try:
            return await self.ssh.run(command, **kwargs)
        except Exception as error:
            if not _transport_failure(error):
                raise
            self.runtime.collector_update.emit({"attempt": self.directory, "phase": "RECOVERING",
                                                "detail": "Restoring the existing Dashboard command channel"})
            self.runtime.batch_phase_update.emit("RECOVERING")
            recovered = False
            for _ in range(2):
                try:
                    await asyncio.wait_for(self.ssh.disconnect(), 5)
                    await asyncio.wait_for(self.ssh.connect(), 15)
                    health = await self.ssh.run("printf WIFI_BATCH_CONTROL_OK", timeout=5)
                    if health.exit_status == 0 and "WIFI_BATCH_CONTROL_OK" in health.stdout:
                        recovered = True
                        break
                except Exception:
                    continue
            if not recovered:
                self.runtime._batch_control_lost.set()
                raise ConnectionError("Shared control path could not be restored") from None
            # Side effects have uncertain outcomes; don't repeat them after a lost reply.
            read_only = bool(re.match(r"(?:iw dev \S+ (?:info|link|station dump)|iw (?:phy|list)|ip (?:route|-[46] addr show)|nmcli -[tgf] |journalctl |hostname(?:ctl --static)?|printf |readlink |ethtool |systemctl is-|uptime|free )", command.lstrip()))
            read_only = read_only and not any(token in command for token in (";", "&&", "||", "|", "`", "$("))
            read_only = read_only and not re.search(r"\b(?:add|del|delete|replace|change|modify|clone|connect|disconnect|up|down)\b", command)
            # Only internal collectors opt in for known read-only RF loops or
            # idempotent, PID/start/ownership-checked iperf cleanup scripts.
            if not (read_only or _batch_retry_safe):
                self.runtime._batch_execution_error = "Control recovered; command outcome uncertain, command was not replayed"
                raise RuntimeError(self.runtime._batch_execution_error) from None
            self.runtime.collector_update.emit({"attempt": self.directory, "phase": "MEASURING",
                                                "detail": "Control restored; continuing the same attempt"})
            self.runtime.batch_phase_update.emit("PRECHECK" if self.directory == "batch-discovery" else "MEASURING")
            try:
                return await self.ssh.run(command, **kwargs)
            except Exception as retry_error:
                if _transport_failure(retry_error):
                    self.runtime._batch_control_lost.set()
                    raise ConnectionError("Shared control path lost again after recovery") from None
                raise
