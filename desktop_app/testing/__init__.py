"""Compatibility package for the LiDAR testing implementation.

The implementation lives in :mod:`devices.livox.testing`. New code must use
that domain-owned package; these exports remain for legacy callers only.
"""

from desktop_app.testing.test_case import (
    AutomationLevel,
    TestCaseDefinition,
)
from desktop_app.testing.test_context import TestContext
from desktop_app.testing.test_execution_service import TestExecutionService
from desktop_app.testing.test_registry import TestRegistry
from desktop_app.testing.test_result import TestResult, TestStatus

__all__ = [
    "AutomationLevel",
    "TestCaseDefinition",
    "TestContext",
    "TestExecutionService",
    "TestRegistry",
    "TestResult",
    "TestStatus",
]
