#!/usr/bin/env python3
"""Print conservative criterion coverage for the retained VD Wi-Fi catalog."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_app.wifi.catalog import load_catalog  # noqa: E402
from desktop_app.wifi.evaluation import coverage_report  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(coverage_report(load_catalog("VD")), ensure_ascii=False, indent=2))
