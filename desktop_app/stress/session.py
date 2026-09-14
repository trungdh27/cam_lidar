from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    AttemptPaths,
    CollectorStatus,
    EnvironmentStatus,
    EvidenceManifest,
    EvidenceRecord,
    StressTestDefinition,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class StressSessionManager:
    def __init__(self, evidence_root: str | Path | None = None):
        self.evidence_root = Path(evidence_root).expanduser() if evidence_root else Path.home() / "Stress_Test_Logs"
        self.session_id: str | None = None
        self.session_dir: Path | None = None
        self.session_data: dict[str, Any] = {}

    def set_evidence_root(self, path: str | Path) -> Path:
        if self.session_dir is not None:
            raise RuntimeError("Evidence Root cannot change after a session has been created")
        candidate = Path(path).expanduser()
        candidate.mkdir(parents=True, exist_ok=True)
        if not candidate.is_dir():
            raise ValueError(f"Evidence root is not a directory: {candidate}")
        probe = candidate / f".stress_write_probe_{os.getpid()}"
        try:
            probe.touch(exist_ok=False)
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"Evidence root is not writable: {candidate}: {exc}") from exc
        self.evidence_root = candidate.resolve()
        return self.evidence_root

    def validate_evidence_root(self) -> tuple[bool, str]:
        try:
            if self.session_dir is None:
                path = self.set_evidence_root(self.evidence_root)
            else:
                path = self.evidence_root
                if not path.is_dir():
                    raise ValueError(f"Evidence root is not a directory: {path}")
                probe = path / f".stress_write_probe_{os.getpid()}"
                probe.touch(exist_ok=False)
                probe.unlink()
            usage = shutil.disk_usage(path)
            return True, f"{usage.free / (1024 ** 3):.1f} GiB free"
        except (OSError, ValueError, RuntimeError) as exc:
            return False, str(exc)

    def disk_free(self) -> int | None:
        try:
            candidate = self.evidence_root
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            return shutil.disk_usage(candidate).free
        except OSError:
            return None

    def create_session(self, platform: str, *, now: datetime | None = None) -> Path:
        if self.session_dir is not None:
            return self.session_dir
        timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        base_id = f"{platform.upper()}_{timestamp}"
        session_id = base_id
        suffix = 1
        while (self.evidence_root / session_id).exists():
            suffix += 1
            session_id = f"{base_id}_{suffix:02d}"
        session_dir = self.evidence_root / session_id
        (session_dir / "pre_test_environment").mkdir(parents=True, exist_ok=False)
        (session_dir / "tests").mkdir()
        (session_dir / "session.log").touch()
        self.session_id = session_id
        self.session_dir = session_dir
        self.session_data = {
            "session_id": session_id,
            "platform": platform.upper(),
            "start_time": utc_now(),
            "end_time": None,
            "evidence_root": str(self.evidence_root),
            "environment_status": EnvironmentStatus.NOT_CHECKED.value,
            "environment_override": False,
            "environment_blocking_reasons": [],
            "selected_tests": [],
        }
        self._write_session()
        return session_dir

    def update_environment(self, status: EnvironmentStatus, reasons: list[str], *, override: bool = False) -> None:
        self._require_session()
        self.session_data.update(
            environment_status=status.value,
            environment_override=bool(override),
            environment_blocking_reasons=list(reasons),
        )
        self._write_session()

    def set_selected_tests(self, test_ids: list[str]) -> None:
        self._require_session()
        self.session_data["selected_tests"] = list(test_ids)
        self._write_session()

    def finish_session(self) -> None:
        self._require_session()
        self.session_data["end_time"] = utc_now()
        self._write_session()

    def create_attempt(self, definition: StressTestDefinition) -> AttemptPaths:
        self._require_session()
        test_dir = self.session_dir / "tests" / definition.test_id
        test_dir.mkdir(parents=True, exist_ok=True)
        used = []
        for child in test_dir.iterdir():
            if child.is_dir() and child.name.startswith("attempt_"):
                suffix = child.name.removeprefix("attempt_")
                if suffix.isdigit():
                    used.append(int(suffix))
        attempt = max(used, default=0) + 1
        attempt_dir = test_dir / f"attempt_{attempt:03d}"
        attempt_dir.mkdir(exist_ok=False)
        logs_dir = attempt_dir / "logs"
        artifacts_dir = attempt_dir / "artifacts"
        logs_dir.mkdir()
        for name in ("screenshots", "video", "rosbag", "other"):
            (artifacts_dir / name).mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            attempt_dir / "test_info.json",
            {
                "test_id": definition.test_id,
                "test_name": definition.test_name,
                "group": definition.group,
                "attempt": attempt,
                "execution_type": definition.execution_type.value,
                "start_time": None,
                "end_time": None,
                "target_duration_sec": definition.duration_seconds,
                "status": "STARTING",
                "robot": None,
                "software_version": None,
                "kernel": None,
                "ros_version": None,
                "test_command_description": definition.workload_description,
                "target_workload": definition.test_name if definition.workload_program else None,
                "executed_by": None,
            },
        )
        records = []
        for index, evidence in enumerate(definition.evidence_plan, start=1):
            filename = f"logs/{index:02d}_{evidence.id}.log"
            manual_required = evidence.manual_required or (
                evidence.collector == "workload" and not definition.workload_program
            )
            record = EvidenceRecord(
                id=evidence.id,
                name=evidence.name,
                type=evidence.type,
                collector=evidence.collector,
                status=CollectorStatus.MANUAL_REQUIRED if manual_required else CollectorStatus.WAITING,
                file=filename,
                command_description=evidence.command_description,
                sample_interval=evidence.sample_interval,
                manual_required=manual_required,
            )
            records.append(record.to_dict())
            if manual_required:
                (attempt_dir / filename).touch()
        atomic_write_json(
            attempt_dir / "evidence_manifest.json",
            {"test_id": definition.test_id, "attempt": attempt, "evidence": records},
        )
        return AttemptPaths(self.session_dir, attempt_dir, logs_dir, artifacts_dir, attempt)

    def update_manifest(self, paths: AttemptPaths, records: list[EvidenceRecord], test_id: str) -> None:
        manifest = EvidenceManifest(test_id, paths.attempt, records)
        atomic_write_json(paths.attempt_dir / "evidence_manifest.json", manifest.to_dict())

    def write_result(self, paths: AttemptPaths, payload: dict[str, Any]) -> None:
        atomic_write_json(paths.attempt_dir / "result.json", payload)

    def pretest_dir(self) -> Path:
        self._require_session()
        return self.session_dir / "pre_test_environment"

    def _write_session(self) -> None:
        atomic_write_json(self.session_dir / "session.json", self.session_data)

    def _require_session(self) -> None:
        if self.session_dir is None:
            raise RuntimeError("Stress session has not been created")
