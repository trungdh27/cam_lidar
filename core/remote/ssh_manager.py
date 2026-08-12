from dataclasses import dataclass
from typing import Optional

import asyncssh


@dataclass
class SSHConfig:
    host: str
    username: str
    port: int = 22
    password: Optional[str] = None


@dataclass
class CommandResult:
    command: str
    stdout: str
    stderr: str
    exit_status: int


class SSHManager:
    def __init__(self, config: SSHConfig):
        self.config = config
        self._connection: asyncssh.SSHClientConnection | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    async def connect(self) -> None:
        kwargs = {
            "host": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
        }

        if self.config.password:
            kwargs["password"] = self.config.password

        self._connection = await asyncssh.connect(**kwargs)

    async def disconnect(self) -> None:
        if self._connection is not None:
            self._connection.close()
            await self._connection.wait_closed()
            self._connection = None

    async def run(
        self,
        command: str,
        timeout: float = 10.0,
    ) -> CommandResult:
        if self._connection is None:
            raise RuntimeError("SSH connection is not established")

        result = await self._connection.run(
            command,
            check=False,
            timeout=timeout,
        )

        return CommandResult(
            command=command,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.exit_status,
        )

    async def probe_jetson(self) -> dict:
        if not self.connected:
            await self.connect()

        hostname = await self.run("hostname")
        architecture = await self.run("uname -m")
        kernel = await self.run("uname -r")
        network = await self.run("ip -brief addr")

        return {
            "hostname": hostname.stdout.strip(),
            "architecture": architecture.stdout.strip(),
            "kernel": kernel.stdout.strip(),
            "network": network.stdout.strip(),
        }
