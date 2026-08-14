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
    remote_process_started = Signal(str)
    remote_process_output = Signal(str, str, str)
    remote_process_finished = Signal(str, int)
    remote_process_failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = None
        self._ssh = None
        self._connect_task = None
        self._operation_lock = None
        self._pending = []
        self._pending_lock = Lock()
        self._remote_processes = {}
        self._remote_process_tasks = {}

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

    def start_remote_process(self, request_id: str, command: str) -> None:
        self._schedule(
            lambda: self._create_remote_process_task(request_id, command)
        )

    def stop_remote_process(self, request_id: str) -> None:
        self._schedule(
            lambda: asyncio.create_task(
                self._stop_remote_process(request_id)
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

    def _create_remote_process_task(
        self,
        request_id: str,
        command: str,
    ) -> None:
        current = self._remote_process_tasks.get(request_id)
        if current is not None and not current.done():
            self.remote_process_failed.emit(
                request_id,
                "Remote process is already running",
            )
            return
        task = asyncio.create_task(
            self._run_remote_process(request_id, command)
        )
        self._remote_process_tasks[request_id] = task

    async def _run_remote_process(
        self,
        request_id: str,
        command: str,
    ) -> None:
        if self._ssh is None or not self._ssh.connected:
            self.remote_process_failed.emit(
                request_id,
                "Jetson is not connected. Connect from Dashboard first.",
            )
            self._remote_process_tasks.pop(request_id, None)
            return

        process = None
        readers = []
        try:
            process = await self._ssh.create_process(command)
            self._remote_processes[request_id] = process
            readers = [
                asyncio.create_task(
                    self._read_remote_output(
                        request_id,
                        "stdout",
                        process.stdout,
                    )
                ),
                asyncio.create_task(
                    self._read_remote_output(
                        request_id,
                        "stderr",
                        process.stderr,
                    )
                ),
            ]
            self.remote_process_started.emit(request_id)
            result = await process.wait()
            await asyncio.gather(*readers, return_exceptions=True)
            exit_status = getattr(result, "exit_status", None)
            if exit_status is None:
                exit_status = process.exit_status
            self.remote_process_finished.emit(
                request_id,
                int(exit_status if exit_status is not None else -1),
            )
        except asyncio.CancelledError:
            if process is not None:
                process.kill()
            raise
        except Exception as exc:
            self.remote_process_failed.emit(
                request_id,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            self._remote_processes.pop(request_id, None)
            self._remote_process_tasks.pop(request_id, None)

    async def _read_remote_output(
        self,
        request_id: str,
        stream_name: str,
        stream,
    ) -> None:
        async for line in stream:
            self.remote_process_output.emit(
                request_id,
                stream_name,
                str(line).rstrip("\r\n"),
            )

    async def _stop_remote_process(self, request_id: str) -> None:
        process = self._remote_processes.get(request_id)
        task = self._remote_process_tasks.get(request_id)
        if process is None or task is None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _stop_all_remote_processes(self) -> None:
        request_ids = list(self._remote_process_tasks)
        if not request_ids:
            return
        await asyncio.gather(
            *(self._stop_remote_process(item) for item in request_ids),
            return_exceptions=True,
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
        await self._stop_all_remote_processes()
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
