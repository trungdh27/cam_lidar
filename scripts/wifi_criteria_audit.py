#!/usr/bin/env python3
"""Render a reproducible source/criterion/dependency audit of all 68 VD cases."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_app.wifi.catalog import load_catalog, load_auto_catalog
from desktop_app.wifi.auto_suite import (AutoSuiteSetup, criteria_for_auto, dependencies_for,
                                         qualification_level_for, qualification_reason_for)
from desktop_app.wifi.evaluation import specs_for
from desktop_app.wifi.runtime import METRIC_FIELDS, metric_kind


def audit_cases():
    setup = AutoSuiteSetup.from_project_config()
    report = []
    for case in [*load_catalog("VD"), *load_auto_catalog()]:
        specs = specs_for(case) if case.suite != "AUTO" else criteria_for_auto(case, setup)
        dependencies = dependencies_for(case, setup) if case.suite == "AUTO" else []
        context = list(METRIC_FIELDS[metric_kind(case)])
        critical_metrics = {s.metric for s in specs if s.importance == "CRITICAL"}
        informational = [s.name for s in specs if s.importance == "INFORMATIONAL"]
        informational += [metric for metric in context if metric not in critical_metrics and metric not in informational]
        hard = [dep.label for dep in dependencies if dep.required]
        optional = [dep.label for dep in dependencies if not dep.required]
        if case.suite != "AUTO":
            if case.mode == "AUTO":
                hard += ["Shared Dashboard SSH for automatic capture", "Configured Wi-Fi interface"]
            else:
                hard += ["Tester confirmation of source setup / acceptance"]
                optional += ["Shared SSH read-only snapshot where implemented", "Original output import for guided/manual measurement"]
            if int(case.test_id.rsplit("C", 1)[1]) in {1, 2, 3, 4, 11, 19}:
                hard += sorted({spec.collector for spec in specs if spec.required and spec.collector})
            optional += sorted({spec.collector for spec in specs if not spec.required and spec.collector})
        report.append({"test_id": case.test_id, "name": case.name, "suite": case.suite,
                       "source_acceptance": case.expected,
                       "source": case.source, "source_setup": case.preconditions,
                       "qualification_level": qualification_level_for(case) if case.suite == "AUTO" else "NONE",
                       "qualification_reason": qualification_reason_for(case) if case.suite == "AUTO" else "Existing-suite policy",
                       "critical": [{"name": s.name, "expected": s.expected} for s in specs if s.importance == "CRITICAL"],
                       "supporting": [{"name": s.name, "expected": s.expected} for s in specs if s.importance == "SUPPORTING"],
                       "informational": informational, "hard_dependencies": hard, "optional_dependencies": optional})
    return report


def markdown():
    lines = ["# Wi-Fi tester UX / acceptance audit", "",
             "Generated with `./.venv/bin/python scripts/wifi_criteria_audit.py`. Covers all 26 retained VD cases and 42 AUTO cases without rewriting either source workbook.", "",
             "Critical checks determine acceptance. Supporting checks diagnose consistency. Informational values provide evidence without an invented threshold. When a source explicitly requires recording measurements, a critical capture check preserves that requirement while the measured values remain informational.", "",
             "The shipped AUTO AP runtime source text is retained verbatim, including channel 6 / 2437 MHz and channel 36 / 5180 MHz examples. Because TC001 is a generic band/runtime validation, channel validity and channel/frequency consistency are SUPPORTING checks. They become CRITICAL exact values only when the matching per-band project RF lock is explicitly enabled. SSID is informational when no expected SSID is configured.", "",
             "Wi-Fi 5 (VHT/802.11ac) and Wi-Fi 6 (HE/802.11ax) are both valid for the Wi-Fi 5/6 capability/runtime cases. Client association cases derive the target SSID and AP IPv4 from the running Jetson AP; missing target identity blocks preflight rather than rendering NOT CONFIGURED. Route, client IPv4 and interface-bound reachability remain independent criteria, and the full qualification path is derived after those checks.", "",
             "Source corrections: STA TCP has a 5 Mbps bound only when the 10 m requirement applies; STA endurance has a 30-minute acceptance minimum and optional two-hour qualification. The existing two-hour collector schedule remains available. Average RTT keeps its 100 ms bound; maximum RTT, RSSI and retransmits gain no invented numeric acceptance limit. Qualitative packet loss / UDP acceptance continues to require review unless an explicit source/project limit resolves it.", "",
             "Attempt-local correction: the current ODS explicitly requires no disconnects for STA-003 (ten RF samples) and STA-005 (RTT). Those remain critical; a source without that requirement gets supporting disconnect telemetry instead. Disconnects come only from interface-filtered, deduplicated current-attempt transitions after the association baseline, never boot-history keyword counts. Missing/malformed event evidence is MEASUREMENT ERROR, not DUT FAIL. NetworkManager/driver/service diagnostics are supporting unless the source explicitly requires them. STA-002 separates route, TCP, authentication, remote command, and authoritative current host identity.", "",
             "Channel numbering and frequency consistency do not prove regulatory authorization. The evaluator uses regulatory-enabled PHY channels when exposed and preserves the separate source-required activation/regulatory-error check. [Linux regulatory documentation](https://docs.kernel.org/networking/regulatory.html).", "",
             "Existing guided/manual cases retain inline acceptance review. Their source setup is documented below; this audit does not introduce new execution gates. Tools supporting automatic criteria must provide the corresponding measurement; optional tools/context do not turn into acceptance thresholds.", ""]
    for row in audit_cases():
        lines += [f"## {row['test_id']} — {row['name']}", "", f"Source: `{row['source']}` · {row['suite']}", "",
                  f"**Qualification:** {row['qualification_level']} — {row['qualification_reason']}", ""]
        lines += ["**Source acceptance:** " + row["source_acceptance"].replace("\n", " · "), ""]
        for key, title in (("critical", "Critical"), ("supporting", "Supporting")):
            items = row[key]
            lines.append(f"**{title}:** " + ("; ".join(f"{item['name']} → {item['expected']}" for item in items) or "None"))
            lines.append("")
        for key, title in (("informational", "Informational/context"), ("hard_dependencies", "Hard dependencies"), ("optional_dependencies", "Optional dependencies")):
            lines += [f"**{title}:** " + ("; ".join(row[key]) or "None"), ""]
        lines += ["**Source setup (tester verification):** " + row["source_setup"].replace("\n", " · "), ""]
    return "\n".join(lines)


if __name__ == "__main__":
    print(markdown())
