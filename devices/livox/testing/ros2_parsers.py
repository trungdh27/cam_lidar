from __future__ import annotations

import re
import statistics


SECTION_RE = re.compile(r"^__LIDAR_SECTION__=([A-Z0-9_]+)$")


def parse_sectioned_output(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = None
    for raw_line in str(output).splitlines():
        match = SECTION_RE.match(raw_line.strip())
        if match:
            current = match.group(1)
            if current in sections:
                raise ValueError(f"Duplicate output section: {current}")
            sections[current] = []
        elif current is not None:
            sections[current].append(raw_line.rstrip())
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def parse_ros2_hz(output: str) -> dict[str, float | int | None]:
    averages = [
        float(value)
        for value in re.findall(r"average rate:\s*([0-9]+(?:\.[0-9]+)?)", output)
    ]
    minimums = [float(value) for value in re.findall(r"min:\s*([0-9.]+)s", output)]
    maximums = [float(value) for value in re.findall(r"max:\s*([0-9.]+)s", output)]
    stddevs = [float(value) for value in re.findall(r"std dev:\s*([0-9.]+)s", output)]
    return {
        "sample_count": len(averages),
        "mean_hz": statistics.fmean(averages) if averages else None,
        "min_interval_sec": min(minimums) if minimums else None,
        "max_interval_sec": max(maximums) if maximums else None,
        "mean_stddev_sec": statistics.fmean(stddevs) if stddevs else None,
    }


def parse_key_values(output: str) -> dict[str, str]:
    values = {}
    for line in str(output).splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def configured_bounds(target, tolerance_percent) -> tuple[float, float] | None:
    if target is None or tolerance_percent is None:
        return None
    target = float(target)
    tolerance = target * float(tolerance_percent) / 100.0
    return target - tolerance, target + tolerance
