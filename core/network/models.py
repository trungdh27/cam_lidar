from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class InterfaceRole(str, Enum):
    MANAGEMENT = "MANAGEMENT"
    LIDAR_CANDIDATE = "LIDAR_CANDIDATE"
    AVAILABLE = "AVAILABLE"
    IGNORED = "IGNORED"


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
            "interfaces": [
                interface.to_dict()
                for interface in self.interfaces
            ],
            "lidar_candidate": self.lidar_candidate,
        }
