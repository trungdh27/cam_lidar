from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class InterfaceRole(str, Enum):
    MANAGEMENT = "MANAGEMENT"
    LIDAR_CANDIDATE = "LIDAR_CANDIDATE"
    AVAILABLE = "AVAILABLE"
    IGNORED = "IGNORED"


class NetworkVerificationStatus(str, Enum):
    NETWORK_READY = "NETWORK_READY"
    INTERFACE_NOT_FOUND = "INTERFACE_NOT_FOUND"
    LINK_DOWN = "LINK_DOWN"
    JETSON_IP_MISMATCH = "JETSON_IP_MISMATCH"
    SUBNET_MISMATCH = "SUBNET_MISMATCH"


@dataclass
class NetworkInterface:
    name: str
    ifindex: int = 0
    mac_address: Optional[str] = None
    mtu: Optional[int] = None
    operstate: str = "UNKNOWN"
    link_type: Optional[str] = None
    carrier: Optional[bool] = None
    physical: bool = False
    wireless: bool = False
    ipv4_addresses: list[str] = field(default_factory=list)
    ipv6_addresses: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    role: InterfaceRole = InterfaceRole.IGNORED
    protected: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ifindex": self.ifindex,
            "mac_address": self.mac_address,
            "mtu": self.mtu,
            "operstate": self.operstate,
            "link_type": self.link_type,
            "carrier": self.carrier,
            "physical": self.physical,
            "wireless": self.wireless,
            "ipv4_addresses": self.ipv4_addresses,
            "ipv6_addresses": self.ipv6_addresses,
            "flags": self.flags,
            "role": self.role.value,
            "protected": self.protected,
            "reason": self.reason,
        }


@dataclass
class ManagementConnection:
    client_ip: Optional[str] = None
    server_ip: Optional[str] = None
    interface: Optional[str] = None
    source_ip: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "client_ip": self.client_ip,
            "server_ip": self.server_ip,
            "interface": self.interface,
            "source_ip": self.source_ip,
        }


@dataclass
class NetworkSnapshot:
    management: ManagementConnection
    interfaces: list[NetworkInterface]
    lidar_candidate: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "management": self.management.to_dict(),
            "interfaces": [item.to_dict() for item in self.interfaces],
            "lidar_candidate": self.lidar_candidate,
        }


@dataclass(frozen=True)
class NetworkVerificationResult:
    ready: bool
    status: NetworkVerificationStatus
    interface: str
    mac_address: Optional[str]
    operstate: str
    carrier: Optional[bool]
    physical: bool
    flags: list[str]
    ipv4_addresses: list[str]
    expected_jetson_cidr: str
    expected_lidar_ip: str
    expected_network: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "status": self.status.value,
            "interface": self.interface,
            "mac_address": self.mac_address,
            "operstate": self.operstate,
            "carrier": self.carrier,
            "physical": self.physical,
            "flags": list(self.flags),
            "ipv4_addresses": list(self.ipv4_addresses),
            "expected_jetson_cidr": self.expected_jetson_cidr,
            "expected_lidar_ip": self.expected_lidar_ip,
            "expected_network": self.expected_network,
            "gateway": None,
            "gateway_required": False,
            "default_route_required": False,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PingResult:
    reachable: bool
    target_ip: str
    interface: str
    transmitted: Optional[int]
    received: Optional[int]
    packet_loss_percent: Optional[float]
    average_rtt_ms: Optional[float]
    command: str
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "target_ip": self.target_ip,
            "interface": self.interface,
            "transmitted": self.transmitted,
            "received": self.received,
            "packet_loss_percent": self.packet_loss_percent,
            "average_rtt_ms": self.average_rtt_ms,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass
class TemporaryIPState:
    interface: str
    cidr: str
    previous_ipv4: list[str] = field(default_factory=list)
    added_by_app: bool = False
    active: bool = False

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "cidr": self.cidr,
            "previous_ipv4": self.previous_ipv4,
            "added_by_app": self.added_by_app,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TemporaryIPState":
        return cls(
            interface=data["interface"],
            cidr=data["cidr"],
            previous_ipv4=data.get("previous_ipv4", []),
            added_by_app=data.get("added_by_app", False),
            active=data.get("active", False),
        )
