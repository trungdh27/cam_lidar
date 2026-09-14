from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .evidence_summary import SystemMetricsSummary, format_local_timestamp
from .session import atomic_write_json


DEFAULT_BASELINE_DURATION_SEC = 30.0
DEFAULT_SAMPLE_INTERVAL_SEC = 1.0


@dataclass(frozen=True)
class BaselineCapture:
    data: dict
    system_metrics_raw: str
    tegrastats_raw: str = ""
    kernel_raw: str = ""
    journal_raw: str = ""
    stress_ng_available: bool | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_local_sample() -> str:
    timestamp = _utc_now()
    lines = [timestamp]
    try:
        for line in Path("/proc/stat").read_text(encoding="ascii", errors="replace").splitlines():
            if not line.startswith("cpu"):
                break
            lines.append(line)
    except OSError:
        pass
    try:
        load = os.getloadavg()
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        hours, remainder = divmod(int(uptime // 60), 60)
        lines.append(
            f"00:00:00 up {hours}:{remainder:02d}, 1 user, "
            f"load average: {load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}"
        )
    except (OSError, ValueError, IndexError):
        pass
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0]) * 1024
        total = memory.get("MemTotal")
        available = memory.get("MemAvailable")
        if total is not None and available is not None:
            used = max(0, total - available)
            lines.append(f"Mem: {total} {used} 0 0 0 {available}")
        swap_total = memory.get("SwapTotal")
        swap_free = memory.get("SwapFree")
        if swap_total is not None and swap_free is not None:
            lines.append(f"Swap: {swap_total} {max(0, swap_total - swap_free)} {swap_free}")
    except (OSError, ValueError, IndexError):
        pass
    return "\n".join(lines) + "\n"


def _parse_tegrastats(raw: str) -> dict:
    import re

    result = {}
    cpu = re.findall(r"CPU(?:@|\s+temp=)([\d.]+)C", raw, re.IGNORECASE)
    gpu = re.findall(r"GPU(?:@|\s+temp=)([\d.]+)C", raw, re.IGNORECASE)
    gpu_load = re.findall(r"GR3D_FREQ\s+(\d+)%", raw)
    frequencies = re.findall(r"CPU\s+\[(.*?)\]", raw)
    power = re.findall(r"\b(VDD_[A-Z0-9_]+)\s+(\d+)mW", raw)
    if cpu:
        result["cpu_c"] = float(cpu[-1])
        result["cpu_avg_c"] = sum(map(float, cpu)) / len(cpu)
        result["cpu_min_c"] = min(map(float, cpu))
        result["cpu_max_c"] = max(map(float, cpu))
    if gpu:
        result["gpu_c"] = float(gpu[-1])
        result["gpu_avg_c"] = sum(map(float, gpu)) / len(gpu)
        result["gpu_min_c"] = min(map(float, gpu))
        result["gpu_max_c"] = max(map(float, gpu))
    if gpu_load:
        result["gpu_percent"] = float(gpu_load[-1])
        result["gpu_avg_percent"] = sum(map(float, gpu_load)) / len(gpu_load)
        result["gpu_min_percent"] = min(map(float, gpu_load))
        result["gpu_max_percent"] = max(map(float, gpu_load))
    if frequencies:
        result["cpu_frequency_raw"] = frequencies[-1]
    if power:
        result["power_mw"] = {name: int(value) for name, value in power}
    return result


def _error_summary(raw: str) -> dict:
    lines = [line for line in raw.splitlines() if line.strip()]
    warnings = sum("warn" in line.lower() for line in lines)
    errors = sum(any(word in line.lower() for word in ("error", " err", "critical", "crit")) for line in lines)
    return {"warning_count": warnings, "error_count": errors}


def build_baseline(
    raw: str,
    *,
    start_time: str,
    end_time: str,
    duration_sec: float,
    tegrastats_raw: str = "",
    kernel_raw: str = "",
    journal_raw: str = "",
) -> dict:
    parser = SystemMetricsSummary()
    parser.feed(raw)
    data = {
        "start_time": start_time,
        "end_time": end_time,
        "measured_at": end_time,
        "sample_duration_sec": round(max(0.0, duration_sec), 3),
    }
    data.update(parser.baseline_data())
    thermal = _parse_tegrastats(tegrastats_raw)
    if thermal:
        data["thermal"] = thermal
    if kernel_raw.strip():
        data["kernel"] = _error_summary(kernel_raw)
    if journal_raw.strip():
        data["journal"] = _error_summary(journal_raw)
    return data


def baseline_summary_text(data: dict) -> str:
    cpu = data.get("cpu", {})
    loads = data.get("load_average", {})
    memory = data.get("memory", {})
    thermal = data.get("thermal", {})
    kernel = data.get("kernel", {})
    journal = data.get("journal", {})

    def percent(value) -> str:
        return "—" if value is None else f"{value:.1f} %"

    def gib(value) -> str:
        return "—" if value is None else f"{value:.2f} GiB"

    return "\n".join(
        [
            "SYSTEM BASELINE",
            "",
            "CPU",
            f"Average        {percent(cpu.get('avg_percent'))}",
            f"Min            {percent(cpu.get('min_percent'))}",
            f"Max            {percent(cpu.get('max_percent'))}",
            "",
            "Load Average",
            f"1m             {loads.get('1m', '—')}",
            f"5m             {loads.get('5m', '—')}",
            f"15m            {loads.get('15m', '—')}",
            "",
            "Memory",
            f"Used           {gib(memory.get('used_gib'))}",
            f"Available      {gib(memory.get('available_gib'))}",
            f"Swap Used      {gib(memory.get('swap_used_gib'))}",
            "",
            "Thermal",
            f"CPU            {thermal.get('cpu_c', '—')} °C",
            f"GPU            {thermal.get('gpu_c', '—')} °C",
            "",
            "Kernel / Journal",
            f"Warnings       {kernel.get('warning_count', '—')} / {journal.get('warning_count', '—')}",
            f"Errors         {kernel.get('error_count', '—')} / {journal.get('error_count', '—')}",
            "",
            f"Last measured  {format_local_timestamp(data.get('measured_at'))}",
            f"Duration       {data.get('sample_duration_sec', 0):g} sec",
        ]
    )


def write_baseline_capture(
    capture: BaselineCapture,
    *,
    json_path: Path,
    metrics_path: Path,
    summary_path: Path | None = None,
    tegrastats_path: Path | None = None,
    kernel_path: Path | None = None,
    journal_path: Path | None = None,
) -> None:
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(capture.system_metrics_raw, encoding="utf-8", errors="replace")
    if tegrastats_path is not None:
        tegrastats_path.write_text(capture.tegrastats_raw, encoding="utf-8", errors="replace")
    if kernel_path is not None:
        kernel_path.write_text(capture.kernel_raw, encoding="utf-8", errors="replace")
    if journal_path is not None:
        journal_path.write_text(capture.journal_raw, encoding="utf-8", errors="replace")
    atomic_write_json(json_path, capture.data)
    if summary_path is not None:
        summary_path.write_text(baseline_summary_text(capture.data) + "\n", encoding="utf-8")


class SystemBaselineCollector:
    def __init__(
        self,
        duration_sec: float = DEFAULT_BASELINE_DURATION_SEC,
        sample_interval_sec: float = DEFAULT_SAMPLE_INTERVAL_SEC,
        *,
        sample_reader: Callable[[], str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.duration_sec = max(0.0, float(duration_sec))
        self.sample_interval_sec = max(0.001, float(sample_interval_sec))
        self.sample_reader = sample_reader or _read_local_sample
        self.sleeper = sleeper

    @property
    def sample_count(self) -> int:
        return max(2, int(math.ceil(self.duration_sec / self.sample_interval_sec)) + 1)

    def collect_local(self, should_stop: Callable[[], bool] | None = None) -> BaselineCapture:
        start = _utc_now()
        started = time.monotonic()
        chunks = []
        for index in range(self.sample_count):
            if should_stop and should_stop():
                break
            chunks.append(self.sample_reader())
            if index + 1 < self.sample_count:
                self.sleeper(self.sample_interval_sec)
        ended = time.monotonic()
        raw = "".join(chunks)
        if should_stop and should_stop():
            end = _utc_now()
            data = build_baseline(raw, start_time=start, end_time=end, duration_sec=ended - started)
            return BaselineCapture(data, raw, stress_ng_available=None)
        tegra = self._local_command(["timeout", "2", "tegrastats", "--interval", "1000"]) if shutil.which("tegrastats") else ""
        kernel = self._local_command(["dmesg", "--level=emerg,alert,crit,err,warn"]) if shutil.which("dmesg") else ""
        journal = self._local_command(["journalctl", "-p", "warning..alert", "-b", "-n", "200", "--no-pager"]) if shutil.which("journalctl") else ""
        end = _utc_now()
        data = build_baseline(raw, start_time=start, end_time=end, duration_sec=ended - started, tegrastats_raw=tegra, kernel_raw=kernel, journal_raw=journal)
        return BaselineCapture(data, raw, tegra, kernel, journal, shutil.which("stress-ng") is not None)

    @staticmethod
    def _local_command(command: list[str]) -> str:
        try:
            result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=5, check=False)
            return (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__}: {exc}\n"

    async def collect_remote(self, ssh) -> BaselineCapture:
        count = self.sample_count
        interval = self.sample_interval_sec
        command = (
            f"i=0; while [ $i -lt {count} ]; do "
            "date --iso-8601=seconds; head -n \"$(( $(nproc) + 1 ))\" /proc/stat; uptime; free -b; "
            f"i=$((i+1)); [ $i -ge {count} ] || sleep {interval:g}; done"
        )
        start = _utc_now()
        started = time.monotonic()
        metrics = await ssh.run(command, timeout=max(10.0, self.duration_sec + 10.0))
        tegra, kernel, journal, stress = await asyncio.gather(
            ssh.run("command -v tegrastats >/dev/null 2>&1 && timeout 2 tegrastats --interval 1000 || true", timeout=5),
            ssh.run("dmesg --level=emerg,alert,crit,err,warn 2>&1 | tail -n 200", timeout=5),
            ssh.run("journalctl -p warning..alert -b -n 200 --no-pager 2>&1", timeout=5),
            ssh.run("command -v stress-ng >/dev/null 2>&1", timeout=5),
        )
        ended = time.monotonic()
        end = _utc_now()
        raw = (metrics.stdout or "") + (metrics.stderr or "")
        tegra_raw = (tegra.stdout or "") + (tegra.stderr or "")
        kernel_raw = (kernel.stdout or "") + (kernel.stderr or "")
        journal_raw = (journal.stdout or "") + (journal.stderr or "")
        data = build_baseline(raw, start_time=start, end_time=end, duration_sec=ended - started, tegrastats_raw=tegra_raw, kernel_raw=kernel_raw, journal_raw=journal_raw)
        return BaselineCapture(data, raw, tegra_raw, kernel_raw, journal_raw, stress.exit_status == 0)
