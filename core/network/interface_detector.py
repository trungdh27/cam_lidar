from typing import Optional

from core.network.models import InterfaceRole, NetworkInterface


VIRTUAL_PREFIXES = (
    "docker", "br-", "virbr", "veth", "tun", "tap", "wg", "tailscale", "zt",
)


def parse_interfaces(link_data: list[dict], addr_data: list[dict], sysfs_metadata: dict[str, dict]) -> list[NetworkInterface]:
    link_by_name = {item.get("ifname"): item for item in link_data if item.get("ifname")}
    addr_by_name = {item.get("ifname"): item for item in addr_data if item.get("ifname")}
    names = sorted(set(link_by_name) | set(addr_by_name) | set(sysfs_metadata))
    interfaces: list[NetworkInterface] = []

    for name in names:
        link = link_by_name.get(name, {})
        addr = addr_by_name.get(name, {})
        sysfs = sysfs_metadata.get(name, {})
        flags = link.get("flags", addr.get("flags", []))
        carrier = sysfs.get("carrier")
        if carrier is None:
            carrier = "LOWER_UP" in flags

        interface = NetworkInterface(
            name=name,
            ifindex=link.get("ifindex", addr.get("ifindex", 0)),
            mac_address=link.get("address", addr.get("address")),
            mtu=link.get("mtu", addr.get("mtu")),
            operstate=link.get("operstate", addr.get("operstate", "UNKNOWN")),
            link_type=link.get("link_type"),
            carrier=carrier,
            physical=sysfs.get("physical", False),
            wireless=sysfs.get("wireless", False),
            flags=flags,
        )

        for address in addr.get("addr_info", []):
            family = address.get("family")
            local = address.get("local")
            prefixlen = address.get("prefixlen")
            if local is None or prefixlen is None:
                continue
            cidr = f"{local}/{prefixlen}"
            if family == "inet":
                interface.ipv4_addresses.append(cidr)
            elif family == "inet6":
                interface.ipv6_addresses.append(cidr)

        interfaces.append(interface)

    return interfaces


def find_interface_by_ip(interfaces: list[NetworkInterface], ip_address: str) -> Optional[str]:
    for interface in interfaces:
        for cidr in interface.ipv4_addresses + interface.ipv6_addresses:
            if cidr.split("/", 1)[0] == ip_address:
                return interface.name
    return None


def _looks_virtual(interface: NetworkInterface) -> bool:
    return interface.name == "lo" or interface.name.startswith(VIRTUAL_PREFIXES)


def classify_interfaces(interfaces: list[NetworkInterface], management_interface: Optional[str]) -> None:
    for interface in interfaces:
        if management_interface and interface.name == management_interface:
            interface.role = InterfaceRole.MANAGEMENT
            interface.protected = True
            interface.reason = "Interface currently carries the SSH session"
            continue
        if _looks_virtual(interface):
            interface.role = InterfaceRole.IGNORED
            interface.reason = "Virtual or loopback interface"
            continue
        if interface.wireless:
            interface.role = InterfaceRole.IGNORED
            interface.reason = "Wireless interface"
            continue
        if not interface.physical:
            interface.role = InterfaceRole.IGNORED
            interface.reason = "Interface is not a physical network device"
            continue
        if interface.carrier:
            interface.role = InterfaceRole.LIDAR_CANDIDATE
            interface.reason = "Physical Ethernet interface with active link"
            continue
        interface.role = InterfaceRole.AVAILABLE
        interface.reason = "Physical Ethernet interface without active link"


def select_best_lidar_candidate(interfaces: list[NetworkInterface]) -> Optional[str]:
    candidates = [i for i in interfaces if i.role == InterfaceRole.LIDAR_CANDIDATE]
    if not candidates:
        return None
    candidates.sort(key=lambda i: (bool(i.ipv4_addresses), i.name))
    return candidates[0].name
