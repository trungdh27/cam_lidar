from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from desktop_app.testing.test_result import TestResult


class EvidenceStore:
    def __init__(self, root: str | Path | None = None):
        project_root = Path(__file__).resolve().parents[2]
        self.root = Path(root) if root is not None else project_root / "evidence"

    def begin_session(self, device: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_id = f"{timestamp}-{uuid4().hex[:8]}"
        (self.root / device.casefold() / session_id).mkdir(
            parents=True,
            exist_ok=False,
        )
        return session_id

    def write_result(
        self,
        session_id: str,
        device: str,
        definition,
        context,
        result: TestResult,
        log_lines: list[str],
    ) -> list[str]:
        test_dir = self.root / device.casefold() / session_id / definition.id
        test_dir.mkdir(parents=True, exist_ok=True)
        result_path = test_dir / "result.json"
        metrics_path = test_dir / "metrics.json"
        log_path = test_dir / "log.txt"
        relative_paths = [
            str(path.relative_to(self.root.parent))
            for path in (result_path, metrics_path, log_path)
        ]
        result.evidence = relative_paths
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
