"""Evidence persistence for the Bluetooth diagnostic test suite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.bluetooth.models import BluetoothTestResult


class BluetoothEvidenceStore:
    """Write test evidence below the project's established ``evidence`` root."""

    def __init__(self, root: str | Path | None = None):
        project_root = Path(__file__).resolve().parents[2]
        self.root = Path(root) if root is not None else project_root / "evidence"

    def begin_session(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_id = f"{stamp}-{uuid4().hex[:8]}"
        directory = self.root / "bluetooth" / session_id
        directory.mkdir(parents=True, exist_ok=False)
        return session_id

    def write_result(
        self, session_id: str, test_name: str, result: BluetoothTestResult
    ) -> BluetoothTestResult:
        test_dir = self._test_dir(session_id, result.test_case_id)
        test_dir.mkdir(parents=True, exist_ok=True)
        result_path = test_dir / "result.json"
        command_path = test_dir / "commands.log"
        result_path.write_text(
            json.dumps(
                result.to_dict() | {"test_name": test_name},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        command_path.write_text(
            "\n\n".join(
                self._format_command(command) for command in result.command_results
            )
            + "\n",
            encoding="utf-8",
        )
        relative = tuple(
            str(path.relative_to(self.root)) for path in (result_path, command_path)
        )
        return BluetoothTestResult(
            test_case_id=result.test_case_id,
            status=result.status,
            message=result.message,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_s=result.duration_s,
            observations=result.observations,
            command_results=result.command_results,
            evidence_paths=relative,
        )

    def _test_dir(self, session_id: str, test_id: str) -> Path:
        if Path(session_id).name != session_id or Path(test_id).name != test_id:
            raise ValueError("Invalid Bluetooth evidence path component")
        return self.root / "bluetooth" / session_id / test_id

    @staticmethod
    def _format_command(command) -> str:
        return "\n".join(
            (
                f"$ {command.command}",
                f"return_code: {command.return_code}",
                f"duration_s: {command.duration_s:.3f}",
                f"timed_out: {command.timed_out}",
                f"error: {command.error or ''}",
                "--- stdout ---",
                command.stdout.rstrip(),
                "--- stderr ---",
                command.stderr.rstrip(),
            )
        )
