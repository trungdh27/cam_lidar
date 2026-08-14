from PySide6.QtCore import QObject, Signal

from core.network.network_manager import NetworkManager
from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from devices.livox.profile import LivoxNetworkProfile
from devices.livox.sdk2_backend import (
    LivoxSDK2Backend,
    LivoxSDK2EnvironmentError,
    LivoxSDK2Error,
    LivoxSDK2InitializationError,
)


class LivoxDiscoveryWorker(QObject):
    success = Signal(dict)
    failed = Signal(str)
    finished = Signal()
    progress = Signal(str, str)
    preflight_updated = Signal(dict)
    stage_changed = Signal(str)

    def __init__(
        self,
        connection_service: JetsonConnectionService,
        network_profile: LivoxNetworkProfile,
        model: str,
        timeout: int = 8,
        parent=None,
    ):
        super().__init__(parent)
        self.connection_service = connection_service
        self.network_profile = network_profile
        self.model = model
        self.timeout = timeout
        self.request_id = None
        self.current_stage = "IDLE"

    def start(self):
        self.progress.emit("INFO", "Verify shared Jetson connection")
        self.connection_service.operation_succeeded.connect(self._on_success)
        self.connection_service.operation_failed.connect(self._on_failed)
        self.request_id = self.connection_service.submit_operation(
            "livox_discovery",
            self._execute,
        )
        if self.request_id is None:
            self._finish_with_error(
                "Jetson is not connected. Connect from Dashboard first."
            )
            return
        self.progress.emit("PASS", "Shared Jetson connection available")

    async def _execute(self, ssh):
        interface_name = self.network_profile.jetson.interface
        jetson_cidr = self.network_profile.jetson.cidr
        lidar_ip = str(self.network_profile.lidar.ip)
        network_manager = NetworkManager(ssh)

        self._set_stage("Network Checking")
        self.progress.emit(
            "INFO",
            f"Verify network interface {interface_name}",
        )
        try:
            snapshot = await network_manager.inspect()
        except Exception as exc:
            self.progress.emit("FAIL", f"Network inspection failed: {exc}")
            return {
                "found": False,
                "status": "NETWORK_ERROR",
                "reason": str(exc),
                "network": None,
                "network_verification": None,
                "ping": None,
            }
        verification = network_manager.verify_lidar_network(
            snapshot=snapshot,
            interface_name=interface_name,
            expected_jetson_cidr=jetson_cidr,
            expected_lidar_ip=lidar_ip,
        )
        preflight = {
            "network": snapshot.to_dict(),
            "network_verification": verification.to_dict(),
            "ping": None,
        }
        if not verification.ready:
            self.progress.emit(
                "FAIL",
                f"Network verification: {verification.status.value}",
            )
            self.preflight_updated.emit(preflight)
            return {
                "found": False,
                "status": verification.status.value,
                "reason": verification.reason,
                **preflight,
            }

        self.progress.emit("PASS", "Network ready")
        self.progress.emit("INFO", f"Jetson LiDAR IP {jetson_cidr}")
        self.progress.emit("INFO", f"Ping LiDAR {lidar_ip}")
        try:
            ping = await network_manager.ping_lidar(
                interface_name=interface_name,
                lidar_ip=lidar_ip,
                count=3,
                wait_seconds=1,
            )
        except Exception as exc:
            self.progress.emit("FAIL", f"LiDAR ping failed: {exc}")
            preflight["ping"] = {
                "reachable": False,
                "target_ip": lidar_ip,
                "interface": interface_name,
                "transmitted": None,
                "received": None,
                "packet_loss_percent": None,
                "average_rtt_ms": None,
                "command": None,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
            }
            self.preflight_updated.emit(preflight)
            return self._failure_result(
                status="PING_FAILED",
                reason=str(exc),
                preflight=preflight,
            )
        preflight["ping"] = ping.to_dict()
        self.preflight_updated.emit(preflight)
        if not ping.reachable:
            self.progress.emit(
                "FAIL",
                f"LiDAR {lidar_ip} did not respond to ping",
            )
            return {
                "found": False,
                "status": "PING_FAILED",
                "reason": "LiDAR did not respond to Jetson interface-bound ping",
                **preflight,
            }

        self._set_stage("LiDAR Reachable")
        if ping.average_rtt_ms is not None:
            ping_message = f"LiDAR reachable: avg {ping.average_rtt_ms:.2f} ms"
        else:
            ping_message = "LiDAR reachable: ICMP response received"
        self.progress.emit("PASS", ping_message)

        backend = LivoxSDK2Backend(
            ssh,
            network_profile=self.network_profile,
        )
        self._set_stage("SDK Checking")
        self.progress.emit("INFO", "Check Livox SDK2 helper")
        try:
            await backend.check_helper()
        except LivoxSDK2EnvironmentError as exc:
            self.progress.emit("FAIL", str(exc))
            return self._failure_result(
                status="SDK_HELPER_MISSING",
                reason=str(exc),
                preflight=preflight,
            )
        self.progress.emit("PASS", "Livox SDK2 helper available")

        self.progress.emit("INFO", "Check Livox SDK2 version")
        try:
            sdk_version = await backend.get_sdk_version()
        except LivoxSDK2EnvironmentError as exc:
            self.progress.emit("FAIL", f"Livox SDK2 version check failed: {exc}")
            return self._failure_result(
                status="SDK_VERSION_ERROR",
                reason=str(exc),
                preflight=preflight,
            )
        environment = {
            "helper_installed": True,
            "helper_path": backend.HELPER,
            "sdk_version": sdk_version,
            "host_ip": str(self.network_profile.jetson.ip),
            "expected_lidar_ip": lidar_ip,
        }
        self.progress.emit("PASS", f"Livox SDK2 {sdk_version}")

        self._set_stage("Discovering Device")
        self.progress.emit("INFO", f"Discovering {self.model}")
        try:
            device = await backend.discover(
                expected_model=self.model,
                timeout=self.timeout,
            )
        except LivoxSDK2InitializationError as exc:
            self.progress.emit("FAIL", f"SDK initialization failed: {exc}")
            return self._failure_result(
                status="SDK_INIT_ERROR",
                reason=str(exc),
                preflight=preflight,
                environment=environment,
            )
        except LivoxSDK2EnvironmentError as exc:
            self.progress.emit("FAIL", str(exc))
            return self._failure_result(
                status="SDK_HELPER_MISSING",
                reason=str(exc),
                preflight=preflight,
                environment=environment,
            )
        except LivoxSDK2Error as exc:
            self.progress.emit("FAIL", f"SDK2 discovery failed: {exc}")
            return self._failure_result(
                status="SDK_DISCOVERY_ERROR",
                reason=str(exc),
                preflight=preflight,
                environment=environment,
            )
        result = {
            **device.to_dict(),
            **preflight,
            "environment": environment,
        }
        if device.found:
            self.progress.emit(
                "PASS",
                f"{device.model or self.model} detected",
            )
            self._set_stage("Device Ready")
        else:
            self.progress.emit(
                "FAIL",
                f"SDK2 discovery result: {device.status.value}",
            )
        return result

    def _set_stage(self, stage: str):
        self.current_stage = stage
        self.stage_changed.emit(stage)

    @staticmethod
    def _failure_result(
        status: str,
        reason: str,
        preflight: dict,
        environment: dict | None = None,
    ) -> dict:
        result = {
            "found": False,
            "status": status,
            "reason": reason,
            **preflight,
        }
        if environment is not None:
            result["environment"] = environment
        return result

    def _on_success(self, request_id: str, result: object):
        if request_id != self.request_id:
            return
        self.success.emit(result)
        self._finish()

    def _on_failed(self, request_id: str, error: str):
        if request_id != self.request_id:
            return
        self._finish_with_error(error)

    def _finish_with_error(self, error: str):
        self.failed.emit(error)
        self._finish()

    def _finish(self):
        try:
            self.connection_service.operation_succeeded.disconnect(
                self._on_success
            )
            self.connection_service.operation_failed.disconnect(
                self._on_failed
            )
        except RuntimeError:
            pass
        self.finished.emit()
