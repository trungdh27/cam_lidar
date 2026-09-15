"""Read the authoritative VD Wi-Fi suite; environments have independent catalogs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

SOURCE = Path(__file__).resolve().parents[2] / "testcases/wifi/source/Jetson_WiFi_TCs_VD_completed.xlsx"
ENVIRONMENTS = ("VD", "VMO", "VR")
EXCLUDED_IDS = frozenset({"TC-WIFI-C08"})
PLACEHOLDER = re.compile(r"^\s*<[^>]+>\s*$")


@dataclass(frozen=True)
class WifiTestCase:
    test_id: str
    target: str
    category: str
    name: str
    purpose: str
    preconditions: str
    requirement: str
    procedure: str
    expected: str
    actual: str
    source_status: str
    notes: str
    mode: str
    phase: str
    source: str
    source_fields: dict[str, str]

    @property
    def group(self) -> str:
        return self.category


def _rows(sheet):
    iterator = sheet.values
    headers = [str(value).strip() if value is not None else "" for value in next(iterator)]
    for values in iterator:
        row = {key: str(value).strip() if value is not None else "" for key, value in zip(headers, values) if key}
        if any(row.values()):
            yield row


def load_config(source: Path = SOURCE) -> dict[str, str]:
    workbook = load_workbook(source, read_only=True, data_only=True)
    try:
        result = {}
        for row in list(workbook["Config"].values)[1:]:
            if row[0]:
                value = str(row[1]).strip() if row[1] is not None else ""
                result[str(row[0]).strip()] = "NOT CONFIGURED" if not value or PLACEHOLDER.fullmatch(value) else value
        return result
    finally:
        workbook.close()


def load_execution_order(source: Path = SOURCE) -> list[dict[str, str]]:
    workbook = load_workbook(source, read_only=True, data_only=True)
    try:
        return list(_rows(workbook["Execution Order"]))
    finally:
        workbook.close()


def source_rows(source: Path = SOURCE) -> list[dict[str, str]]:
    workbook = load_workbook(source, read_only=True, data_only=True)
    try:
        rows = list(_rows(workbook["Consolidated TCs"]))
        ids = [row["ID TC"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate Wi-Fi source test ID")
        return rows
    finally:
        workbook.close()


def _phase(test_id: str, order: list[dict[str, str]]) -> str:
    number = int(test_id.rsplit("C", 1)[1])
    for item in order:
        numbers = [int(value) for value in re.findall(r"C(\d+)", item["Consolidated TCs"])]
        if numbers and min(numbers) <= number <= max(numbers):
            return item["Phase"]
    return ""


def load_catalog(environment: str, source: Path = SOURCE) -> list[WifiTestCase]:
    if environment not in ENVIRONMENTS:
        raise ValueError(f"Unknown Wi-Fi environment: {environment}")
    if environment != "VD":
        return []
    order = load_execution_order(source)
    cases = []
    for row in source_rows(source):
        test_id = row["ID TC"]
        if test_id in EXCLUDED_IDS:
            continue
        number = int(test_id.rsplit("C", 1)[1])
        # Only C01 and C03 currently have complete machine-verifiable source coverage.
        # Other baseline cases still run the safe read-only capture, then need review.
        mode = "AUTO" if number in {1, 3} else "GUIDED"
        cases.append(WifiTestCase(
            test_id=test_id, target="Jetson", category=row["Group"], name=row["Test Case"],
            purpose=row["Purpose"], preconditions=row["Preconditions"],
            requirement=row["Requirement"], procedure=row["Operation / Procedure"],
            expected=row["Expected Result"], actual=row["Actual Results"],
            source_status=row["Status"], notes=row["Notes"], mode=mode,
            phase=_phase(test_id, order), source=source.name, source_fields=row,
        ))
    return cases
