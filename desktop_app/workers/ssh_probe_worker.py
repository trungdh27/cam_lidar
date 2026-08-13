import asyncio

from PySide6.QtCore import QThread, Signal

from core.network.network_manager import NetworkManager
from core.remote.ssh_manager import SSHConfig, SSHManager


class SSHProbeWorker(QThread):
    success = Signal(dict)
    failed = Signal(str)

    def __init__(self, host: str, username: str, password: str | None = None, port: int = 22, parent=None):
        super().__init__(parent)
        self.config = SSHConfig(host=host, username=username, password=password, port=port)

    def run(self):
        try:
            self.success.emit(asyncio.run(self._probe()))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    async def _probe(self):
        ssh = SSHManager(self.config)
        try:
            await ssh.connect()
            return {
                "jetson": await ssh.probe_jetson(),
                "network": (await NetworkManager(ssh).inspect()).to_dict(),
            }
        finally:
            await ssh.disconnect()
