"""Wi-Fi attempts and evidence, independent of other device domains."""
from __future__ import annotations

import json
import re
import shlex
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from core.remote.ssh_manager import CommandResult

from .catalog import WifiTestCase, load_config
from .parsers import (parse_iperf3, parse_ping, parse_recovery, parse_rf, recovery_metrics,
                      parse_nmcli_device_status, parse_nmcli_device_show, parse_driver,
                      parse_kernel_device_errors, parse_ip_link)
from .parsers import (parse_iw_interface_role, parse_nmcli_profile_list, parse_service_active,
                      parse_iw_phy_capabilities, parse_nmcli_profile_show)
from .evaluation import evaluate


EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / "evidence" / "wifi"


def metric_kind(case: WifiTestCase) -> str:
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

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int((datetime.now(timezone.utc) - self.started).total_seconds()))

    def update_metrics(self, values: dict) -> None:
        self.metrics.update(values)
        for key, value in values.items():
            if isinstance(value, (float, int)):
                self.trends.setdefault(key, deque(maxlen=120)).append((self.elapsed_seconds, value))


class WifiRuntime(QObject):
    changed = Signal()
    message = Signal(str)

    def __init__(self, jetson_service, evidence_root: Path = EVIDENCE_ROOT, parent=None):
        super().__init__(parent)
        self.services = {"Jetson": jetson_service}
        self.evidence_root = Path(evidence_root)
        self.session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.active: WifiAttempt | None = None
        self._request_id: str | None = None
        for service in set(filter(None, self.services.values())):
            service.operation_succeeded.connect(self._operation_succeeded)
            service.operation_failed.connect(self._operation_failed)

    def attempts(self, environment: str, test_id: str) -> list[Path]:
        root = self.evidence_root / environment
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

    def latest_status(self, environment: str, test_id: str) -> str:
        if self.active and (self.active.environment, self.active.case.test_id) == (environment, test_id):
            return self.active.final_result if self.active.status in {"COMPLETED", "ERROR"} else self.active.status
        paths = self.attempts(environment, test_id)
        if not paths:
            return "NOT RUN"
        if paths[-1].name != "result.json":
            return "NEEDS REVIEW"
        try:
            data = json.loads(paths[-1].read_text(encoding="utf-8"))
            return data.get("final_result", data["status"])
        except (OSError, ValueError, KeyError):
            return "NEEDS REVIEW"

    def start(self, environment: str, case: WifiTestCase) -> WifiAttempt | None:
        if self.active and self.active.status == "RUNNING":
            self.message.emit("A Wi-Fi test is already running.")
            return None
        if environment not in {"VD", "VMO", "VR"}:
            raise ValueError(environment)
        root = self.evidence_root / environment
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
            "started": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.active = WifiAttempt(environment, case, number, directory)
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
        summary = {key: sum(row["status"] == state for row in attempt.criteria if row["required"])
                   for key, state in (("pass", "PASS"), ("fail", "FAIL"), ("unknown", "UNKNOWN"),
                                      ("manual_required", "MANUAL_REQUIRED"), ("not_collected", "NOT_COLLECTED"))}
        summary["optional_unknown"] = sum(not row["required"] and row["status"] in {"UNKNOWN", "NOT_COLLECTED"}
                                          for row in attempt.criteria)
        (attempt.directory / "result.json").write_text(json.dumps({
            "status": attempt.final_result, "execution_state": attempt.status,
            "auto_result": attempt.auto_result, "final_result": attempt.final_result,
            "result_reason": attempt.result_reason, "criteria_summary": summary,
            "criteria": attempt.criteria, "evidence": attempt.evidence,
            "override_by": attempt.override_by, "override_reason": attempt.override_reason,
            "override_timestamp": attempt.override_timestamp,
            "comment": attempt.comment, "started": attempt.started.isoformat(),
            "finished": datetime.now(timezone.utc).isoformat(),
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
        self.active.result_reason = f"Capture error: {error}. {self.active.result_reason}"
        self._persist_result()

    def _save_summary(self) -> None:
        if self.active:
            attempt = self.active
            (attempt.directory / "summary.json").write_text(json.dumps({
                "status": attempt.status, "metrics": attempt.metrics,
                "auto_result": attempt.auto_result, "final_result": attempt.final_result,
                "criteria": attempt.criteria, "evidence": attempt.evidence,
                "elapsed_seconds": attempt.elapsed_seconds,
                "started": attempt.started.isoformat(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _operation_succeeded(self, request_id: str, results: object) -> None:
        if request_id != self._request_id or not self.active:
            return
        interface = load_config()["Wi-Fi interface"]
        c01 = self.active.case.test_id == "TC-WIFI-C01"
        status_seen = False
        ip_missing = False
        for index, result in enumerate(results):
            raw = f"$ {result.command}\n{result.stdout}{result.stderr}\n[exit {result.exit_status}]\n"
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
        if request_id == self._request_id:
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
                load_config()["Wi-Fi interface"], attempt.elapsed_seconds)
            attempt.criteria = criteria
            if final:
                attempt.auto_result = result
                attempt.result_reason = reason
