"""Compatibility shim for the LiDAR testing implementation.

New code must import from :mod:`devices.livox.testing`.
"""

from devices.livox.testing.lidar_tests import (
    ContinuousOperationExecutor,
    DeviceDiscoveryExecutor,
    EthernetInterfaceExecutor,
    ImuDataPathExecutor,
    ImuRateExecutor,
    ImmediateExecutor,
    IpProfileVerificationExecutor,
    MetricWindowExecutor,
    PacketLossExecutor,
    PointDataPathExecutor,
    PointRateExecutor,
    TimestampMonotonicityExecutor,
    build_lidar_test_registry,
)

__all__ = [
    "ContinuousOperationExecutor",
    "DeviceDiscoveryExecutor",
    "EthernetInterfaceExecutor",
    "ImuDataPathExecutor",
    "ImuRateExecutor",
    "ImmediateExecutor",
    "IpProfileVerificationExecutor",
    "MetricWindowExecutor",
    "PacketLossExecutor",
    "PointDataPathExecutor",
    "PointRateExecutor",
    "TimestampMonotonicityExecutor",
    "build_lidar_test_registry",
]
