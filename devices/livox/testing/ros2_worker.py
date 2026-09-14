from __future__ import annotations

import asyncio
from collections import deque
from threading import Lock

from PySide6.QtCore import QThread, Signal

from core.remote.ssh_manager import SSHConfig, SSHManager


class LidarRos2Worker(QThread):
    """One serialized SSH owner for the production LiDAR ROS2 target."""

    connected = Signal()
    connection_failed = Signal(str)
    request_succeeded = Signal(str, object)
    request_failed = Signal(str, str)
    request_cancelled = Signal(str)
    stopped = Signal()

    def __init__(self, config: SSHConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self._loop = None
        self._ssh = None
        self._pending = deque()
        self._pending_lock = Lock()
        self._active_task = None
        self._active_process = None
        self._active_request_id = None
        self._shutdown_requested = False

    def submit(self, request_id: str, command: str, timeout: float, max_output: int) -> None:
        with self._pending_lock:
            self._pending.append((request_id, command, timeout, max_output))
        self._schedule_drain()

    def cancel(self, request_id: str) -> None:
        with self._pending_lock:
            retained = deque()
            removed = False
            while self._pending:
                item = self._pending.popleft()
                if item[0] == request_id:
                    removed = True
                else:
                    retained.append(item)
            self._pending = retained
        if removed:
            self.request_cancelled.emit(request_id)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._cancel_active, request_id)

    def shutdown(self) -> None:
        self._shutdown_requested = True
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._shutdown_async())
            )

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.call_soon(self._drain)
        try:
            loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()
            self._loop = None

    def _schedule_drain(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._drain)

    def _drain(self) -> None:
        if self._shutdown_requested or self._active_task is not None:
            return
        with self._pending_lock:
            item = self._pending.popleft() if self._pending else None
        if item is None:
            return
        self._active_task = asyncio.create_task(self._execute(*item))

    async def _ensure_connected(self) -> None:
        if self._ssh is not None and self._ssh.connected:
            return
        ssh = SSHManager(self.config)
        try:
            await ssh.connect()
        except Exception:
            await ssh.disconnect()
            raise
        self._ssh = ssh
        self.connected.emit()

    async def _execute(
        self, request_id: str, command: str, timeout: float, max_output: int
    ) -> None:
        self._active_request_id = request_id
        readers = []
        try:
            await self._ensure_connected()
            process = await self._ssh.create_process(command)
            self._active_process = process
            stdout_task = asyncio.create_task(_read_limited(process.stdout, max_output))
            stderr_task = asyncio.create_task(_read_limited(process.stderr, max_output))
            readers = [stdout_task, stderr_task]
            result = await asyncio.wait_for(process.wait(), timeout=timeout)
            stdout_result, stderr_result = await asyncio.gather(*readers)
            stdout, stdout_truncated = stdout_result
            stderr, stderr_truncated = stderr_result
            exit_status = getattr(result, "exit_status", process.exit_status)
            self.request_succeeded.emit(
                request_id,
                {
                    "command": command,
                    "exit_status": int(exit_status if exit_status is not None else -1),
                    "stdout": stdout,
                    "stderr": stderr,
                    "output_truncated": stdout_truncated or stderr_truncated,
                },
            )
        except asyncio.CancelledError:
            if self._active_process is not None:
                self._active_process.kill()
            self.request_cancelled.emit(request_id)
        except asyncio.TimeoutError:
            if self._active_process is not None:
                self._active_process.kill()
            self.request_failed.emit(request_id, f"Remote command timed out after {timeout:.1f} seconds")
        except Exception as exc:
            self.request_failed.emit(request_id, f"{type(exc).__name__}: {exc}")
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            self._active_process = None
            self._active_request_id = None
            self._active_task = None
            self._drain()

    def _cancel_active(self, request_id: str) -> None:
        if self._active_request_id == request_id and self._active_task is not None:
            self._active_task.cancel()

    async def _shutdown_async(self) -> None:
        with self._pending_lock:
            pending_ids = [item[0] for item in self._pending]
            self._pending.clear()
        for request_id in pending_ids:
            self.request_cancelled.emit(request_id)
        if self._active_task is not None:
            self._active_task.cancel()
            await asyncio.gather(self._active_task, return_exceptions=True)
        if self._ssh is not None:
            await self._ssh.disconnect()
            self._ssh = None
        self.stopped.emit()
        asyncio.get_running_loop().stop()


async def _read_limited(stream, limit: int) -> tuple[str, bool]:
    chunks = []
    size = 0
    truncated = False
    async for chunk in stream:
        text = str(chunk)
        if size < limit:
            kept = text[: limit - size]
            chunks.append(kept)
            size += len(kept)
            if len(kept) < len(text):
                truncated = True
        else:
            truncated = True
    return "".join(chunks), truncated
