from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from devices.livox.testing.test_result import TestResult


class LidarEvidenceStore:
    """Persist LiDAR test evidence below the dedicated ``lidar`` namespace."""

    DEVICE_NAMESPACE = "lidar"
    SESSION_FIELDS = frozenset(
        {
            "selected_model",
            "detected_model",
            "serial",
            "lidar_ip",
            "jetson_host",
            "jetson_lidar_interface",
            "test_ids",
            "stream_was_running_before_tests",
            "runner_started_stream",
        }
    )

    def __init__(self, root: str | Path | None = None):
        project_root = Path(__file__).resolve().parents[3]
        self.root = Path(root) if root is not None else project_root / "evidence"

    def begin_session(self, device: str) -> str:
        self._require_lidar(device)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_id = f"{timestamp}-{uuid4().hex[:8]}"
        (self.root / self.DEVICE_NAMESPACE / session_id).mkdir(
            parents=True,
            exist_ok=False,
        )
        self._write_manifest(
            session_id,
            {
                "domain": self.DEVICE_NAMESPACE,
                "session_id": session_id,
                "status": "RUNNING",
                "selected_model": None,
                "detected_model": None,
                "serial": None,
                "lidar_ip": None,
                "jetson_host": None,
                "jetson_lidar_interface": None,
                "started_at": _now(),
                "finished_at": None,
                "duration_sec": None,
                "test_ids": [],
                "counts": {},
                "stream_was_running_before_tests": False,
                "runner_started_stream": False,
                "cancelled": False,
            },
        )
        return session_id

    def initialize_session(
        self,
        session_id: str,
        metadata: dict[str, Any],
    ) -> None:
        manifest = self._load_manifest(session_id)
        for field in self.SESSION_FIELDS:
            if field in metadata:
                manifest[field] = metadata[field]
        self._write_manifest(session_id, manifest)

    def finalize_session(
        self,
        session_id: str,
        *,
        counts: dict[str, int],
        stream_was_running_before_tests: bool,
        runner_started_stream: bool,
        cancelled: bool,
        duration_sec: float,
        detected_model: str | None = None,
        serial: str | None = None,
    ) -> dict[str, Any]:
        manifest = self._load_manifest(session_id)
        normalized_counts = {
            str(status): int(count)
            for status, count in counts.items()
            if int(count) > 0
        }
        manifest.update(
            {
                "status": _session_status(normalized_counts, cancelled),
                "finished_at": _now(),
                "duration_sec": max(0.0, float(duration_sec)),
                "counts": normalized_counts,
                "stream_was_running_before_tests": bool(
                    stream_was_running_before_tests
                ),
                "runner_started_stream": bool(runner_started_stream),
                "cancelled": bool(cancelled),
            }
        )
        if detected_model:
            manifest["detected_model"] = str(detected_model)
        if serial:
            manifest["serial"] = str(serial)
        self._write_manifest(session_id, manifest)
        return dict(manifest)

    def write_result(
        self,
        session_id: str,
        device: str,
        definition,
        context,
        result: TestResult,
        log_lines: list[str],
    ) -> list[str]:
        self._require_lidar(device)
        test_dir = (
            self.root / self.DEVICE_NAMESPACE / session_id / definition.id
        )
        test_dir.mkdir(parents=True, exist_ok=True)
        result_path = test_dir / "result.json"
        metrics_path = test_dir / "metrics.json"
        log_path = test_dir / "log.txt"
        relative_paths = [
            str(path.relative_to(self.root.parent))
            for path in (result_path, metrics_path, log_path)
        ]
        references = result.measurements.get("evidence_references", [])
        if not isinstance(references, list):
            references = []
        safe_references = [
            value
            for value in references
            if _safe_evidence_reference(value)
        ]
        if "evidence_references" in result.measurements:
            result.measurements["evidence_references"] = safe_references
        result.evidence = relative_paths + safe_references
        identity = context.discovery_result or {}
        payload = {
            **result.to_dict(),
            "device": device,
            "name": definition.name,
            "model": identity.get("model") or context.selected_model,
            "serial": identity.get("serial"),
            "start_time": result.started_at,
            "end_time": result.ended_at,
            "duration": result.duration_sec,
            "group": getattr(definition, "group", ""),
            "automation_level": getattr(
                getattr(definition, "automation_level", None),
                "value",
                "",
            ),
            "source_test_id": getattr(definition, "source_test_id", ""),
            "implemented_by": getattr(definition, "implemented_by", ""),
        }
        result_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        metrics_path.write_text(
            json.dumps(result.measurements, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return relative_paths

    @classmethod
    def _require_lidar(cls, device: str) -> None:
        if str(device).strip().casefold() != cls.DEVICE_NAMESPACE:
            raise ValueError("LidarEvidenceStore only accepts the lidar domain")

    def _session_dir(self, session_id: str) -> Path:
        value = str(session_id)
        if not value or value in {".", ".."} or Path(value).name != value:
            raise ValueError("Invalid LiDAR evidence session id")
        path = self.root / self.DEVICE_NAMESPACE / value
        domain_root = (self.root / self.DEVICE_NAMESPACE).resolve()
        if path.resolve().parent != domain_root:
            raise ValueError("LiDAR evidence session escapes its domain")
        return path

    def _load_manifest(self, session_id: str) -> dict[str, Any]:
        path = self._session_dir(session_id) / "session.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("LiDAR session manifest must be an object")
        return payload

    def _write_manifest(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        path = self._session_dir(session_id) / "session.json"
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


# Backward compatibility for callers that still use the Phase 0 legacy path.
EvidenceStore = LidarEvidenceStore


def _session_status(counts: dict[str, int], cancelled: bool) -> str:
    if counts.get("ERROR", 0):
        return "ERROR"
    if counts.get("FAIL", 0):
        return "FAIL"
    if cancelled or counts.get("CANCELLED", 0):
        return "CANCELLED"
    total = sum(counts.values())
    if total and counts.get("PASS", 0) == total:
        return "PASS"
    return "COMPLETED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_evidence_reference(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        return False
    lowered = value.casefold()
    if "\n" in value or "\r" in value:
        return False
    return not any(secret in lowered for secret in ("password", "token", "private_key"))
