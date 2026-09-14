from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

import yaml

from devices.livox.testing.test_case import AutomationLevel, TestCaseDefinition


DEFAULT_SOURCE_POLICY_PATH = (
    Path(__file__).resolve().parents[3]
    / "testcases"
    / "lidar"
    / "definitions"
    / "system_vmo_lidar.yaml"
)

SOURCE_FIELDS = (
    "ID",
    "Group",
    "Test Case",
    "Purpose",
    "Precondition",
    "Requirement",
    "Operation Procedures",
    "Expected Result",
    "Status",
    "Actual Result",
    "Executed By",
    "Executed Date",
    "Note",
)
REQUIRED_SOURCE_FIELDS = SOURCE_FIELDS[:8]
REQUIRED_POLICY_FIELDS = frozenset(
    {
        "automation_level",
        "priority",
        "timeout",
        "executor",
        "requires_ros2_target",
        "related_existing_tests",
        "parameters",
    }
)


class LidarSourceCatalogError(ValueError):
    pass


def load_source_test_definitions(
    executor_map: Mapping[str, type[Any]],
    policy_path: str | Path = DEFAULT_SOURCE_POLICY_PATH,
) -> list[TestCaseDefinition]:
    policy_file = Path(policy_path)
    policy = _load_policy(policy_file)
    source_path = (policy_file.parent / _text(policy, "source_csv")).resolve()
    encoding = _text(policy, "source_encoding")
    rows = _load_source_rows(source_path, encoding)
    case_policy = policy.get("cases")
    if not isinstance(case_policy, Mapping):
        raise LidarSourceCatalogError("cases must be a mapping")
    source_ids = [row["ID"] for row in rows]
    policy_ids = [str(value) for value in case_policy]
    if set(source_ids) != set(policy_ids) or len(source_ids) != len(policy_ids):
        missing = sorted(set(source_ids) - set(policy_ids))
        extra = sorted(set(policy_ids) - set(source_ids))
        raise LidarSourceCatalogError(
            f"CSV/YAML ID mismatch; missing policy={missing}, extra policy={extra}"
        )
    definitions = []
    for order, row in enumerate(rows, 1):
        test_id = row["ID"]
        item = case_policy[test_id]
        if not isinstance(item, Mapping):
            raise LidarSourceCatalogError(f"Policy for {test_id} must be a mapping")
        missing_fields = REQUIRED_POLICY_FIELDS - set(item)
        if missing_fields:
            raise LidarSourceCatalogError(
                f"Policy for {test_id} misses {sorted(missing_fields)}"
            )
        executor_key = _text(item, "executor")
        try:
            executor = executor_map[executor_key]
        except KeyError as exc:
            raise LidarSourceCatalogError(
                f"Unknown executor {executor_key!r} for {test_id}"
            ) from exc
        parameters = _string_list(item, "parameters")
        related = _string_list(item, "related_existing_tests", allow_empty=True)
        try:
            automation = AutomationLevel(_text(item, "automation_level"))
        except ValueError as exc:
            raise LidarSourceCatalogError(
                f"Invalid automation level for {test_id}"
            ) from exc
        timeout = item.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise LidarSourceCatalogError(f"Invalid timeout for {test_id}")
        requires_ros2 = item.get("requires_ros2_target")
        if not isinstance(requires_ros2, bool):
            raise LidarSourceCatalogError(
                f"requires_ros2_target for {test_id} must be boolean"
            )
        definitions.append(
            TestCaseDefinition(
                id=test_id,
                device=_text(policy, "device"),
                name=row["Test Case"],
                group=row["Group"],
                description=row["Purpose"],
                automation_level=automation,
                priority=_text(item, "priority"),
                timeout_sec=float(timeout),
                executor=executor,
                order=1000 + order,
                purpose=row["Purpose"],
                precondition=row["Precondition"],
                requirements=row["Requirement"],
                procedure=row["Operation Procedures"],
                expected_result=row["Expected Result"],
                parameters=parameters,
                source=str(source_path.relative_to(Path(__file__).resolve().parents[3])),
                source_test_id=test_id,
                source_status=row["Status"],
                source_actual_result=row["Actual Result"],
                source_executed_by=row["Executed By"],
                source_executed_date=row["Executed Date"],
                source_note=row["Note"],
                implemented_by=executor.__name__,
                related_existing_tests=related,
                requires_ros2_target=requires_ros2,
                tags=("source:system_vmo_lidar",),
            )
        )
    return definitions


def _load_policy(path: Path) -> Mapping[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LidarSourceCatalogError(f"Unable to load catalog policy {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise LidarSourceCatalogError("Catalog policy must be a mapping")
    return payload


def _load_source_rows(path: Path, encoding: str) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding=encoding, newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader)
            if tuple(header[: len(SOURCE_FIELDS)]) != SOURCE_FIELDS:
                raise LidarSourceCatalogError("CSV required headers do not match")
            if any(value.strip() for value in header[len(SOURCE_FIELDS) :]):
                raise LidarSourceCatalogError("CSV has unexpected non-empty trailing headers")
            rows = []
            seen = set()
            for line_number, values in enumerate(reader, 2):
                if not values or not any(value.strip() for value in values):
                    continue
                if len(values) < len(SOURCE_FIELDS):
                    raise LidarSourceCatalogError(f"CSV row {line_number} is incomplete")
                if any(value.strip() for value in values[len(SOURCE_FIELDS) :]):
                    raise LidarSourceCatalogError(
                        f"CSV row {line_number} has unexpected trailing data"
                    )
                row = dict(zip(SOURCE_FIELDS, values[: len(SOURCE_FIELDS)]))
                for field in REQUIRED_SOURCE_FIELDS:
                    if not row[field].strip():
                        raise LidarSourceCatalogError(
                            f"CSV row {line_number} has empty {field}"
                        )
                test_id = row["ID"].strip()
                if test_id in seen:
                    raise LidarSourceCatalogError(f"Duplicate source test id: {test_id}")
                seen.add(test_id)
                rows.append({key: value.strip() for key, value in row.items()})
    except (OSError, UnicodeError) as exc:
        raise LidarSourceCatalogError(f"Unable to load source CSV {path}: {exc}") from exc
    return rows


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LidarSourceCatalogError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(
    data: Mapping[str, Any], key: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        raise LidarSourceCatalogError(f"{key} must be a string list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise LidarSourceCatalogError(f"{key} must contain non-empty strings")
    return tuple(item.strip() for item in value)
