import asyncio

from PySide6.QtCore import QThread, Signal

from core.remote.ssh_manager import SSHConfig, SSHManager
from devices.livox.sdk2_backend import LivoxSDK2Backend


class LivoxDiscoveryWorker(QThread):
    success = Signal(dict)
    failed = Signal(str)

    def __init__(self, ssh_config: SSHConfig, host_ip: str, model: str, timeout: int = 8, parent=None):
        super().__init__(parent)
        self.ssh_config = ssh_config
        self.host_ip = host_ip
        self.model = model
        self.timeout = timeout

    def run(self):
        try:
            self.success.emit(asyncio.run(self._execute()))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    async def _execute(self):
        ssh = SSHManager(self.ssh_config)
        try:
            await ssh.connect()
            device = await LivoxSDK2Backend(ssh).discover(
                host_ip=self.host_ip,
                expected_model=self.model,
                timeout=self.timeout,
            )
            return device.to_dict()
        finally:
            await ssh.disconnect()
