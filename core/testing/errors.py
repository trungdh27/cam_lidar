class TestFrameworkError(RuntimeError):
    code = "INTERNAL_ERROR"


class DefinitionValidationError(TestFrameworkError):
    code = "INVALID_DEFINITION"


class UnknownAutomationKeyError(DefinitionValidationError):
    code = "UNKNOWN_AUTOMATION_KEY"


class TestBlockedError(TestFrameworkError):
    code = "PREREQUISITE_BLOCKED"


class TestCancelledError(TestFrameworkError):
    code = "TEST_CANCELLED"


class TestTimeoutError(TestFrameworkError):
    code = "TEST_TIMEOUT"
