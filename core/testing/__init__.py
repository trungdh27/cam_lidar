"""Camera-owned test primitives kept at a legacy import path.

Application code should import these through :mod:`devices.camera.testing`.
The implementation remains here in Phase 0 to avoid risky runner churn.
"""

from core.testing.evaluator import TestEvaluator
from core.testing.handler import TestHandler
from core.testing.models import TestCaseDefinition, TestContext, TestResult, TestStatus
from core.testing.registry import TestRegistry
from core.testing.runner import TestRunner

__all__ = ["TestCaseDefinition", "TestContext", "TestEvaluator", "TestHandler", "TestRegistry", "TestResult", "TestRunner", "TestStatus"]
