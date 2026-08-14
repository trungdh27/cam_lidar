import json
from pathlib import Path

from core.testing.errors import DefinitionValidationError
from core.testing.evaluator import TestEvaluator
from core.testing.models import TestCaseDefinition


REQUIRED = {"id", "name", "group", "automation_key", "priority", "timeout_s", "parameters", "rules"}


def load_definitions(path, registry):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DefinitionValidationError(f"Cannot load test definitions: {exc}") from exc
    if not isinstance(raw, list):
        raise DefinitionValidationError("Definition root must be a JSON array.")
    definitions, identifiers = [], set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not REQUIRED.issubset(item):
            raise DefinitionValidationError(f"Definition #{index + 1} is missing required fields.")
        if item["id"] in identifiers:
            raise DefinitionValidationError(f"Duplicate test ID: {item['id']}")
        identifiers.add(item["id"])
        if item["automation_key"] not in registry:
            raise DefinitionValidationError(f"Unknown automation_key: {item['automation_key']}")
        if not isinstance(item["timeout_s"], (int, float)) or item["timeout_s"] <= 0:
            raise DefinitionValidationError(f"Invalid timeout for {item['id']}")
        if not isinstance(item["parameters"], dict) or not isinstance(item["rules"], list):
            raise DefinitionValidationError(f"Invalid parameters/rules for {item['id']}")
        for rule in item["rules"]:
            if not isinstance(rule, dict) or not {"metric", "operator", "expected"}.issubset(rule):
                raise DefinitionValidationError(f"Malformed rule in {item['id']}")
            if rule["operator"] not in TestEvaluator.OPERATORS:
                raise DefinitionValidationError(f"Unsupported operator in {item['id']}: {rule['operator']}")
        definitions.append(TestCaseDefinition(
            test_id=item["id"], name=item["name"], group=item["group"],
            automation_key=item["automation_key"], priority=item["priority"],
            timeout_s=float(item["timeout_s"]), parameters=item["parameters"],
            rules=tuple(item["rules"]),
        ))
    return definitions
