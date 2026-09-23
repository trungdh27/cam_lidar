from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import RuntimeStatus
from .session import atomic_write_json, utc_now


REVIEW_STATUSES = frozenset(
    {RuntimeStatus.PASS, RuntimeStatus.FAIL, RuntimeStatus.BLOCKED}
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_result_snapshot(attempt_dir: str | Path) -> dict[str, Any]:
    """Load a completed attempt without requiring a live EvidenceManager.

    Every component is optional so attempts created before final_metrics.json was
    introduced remain viewable.
    """

    folder = Path(attempt_dir)
    return {
        "attempt_dir": folder,
        "test_info": _read_json(folder / "test_info.json"),
        "result": _read_json(folder / "result.json"),
        "final_metrics": _read_json(folder / "final_metrics.json"),
        "load_strategy": _read_json(folder / "load_strategy.json"),
        "pretest_baseline": _read_json(folder / "pretest_baseline.json"),
        "evidence_manifest": _read_json(folder / "evidence_manifest.json"),
    }


def latest_result_attempt(evidence_root: str | Path, test_id: str) -> Path | None:
    """Return the newest meaningful attempt, ignoring orphan prepared folders."""

    root = Path(evidence_root).expanduser()
    candidates: list[tuple[str, str, int, Path]] = []
    if not root.is_dir():
        return None
    for result_path in root.glob(f"*_*/tests/{test_id}/attempt_*/result.json"):
        result = _read_json(result_path)
        info = _read_json(result_path.parent / "test_info.json")
        if not result:
            continue
        attempt_text = result_path.parent.name.removeprefix("attempt_")
        attempt = int(attempt_text) if attempt_text.isdigit() else 0
        timestamp = str(
            result.get("end_time")
            or info.get("end_time")
            or result.get("start_time")
            or info.get("start_time")
            or result_path.parents[3].name
        )
        candidates.append((timestamp, result_path.parents[3].name, attempt, result_path.parent))
    return max(candidates, default=("", "", 0, None))[3]


def persist_final_metrics(attempt_dir: str | Path, snapshot: dict[str, Any]) -> Path:
    path = Path(attempt_dir) / "final_metrics.json"
    atomic_write_json(path, snapshot)
    result_path = path.parent / "result.json"
    result = _read_json(result_path)
    if result:
        result.setdefault("review_status", None)
        result.setdefault("review_comment", "")
        result.setdefault("reviewed_at", None)
        result["final_metrics_file"] = path.name
        atomic_write_json(result_path, result)
    info_path = path.parent / "test_info.json"
    info = _read_json(info_path)
    if info:
        info["final_metrics_file"] = path.name
        atomic_write_json(info_path, info)
    return path


def review_attempt(
    attempt_dir: str | Path,
    status: RuntimeStatus | str,
    comment: str = "",
    *,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    """Persist a human verdict on an existing attempt without rerunning it."""

    try:
        review_status = status if isinstance(status, RuntimeStatus) else RuntimeStatus(str(status))
    except ValueError as exc:
        raise ValueError(f"Unsupported review status: {status}") from exc
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"Unsupported review status: {review_status.value}")

    folder = Path(attempt_dir)
    result_path = folder / "result.json"
    result = _read_json(result_path)
    if not result:
        raise FileNotFoundError(f"No result.json exists in {folder}")

    timestamp = reviewed_at or utc_now()
    result.update(
        status=review_status.value,
        review_status=review_status.value,
        review_comment=comment.strip(),
        reviewed_at=timestamp,
    )
    atomic_write_json(result_path, result)

    info_path = folder / "test_info.json"
    info = _read_json(info_path)
    if info:
        info.update(
            status=review_status.value,
            review_status=review_status.value,
            review_comment=comment.strip(),
            reviewed_at=timestamp,
        )
        atomic_write_json(info_path, info)
    return result
