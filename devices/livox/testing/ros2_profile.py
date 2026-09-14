from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, ip_address, ip_interface
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml


DEFAULT_ROS2_PROFILE_PATH = (
    Path(__file__).resolve().parents[3]
    / "testcases"
    / "lidar"
    / "profiles"
    / "production_ros2.yaml"
)


class LidarRos2ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Ros2TopicProfile:
    name: str
    message_type: str


@dataclass(frozen=True)
class LidarRos2TargetProfile:
    profile_id: str
    enabled: bool
    host: str
    username: str
    port: int
    ros_distro: str
    setup_commands: tuple[str, ...]
    production_ros_domain_id: int
    isolated_ros_domain_id: int | None
    container_name: str
    container_network_mode: str
    lidar_interface: str
    host_cidr: str
    lidar_ip: str
    udp_ports: tuple[int, ...]
    lidar_publisher: str
    slam_node: str | None
    topics: Mapping[str, Ros2TopicProfile]
    pointcloud_frame: str
    imu_frame: str | None
    robot_base_frame: str | None
    sampling: Mapping[str, Any]
    acceptance: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "LidarRos2TargetProfile":
        connection = _mapping(data, "connection")
        ros = _mapping(data, "ros")
        container = _mapping(data, "container")
        network = _mapping(data, "network")
        nodes = _mapping(data, "nodes")
        topic_data = _mapping(data, "topics")
        frames = _mapping(data, "frames")
        sampling = _mapping(data, "sampling")
        acceptance = _mapping(data, "acceptance")
        host = _ipv4_text(connection, "host")
        host_cidr = _interface_text(network, "host_cidr")
        lidar_ip = _ipv4_text(network, "lidar_ip")
        if IPv4Address(host) != IPv4Interface(host_cidr).ip:
            raise LidarRos2ProfileError("connection.host must match network.host_cidr")
        if IPv4Address(lidar_ip) not in IPv4Interface(host_cidr).network:
            raise LidarRos2ProfileError("LiDAR and ROS2 target must share the configured subnet")
        production_domain = _integer(ros, "production_ros_domain_id", 0, 232)
        isolated_raw = ros.get("isolated_ros_domain_id")
        isolated_domain = None
        if isolated_raw is not None:
            isolated_domain = _integer(ros, "isolated_ros_domain_id", 0, 232)
            if isolated_domain == production_domain:
                raise LidarRos2ProfileError(
                    "isolated_ros_domain_id must differ from production_ros_domain_id"
                )
        topics = {}
        for key in ("pointcloud2", "custom", "imu"):
            value = _mapping(topic_data, key)
            topics[key] = Ros2TopicProfile(
                name=_text(value, "name"),
                message_type=_text(value, "type"),
            )
        setup = ros.get("setup_commands")
        if not isinstance(setup, list) or not setup or not all(
            isinstance(item, str) and item.strip() for item in setup
        ):
            raise LidarRos2ProfileError("ros.setup_commands must be a non-empty list")
        ports = network.get("udp_ports")
        if not isinstance(ports, list) or not ports:
            raise LidarRos2ProfileError("network.udp_ports must be a non-empty list")
        parsed_ports = tuple(_port_value(item) for item in ports)
        if len(set(parsed_ports)) != len(parsed_ports):
            raise LidarRos2ProfileError("network.udp_ports must be unique")
        return cls(
            profile_id=_text(data, "profile_id"),
            enabled=_boolean(data, "enabled"),
            host=host,
            username=_text(connection, "username"),
            port=_integer(connection, "port", 1, 65535),
            ros_distro=_text(ros, "distro"),
            setup_commands=tuple(item.strip() for item in setup),
            production_ros_domain_id=production_domain,
            isolated_ros_domain_id=isolated_domain,
            container_name=_text(container, "name"),
            container_network_mode=_text(container, "network_mode"),
            lidar_interface=_text(network, "lidar_interface"),
            host_cidr=host_cidr,
            lidar_ip=lidar_ip,
            udp_ports=parsed_ports,
            lidar_publisher=_text(nodes, "lidar_publisher"),
            slam_node=_optional_text(nodes.get("slam")),
            topics=MappingProxyType(topics),
            pointcloud_frame=_text(frames, "pointcloud"),
            imu_frame=_optional_text(frames.get("imu")),
            robot_base_frame=_optional_text(frames.get("robot_base")),
            sampling=MappingProxyType(dict(sampling)),
            acceptance=MappingProxyType(dict(acceptance)),
        )

    @property
    def playback_isolation_configured(self) -> bool:
        return (
            self.isolated_ros_domain_id is not None
            and self.isolated_ros_domain_id != self.production_ros_domain_id
        )


def load_lidar_ros2_profile(
    path: str | Path = DEFAULT_ROS2_PROFILE_PATH,
) -> LidarRos2TargetProfile:
    profile_path = Path(path)
    try:
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LidarRos2ProfileError(
            f"Unable to load ROS2 target profile {profile_path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise LidarRos2ProfileError("ROS2 target profile must be a mapping")
    return LidarRos2TargetProfile.from_mapping(payload)


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise LidarRos2ProfileError(f"{key} must be a mapping")
    return value


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LidarRos2ProfileError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LidarRos2ProfileError("Optional text must be null or a non-empty string")
    return value.strip()


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise LidarRos2ProfileError(f"{key} must be a boolean")
    return value


def _integer(data: Mapping[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LidarRos2ProfileError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _ipv4_text(data: Mapping[str, Any], key: str) -> str:
    try:
        parsed = ip_address(_text(data, key))
    except ValueError as exc:
        raise LidarRos2ProfileError(f"{key} must be an IPv4 address") from exc
    if not isinstance(parsed, IPv4Address):
        raise LidarRos2ProfileError(f"{key} must be an IPv4 address")
    return str(parsed)


def _interface_text(data: Mapping[str, Any], key: str) -> str:
    try:
        parsed = ip_interface(_text(data, key))
    except ValueError as exc:
        raise LidarRos2ProfileError(f"{key} must be an IPv4 interface") from exc
    if not isinstance(parsed, IPv4Interface):
        raise LidarRos2ProfileError(f"{key} must be an IPv4 interface")
    return str(parsed)


def _port_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise LidarRos2ProfileError("UDP ports must be integers from 1 to 65535")
    return value
