from core.testing.errors import UnknownAutomationKeyError


class TestRegistry:
    def __init__(self):
        self._handlers = {}

    def register(self, automation_key, handler):
        if not automation_key or automation_key in self._handlers:
            raise ValueError(f"Duplicate or empty automation key: {automation_key}")
        self._handlers[automation_key] = handler

    def resolve(self, automation_key):
        try:
            return self._handlers[automation_key]
        except KeyError as exc:
            raise UnknownAutomationKeyError(f"Unknown automation_key: {automation_key}") from exc

    def __contains__(self, automation_key):
        return automation_key in self._handlers
