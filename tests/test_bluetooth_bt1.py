import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

from core.bluetooth import BluetoothManager, BluetoothTestResult, BluetoothTestRunner, BluetoothTestStatus


class FakeJetsonService(QObject):
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.operations = {}

    def submit_operation(self, name, operation):
        request_id = f"{name}:1"
        self.operations[request_id] = operation
        return request_id

    @property
    def is_connected(self):
        return True


class FakeSsh:
    def __init__(self, responses):
        self.responses = responses

    async def run(self, command, timeout):
        response = self.responses.get(command, (127, "", "command unavailable"))
        if isinstance(response, BaseException):
            raise response
        code, stdout, stderr = response
        return SimpleNamespace(exit_status=code, stdout=stdout, stderr=stderr)


class ImmediateManager(QObject):
    test_completed = Signal(str, object)

    def start_test(self, test_id):
        now = datetime.now(timezone.utc)
        self.test_completed.emit(test_id, BluetoothTestResult(
            test_case_id=test_id, status=BluetoothTestStatus.PASS,
            message="mock pass", started_at=now, finished_at=now,
        ))
        return "mock:1"


class BluetoothBt1Tests(unittest.TestCase):
    def setUp(self):
        self.service = FakeJetsonService()
        self.manager = BluetoothManager(self.service)

    def _result(self, method, responses):
        return asyncio.run(method(FakeSsh(responses)))

    def _controller_responses(self, sysfs="hci0\n"):
        return {
            self.manager._CONTROLLER_SYSFS: (0, sysfs, ""),
            self.manager._BLUETOOTHCTL_LIST: (0, "Controller AA:BB:CC:DD:EE:FF Jetson Bluetooth [default]\n", ""),
            self.manager._BTMGMT_INFO: (0, "hci0: Primary controller\n  addr AA:BB:CC:DD:EE:FF\n  name Jetson Bluetooth\n", ""),
        }

    def _service_responses(self, *, active="active", powered="yes"):
        return {
            self.manager._SERVICE_ACTIVE: (0 if active == "active" else 3, active + "\n", ""),
            self.manager._SERVICE_SHOW: (0, f"LoadState=loaded\nActiveState={active}\nSubState={'running' if active == 'active' else 'dead'}\n", ""),
            self.manager._BLUETOOTHCTL_SHOW: (0, f"Controller AA:BB:CC:DD:EE:FF\nPowered: {powered}\n", ""),
        }

    def test_controller_pass_no_controller_and_command_error(self):
        result = self._result(self.manager.detect_controller, self._controller_responses())
        self.assertEqual(result.status, BluetoothTestStatus.PASS)
        self.assertEqual(result.observations["interface"], "hci0")

        no_controller = self._controller_responses(sysfs="")
        no_controller[self.manager._BLUETOOTHCTL_LIST] = (0, "", "")
        no_controller[self.manager._BTMGMT_INFO] = (0, "", "")
        result = self._result(self.manager.detect_controller, no_controller)
        self.assertEqual(result.status, BluetoothTestStatus.FAIL)

        errors = {command: RuntimeError("remote failure") for command in (
            self.manager._CONTROLLER_SYSFS, self.manager._BLUETOOTHCTL_LIST, self.manager._BTMGMT_INFO
        )}
        result = self._result(self.manager.detect_controller, errors)
        self.assertEqual(result.status, BluetoothTestStatus.ERROR)

    def test_service_active_inactive_and_powered_off(self):
        result = self._result(self.manager.check_bluetooth_service, self._service_responses())
        self.assertEqual(result.status, BluetoothTestStatus.PASS)

        result = self._result(self.manager.check_bluetooth_service, self._service_responses(active="inactive"))
        self.assertEqual(result.status, BluetoothTestStatus.FAIL)

        result = self._result(self.manager.check_bluetooth_service, self._service_responses(powered="no"))
        self.assertEqual(result.status, BluetoothTestStatus.FAIL)
        self.assertIn("not powered", result.message)

    def test_explicit_and_fallback_peripheral_capability(self):
        explicit = {
            self.manager._BLUETOOTHCTL_SHOW: (0, "\tRoles: central\n\tRoles: peripheral\n", ""),
            self.manager._BTMGMT_INFO: (0, "supported settings:\nle\nadvertising\ncurrent settings:\nle\n", ""),
            self.manager._BTMGMT_ADVINFO: (1, "", "Reading adv features failed with status 0x14 (Permission Denied)"),
        }
        result = self._result(self.manager.detect_peripheral_capability, explicit)
        self.assertEqual(result.status, BluetoothTestStatus.PASS)
        self.assertEqual(result.observations["roles"], ["central", "peripheral"])
        self.assertEqual(result.observations["actual_role"], "Peripheral Capable")
        self.assertIn("advinfo unavailable", result.message)

        no_peripheral = dict(explicit)
        no_peripheral[self.manager._BLUETOOTHCTL_SHOW] = (0, "Roles: central\n", "")
        no_peripheral[self.manager._BTMGMT_INFO] = (0, "supported settings:\n le\ncurrent settings:\n le\n", "")
        result = self._result(self.manager.detect_peripheral_capability, no_peripheral)
        self.assertEqual(result.status, BluetoothTestStatus.FAIL)

        fallback = {
            self.manager._BLUETOOTHCTL_SHOW: (0, "Controller AA:BB\n", ""),
            self.manager._BTMGMT_INFO: (0, "supported settings:\n powered\n le\n advertising\ncurrent settings:\n le\n", ""),
            self.manager._BTMGMT_ADVINFO: (0, "Available instances: 1\n", ""),
        }
        result = self._result(self.manager.detect_peripheral_capability, fallback)
        self.assertEqual(result.status, BluetoothTestStatus.PASS)
        self.assertEqual(result.observations["actual_role"], "Peripheral Capable")

    def test_discovery_returns_structured_status_for_the_ui(self):
        responses = self._controller_responses()
        responses[self.manager._BTMGMT_INFO] = (
            0,
            "hci0: Primary controller\n  addr AA:BB:CC:DD:EE:FF\n"
            "  name Jetson Bluetooth\n  supported settings: powered le advertising\n",
            "",
        )
        responses.update(self._service_responses())
        responses[self.manager._BLUETOOTHCTL_SHOW] = (
            0, "Name: Jetson Bluetooth\nPowered: yes\nRoles: central\nRoles: peripheral\n"
            "Advertising Features:\n ActiveInstances: 0x00 (0)\n SupportedInstances: 0x04 (4)\n", ""
        )
        responses[self.manager._BTMGMT_ADVINFO] = (0, "Available instances: 1\n", "")
        discovery = asyncio.run(self.manager.discover_with_ssh(FakeSsh(responses)))
        self.assertTrue(discovery.device.controller.detected)
        self.assertEqual(discovery.device.controller.interface, "hci0")
        self.assertTrue(discovery.device.service_active)
        self.assertTrue(discovery.device.controller.powered)
        self.assertTrue(discovery.device.controller.ble_supported)
        self.assertEqual(discovery.device.controller.roles, ("central", "peripheral"))
        self.assertTrue(discovery.device.controller.advertising_supported)
        self.assertEqual(discovery.device.controller.supported_instances, 4)
        self.assertEqual(discovery.device.controller.active_instances, 0)
        self.assertEqual(discovery.device.controller.actual_role, "Peripheral Capable")
        self.assertTrue(discovery.commands)

    def test_bluetoothctl_advertising_features_use_supported_not_active_instances(self):
        supported = {
            self.manager._BLUETOOTHCTL_SHOW: (0, "Advertising Features:\n ActiveInstances: 0x00 (0)\n SupportedInstances: 0x04 (4)\n", ""),
            self.manager._BTMGMT_INFO: (127, "", "missing"),
            self.manager._BTMGMT_ADVINFO: (1, "", "Permission Denied"),
        }
        result = self._result(self.manager.detect_peripheral_capability, supported)
        self.assertEqual(result.status, BluetoothTestStatus.PASS)
        self.assertTrue(result.observations["advertising_supported"])
        self.assertEqual(result.observations["supported_instances"], 4)
        self.assertEqual(result.observations["active_instances"], 0)

        unsupported = dict(supported)
        unsupported[self.manager._BLUETOOTHCTL_SHOW] = (0, "ActiveInstances: 0x00 (0)\nSupportedInstances: 0x00 (0)\n", "")
        result = self._result(self.manager.detect_peripheral_capability, unsupported)
        self.assertEqual(result.status, BluetoothTestStatus.FAIL)

    def test_btmgmt_uses_supported_settings_and_dbus_is_a_fallback(self):
        btmgmt = {
            self.manager._BLUETOOTHCTL_SHOW: (0, "Controller AA:BB\n", ""),
            self.manager._BTMGMT_INFO: (0, "supported settings:\n le\n advertising\ncurrent settings:\n le\n", ""),
            self.manager._BTMGMT_ADVINFO: (1, "", "Permission Denied"),
        }
        result = self._result(self.manager.detect_peripheral_capability, btmgmt)
        self.assertEqual(result.status, BluetoothTestStatus.PASS)
        self.assertEqual(result.observations["detection_source"], "btmgmt supported settings")

        dbus = {
            self.manager._BLUETOOTHCTL_SHOW: (127, "", "missing"),
            self.manager._BTMGMT_INFO: (127, "", "missing"),
            self.manager._BTMGMT_ADVINFO: (127, "", "missing"),
            self.manager._BLUEZ_ADV_MANAGER: (0, "interface org.bluez.LEAdvertisingManager1\n", ""),
            self.manager._BLUEZ_SUPPORTED_INSTANCES: (0, "u 4\n", ""),
            self.manager._BLUEZ_ACTIVE_INSTANCES: (0, "u 0\n", ""),
        }
        result = self._result(self.manager.detect_peripheral_capability, dbus)
        self.assertEqual(result.status, BluetoothTestStatus.PASS)
        self.assertEqual(result.observations["detection_source"], "BlueZ LEAdvertisingManager1")

    def test_unknown_role_and_timeout_are_errors(self):
        unavailable = {
            self.manager._BLUETOOTHCTL_SHOW: (127, "", "missing"),
            self.manager._BTMGMT_INFO: (127, "", "missing"),
            self.manager._BTMGMT_ADVINFO: (127, "", "missing"),
        }
        result = self._result(self.manager.detect_peripheral_capability, unavailable)
        self.assertEqual(result.status, BluetoothTestStatus.ERROR)

        timeout = {command: asyncio.TimeoutError() for command in (
            self.manager._CONTROLLER_SYSFS, self.manager._BLUETOOTHCTL_LIST, self.manager._BTMGMT_INFO
        )}
        result = self._result(self.manager.detect_controller, timeout)
        self.assertEqual(result.status, BluetoothTestStatus.ERROR)
        self.assertIn("timed out", result.message)

    def test_runner_summary_updates_and_never_uses_not_implemented(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = BluetoothTestRunner(ImmediateManager(), evidence_root=temporary)
            runner.run_all()
            self.assertEqual(runner.summary()[BluetoothTestStatus.PASS], 3)
            self.assertEqual(runner.summary()[BluetoothTestStatus.NOT_IMPLEMENTED], 0)
            self.assertTrue(all(result.evidence_paths for result in runner.results.values()))

    def test_manager_schedules_on_the_shared_service_without_blocking(self):
        completed = []
        self.manager.test_completed.connect(
            lambda test_id, result: completed.append((test_id, result))
        )
        request_id = self.manager.start_test("TC-JBT-ENV-001")
        self.assertEqual(request_id, "bluetooth_tc-jbt-env-001:1")
        self.assertIn(request_id, self.service.operations)
        self.assertEqual(completed, [])

        now = datetime.now(timezone.utc)
        expected = BluetoothTestResult(
            test_case_id="TC-JBT-ENV-001", status=BluetoothTestStatus.PASS,
            message="mock controller", started_at=now, finished_at=now,
        )
        self.service.operation_succeeded.emit(request_id, expected)
        self.assertEqual(completed, [("TC-JBT-ENV-001", expected)])


if __name__ == "__main__":
    unittest.main()
