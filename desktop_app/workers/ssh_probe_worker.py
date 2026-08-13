import asyncio
from collections.abc import Awaitable, Callable
from threading import Lock

from PySide6.QtCore import QThread, Signal

from core.network.network_manager import NetworkManager
from core.remote.ssh_manager import SSHConfig, SSHManager


class SSHProbeWorker(QThread):
    connected = Signal(dict)
    connection_failed = Signal(str)
    disconnected = Signal()
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = None
        self._ssh = None
        self._connect_task = None
        self._operation_lock = None
        self._pending = []
        self._pending_lock = Lock()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._operation_lock = asyncio.Lock()
        loop.call_soon(self._drain_pending)

        try:
            loop.run_forever()
        finally:
            pending_tasks = asyncio.all_tasks(loop)
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                loop.run_until_complete(
                    asyncio.gather(*pending_tasks, return_exceptions=True)
                )
            loop.close()
            self._loop = None

    def connect_to(self, config: SSHConfig) -> None:
        self._schedule(lambda: self._start_connect(config))

    def disconnect_from_jetson(self) -> None:
        self._schedule(
            lambda: asyncio.create_task(self._disconnect(emit_signal=True))
        )

    def submit_operation(
        self,
        request_id: str,
        operation: Callable[[SSHManager], Awaitable[object]],
    ) -> None:
        self._schedule(
            lambda: asyncio.create_task(
                self._execute_operation(request_id, operation)
            )
        )

    def shutdown(self) -> None:
        self._schedule(lambda: asyncio.create_task(self._shutdown()))

    def _schedule(self, callback: Callable[[], None]) -> None:
        with self._pending_lock:
            self._pending.append(callback)

    def _drain_pending(self) -> None:
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()

        for callback in pending:
            callback()

        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_later(0.05, self._drain_pending)

    def _start_connect(self, config: SSHConfig) -> None:
        if self._connect_task and not self._connect_task.done():
            return
        self._connect_task = asyncio.create_task(self._connect(config))

    async def _connect(self, config: SSHConfig) -> None:
        await self._close_connection()
        ssh = SSHManager(config)
        try:
            await ssh.connect()
            result = {
                "jetson": await ssh.probe_jetson(),
                "network": (await NetworkManager(ssh).inspect()).to_dict(),
            }
            self._ssh = ssh
            self.connected.emit(result)
        except asyncio.CancelledError:
            await ssh.disconnect()
            raise
        except Exception as exc:
            await ssh.disconnect()
            self._ssh = None
            self.connection_failed.emit(f"{type(exc).__name__}: {exc}")

    async def _execute_operation(
        self,
        request_id: str,
        operation: Callable[[SSHManager], Awaitable[object]],
    ) -> None:
        if self._ssh is None or not self._ssh.connected:
            self.operation_failed.emit(
                request_id,
                "Jetson is not connected. Connect from Dashboard first.",
            )
            return

        try:
            async with self._operation_lock:
                result = await operation(self._ssh)
            self.operation_succeeded.emit(request_id, result)
        except Exception as exc:
            self.operation_failed.emit(
                request_id,
                f"{type(exc).__name__}: {exc}",
            )

    async def _disconnect(self, emit_signal: bool) -> None:
        connect_task = self._connect_task
        current_task = asyncio.current_task()
        if (
            connect_task
            and connect_task is not current_task
            and not connect_task.done()
        ):
            connect_task.cancel()
            await asyncio.gather(connect_task, return_exceptions=True)
        await self._close_connection()
        if emit_signal:
            self.disconnected.emit()

    async def _close_connection(self) -> None:
        if self._ssh is None:
            return
        ssh = self._ssh
        self._ssh = None
        await ssh.disconnect()

    async def _shutdown(self) -> None:
        await self._disconnect(emit_signal=False)
        asyncio.get_running_loop().call_soon(
            asyncio.get_running_loop().stop
        )
