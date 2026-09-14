"""Compatibility shim for the LiDAR testing implementation.

New code must import from :mod:`devices.livox.testing`.
"""

from devices.livox.testing.test_result import (
    TestOutcome,
    TestResult,
    TestStatus,
)

__all__ = ["TestOutcome", "TestResult", "TestStatus"]
