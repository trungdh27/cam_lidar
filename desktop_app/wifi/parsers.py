"""Presentation-only Wi-Fi parsers. Original command output is evidence."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath


def parse_nmcli_device_status(raw: str, interface: str) -> dict[str, str]:
    """Parse terse DEVICE:TYPE:STATE:CONNECTION rows, including escaped colons."""
    for line in raw.splitlines():
        fields = re.split(r"(?<!\\):", line.strip())
        fields = [field.replace(r"\:", ":").strip() for field in fields]
        if len(fields) >= 3 and fields[0] == interface:
            return {"Interface": fields[0], "Device Type": fields[1],
                    "NetworkManager": fields[2], "NetworkManager state": fields[2]}
    return {}


def parse_nmcli_device_show(raw: str, interface: str) -> dict[str, str]:
    fields = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    if fields.get("GENERAL.DEVICE") != interface:
        return {}
    values = {"Interface": interface}
    if fields.get("GENERAL.TYPE"):
        values["Device Type"] = fields["GENERAL.TYPE"]
    if fields.get("GENERAL.STATE"):
        state = fields["GENERAL.STATE"]
        state = re.sub(r"^\d+\s*\((.+)\)$", r"\1", state)
        values["NetworkManager"] = state
        values["NetworkManager state"] = state
    return values


def parse_ip_link(raw: str, interface: str) -> dict[str, str]:
    return {"Interface": interface} if any(line.split() and line.split()[0] == interface
                                        for line in raw.splitlines()) else {}


def parse_iw_interface_role(raw: str, interface: str) -> dict[str, str]:
    match = re.search(r"^\s*Interface\s+" + re.escape(interface) + r"\s*$([\s\S]*?)(?=^\s*Interface\s+|\Z)",
                      raw, re.MULTILINE)
    if not match:
        return {}
    values = {"Interface": interface}
    role = re.search(r"^\s*type\s+(\S+)", match.group(1), re.MULTILINE)
    if role:
        values["Role"] = role.group(1)
    return values


def parse_nmcli_profile_list(raw: str, profile: str) -> dict[str, str]:
    found = any(line.strip() == profile or line.startswith(profile + " ")
                for line in raw.splitlines())
    return {"Hotspot profile": "Present" if found else "Absent"}


def parse_service_active(raw: str) -> dict[str, str]:
    state = raw.strip().splitlines()[0] if raw.strip() else ""
    return {"NetworkManager": state} if state else {}


def parse_iw_phy_capabilities(raw: str) -> dict[str, object]:
    modes = re.search(r"Supported interface modes:([\s\S]*?)(?=^\S|\Z)", raw, re.MULTILINE)
    ap = bool(modes and re.search(r"^\s*\*\s+AP\s*$", modes.group(1), re.MULTILINE))
    frequencies = [int(value) for value in re.findall(r"\*\s*(\d+)\s*MHz", raw)]
    bands = []
    if any(2400 <= value < 2500 for value in frequencies): bands.append("2.4 GHz")
    if any(4900 <= value < 5900 for value in frequencies): bands.append("5 GHz")
    if any(5925 <= value <= 7125 for value in frequencies): bands.append("6 GHz")
    return {"AP capability": "Supported" if ap else "Unsupported",
            "Supported Bands": bands} if raw.strip() else {}


def parse_nmcli_profile_show(raw: str) -> dict[str, str]:
    mapping = {"802-11-wireless.mode": "Mode", "802-11-wireless.ssid": "SSID",
               "802-11-wireless.band": "Band", "802-11-wireless.channel": "Channel",
               "802-11-wireless-security.key-mgmt": "Security", "ipv4.method": "IPv4 method",
               "connection.autoconnect": "Autoconnect"}
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in mapping:
            values[mapping[key.strip()]] = value.strip()
    return values


def parse_driver(raw: str, command: str) -> dict[str, str]:
    if command.startswith("readlink"):
        path = raw.strip().splitlines()[0] if raw.strip() else ""
        name = PurePosixPath(path).name if path.startswith("/") else ""
        return {"Driver/PHY": name} if name and name not in {"driver", "device"} else {}
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if not separator or not value.strip():
            continue
        if key.strip() == "driver": values["Driver/PHY"] = value.strip()
        if key.strip() == "firmware-version" and value.strip().lower() not in {"n/a", "unknown"}:
            values["Firmware"] = value.strip()
    return values


def parse_kernel_device_errors(raw: str, interface: str) -> dict[str, int]:
    relevant = [line for line in raw.splitlines() if re.search(r"device not found", line, re.I)
                and (interface.lower() in line.lower() or re.search(r"wifi|wi-fi|wlan|802\.11", line, re.I))]
    return {"Kernel Device Errors": len(relevant)}


def parse_iperf3(raw: str, direction: str) -> dict[str, float]:
    """Read an iperf3 JSON report. Direction is the tester's observed path."""
    if direction not in {"Upload", "Download"}:
        raise ValueError(direction)
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(report, dict):
        return {}
    end = report.get("end", {})
    if not isinstance(end, dict):
        return {}
    summary = end.get("sum_received") or end.get("sum_sent") or {}
    intervals = report.get("intervals", [])
    rates = []
    for interval in intervals:
        sample = interval.get("sum", {}) if isinstance(interval, dict) else {}
        rate = sample.get("bits_per_second")
        if isinstance(rate, (int, float)):
            rates.append(rate / 1_000_000)
    average = summary.get("bits_per_second")
    if not isinstance(average, (int, float)):
        return {}
    average /= 1_000_000
    current = rates[-1] if rates else average
    metrics = {
        direction: round(current, 3),
        f"Current {direction.lower()}": round(current, 3),
        f"Average {direction.lower()}": round(average, 3),
        f"Min {direction.lower()}": round(min(rates), 3) if rates else round(average, 3),
        f"Max {direction.lower()}": round(max(rates), 3) if rates else round(average, 3),
    }
    sent = end.get("sum_sent", {})
    received = end.get("sum_received", {})
    if isinstance(sent, dict) and isinstance(sent.get("bits_per_second"), (int, float)):
        metrics["Sender Mbps"] = round(sent["bits_per_second"] / 1_000_000, 3)
    if isinstance(received, dict) and isinstance(received.get("bits_per_second"), (int, float)):
        metrics["Receiver Mbps"] = round(received["bits_per_second"] / 1_000_000, 3)
    if isinstance(sent, dict) and isinstance(sent.get("retransmits"), int):
        metrics["Retransmits"] = sent["retransmits"]
    udp = end.get("sum") if isinstance(end.get("sum"), dict) else received
    if isinstance(udp, dict):
        if isinstance(udp.get("lost_percent"), (int, float)):
            metrics["UDP loss"] = float(udp["lost_percent"])
        if isinstance(udp.get("jitter_ms"), (int, float)):
            metrics["UDP jitter"] = float(udp["jitter_ms"])
    return metrics


def parse_ping(raw: str) -> dict[str, float]:
    """Parse measurements from a Wi-Fi latency run, never infer a result."""
    metrics = {}
    samples = [float(value) for value in re.findall(r"\btime[=<]([\d.]+)\s*ms\b", raw)]
    if samples:
        metrics["RTT"] = samples[-1]
        metrics["Current RTT"] = samples[-1]
    match = re.search(r"([\d.]+)%\s*packet loss", raw)
    if match:
        metrics["Packet loss"] = float(match.group(1))
    summary = re.search(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets )?received", raw)
    if summary:
        metrics["Packets transmitted"] = int(summary.group(1))
        metrics["Packets received"] = int(summary.group(2))
    match = re.search(r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
                      r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", raw)
    if match:
        low, average, high, jitter = map(float, match.groups())
        metrics.update({"Min RTT": low, "Avg RTT": average, "Average RTT": average,
                        "Max RTT": high, "Jitter": jitter})
    return metrics


def parse_rf(raw: str) -> dict[str, str | float]:
    """Parse common `iw link/info` fields; missing fields remain unknown."""
    metrics: dict[str, str | float] = {}
    patterns = {
        "SSID": r"^\s*SSID:\s*(.+)$",
        "BSSID": r"^\s*Connected to\s+([0-9a-f:]{17})",
        "Channel": r"\bchannel\s+(\d+)",
        "Frequency": r"\bfreq:\s*(\d+)",
        "Width": r"\bwidth:\s*([^,\n]+)",
        "RSSI": r"\bsignal:\s*(-?\d+(?:\.\d+)?)\s*dBm",
        "Security": r"^\s*security:\s*(.+)$",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, raw, re.MULTILINE | re.IGNORECASE)
        if match:
            metrics[name] = float(match.group(1)) if name == "RSSI" else match.group(1).strip()
    if "Frequency" in metrics:
        frequency = int(metrics["Frequency"])
        if 2400 <= frequency < 2500:
            metrics["Band"] = "2.4 GHz"
        elif 4900 <= frequency < 5900:
            metrics["Band"] = "5 GHz"
        elif 5925 <= frequency <= 7125:
            metrics["Band"] = "6 GHz"
    return metrics


def parse_recovery(raw: str) -> dict[str, object]:
    """Extract explicitly recorded cycle outcomes, without assuming success."""
    entries = re.findall(r"^\s*Cycle\s+(\d+)\s*[:\-]?\s*(PASS|FAIL)\s*(?:([\d.]+)\s*s)?",
                         raw, re.MULTILINE | re.IGNORECASE)
    if not entries:
        return {}
    records = {number: {"status": status.upper(), "seconds": float(duration) if duration else None}
               for number, status, duration in entries}
    return recovery_metrics(records)


def recovery_metrics(records: dict[str, dict]) -> dict[str, object]:
    ordered = sorted(records.items(), key=lambda item: int(item[0]))
    cycles = [f"Cycle {number}: {entry['status']}"
              + (f" {entry['seconds']:g} s" if entry["seconds"] is not None else "")
              for number, entry in ordered]
    times = [entry["seconds"] for _, entry in ordered
             if entry["status"] == "PASS" and entry["seconds"] is not None]
    passed = sum(entry["status"] == "PASS" for _, entry in ordered)
    metrics: dict[str, object] = {"Cycle records": records, "Cycles": cycles,
                                  "Current cycle": int(ordered[-1][0]),
                                  "Completed cycles": len(ordered),
                                  "Successful cycles": passed,
                                  "Failed cycles": len(ordered) - passed,
                                  "Failures": len(ordered) - passed}
    if times:
        metrics.update({"Avg recovery": round(sum(times) / len(times), 3),
                        "Max recovery": max(times)})
    return metrics
