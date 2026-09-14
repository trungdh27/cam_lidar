"""Compatibility shim for the LiDAR testing implementation.

New code must import from :mod:`devices.livox.testing`.
"""

from devices.livox.testing.test_case import AutomationLevel, TestCaseDefinition

__all__ = ["AutomationLevel", "TestCaseDefinition"]
