"""Compatibility shim for the LiDAR testing implementation.

New code must import from :mod:`devices.livox.testing`.
"""

from devices.livox.testing.test_execution_service import TestExecutionService

__all__ = ["TestExecutionService"]
