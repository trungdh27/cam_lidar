from __future__ import annotations

from desktop_app.testing.test_case import TestCaseDefinition


class TestRegistry:
    def __init__(self):
        self._definitions: dict[str, TestCaseDefinition] = {}

    def register(self, definition: TestCaseDefinition) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"Duplicate test id: {definition.id}")
        self._definitions[definition.id] = definition

    def register_many(self, definitions) -> None:
        for definition in definitions:
            self.register(definition)

    def get(self, test_id: str) -> TestCaseDefinition:
        try:
            return self._definitions[test_id]
        except KeyError as exc:
            raise KeyError(f"Unknown test id: {test_id}") from exc

    def get_tests(self, device: str | None = None) -> list[TestCaseDefinition]:
        definitions = list(self._definitions.values())
        if device is not None:
            normalized = device.strip().casefold()
            definitions = [
                item
                for item in definitions
                if item.device.strip().casefold() == normalized
            ]
        return sorted(definitions, key=lambda item: (item.order, item.id))

    def resolve(self, test_ids: list[str]) -> list[TestCaseDefinition]:
        seen = set()
        resolved = []
        for test_id in test_ids:
            if test_id in seen:
                continue
            seen.add(test_id)
            definition = self.get(test_id)
            if definition.enabled:
                resolved.append(definition)
        return resolved
