"""Attempt-local, interface-filtered association transitions, not keyword counts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone


def disconnect_transitions(raw, interface, *, connected, started=None, completed=None):
    state = "CONNECTED" if connected else "DISCONNECTED"
    events, reconnects = [], 0
    for line in raw.splitlines():
        timestamp = None
        try:
            record = json.loads(line)
            line = record.get("MESSAGE", "")
            timestamp = datetime.fromtimestamp(int(record["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc)
        except (ValueError, TypeError, KeyError):
            stamp = re.match(r"(\d{4}-\d\d-\d\dT\S+)\s", line)
            if stamp:
                try:
                    timestamp = datetime.fromisoformat(stamp.group(1))
                except ValueError:
                    continue
        if started is not None:
            if timestamp is None or timestamp.tzinfo is None or timestamp < started or completed is not None and timestamp > completed:
                continue
        match = re.search(r"device\s+\(" + re.escape(interface) + r"\):\s*state change:\s*(\w+)\s*->\s*(\w+)", line, re.I)
        if not match:
            continue
        previous, current = (value.lower() for value in match.groups())
        if current in {"activated", "connected"}:
            if state == "DISCONNECTED":
                reconnects += 1
            state = "CONNECTED"
        elif current in {"disconnected", "deactivating", "unavailable", "failed"}:
            if state == "CONNECTED":
                events.append({"timestamp": timestamp.isoformat() if timestamp else "attempt cursor window",
                               "interface": interface, "previous_state": "CONNECTED",
                               "new_state": "DISCONNECTED", "source": "NetworkManager"})
            state = "DISCONNECTED"
    return events, reconnects


def disconnect_measurement_error(measurements):
    count = measurements.get("Disconnects")
    events = measurements.get("Disconnect events")
    if count is None:
        return None
    if count == 0 and events is None:
        return None  # Legacy/synthetic callers; live collector separately requires event evidence.
    if not isinstance(count, int) or count < 0 or not isinstance(events, list) or count != len(events):
        return "Disconnect metric is inconsistent with event evidence."
    duration = measurements.get("Disconnect window seconds")
    if isinstance(duration, (int, float)) and count > max(1, duration * 2):
        return "Disconnect metric is inconsistent with event evidence (implausible transition rate)."
    if any(not isinstance(event, dict) or not all(event.get(key) for key in ("timestamp", "interface", "source"))
           or event.get("previous_state") != "CONNECTED" or event.get("new_state") != "DISCONNECTED" for event in events):
        return "Disconnect metric is inconsistent with event evidence (invalid transition record)."
    return None
