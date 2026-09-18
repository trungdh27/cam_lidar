"""Bluetooth domain primitives used by the desktop application.

BT-0 intentionally contains no controller commands.  The module owns the
Bluetooth-facing data and execution interfaces so later phases can add remote
checks without placing transport or subprocess logic in Qt widgets.
"""

from core.bluetooth.bluetooth_manager import BluetoothManager
from core.bluetooth.evidence_store import BluetoothEvidenceStore
from core.bluetooth.bluetooth_test_runner import (
    BluetoothTestRunner,
    initial_bluetooth_test_cases,
)
from core.bluetooth.models import (
    BluetoothControllerInfo,
    BluetoothCommandResult,
    BluetoothDiscoveryResult,
    BluetoothDeviceInfo,
    BluetoothTestCase,
    BluetoothTestMode,
    BluetoothTestResult,
    BluetoothTestStatus,
)

__all__ = [
    "BluetoothControllerInfo",
    "BluetoothCommandResult",
    "BluetoothDiscoveryResult",
    "BluetoothDeviceInfo",
    "BluetoothEvidenceStore",
    "BluetoothManager",
    "BluetoothTestCase",
    "BluetoothTestMode",
    "BluetoothTestResult",
    "BluetoothTestRunner",
    "BluetoothTestStatus",
    "initial_bluetooth_test_cases",
]
