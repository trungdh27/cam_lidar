import asyncio

from PySide6.QtCore import QThread, Signal

from core.remote.ssh_manager import SSHConfig, SSHManager


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
            result = asyncio.run(self._probe())
            self.success.emit(result)

        except Exception as exc:
            self.failed.emit(str(exc))

    async def _probe(self):
        ssh = SSHManager(self.config)

        try:
            await ssh.connect()
            return await ssh.probe_jetson()

        finally:
            await ssh.disconnect()
