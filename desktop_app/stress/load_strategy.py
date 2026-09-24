from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from .evidence_summary import target_cpu_percent


SUPPORTED_ADAPTIVE_CPU_IDS = frozenset(f"ST-CPU-{index:03d}" for index in range(1, 7))


class LoadStrategy(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    LOAD_ASSIST = "LOAD_ASSIST"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class TargetSemantics(str, Enum):
    MINIMUM = "MINIMUM"
    APPROXIMATE = "APPROXIMATE"


@dataclass(frozen=True)
class LoadStrategyDecision:
    strategy: LoadStrategy
    target_percent: float | None
    baseline_percent: float | None
    gap_percent: float | None
    reason: str
    target_semantics: TargetSemantics | None
    stress_ng_available: bool | None = None

    @property
    def can_start(self) -> bool:
        if self.strategy == LoadStrategy.NEEDS_REVIEW:
            return False
        return self.strategy != LoadStrategy.LOAD_ASSIST or self.stress_ng_available is True

    def to_dict(self) -> dict:
        result = asdict(self)
        result["strategy"] = self.strategy.value
        result["target_semantics"] = self.target_semantics.value if self.target_semantics else None
        result["can_start"] = self.can_start
        return result


def target_semantics(definition) -> TargetSemantics | None:
    if target_cpu_percent(definition) is None:
        return None
    fields = getattr(definition, "source_fields", {}) or {}
    text = " ".join(
        fields.get(key, "")
        for key in ("Purpose", "Acceptance Criteria", "Expected result")
    ).casefold()
    if any(word in text for word in ("approximately", "approximate", "khoảng", "gần", "near")):
        return TargetSemantics.APPROXIMATE
    if any(mark in text for mark in (">=", "≥", "minimum", "at least", "tối thiểu")):
        return TargetSemantics.MINIMUM
    return None


def decide_cpu_strategy(
    *,
    target_percent: float | None,
    baseline_percent: float | None,
    semantics: TargetSemantics | None,
    stress_ng_available: bool | None = None,
) -> LoadStrategyDecision:
    if target_percent is None:
        return LoadStrategyDecision(LoadStrategy.NEEDS_REVIEW, None, baseline_percent, None, "No explicit CPU target is available; no target was inferred.", semantics, stress_ng_available)
    if baseline_percent is None:
        return LoadStrategyDecision(LoadStrategy.NEEDS_REVIEW, target_percent, None, None, "No valid PRE-TEST CPU baseline is available; artificial load will not be injected.", semantics, stress_ng_available)
    if semantics is None:
        return LoadStrategyDecision(LoadStrategy.NEEDS_REVIEW, target_percent, baseline_percent, None, "Target semantics are ambiguous; no safe automatic load strategy exists.", semantics, stress_ng_available)
    if semantics == TargetSemantics.MINIMUM and baseline_percent >= target_percent:
        return LoadStrategyDecision(LoadStrategy.OBSERVE_ONLY, target_percent, baseline_percent, 0.0, "Current DUT workload already satisfies the minimum target.", semantics, stress_ng_available)
    if semantics == TargetSemantics.APPROXIMATE and baseline_percent > target_percent:
        return LoadStrategyDecision(LoadStrategy.NEEDS_REVIEW, target_percent, baseline_percent, 0.0, "Current load is above the approximate target and cannot be safely reduced automatically.", semantics, stress_ng_available)
    if baseline_percent == target_percent:
        return LoadStrategyDecision(LoadStrategy.OBSERVE_ONLY, target_percent, baseline_percent, 0.0, "Current DUT workload already matches the selected target.", semantics, stress_ng_available)
    gap = max(0.0, target_percent - baseline_percent)
    return LoadStrategyDecision(LoadStrategy.LOAD_ASSIST, target_percent, baseline_percent, gap, "Current DUT load is below target; conservative feedback-based assistance is required.", semantics, stress_ng_available)


def decision_for_definition(definition, baseline: dict, stress_ng_available: bool | None) -> LoadStrategyDecision:
    cpu = baseline.get("cpu", {}) if baseline else {}
    return decide_cpu_strategy(
        target_percent=target_cpu_percent(definition),
        baseline_percent=cpu.get("avg_percent"),
        semantics=target_semantics(definition),
        stress_ng_available=stress_ng_available,
    )
