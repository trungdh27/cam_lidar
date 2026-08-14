from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from desktop_app.state.lidar_runtime_state import LidarStreamStatus


@dataclass
class TestContext:
    device: str
    jetson_state: Any
    jetson_service: Any
    device_registry: Any
    lidar_runtime_state: Any
    lidar_stream_service: Any
    lidar_discovery_service: Any
    network_profile: Any
    selected_model: str
    discovery_result: dict | None = None
    network_verification: dict | None = None
    ping_result: dict | None = None

    @property
    def jetson_connected(self) -> bool:
        return bool(
            getattr(self.jetson_service, "is_connected", False)
            and getattr(self.jetson_state, "connected", False)
        )

    @property
    def network_ready(self) -> bool:
        verification = self.network_verification or {}
        return bool(
            verification.get("ready") is True
            and verification.get("status") == "NETWORK_READY"
        )

    @property
    def device_ready(self) -> bool:
        result = self.discovery_result or {}
        return bool(
            result.get("found") is True
            and result.get("status") == "FOUND"
            and result.get("model") == self.selected_model
            and result.get("lidar_ip") == str(self.network_profile.lidar.ip)
            and self.network_ready
        )

    @property
    def stream_status(self) -> LidarStreamStatus:
        return self.lidar_runtime_state.stream_status

    @property
    def stream_running(self) -> bool:
        return self.stream_status is LidarStreamStatus.STREAMING

    @property
    def stream_process_active(self) -> bool:
        return bool(
            self.lidar_stream_service.active
            or self.stream_status
            in {
                LidarStreamStatus.STARTING,
                LidarStreamStatus.STREAMING,
                LidarStreamStatus.STALE,
                LidarStreamStatus.STOPPING,
            }
        )

    @property
    def detected_model(self) -> str | None:
        return (self.discovery_result or {}).get("model")

    @property
    def serial(self) -> str | None:
        return (self.discovery_result or {}).get("serial")

    @property
    def host_ip(self) -> str:
        return str(self.network_profile.jetson.ip)

    @property
    def lidar_ip(self) -> str:
        return str(self.network_profile.lidar.ip)

    def update_discovery(self, result: dict) -> None:
        self.discovery_result = dict(result)
        verification = result.get("network_verification")
        if isinstance(verification, dict):
            self.network_verification = dict(verification)
        ping = result.get("ping")
        if isinstance(ping, dict):
            self.ping_result = dict(ping)

    def runtime_snapshot(self) -> dict:
        return self.lidar_runtime_state.snapshot()
