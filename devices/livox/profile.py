from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    ip_address,
    ip_interface,
)
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml


DEFAULT_PROFILE_PATH = Path(__file__).with_name("profiles") / "mid360.yaml"


class LivoxProfileError(ValueError):
    pass


@dataclass(frozen=True)
class LivoxModelProfile:
    display_name: str
    sdk_profile: str


@dataclass(frozen=True)
class MeasurementThreshold:
    target: float | None
    tolerance_percent: float | None

    @property
    def configured(self) -> bool:
        return self.target is not None and self.tolerance_percent is not None

    def bounds(self) -> tuple[float, float] | None:
        if not self.configured:
            return None
        tolerance = self.target * self.tolerance_percent / 100.0
        return self.target - tolerance, self.target + tolerance


@dataclass(frozen=True)
class LivoxTestThresholds:
    imu_rate: MeasurementThreshold
    point_rate: MeasurementThreshold
    packet_loss_max_percent: float | None


@dataclass(frozen=True)
class ContinuousOperationProfile:
    mode: str
    development_duration_sec: float
    qualification_duration_sec: float
    min_point_packet_rate: float
    min_point_rate: float
    min_imu_rate: float

    @property
    def duration_sec(self) -> float:
        if self.mode == "qualification":
            return self.qualification_duration_sec
        return self.development_duration_sec


@dataclass(frozen=True)
class LivoxTestingProfile:
    sample_window_sec: float
    stream_start_timeout_sec: float
    continuous_operation: ContinuousOperationProfile
    thresholds: Mapping[str, LivoxTestThresholds]

    def model_thresholds(self, model_name: str) -> LivoxTestThresholds:
        normalized = str(model_name).strip().upper()
        try:
            return self.thresholds[normalized]
        except KeyError as exc:
            raise LivoxProfileError(
                f"Missing test thresholds for model: {model_name}"
            ) from exc


@dataclass(frozen=True)
class JetsonNetworkProfile:
    interface: str
    ip: IPv4Address
    prefix: int

    @property
    def cidr(self) -> str:
        return str(IPv4Interface(f"{self.ip}/{self.prefix}"))

    @property
    def network(self) -> str:
        return str(IPv4Interface(self.cidr).network)

    def matches(self, interface_name: str, ipv4_addresses: list[str]) -> bool:
        if interface_name != self.interface:
            return False
        expected = IPv4Interface(self.cidr)
        for value in ipv4_addresses:
            try:
                current = ip_interface(value)
            except ValueError:
                continue
            if current == expected:
                return True
        return False


@dataclass(frozen=True)
class LidarIdentityProfile:
    ip: IPv4Address
    expected_serial: str
    strict_serial_verification: bool
    ip_access: str


@dataclass(frozen=True)
class UdpPortProfile:
    command: int
    push: int
    point_cloud: int
    imu: int
    log: int

    def to_sdk_dict(self) -> dict[str, int]:
        return {
            "cmd_data_port": self.command,
            "push_msg_port": self.push,
            "point_data_port": self.point_cloud,
            "imu_data_port": self.imu,
            "log_data_port": self.log,
        }


@dataclass(frozen=True)
class LivoxNetworkProfile:
    profile_id: str
    models: Mapping[str, LivoxModelProfile]
    sdk_config_key: str
    master_sdk: bool
    lidar_log_enable: bool
    multicast_ip: IPv4Address
    jetson: JetsonNetworkProfile
    lidar: LidarIdentityProfile
    discovery_port: int
    lidar_ports: UdpPortProfile
    host_ports: UdpPortProfile
    testing: LivoxTestingProfile

    def model(self, model_name: str) -> LivoxModelProfile:
        normalized = str(model_name).strip().upper()
        try:
            return self.models[normalized]
        except KeyError as exc:
            raise LivoxProfileError(
                f"Unsupported Livox model for {self.profile_id}: {model_name}"
            ) from exc

    def sdk_config(self) -> dict:
        return {
            "master_sdk": self.master_sdk,
            "lidar_log_enable": self.lidar_log_enable,
            self.sdk_config_key: {
                "lidar_net_info": self.lidar_ports.to_sdk_dict(),
                "host_net_info": [
                    {
                        "host_ip": str(self.jetson.ip),
                        "multicast_ip": str(self.multicast_ip),
                        **self.host_ports.to_sdk_dict(),
                    }
                ],
            },
        }

    @classmethod
    def from_mapping(cls, data: Mapping) -> "LivoxNetworkProfile":
        try:
            models_raw = _mapping(data, "models")
            sdk = _mapping(data, "sdk")
            jetson = _mapping(data, "jetson")
            lidar = _mapping(data, "lidar")
            udp = _mapping(data, "udp")
            testing = _mapping(data, "testing")

            models = {
                str(name).strip().upper(): _model_profile(name, value)
                for name, value in models_raw.items()
            }
            if not models:
                raise LivoxProfileError("At least one Livox model is required")

            jetson_ip = _ipv4(jetson, "ip")
            prefix = _integer(jetson, "prefix", minimum=1, maximum=32)
            ip_interface(f"{jetson_ip}/{prefix}")
            lidar_ip = _ipv4(lidar, "ip")
            network = IPv4Network(f"{jetson_ip}/{prefix}", strict=False)
            if lidar_ip not in network:
                raise LivoxProfileError(
                    "LiDAR IP must be in the configured Jetson subnet"
                )
            if lidar_ip == jetson_ip:
                raise LivoxProfileError("Jetson and LiDAR IP addresses must differ")

            lidar_access = _text(lidar, "ip_access").lower()
            if lidar_access != "read_only":
                raise LivoxProfileError("LiDAR IP access must be read_only")

            multicast_ip = _ipv4(sdk, "multicast_ip")
            if not multicast_ip.is_multicast:
                raise LivoxProfileError("multicast_ip must be multicast")

            result = cls(
                profile_id=_text(data, "profile_id"),
                models=MappingProxyType(models),
                sdk_config_key=_text(sdk, "config_key"),
                master_sdk=_boolean(sdk, "master_sdk"),
                lidar_log_enable=_boolean(sdk, "lidar_log_enable"),
                multicast_ip=multicast_ip,
                jetson=JetsonNetworkProfile(
                    interface=_text(jetson, "interface"),
                    ip=jetson_ip,
                    prefix=prefix,
                ),
                lidar=LidarIdentityProfile(
                    ip=lidar_ip,
                    expected_serial=_text(lidar, "expected_serial"),
                    strict_serial_verification=_boolean(
                        lidar, "strict_serial_verification"
                    ),
                    ip_access=lidar_access,
                ),
                discovery_port=_port(udp, "discovery"),
                lidar_ports=_ports(_mapping(udp, "lidar"), "udp.lidar"),
                host_ports=_ports(_mapping(udp, "host"), "udp.host"),
                testing=_testing_profile(testing, models),
            )
            all_ports = (
                result.discovery_port,
                *result.lidar_ports.to_sdk_dict().values(),
                *result.host_ports.to_sdk_dict().values(),
            )
            if len(set(all_ports)) != len(all_ports):
                raise LivoxProfileError(
                    "Discovery, LiDAR, and host UDP ports must be unique"
                )
            return result
        except LivoxProfileError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise LivoxProfileError(f"Invalid Livox profile: {exc}") from exc


@lru_cache(maxsize=1)
def load_default_livox_profile() -> LivoxNetworkProfile:
    return load_livox_profile(DEFAULT_PROFILE_PATH)


def load_livox_profile(path: str | Path) -> LivoxNetworkProfile:
    profile_path = Path(path)
    try:
        with profile_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise LivoxProfileError(
            f"Unable to load Livox profile {profile_path}: {exc}"
        ) from exc
    if not isinstance(data, Mapping):
        raise LivoxProfileError(f"Livox profile {profile_path} must contain a mapping")
    return LivoxNetworkProfile.from_mapping(data)


def _mapping(data: Mapping, key: str) -> Mapping:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise LivoxProfileError(f"{key} must be a mapping")
    return value


def _as_mapping(value, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise LivoxProfileError(f"{name} must be a mapping")
    return value


def _model_profile(name, value) -> LivoxModelProfile:
    data = _as_mapping(value, f"models.{name}")
    return LivoxModelProfile(
        display_name=_text(data, "display_name"),
        sdk_profile=_text(data, "sdk_profile").lower(),
    )


def _text(data: Mapping, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LivoxProfileError(f"{key} must be a non-empty string")
    return value.strip()


def _boolean(data: Mapping, key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise LivoxProfileError(f"{key} must be a boolean")
    return value


def _integer(data: Mapping, key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LivoxProfileError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise LivoxProfileError(f"{key} must be between {minimum} and {maximum}")
    return value


def _port(data: Mapping, key: str) -> int:
    return _integer(data, key, minimum=1, maximum=65535)


def _ports(data: Mapping, section: str) -> UdpPortProfile:
    values = {
        key: _port(data, key)
        for key in ("command", "push", "point_cloud", "imu", "log")
    }
    if len(set(values.values())) != len(values):
        raise LivoxProfileError(f"{section} ports must be unique")
    return UdpPortProfile(**values)


def _testing_profile(
    data: Mapping,
    models: Mapping[str, LivoxModelProfile],
) -> LivoxTestingProfile:
    continuous = _mapping(data, "continuous_operation")
    mode = _text(continuous, "mode").lower()
    if mode not in {"development", "qualification"}:
        raise LivoxProfileError(
            "continuous_operation.mode must be development or qualification"
        )
    thresholds_raw = _mapping(data, "thresholds")
    thresholds = {}
    for model_name in models:
        model_data = _mapping(thresholds_raw, model_name)
        imu_rate = _mapping(model_data, "imu_rate_hz")
        point_rate = _mapping(model_data, "point_rate_pts_s")
        packet_loss = _mapping(model_data, "packet_loss")
        thresholds[model_name] = LivoxTestThresholds(
            imu_rate=MeasurementThreshold(
                target=_optional_number(imu_rate, "target"),
                tolerance_percent=_optional_number(
                    imu_rate,
                    "tolerance_percent",
                    minimum=0.0,
                ),
            ),
            point_rate=MeasurementThreshold(
                target=_optional_number(point_rate, "target"),
                tolerance_percent=_optional_number(
                    point_rate,
                    "tolerance_percent",
                    minimum=0.0,
                ),
            ),
            packet_loss_max_percent=_optional_number(
                packet_loss,
                "max_percent",
                minimum=0.0,
            ),
        )
    return LivoxTestingProfile(
        sample_window_sec=_number(
            data,
            "sample_window_sec",
            minimum=0.1,
        ),
        stream_start_timeout_sec=_number(
            data,
            "stream_start_timeout_sec",
            minimum=1.0,
        ),
        continuous_operation=ContinuousOperationProfile(
            mode=mode,
            development_duration_sec=_number(
                continuous,
                "development_duration_sec",
                minimum=1.0,
            ),
            qualification_duration_sec=_number(
                continuous,
                "qualification_duration_sec",
                minimum=1.0,
            ),
            min_point_packet_rate=_number(
                continuous,
                "min_point_packet_rate",
                minimum=0.0,
            ),
            min_point_rate=_number(
                continuous,
                "min_point_rate",
                minimum=0.0,
            ),
            min_imu_rate=_number(
                continuous,
                "min_imu_rate",
                minimum=0.0,
            ),
        ),
        thresholds=MappingProxyType(thresholds),
    )


def _number(data: Mapping, key: str, minimum: float | None = None) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LivoxProfileError(f"{key} must be a number")
    parsed = float(value)
    if minimum is not None and parsed < minimum:
        raise LivoxProfileError(f"{key} must be at least {minimum}")
    return parsed


def _optional_number(
    data: Mapping,
    key: str,
    minimum: float | None = None,
) -> float | None:
    if data.get(key) is None:
        return None
    return _number(data, key, minimum)


def _ipv4(data: Mapping, key: str) -> IPv4Address:
    value = data.get(key)
    try:
        parsed = ip_address(str(value))
    except ValueError as exc:
        raise LivoxProfileError(f"{key} must be a valid IPv4 address") from exc
    if not isinstance(parsed, IPv4Address):
        raise LivoxProfileError(f"{key} must be an IPv4 address")
    return parsed
