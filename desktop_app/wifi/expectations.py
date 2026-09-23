"""Serializable expected-value rules, independent of presentation strings."""
from __future__ import annotations

from dataclasses import dataclass

CRITICAL = "CRITICAL"
SUPPORTING = "SUPPORTING"
INFORMATIONAL = "INFORMATIONAL"


@dataclass(frozen=True)
class ExpectedValue:
    kind: str
    value: object = None
    unit: str = ""

    def matches(self, actual, measurements=None, config=None) -> bool:
        measurements = measurements or {}
        if self.kind in {"exact", "band"}:
            return actual == self.value
        if self.kind == "minimum":
            return isinstance(actual, (int, float)) and actual >= self.value
        if self.kind == "maximum":
            return isinstance(actual, (int, float)) and actual <= self.value
        if self.kind == "one_of":
            return actual in self.value
        if self.kind == "configured_value":
            return actual == (config or {}).get(self.value)
        if self.kind == "valid_channel_for_band":
            if not isinstance(actual, int):
                return False
            allowed = measurements.get("Allowed channels")
            if self.value == "2.4 GHz":
                return 1 <= actual <= 14 and (allowed is None or actual in allowed)
            # The PHY's regulatory-enabled list, when available, is authoritative.
            allowed = measurements.get("Allowed channels")
            return actual in allowed if allowed is not None else channel_frequency(actual, "5 GHz") is not None
        if self.kind == "frequency_matches_channel":
            frequency = channel_frequency(measurements.get("Channel"), measurements.get("Band"))
            return frequency is not None and actual == frequency
        if self.kind == "frequency_belongs_to_band":
            if not isinstance(actual, (int, float)):
                return False
            if self.value == "2.4 GHz":
                return 2412 <= actual <= 2484
            return self.value == "5 GHz" and 5000 <= actual < 6000
        if self.kind == "informational":
            return True
        raise ValueError(f"Unsupported expected-value rule: {self.kind}")


def channel_frequency(channel, band):
    if not isinstance(channel, int):
        return None
    if band == "2.4 GHz":
        if channel == 14:
            return 2484
        return 2407 + 5 * channel if 1 <= channel <= 13 else None
    if band == "5 GHz":
        # Channel numbering is not regulatory authorization. Runtime regulatory
        # errors and the PHY channel list are separate evidence.
        if channel in ({36, 40, 44, 48}
                       | set(range(52, 65, 4))
                       | set(range(100, 145, 4))
                       | set(range(149, 178, 4))):
            return 5000 + 5 * channel
        if channel in {184, 188, 192, 196}:
            return 4000 + 5 * channel
    return None


def Exact(value): return ExpectedValue("exact", value)
def Band(value): return ExpectedValue("band", value)
def ValidChannelForBand(value): return ExpectedValue("valid_channel_for_band", value)
def FrequencyMatchesChannel(): return ExpectedValue("frequency_matches_channel")
def FrequencyBelongsToBand(value): return ExpectedValue("frequency_belongs_to_band", value)
def Minimum(value, unit=""): return ExpectedValue("minimum", value, unit)
def Maximum(value, unit=""): return ExpectedValue("maximum", value, unit)
def OneOf(values): return ExpectedValue("one_of", tuple(values))
def ConfiguredValue(key): return ExpectedValue("configured_value", key)
def Informational(): return ExpectedValue("informational")
