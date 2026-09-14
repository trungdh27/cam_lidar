"""Read-only history view over LiDAR evidence sessions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class LidarHistoryStore:
    """Read LiDAR sessions without duplicating evidence into another store."""

    def __init__(self, root: str | Path | None = None):
        project_root = Path(__file__).resolve().parents[3]
        self.root = (
            Path(root)
            if root is not None
            else project_root / "evidence" / "lidar"
        )

    def list_sessions(self, limit: int | None = None) -> list[dict]:
        if limit is not None and limit < 0:
            raise ValueError("History limit cannot be negative")
        if not self.root.is_dir():
            return []
        sessions = []
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            try:
                session = self.load_session(entry.name)
            except (OSError, ValueError):
                continue
            sessions.append(session)
        sessions.sort(key=self._sort_key, reverse=True)
        return sessions if limit is None else sessions[:limit]

    def load_session(self, session_id: str) -> dict:
        session_dir = self._safe_child(session_id)
        if not session_dir.is_dir():
            raise FileNotFoundError(f"Unknown LiDAR session: {session_id}")
        manifest_path = session_dir / "session.json"
        manifest_state = "LEGACY"
        manifest = None
        if manifest_path.is_file():
            try:
                value = self._read_json(manifest_path)
                if not isinstance(value, dict):
                    raise ValueError("session.json must contain an object")
                manifest = value
                manifest_state = "OK"
            except (OSError, ValueError, json.JSONDecodeError):
                manifest_state = "CORRUPT"
        derived = self._derive_session(session_id)
        fallback = dict(derived)
        if manifest is not None:
            derived.update(manifest)
        if not isinstance(derived.get("counts"), dict):
            derived["counts"] = fallback["counts"]
            manifest_state = "CORRUPT"
        if not isinstance(derived.get("test_ids"), list):
            derived["test_ids"] = fallback["test_ids"]
            manifest_state = "CORRUPT"
        derived["domain"] = "lidar"
        derived["session_id"] = session_id
        derived["manifest_state"] = manifest_state
        derived["test_count"] = len(self.list_results(session_id))
        return derived

    def list_results(self, session_id: str) -> list[dict]:
        session_dir = self._safe_child(session_id)
        if not session_dir.is_dir():
            raise FileNotFoundError(f"Unknown LiDAR session: {session_id}")
        results = []
        for entry in sorted(session_dir.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            try:
                results.append(self.load_result(session_id, entry.name))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return results

    def load_result(self, session_id: str, test_id: str) -> dict:
        session_dir = self._safe_child(session_id)
        test_dir = self._safe_child(test_id, parent=session_dir)
        path = test_dir / "result.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"Unknown LiDAR result: {session_id}/{test_id}"
            )
        payload = self._read_json(path)
        if not isinstance(payload, dict):
            raise ValueError("LiDAR result must contain an object")
        result = dict(payload)
        result.setdefault("test_id", test_id)
        result.setdefault("status", "UNKNOWN")
        result.setdefault("actual_result", "UNKNOWN")
        result.setdefault("measurements", {})
        result.setdefault("evidence", [])
        if not isinstance(result["measurements"], dict):
            result["measurements"] = {}
        if not isinstance(result["evidence"], list):
            result["evidence"] = []
        return result

    def _derive_session(self, session_id: str) -> dict[str, Any]:
        results = self.list_results(session_id)
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status") or "UNKNOWN")
            counts[status] = counts.get(status, 0) + 1
        first = results[0] if results else {}
        timestamps = [
            value
            for result in results
            for value in (result.get("started_at"), result.get("start_time"))
            if value
        ]
        durations = [
            float(value)
            for result in results
            for value in (result.get("duration_sec"),)
            if isinstance(value, (int, float))
        ]
        return {
            "domain": "lidar",
            "session_id": session_id,
            "status": _status_from_counts(counts),
            "selected_model": first.get("model") or "UNKNOWN",
            "detected_model": first.get("model") or "UNKNOWN",
            "serial": first.get("serial") or "UNKNOWN",
            "lidar_ip": first.get("lidar_ip") or "UNKNOWN",
            "jetson_host": "UNKNOWN",
            "jetson_lidar_interface": "UNKNOWN",
            "started_at": min(timestamps) if timestamps else None,
            "finished_at": None,
            "duration_sec": sum(durations) if durations else None,
            "test_ids": [result.get("test_id") for result in results],
            "counts": counts,
            "stream_was_running_before_tests": False,
            "runner_started_stream": False,
            "cancelled": counts.get("CANCELLED", 0) > 0,
        }

    def _safe_child(self, identifier: str, parent: Path | None = None) -> Path:
        value = str(identifier)
        if not value or value in {".", ".."} or Path(value).name != value:
            raise ValueError("Invalid LiDAR history identifier")
        base = (parent or self.root).resolve()
        candidate = (base / value).resolve()
        if candidate.parent != base:
            raise ValueError("LiDAR history path escapes evidence/lidar")
        return candidate

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _sort_key(session: dict) -> tuple[float, str]:
        started_at = session.get("started_at")
        if isinstance(started_at, str):
            try:
                return datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp(), str(
                    session.get("session_id") or ""
                )
            except ValueError:
                pass
        return 0.0, str(session.get("session_id") or "")


def _status_from_counts(counts: dict[str, int]) -> str:
    if counts.get("ERROR", 0):
        return "ERROR"
    if counts.get("FAIL", 0):
        return "FAIL"
    if counts.get("CANCELLED", 0):
        return "CANCELLED"
    total = sum(counts.values())
    if total and counts.get("PASS", 0) == total:
        return "PASS"
    return "COMPLETED" if total else "UNKNOWN"
