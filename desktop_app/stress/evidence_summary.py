from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo


_STREAM_PREFIX = re.compile(r"^\[(?:stdout|stderr)\]\s*")
_CPU_LINE = re.compile(r"^(cpu\d*)\s+(.+)$")
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$")


def format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    return f"{value / 1024**2:.2f} MiB"


def format_local_timestamp(value: str | None, local_timezone: tzinfo | None = None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        localized = parsed.astimezone(local_timezone) if local_timezone else parsed.astimezone()
    except ValueError:
        return value
    rendered = localized.strftime("%Y-%m-%d %H:%M:%S %z")
    return f"{rendered[:-2]}:{rendered[-2:]}" if len(rendered) >= 5 else rendered


def _format_percent(value: float | None) -> str:
    return "Waiting for next sample" if value is None else f"{value:.1f} %"


def _format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total_minutes = max(0, int(seconds // 60))
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@dataclass
class SystemMetricsState:
    timestamp: str | None = None
    cpu_usage: dict[str, float] = field(default_factory=dict)
    loads: tuple[float, float, float] | None = None
    uptime_seconds: float | None = None
    ram_total: int | None = None
    ram_used: int | None = None
    ram_available: int | None = None
    swap_total: int | None = None
    swap_used: int | None = None

    @property
    def ram_usage_percent(self) -> float | None:
        if not self.ram_total or self.ram_used is None:
            return None
        return 100.0 * self.ram_used / self.ram_total


class SystemMetricsSummary:
    """Incremental presentation state for existing System Metrics raw logs."""

    def __init__(self, *, target_cpu_percent: float | None = None, local_timezone: tzinfo | None = None):
        self.target_cpu_percent = target_cpu_percent
        self.local_timezone = local_timezone
        self.state = SystemMetricsState()
        self._previous_cpu: dict[str, tuple[int, int]] = {}
        self._overall_samples: list[float] = []
        self._cpu_samples: dict[str, list[float]] = {}

    @property
    def current_cpu(self) -> float | None:
        return self.state.cpu_usage.get("cpu")

    @property
    def average_cpu(self) -> float | None:
        return sum(self._overall_samples) / len(self._overall_samples) if self._overall_samples else None

    @property
    def minimum_cpu(self) -> float | None:
        return min(self._overall_samples) if self._overall_samples else None

    @property
    def maximum_cpu(self) -> float | None:
        return max(self._overall_samples) if self._overall_samples else None

    def feed(self, content: str) -> None:
        snapshot: dict = {"cpu": {}}
        for raw_line in content.splitlines():
            line = _STREAM_PREFIX.sub("", raw_line).strip()
            if not line or line.startswith("[Log file"):
                continue
            if _ISO_TIMESTAMP.match(line):
                if snapshot["cpu"]:
                    self._apply_snapshot(snapshot)
                    snapshot = {"cpu": {}}
                snapshot["timestamp"] = line
                continue
            if self._parse_compact_line(line, snapshot):
                self._apply_snapshot(snapshot)
                snapshot = {"cpu": {}}
                continue
            cpu = _CPU_LINE.match(line)
            if cpu:
                snapshot["cpu_seen"] = True
                counters = self._cpu_counters(cpu.group(2))
                if counters is not None:
                    snapshot["cpu"][cpu.group(1)] = counters
                continue
            if "load average:" in line:
                loads = re.search(r"load average:\s*([\d.]+),?\s+([\d.]+),?\s+([\d.]+)", line)
                if loads:
                    snapshot["loads"] = tuple(float(loads.group(index)) for index in range(1, 4))
                snapshot["uptime_seconds"] = self._parse_uptime(line)
                continue
            if line.startswith("Mem:"):
                values = self._integer_values(line.removeprefix("Mem:"))
                if len(values) >= 6:
                    snapshot["ram_total"], snapshot["ram_used"], snapshot["ram_available"] = values[0], values[1], values[5]
                continue
            if line.startswith("Swap:"):
                values = self._integer_values(line.removeprefix("Swap:"))
                if len(values) >= 2:
                    snapshot["swap_total"], snapshot["swap_used"] = values[0], values[1]
                self._apply_snapshot(snapshot)
                snapshot = {"cpu": {}}
        if snapshot["cpu"] or any(key in snapshot for key in ("ram_total", "loads", "timestamp")):
            self._apply_snapshot(snapshot)

    @staticmethod
    def _cpu_counters(value: str) -> tuple[int, int] | None:
        try:
            fields = [int(part) for part in value.split()]
        except ValueError:
            return None
        if len(fields) < 4:
            return None
        counted = fields[:8]
        total = sum(counted)
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return total, idle

    @staticmethod
    def _integer_values(value: str) -> list[int]:
        result = []
        for part in value.split():
            try:
                result.append(int(part))
            except ValueError:
                return []
        return result

    @staticmethod
    def _parse_uptime(line: str) -> float | None:
        match = re.search(r"\bup\s+(?:(\d+)\s+days?,\s*)?(?:(\d+):(\d+)|(\d+)\s+min)", line)
        if not match:
            return None
        days = int(match.group(1) or 0)
        if match.group(4):
            hours, minutes = 0, int(match.group(4))
        else:
            hours, minutes = int(match.group(2) or 0), int(match.group(3) or 0)
        return float(days * 86400 + hours * 3600 + minutes * 60)

    def _parse_compact_line(self, line: str, snapshot: dict) -> bool:
        if not _ISO_TIMESTAMP.match(line.split(maxsplit=1)[0]) or "load1=" not in line:
            return False
        parts = line.split()
        snapshot["timestamp"] = parts[0]
        compact_cpu = {}
        for key, value in re.findall(r"\b(cpu\d*)=([\d.]+)%", line):
            # Local collection already calculates deltas at collection time.
            compact_cpu[key] = float(value)
        snapshot["computed_cpu"] = compact_cpu
        values = dict(re.findall(r"\b(load1|load5|load15|uptime_sec)=([\d.]+)", line))
        if all(key in values for key in ("load1", "load5", "load15")):
            snapshot["loads"] = tuple(float(values[key]) for key in ("load1", "load5", "load15"))
        if "uptime_sec" in values:
            snapshot["uptime_seconds"] = float(values["uptime_sec"])
        available = re.search(r"\bmem_available=(\d+)\s*(kB|KiB|MB|MiB|GB|GiB)?", line, re.IGNORECASE)
        if available:
            multiplier = {"kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2, "gb": 1024**3, "gib": 1024**3}.get((available.group(2) or "").lower(), 1)
            snapshot["ram_available"] = int(available.group(1)) * multiplier
        return True

    def _apply_snapshot(self, snapshot: dict) -> None:
        usage: dict[str, float] = dict(snapshot.get("computed_cpu", {}))
        current_cpu = snapshot.get("cpu", {})
        for name, (total, idle) in current_cpu.items():
            previous = self._previous_cpu.get(name)
            if previous is None:
                continue
            delta_total = total - previous[0]
            delta_idle = idle - previous[1]
            if delta_total > 0 and 0 <= delta_idle <= delta_total:
                usage[name] = 100.0 * (delta_total - delta_idle) / delta_total
        if snapshot.get("cpu_seen"):
            # Replace instead of merging so a temporarily missing core cannot be
            # compared with a stale sample from multiple intervals ago.
            self._previous_cpu = dict(current_cpu)
        if "cpu" in usage:
            self._overall_samples.append(usage["cpu"])
        for name, value in usage.items():
            self._cpu_samples.setdefault(name, []).append(value)
        self.state.cpu_usage = usage
        for key in (
            "timestamp",
            "loads",
            "uptime_seconds",
            "ram_total",
            "ram_used",
            "ram_available",
            "swap_total",
            "swap_used",
        ):
            if key in snapshot:
                setattr(self.state, key, snapshot[key])

    def render(self, status: str, *, refresh_seconds: int = 10) -> str:
        lines = [
            f"SYSTEM METRICS                         ● {status}",
            "",
            f"Last update    {format_local_timestamp(self.state.timestamp, self.local_timezone)}",
            f"Refresh        {refresh_seconds} sec",
            "",
            "CPU",
            f"Overall        {_format_percent(self.current_cpu)}",
        ]
        if self.target_cpu_percent is not None:
            lines.extend(
                [
                    f"Target CPU     {self.target_cpu_percent:g} %",
                    f"Current CPU    {_format_percent(self.current_cpu)}",
                    f"Average CPU    {_format_percent(self.average_cpu)}",
                    f"Minimum CPU    {_format_percent(self.minimum_cpu)}",
                    f"Maximum CPU    {_format_percent(self.maximum_cpu)}",
                ]
            )
        loads = self.state.loads or (None, None, None)
        lines.extend(
            [
                "",
                f"Load 1m        {'—' if loads[0] is None else f'{loads[0]:.2f}'}",
                f"Load 5m        {'—' if loads[1] is None else f'{loads[1]:.2f}'}",
                f"Load 15m       {'—' if loads[2] is None else f'{loads[2]:.2f}'}",
                f"Uptime         {_format_uptime(self.state.uptime_seconds)}",
                "",
                "PER-CORE CPU",
            ]
        )
        cores = sorted(
            ((name, value) for name, value in self.state.cpu_usage.items() if name != "cpu"),
            key=lambda item: int(item[0][3:]),
        )
        lines.extend(f"{name.upper():<14}{value:.1f} %" for name, value in cores)
        if not cores:
            lines.append("Waiting for next sample")
        lines.extend(
            [
                "",
                "MEMORY",
                f"RAM Total      {format_bytes(self.state.ram_total)}",
                f"RAM Used       {format_bytes(self.state.ram_used)}",
                f"RAM Available  {format_bytes(self.state.ram_available)}",
                f"RAM Usage      {'—' if self.state.ram_usage_percent is None else f'{self.state.ram_usage_percent:.1f} %'}",
                f"Swap Total     {format_bytes(self.state.swap_total)}",
                f"Swap Used      {format_bytes(self.state.swap_used)}",
            ]
        )
        return "\n".join(lines)

    def baseline_data(self) -> dict:
        result: dict = {}
        if self._overall_samples:
            result["cpu"] = {
                "current_percent": self.current_cpu,
                "avg_percent": sum(self._overall_samples) / len(self._overall_samples),
                "min_percent": min(self._overall_samples),
                "max_percent": max(self._overall_samples),
                "sample_count": len(self._overall_samples),
                "per_core_avg": {
                    name: sum(values) / len(values)
                    for name, values in sorted(self._cpu_samples.items())
                    if name != "cpu" and values
                },
            }
        memory = {}
        if self.state.ram_total is not None:
            memory["total_gib"] = self.state.ram_total / 1024**3
        if self.state.ram_used is not None:
            memory["used_gib"] = self.state.ram_used / 1024**3
        if self.state.ram_available is not None:
            memory["available_gib"] = self.state.ram_available / 1024**3
        if self.state.ram_usage_percent is not None:
            memory["usage_percent"] = self.state.ram_usage_percent
        if self.state.swap_total is not None:
            memory["swap_total_gib"] = self.state.swap_total / 1024**3
        if self.state.swap_used is not None:
            memory["swap_used_gib"] = self.state.swap_used / 1024**3
        if memory:
            result["memory"] = memory
        if self.state.loads is not None:
            result["load_average"] = dict(zip(("1m", "5m", "15m"), self.state.loads, strict=True))
        if self.state.uptime_seconds is not None:
            result["system"] = {"uptime_sec": self.state.uptime_seconds}
        return result


class KernelWarningSummary:
    def __init__(self, *, local_timezone: tzinfo | None = None):
        self.local_timezone = local_timezone
        self.lines: list[str] = []
        self.last_timestamp: str | None = None

    def feed(self, content: str) -> None:
        for raw_line in content.splitlines():
            line = _STREAM_PREFIX.sub("", raw_line).strip()
            if not line or line.startswith("[Log file"):
                continue
            self.lines.append(line)
            timestamp = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})", line)
            if timestamp:
                self.last_timestamp = timestamp.group(0)
        self.lines = self.lines[-100:]

    def render(self, status: str) -> str:
        recent = self.lines[-10:]
        return "\n".join(
            [
                f"KERNEL ERROR LOG                     ⚠ {status}",
                "",
                f"Warning Count   {len(self.lines)}",
                f"Last Warning    {format_local_timestamp(self.last_timestamp, self.local_timezone)}",
                "",
                "RECENT WARNING LINES",
                *(recent or ["No warning lines captured."]),
            ]
        )


def target_cpu_percent(definition) -> float | None:
    value = getattr(definition, "target_cpu_percent", None)
    return float(value) if value is not None else None
