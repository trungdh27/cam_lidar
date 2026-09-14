from __future__ import annotations

from itertools import count
import shlex

from PySide6.QtCore import QObject, Signal

from core.remote.ssh_manager import SSHConfig
from devices.livox.testing.ros2_executors import Ros2EnvironmentExecutor
from devices.livox.testing.ros2_worker import LidarRos2Worker


class LidarRos2Service(QObject):
    """Managed, bounded and cancellable production ROS2 command service."""

    request_succeeded = Signal(str, object)
    request_failed = Signal(str, str)
    request_cancelled = Signal(str)

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self._ids = count(1)
        self._worker = None

    @property
    def active(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def execute(self, name: str, command: str, *, timeout: float = 30, max_output: int = 131072) -> str | None:
        if not self.profile.enabled:
            return None
        if not 1 <= int(max_output) <= 1_048_576:
            raise ValueError("max_output must be between 1 and 1048576 bytes")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._ensure_worker()
        request_id = f"lidar_ros2:{name}:{next(self._ids)}"
        self._worker.submit(request_id, command, float(timeout), int(max_output))
        return request_id

    def check_environment(self) -> str | None:
        command = self._environment_command()
        return self.execute("environment", command, timeout=30)

    def refresh_graph(self) -> str | None:
        command = self._environment_command()
        return self.execute("graph", command, timeout=30)

    def topic_type(self, topic: str) -> str | None:
        return self._topic_request(
            "type",
            f"ros2 topic type {shlex.quote(topic)}",
        )

    def topic_info(self, topic: str) -> str | None:
        return self._topic_request(
            "info",
            f"ros2 topic info -v {shlex.quote(topic)}",
        )

    def topic_hz(self, topic: str) -> str | None:
        duration = int(self.profile.sampling.get("topic_rate_duration_sec", 10))
        duration = max(1, min(duration, 30))
        command = f"timeout {duration} ros2 topic hz {shlex.quote(topic)}"
        return self._topic_request("hz", command, timeout=duration + 5)

    def topic_echo_once(self, topic: str) -> str | None:
        command = f"timeout 10 ros2 topic echo {shlex.quote(topic)} --once"
        return self._topic_request(
            "echo",
            command,
            timeout=15,
            max_output=16384,
        )

    def cancel(self, request_id: str) -> None:
        if self._worker is not None:
            self._worker.cancel(request_id)

    def _topic_request(
        self,
        name: str,
        operation: str,
        *,
        timeout: float = 30,
        max_output: int = 32768,
    ) -> str | None:
        setup = list(self.profile.setup_commands)
        setup.append(f"export ROS_DOMAIN_ID={int(self.profile.production_ros_domain_id)}")
        command = "LC_ALL=C; " + "; ".join(setup + [operation])
        return self.execute(
            f"topic_{name}",
            command,
            timeout=timeout,
            max_output=max_output,
        )

    def _environment_command(self) -> str:
        base = Ros2EnvironmentExecutor().build_command(self.profile)
        extra = (
            "printf '%s\\n' '__LIDAR_SECTION__=HOSTNAME'; hostname; "
            "printf '%s\\n' '__LIDAR_SECTION__=ROS_DISTRO'; "
            "printf '%s\\n' \"${ROS_DISTRO:-}\"; "
            "printf '%s\\n' '__LIDAR_SECTION__=ROS_VERSION'; "
            "ros2 --version 2>/dev/null || true; "
            "printf '%s\\n' '__LIDAR_SECTION__=DOMAIN'; "
            "printf '%s\\n' \"${ROS_DOMAIN_ID:-}\""
        )
        return f"{base}; {extra}"

    def shutdown(self, wait_ms: int = 5000) -> None:
        worker = self._worker
        if worker is None:
            return
        worker.shutdown()
        if worker.wait(wait_ms):
            self._worker = None

    def _ensure_worker(self) -> None:
        if self.active:
            return
        worker = LidarRos2Worker(
            SSHConfig(
                host=self.profile.host,
                username=self.profile.username,
                port=self.profile.port,
            ),
            self,
        )
        worker.request_succeeded.connect(self.request_succeeded.emit)
        worker.request_failed.connect(self.request_failed.emit)
        worker.request_cancelled.connect(self.request_cancelled.emit)
        self._worker = worker
        worker.start()
