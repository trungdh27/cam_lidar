import asyncio

from PySide6.QtCore import QThread, Signal

from core.network.models import TemporaryIPState
from core.network.network_manager import NetworkManager
from core.network.temporary_ip_manager import TemporaryIPManager
from core.remote.ssh_manager import SSHConfig, SSHManager


class TemporaryIPWorker(QThread):
    success = Signal(dict)
    failed = Signal(str)

    def __init__(self, ssh_config: SSHConfig, action: str, interface: str | None = None,
                 ip_address: str | None = None, prefix: int = 24, sudo_password: str | None = None,
                 state: dict | None = None, parent=None):
        super().__init__(parent)
        self.ssh_config = ssh_config
        self.action = action
        self.interface = interface
        self.ip_address = ip_address
        self.prefix = prefix
        self.sudo_password = sudo_password
        self.state = state

    def run(self):
        try:
            self.success.emit(asyncio.run(self._execute()))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    async def _execute(self):
        ssh = SSHManager(self.ssh_config)
        try:
            await ssh.connect()
            manager = TemporaryIPManager(ssh)
            if self.action == "apply":
                if not self.interface or not self.ip_address:
                    raise RuntimeError("Temporary IP apply parameters are incomplete")
                state = await manager.apply(
                    interface_name=self.interface,
                    ip_address=self.ip_address,
                    prefix=self.prefix,
                    sudo_password=self.sudo_password,
                )
            elif self.action == "restore":
                if not self.state:
                    raise RuntimeError("Temporary IP state is missing")
                state = await manager.restore(
                    TemporaryIPState.from_dict(self.state),
                    sudo_password=self.sudo_password,
                )
            else:
                raise RuntimeError(f"Unknown action: {self.action}")
            network = await NetworkManager(ssh).inspect()
            return {"action": self.action, "state": state.to_dict(), "network": network.to_dict()}
        finally:
            await ssh.disconnect()
