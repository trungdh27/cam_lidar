"""Compatibility shim for the LiDAR testing implementation.

New code must import from :mod:`devices.livox.testing`.
"""

from devices.livox.testing.test_registry import TestRegistry

__all__ = ["TestRegistry"]
