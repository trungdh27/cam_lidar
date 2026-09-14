"""Compatibility shim for the LiDAR testing implementation.

New code must import from :mod:`devices.livox.testing`.
"""

from devices.livox.testing.evidence_store import (
    EvidenceStore,
    LidarEvidenceStore,
)

__all__ = ["EvidenceStore", "LidarEvidenceStore"]
