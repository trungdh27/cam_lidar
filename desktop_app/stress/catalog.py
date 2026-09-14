from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .evidence_plan import (
    build_evidence_plan,
    classify_execution,
    infer_dependencies,
    safe_cpu_workload,
)
from .models import ExecutionType, RuntimeStatus, StressTestDefinition


DEFAULT_VD_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "testcases"
    / "stress"
    / "source"
    / "stress_system_VD.xlsx"
)
LEGACY_VD_CSV = DEFAULT_VD_SOURCE.with_suffix(".csv")


class StressCatalogError(ValueError):
    pass


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _find_header(headers: list[str], aliases: set[str], *, required: bool = True) -> str | None:
    for header in headers:
        if _normalized(header) in aliases:
            return header
    if required:
        raise StressCatalogError(
            f"Required CSV column missing; expected one of: {', '.join(sorted(aliases))}"
        )
    return None


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def parse_duration_seconds(value: str) -> int | None:
    text = value.strip().lower().replace(",", ".")
    if not text or any(mark in text for mark in ("–", "-", "/", "~")):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    multiplier = 1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600
    return int(amount * multiplier)


class StressCatalog:
    def __init__(self, platform: str, definitions: list[StressTestDefinition], source_path: Path | None = None, encoding: str | None = None, source_warnings: list[str] | None = None):
        self.platform = platform.upper()
        self.definitions = definitions
        self.source_path = source_path
        self.encoding = encoding
        self.source_warnings = list(source_warnings or [])

    @classmethod
    def load_vd(cls, source_path: str | Path = DEFAULT_VD_SOURCE, *, expected_count: int = 100) -> "StressCatalog":
        path = Path(source_path)
        if not path.is_file():
            raise StressCatalogError(f"Stress catalog does not exist: {path}")
        if path.suffix.lower() != ".xlsx":
            raise StressCatalogError(
                f"Authoritative VD catalog must be an XLSX workbook, not {path.suffix or 'an extensionless file'}: {path}"
            )
        try:
            import openpyxl
        except ImportError as exc:
            raise StressCatalogError(
                "openpyxl is required to read the authoritative VD XLSX catalog"
            ) from exc
        try:
            workbook = openpyxl.load_workbook(
                path,
                read_only=True,
                data_only=True,
            )
        except (OSError, ValueError, KeyError) as exc:
            raise StressCatalogError(f"Cannot read stress XLSX catalog {path}: {exc}") from exc

        try:
            matching_sheets = []
            for sheet in workbook.worksheets:
                first_row = next(
                    sheet.iter_rows(min_row=1, max_row=1, values_only=True),
                    (),
                )
                headers = [_cell_text(value).strip() for value in first_row]
                if any(_normalized(header) == "test id" for header in headers):
                    matching_sheets.append((sheet, headers))
            if len(matching_sheets) != 1:
                raise StressCatalogError(
                    "VD XLSX must contain exactly one worksheet with a 'Test ID' header; "
                    f"found {len(matching_sheets)}"
                )
            sheet, headers = matching_sheets[0]
            if not headers:
                raise StressCatalogError(f"Stress XLSX catalog has no header row: {path}")
            if any(not header for header in headers):
                raise StressCatalogError("VD XLSX header row contains an empty column name")
            if len(set(headers)) != len(headers):
                raise StressCatalogError("VD XLSX header row contains duplicate column names")
            id_header = _find_header(headers, {"test id", "test case id", "tc id"})
            group_header = _find_header(headers, {"group", "nh m"})
            name_header = _find_header(headers, {"test name", "name"})
            duration_header = _find_header(headers, {"duration", "th i gian"}, required=False)
            severity_header = _find_header(headers, {"severity", "severity khi fail"}, required=False)
            procedure_header = _find_header(headers, {"test procedure", "procedure"}, required=False)
            precondition_header = _find_header(headers, {"pre condition", "precondition"}, required=False)
            equipment_header = _find_header(headers, {"test equipment", "equipment"}, required=False)
            rows = []
            for values in sheet.iter_rows(
                min_row=2,
                max_col=len(headers),
                values_only=True,
            ):
                converted = [_cell_text(value) for value in values]
                if not any(value.strip() for value in converted):
                    continue
                rows.append(dict(zip(headers, converted, strict=True)))
        finally:
            workbook.close()

        definitions: list[StressTestDefinition] = []
        seen: dict[str, int] = {}
        for row_number, row in enumerate(rows, start=2):
            test_id = row[id_header].strip()
            if not test_id:
                raise StressCatalogError(f"Invalid XLSX row {row_number}: Test ID is empty")
            if not test_id.startswith("ST-"):
                raise StressCatalogError(
                    f"Invalid XLSX row {row_number}: Test ID must start with 'ST-'; found {test_id!r}"
                )
            if test_id in seen:
                raise StressCatalogError(f"Duplicate Test ID {test_id!r} at rows {seen[test_id]} and {row_number}")
            seen[test_id] = row_number
            group = row[group_header].strip()
            name = row[name_header].strip()
            if not group or not name:
                raise StressCatalogError(f"Invalid XLSX row {row_number} ({test_id}): Group and Test Name are required")
            source_fields = {header: (row.get(header) or "") for header in headers}
            duration_text = row.get(duration_header, "").strip() if duration_header else ""
            duration_seconds = parse_duration_seconds(duration_text)
            procedure = row.get(procedure_header, "") if procedure_header else ""
            precondition = row.get(precondition_header, "") if precondition_header else ""
            equipment = row.get(equipment_header, "") if equipment_header else ""
            execution_type = classify_execution(group, procedure, equipment)
            program, arguments, description, target_cpu = safe_cpu_workload(test_id, name, duration_seconds)
            if program:
                execution_type = ExecutionType.AUTO
            dependencies = set(infer_dependencies(group, precondition, procedure))
            if program:
                dependencies.add("workload")
            definitions.append(
                StressTestDefinition(
                    test_id=test_id,
                    group=group,
                    test_name=name,
                    source_fields=source_fields,
                    duration_text=duration_text,
                    duration_seconds=duration_seconds,
                    severity=row.get(severity_header, "").strip() if severity_header else "",
                    runtime_status=RuntimeStatus.NOT_RUN,
                    execution_type=execution_type,
                    dependencies=frozenset(dependencies),
                    evidence_plan=build_evidence_plan(group, name),
                    workload_program=program,
                    workload_arguments=arguments,
                    workload_description=description,
                    target_cpu_percent=target_cpu,
                )
            )
        if len(definitions) != expected_count:
            raise StressCatalogError(f"VD catalog must contain exactly {expected_count} unique cases; found {len(definitions)}")
        return cls("VD", definitions, path, "xlsx-unicode")

    def filter(self, *, search: str = "", group: str = "", status: str = "", severity: str = "") -> list[StressTestDefinition]:
        needle = search.casefold().strip()
        result = []
        for definition in self.definitions:
            haystack = " ".join([definition.test_id, definition.group, definition.test_name, *definition.source_fields.values()]).casefold()
            if needle and needle not in haystack:
                continue
            if group and group != "All Groups" and definition.group != group:
                continue
            if status and status != "All" and definition.runtime_status.value != status:
                continue
            if severity and severity != "All" and definition.severity != severity:
                continue
            result.append(definition)
        return result

    def get(self, test_id: str) -> StressTestDefinition:
        for definition in self.definitions:
            if definition.test_id == test_id:
                return definition
        raise KeyError(test_id)
