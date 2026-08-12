import asyncio

from PySide6.QtCore import QThread, Signal

from core.network.network_manager import NetworkManager
from core.remote.ssh_manager import (
    SSHConfig,
    SSHManager,
)


class SSHProbeWorker(QThread):
    success = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        port: int = 22,
        parent=None,
    ):
        super().__init__(parent)

        self.config = SSHConfig(
            host=host,
            username=username,
            password=password,
            port=port,
        )

    def run(self):
        try:
            result = asyncio.run(
                self._probe()
            )

            self.success.emit(result)

        except Exception as exc:
            self.failed.emit(
                f"{type(exc).__name__}: {exc}"
            )

    async def _probe(self):
        ssh = SSHManager(self.config)

        try:
            await ssh.connect()

            jetson_info = (
                await ssh.probe_jetson()
            )

            network_manager = NetworkManager(
                ssh
            )

            network_snapshot = (
                await network_manager.inspect()
            )

            return {
                "jetson": jetson_info,
                "network": network_snapshot.to_dict(),
            }

        finally:
            await ssh.disconnect()
