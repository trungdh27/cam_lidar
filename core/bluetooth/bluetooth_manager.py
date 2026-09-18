"""Read-only Bluetooth diagnostics over the application's shared Jetson SSH."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal

from core.bluetooth.models import (
    BluetoothCommandResult, BluetoothControllerInfo, BluetoothDeviceInfo,
    BluetoothDiscoveryResult, BluetoothTestResult, BluetoothTestStatus,
)


class BluetoothManager(QObject):
    """Own Bluetooth probing while using the one shared Jetson service only."""

    discovery_completed = Signal(object)
    test_completed = Signal(str, object)

    COMMAND_TIMEOUT_S = 8.0
    _CONTROLLER_SYSFS = "ls -1 /sys/class/bluetooth 2>/dev/null"
    _BLUETOOTHCTL_LIST = "bluetoothctl list"
    _BLUETOOTHCTL_SHOW = "bluetoothctl show"
    _BTMGMT_INFO = "btmgmt info"
    _BTMGMT_ADVINFO = "btmgmt advinfo"
    _BLUEZ_ADV_MANAGER = (
        "busctl introspect org.bluez /org/bluez/hci0 "
        "org.bluez.LEAdvertisingManager1"
    )
    _BLUEZ_SUPPORTED_INSTANCES = (
        "busctl get-property org.bluez /org/bluez/hci0 "
        "org.bluez.LEAdvertisingManager1 SupportedInstances"
    )
    _BLUEZ_ACTIVE_INSTANCES = (
        "busctl get-property org.bluez /org/bluez/hci0 "
        "org.bluez.LEAdvertisingManager1 ActiveInstances"
    )
    _SERVICE_ACTIVE = "systemctl is-active bluetooth"
    _SERVICE_SHOW = (
        "systemctl show bluetooth --property=LoadState "
        "--property=ActiveState --property=SubState --no-pager"
    )

    def __init__(self, jetson_service, parent=None):
        super().__init__(parent)
        self.jetson_service = jetson_service
        self._pending: dict[str, tuple[str, str | None]] = {}
        jetson_service.operation_succeeded.connect(self._operation_succeeded)
        jetson_service.operation_failed.connect(self._operation_failed)

    @property
    def jetson_connected(self) -> bool:
        return bool(self.jetson_service and self.jetson_service.is_connected)

    def status_snapshot(self) -> BluetoothDeviceInfo:
        return BluetoothDeviceInfo(jetson_connected=self.jetson_connected)

    def refresh(self) -> BluetoothDeviceInfo:
        """Return shared-connection state without a remote operation."""
        return self.status_snapshot()

    def start_discovery(self) -> str | None:
        """Schedule complete diagnostic discovery on the shared SSH worker."""
        if not self.jetson_connected:
            self.discovery_completed.emit(BluetoothDiscoveryResult(
                device=self.status_snapshot(),
                message="Jetson is not connected. Connect from Dashboard first.",
            ))
            return None
        request_id = self.jetson_service.submit_operation(
            "bluetooth_discovery", self.discover_with_ssh
        )
        if request_id is not None:
            self._pending[request_id] = ("discovery", None)
        return request_id

    def start_test(self, test_case_id: str) -> str | None:
        """Schedule one test. Results are emitted asynchronously."""
        if not self.jetson_connected:
            self.test_completed.emit(test_case_id, self._error_result(
                test_case_id, "Jetson is not connected. Connect from Dashboard first."
            ))
            return None
        operation = {
            "TC-JBT-ENV-001": self.detect_controller,
            "TC-JBT-ENV-002": self.check_bluetooth_service,
            "TC-JBT-ROLE-001": self.detect_peripheral_capability,
        }.get(test_case_id)
        if operation is None:
            self.test_completed.emit(test_case_id, self._error_result(
                test_case_id, "Unknown Bluetooth test case."
            ))
            return None
        request_id = self.jetson_service.submit_operation(
            f"bluetooth_{test_case_id.lower()}", operation
        )
        if request_id is not None:
            self._pending[request_id] = ("test", test_case_id)
        return request_id

    async def discover_with_ssh(self, ssh) -> BluetoothDiscoveryResult:
        """Collect UI state with bounded, read-only commands on Jetson."""
        controller, controller_commands = await self._controller_info(ssh)
        service, service_commands = await self._service_and_adapter_info(ssh)
        role, ble_supported, role_details, role_commands = await self._role_info(
            ssh, service_commands + controller_commands
        )
        powered = self._powered_from_commands(service_commands)
        device = BluetoothDeviceInfo(
            jetson_connected=True,
            controller=replace(controller, powered=powered,
                               ble_supported=ble_supported,
                               roles=tuple(role_details["roles"]),
                               advertising_supported=role_details["advertising_supported"],
                               supported_instances=role_details["supported_instances"],
                               active_instances=role_details["active_instances"],
                               actual_role=role),
            service_active=service["active"],
            service_load_state=service["load_state"],
            service_active_state=service["active_state"],
            service_sub_state=service["sub_state"],
        )
        return BluetoothDiscoveryResult(
            device=device,
            commands=tuple(controller_commands + service_commands + role_commands),
            message="Bluetooth discovery completed.",
        )

    async def discover(self, ssh) -> BluetoothDiscoveryResult:
        """Public discovery API for callers that already own a shared SSH job."""
        return await self.discover_with_ssh(ssh)

    async def get_adapter_info(self, ssh) -> BluetoothDeviceInfo:
        """Return controller and powered-state information from read-only probes."""
        controller, _controller_commands = await self._controller_info(ssh)
        _service, service_commands = await self._service_and_adapter_info(ssh)
        return BluetoothDeviceInfo(
            jetson_connected=True,
            controller=replace(
                controller, powered=self._powered_from_commands(service_commands)
            ),
        )

    async def check_ble_support(self, ssh) -> bool | None:
        """Return only explicitly verified LE support; unknown remains ``None``."""
        info = await self._run_command(ssh, self._BTMGMT_INFO)
        _role, ble_supported, _extra = self._role_info_from_commands([info])
        return ble_supported

    async def detect_controller(self, ssh) -> BluetoothTestResult:
        started = self._now()
        controller, commands = await self._controller_info(ssh)
        if controller.detected is True:
            status = BluetoothTestStatus.PASS
            message = f"Controller detected: {controller.interface or controller.name or 'Bluetooth controller'}."
        elif controller.detected is False:
            status, message = BluetoothTestStatus.FAIL, "No Bluetooth HCI controller was found."
        else:
            status = BluetoothTestStatus.ERROR
            message = self._command_error_message(commands, "Controller detection could not be completed.")
        return self._result("TC-JBT-ENV-001", status, message, started, commands, {
            "controller_detected": controller.detected, "interface": controller.interface,
            "address": controller.address, "controller_name": controller.name,
            "manufacturer": controller.manufacturer, "version": controller.version,
        })

    async def check_bluetooth_service(self, ssh) -> BluetoothTestResult:
        started = self._now()
        service, commands = await self._service_and_adapter_info(ssh)
        powered = self._powered_from_commands(commands)
        by_command = {item.command: item for item in commands}
        show, ctl_show = by_command.get(self._SERVICE_SHOW), by_command.get(self._BLUETOOTHCTL_SHOW)
        fully_known = bool(show and show.success and ctl_show and ctl_show.success)
        active = (service["load_state"] == "loaded" and service["active_state"] == "active"
                  and service["sub_state"] == "running")
        observations = {"load_state": service["load_state"], "active_state": service["active_state"],
                        "sub_state": service["sub_state"], "adapter_powered": powered}
        if active and fully_known and powered is True:
            status, message = BluetoothTestStatus.PASS, "Bluetooth service is active and adapter is powered."
        elif active and fully_known and powered is False:
            status, message = BluetoothTestStatus.FAIL, "Bluetooth service is active but adapter is not powered."
        elif self._service_state_known(service) or powered is False:
            status, message = BluetoothTestStatus.FAIL, self._service_failure_message(service, powered, ctl_show)
        else:
            status = BluetoothTestStatus.ERROR
            message = self._command_error_message(commands, "Bluetooth service readiness could not be completed.")
        return self._result("TC-JBT-ENV-002", status, message, started, commands, observations)

    async def detect_peripheral_capability(self, ssh) -> BluetoothTestResult:
        started = self._now()
        role, ble_supported, details, commands = await self._role_info(ssh, [])
        observations = {
            "ble_supported": ble_supported,
            "roles": list(details["roles"]),
            "advertising_supported": details["advertising_supported"],
            "supported_instances": details["supported_instances"],
            "active_instances": details["active_instances"],
            "actual_role": role,
            "detection_source": details["source"],
        }
        diagnostic = self._advinfo_diagnostic(commands)
        if role == "Peripheral Capable":
            status = BluetoothTestStatus.PASS
            message = self._peripheral_success_message(details, diagnostic)
        elif role == "Unsupported":
            status, message = BluetoothTestStatus.FAIL, "Adapter does not report peripheral or advertising capability."
        else:
            status = BluetoothTestStatus.ERROR
            message = self._command_error_message(commands, "Peripheral capability could not be verified.")
        return self._result("TC-JBT-ROLE-001", status, message, started, commands, observations)

    async def _controller_info(self, ssh):
        sysfs = await self._run_command(ssh, self._CONTROLLER_SYSFS)
        ctl_list = await self._run_command(ssh, self._BLUETOOTHCTL_LIST)
        mgmt = await self._run_command(ssh, self._BTMGMT_INFO)
        interfaces = re.findall(r"(?m)^\s*(hci\d+)\s*$", sysfs.stdout)
        list_match = re.search(r"(?mi)^\s*Controller\s+([0-9A-F:]{17})\s+(.+?)(?:\s+\[default\])?\s*$", ctl_list.stdout)
        mgmt_match = re.search(r"(?mi)^\s*(hci\d+):", mgmt.stdout)
        interface = interfaces or ([mgmt_match.group(1)] if mgmt_match else [])
        address = list_match.group(1) if list_match else self._match(mgmt.stdout, r"(?mi)^\s*addr\s+([0-9A-F:]{17})")
        name = (list_match.group(2).strip() if list_match else None) or self._match(mgmt.stdout, r"(?mi)^\s*name\s+(.+)$")
        detected = True if interface or list_match else False if (sysfs.success or ctl_list.success) else None
        return BluetoothControllerInfo(
            detected=detected, interface=interface[0] if interface else None,
            address=address, name=name,
            manufacturer=self._match(mgmt.stdout, r"(?mi)^\s*manufacturer\s+(.+)$"),
            version=self._match(mgmt.stdout, r"(?mi)^\s*version\s+(.+)$"),
        ), [sysfs, ctl_list, mgmt]

    async def _service_and_adapter_info(self, ssh):
        active = await self._run_command(ssh, self._SERVICE_ACTIVE)
        show = await self._run_command(ssh, self._SERVICE_SHOW)
        ctl_show = await self._run_command(ssh, self._BLUETOOTHCTL_SHOW)
        values = self._properties(show.stdout)
        active_state = values.get("ActiveState") or active.stdout.strip().lower() or None
        return {"load_state": values.get("LoadState"), "active_state": active_state,
                "sub_state": values.get("SubState"), "active": active_state == "active"}, [active, show, ctl_show]

    async def _role_info(self, ssh, prior_commands):
        info = next((item for item in prior_commands if item.command == self._BTMGMT_INFO), None)
        show = next((item for item in prior_commands if item.command == self._BLUETOOTHCTL_SHOW), None)
        extra = []
        if show is None:
            show = await self._run_command(ssh, self._BLUETOOTHCTL_SHOW); extra.append(show)
        if info is None:
            info = await self._run_command(ssh, self._BTMGMT_INFO); extra.append(info)
        advinfo = await self._run_command(ssh, self._BTMGMT_ADVINFO)
        commands = [show, info, advinfo]
        collected = extra + [advinfo]
        role, ble_supported, details = self._role_info_from_commands(commands)
        if role == "Unknown":
            dbus_commands = [
                await self._run_command(ssh, self._BLUEZ_ADV_MANAGER),
                await self._run_command(ssh, self._BLUEZ_SUPPORTED_INSTANCES),
                await self._run_command(ssh, self._BLUEZ_ACTIVE_INSTANCES),
            ]
            commands.extend(dbus_commands)
            collected.extend(dbus_commands)
            role, ble_supported, details = self._role_info_from_commands(commands)
        return role, ble_supported, details, collected

    async def _run_command(self, ssh, command: str) -> BluetoothCommandResult:
        started = time.monotonic()
        try:
            response = await ssh.run(command, timeout=self.COMMAND_TIMEOUT_S)
            return BluetoothCommandResult(command=command, return_code=response.exit_status,
                stdout=response.stdout or "", stderr=response.stderr or "", duration_s=time.monotonic() - started)
        except asyncio.TimeoutError:
            return BluetoothCommandResult(command=command, return_code=None,
                duration_s=time.monotonic() - started, timed_out=True,
                error=f"Remote command timed out after {self.COMMAND_TIMEOUT_S:g} seconds.")
        except Exception as exc:
            return BluetoothCommandResult(command=command, return_code=None,
                duration_s=time.monotonic() - started, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _properties(output: str) -> dict[str, str]:
        return {key.strip(): value.strip().lower() for line in output.splitlines() if "=" in line
                for key, value in [line.split("=", 1)]}

    @staticmethod
    def _match(output: str, expression: str) -> str | None:
        match = re.search(expression, output)
        return match.group(1).strip() if match else None

    @staticmethod
    def _powered_from_commands(commands) -> bool | None:
        show = next((item for item in commands if item.command == BluetoothManager._BLUETOOTHCTL_SHOW), None)
        if show is None: return None
        match = re.search(r"(?mi)^\s*Powered:\s*(yes|no)\s*$", show.stdout)
        return {"yes": True, "no": False}.get(match.group(1).lower()) if match else None

    @staticmethod
    def _role_info_from_commands(commands):
        show = next((item for item in commands if item.command == BluetoothManager._BLUETOOTHCTL_SHOW), None)
        info = next((item for item in commands if item.command == BluetoothManager._BTMGMT_INFO), None)
        advinfo = next((item for item in commands if item.command == BluetoothManager._BTMGMT_ADVINFO), None)
        dbus_manager = next((item for item in commands if item.command == BluetoothManager._BLUEZ_ADV_MANAGER), None)
        dbus_supported = next((item for item in commands if item.command == BluetoothManager._BLUEZ_SUPPORTED_INSTANCES), None)
        dbus_active = next((item for item in commands if item.command == BluetoothManager._BLUEZ_ACTIVE_INSTANCES), None)

        roles = BluetoothManager._roles_from_show(show.stdout if show else "")
        supported_settings = BluetoothManager._supported_settings(info.stdout if info else "")
        ble_supported = (
            True if "le" in supported_settings else
            False if info and info.success else None
        )
        btctl_supported = BluetoothManager._feature_instances(
            show.stdout if show else "", "SupportedInstances"
        )
        btctl_active = BluetoothManager._feature_instances(
            show.stdout if show else "", "ActiveInstances"
        )
        dbus_supported_instances = BluetoothManager._busctl_integer(
            dbus_supported.stdout if dbus_supported and dbus_supported.success else ""
        )
        dbus_active_instances = BluetoothManager._busctl_integer(
            dbus_active.stdout if dbus_active and dbus_active.success else ""
        )
        supported_instances = (
            btctl_supported if btctl_supported is not None else dbus_supported_instances
        )
        active_instances = (
            btctl_active if btctl_active is not None else dbus_active_instances
        )
        advertising_supported = (
            True if supported_instances is not None and supported_instances > 0 else
            False if supported_instances == 0 else
            True if "advertising" in supported_settings else
            False if info and info.success else None
        )
        details = {
            "roles": tuple(sorted(roles)),
            "advertising_supported": advertising_supported,
            "supported_instances": supported_instances,
            "active_instances": active_instances,
            "source": None,
        }
        if "peripheral" in roles:
            details["source"] = "bluetoothctl role"
            return "Peripheral Capable", ble_supported, details
        if btctl_supported is not None and btctl_supported > 0:
            details["source"] = "bluetoothctl advertising features"
            return "Peripheral Capable", ble_supported, details
        if "le" in supported_settings and "advertising" in supported_settings:
            details["source"] = "btmgmt supported settings"
            return "Peripheral Capable", True, details
        if dbus_manager and dbus_manager.success and dbus_supported_instances and dbus_supported_instances > 0:
            details["source"] = "BlueZ LEAdvertisingManager1"
            return "Peripheral Capable", ble_supported, details

        # Only definitive negative sources produce FAIL. A failed optional
        # diagnostic (for example btmgmt advinfo permission denied) is unknown.
        if roles or btctl_supported == 0 or (info and info.success and "advertising" not in supported_settings):
            return "Unsupported", ble_supported, details
        return "Unknown", ble_supported, details

    @staticmethod
    def _advertising_instances(output: str) -> int | None:
        match = re.search(r"(?mi)(?:available|supported)\s+(?:advertising\s+)?instances:\s*(\d+)", output)
        return int(match.group(1)) if match else None

    @staticmethod
    def _roles_from_show(output: str) -> set[str]:
        """Collect all BlueZ role entries; 5.64 prints one role per line."""
        values = re.findall(r"(?mi)^[ \t]*Roles:[ \t]*(.*?)[ \t]*$", output)
        return {
            role.casefold()
            for value in values
            for role in re.split(r"[\s,]+", value.strip())
            if role.strip()
        }

    @staticmethod
    def _feature_instances(output: str, name: str) -> int | None:
        match = re.search(
            rf"(?mi)^[ \t]*{re.escape(name)}:[ \t]*(?:0x[0-9a-f]+[ \t]*)?\(?[ \t]*(\d+)[ \t]*\)?[ \t]*$",
            output,
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def _supported_settings(output: str) -> set[str]:
        """Read only the btmgmt *supported* block, never current settings."""
        block = re.search(
            r"(?ims)^[ \t]*supported settings:[ \t]*(.*?)(?=^[ \t]*current settings:|\Z)",
            output,
        )
        if not block:
            return set()
        return {
            token.casefold()
            for line in block.group(1).splitlines()
            for token in line.strip().split()
            if token.strip()
        }

    @staticmethod
    def _busctl_integer(output: str) -> int | None:
        match = re.search(r"(?m)(\d+)\s*$", output.strip())
        return int(match.group(1)) if match else None

    @staticmethod
    def _advinfo_diagnostic(commands) -> str | None:
        advinfo = next((item for item in commands if item.command == BluetoothManager._BTMGMT_ADVINFO), None)
        if not advinfo:
            return None
        detail = (advinfo.stderr or advinfo.stdout or advinfo.error or "unavailable").strip()
        if advinfo.success and "permission denied" not in detail.casefold():
            return None
        return f"btmgmt advinfo unavailable: {detail}"

    @staticmethod
    def _peripheral_success_message(details: dict, diagnostic: str | None) -> str:
        source = details["source"] or "Bluetooth capability evidence"
        instances = details["supported_instances"]
        suffix = f"; advertising supports {instances} instances" if instances and instances > 0 else ""
        message = f"BLE Peripheral capability confirmed by {source}{suffix}."
        return f"{message} {diagnostic}; using other capability evidence." if diagnostic else message

    @staticmethod
    def _service_state_known(service: dict) -> bool:
        return any(service.get(key) is not None for key in ("load_state", "active_state", "sub_state"))

    @staticmethod
    def _service_failure_message(service, powered, ctl_show) -> str:
        if powered is False:
            return "Bluetooth service is active but adapter is not powered." if service["active"] else "Bluetooth adapter is not powered."
        if service["load_state"] != "loaded": return f"Bluetooth service is not loaded ({service['load_state'] or 'unknown'})."
        if service["active_state"] != "active": return f"Bluetooth service is not active ({service['active_state'] or 'unknown'})."
        if service["sub_state"] != "running": return f"Bluetooth service is not running ({service['sub_state'] or 'unknown'})."
        if ctl_show and not ctl_show.success: return "bluetoothctl show did not respond successfully."
        return "Bluetooth service readiness requirements were not met."

    @staticmethod
    def _command_error_message(commands, fallback: str) -> str:
        timeout = next((item for item in commands if item.timed_out), None)
        if timeout: return timeout.error or fallback
        error = next((item for item in commands if item.error), None)
        return (error.error if error else None) or fallback

    def _operation_succeeded(self, request_id: str, result: object) -> None:
        context = self._pending.pop(request_id, None)
        if context is None: return
        kind, test_case_id = context
        if kind == "discovery": self.discovery_completed.emit(result)
        else: self.test_completed.emit(str(test_case_id), result)

    def _operation_failed(self, request_id: str, error: str) -> None:
        context = self._pending.pop(request_id, None)
        if context is None: return
        kind, test_case_id = context
        if kind == "discovery":
            self.discovery_completed.emit(BluetoothDiscoveryResult(device=self.status_snapshot(), message=str(error)))
        else: self.test_completed.emit(str(test_case_id), self._error_result(str(test_case_id), str(error)))

    def _error_result(self, test_case_id: str, message: str) -> BluetoothTestResult:
        now = self._now()
        return BluetoothTestResult(test_case_id=test_case_id, status=BluetoothTestStatus.ERROR,
            message=message, started_at=now, finished_at=now)

    def _result(self, test_id, status, message, started, commands, observations):
        finished = self._now()
        return BluetoothTestResult(test_case_id=test_id, status=status, message=message,
            started_at=started, finished_at=finished,
            duration_s=max(0.0, (finished - started).total_seconds()),
            observations=observations, command_results=tuple(commands))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
