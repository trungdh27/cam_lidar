from typing import Optional

from core.network.models import (
    InterfaceRole,
    NetworkInterface,
)


VIRTUAL_PREFIXES = (
    "docker",
    "br-",
    "virbr",
    "veth",
    "tun",
    "tap",
    "wg",
    "tailscale",
    "zt",
)


def parse_interfaces(
    link_data: list[dict],
    addr_data: list[dict],
    sysfs_metadata: dict[str, dict],
) -> list[NetworkInterface]:
    """
    Merge information from:

        ip -json link
        ip -json addr
        /sys/class/net

    into normalized NetworkInterface objects.
    """

    link_by_name = {
        item.get("ifname"): item
        for item in link_data
        if item.get("ifname")
    }

    addr_by_name = {
        item.get("ifname"): item
        for item in addr_data
        if item.get("ifname")
    }

    names = sorted(
        set(link_by_name.keys())
        | set(addr_by_name.keys())
        | set(sysfs_metadata.keys())
    )

    interfaces: list[NetworkInterface] = []

    for name in names:
        link = link_by_name.get(name, {})
        addr = addr_by_name.get(name, {})
        sysfs = sysfs_metadata.get(name, {})

        flags = link.get(
            "flags",
            addr.get("flags", []),
        )

        operstate = link.get(
            "operstate",
            addr.get("operstate", "UNKNOWN"),
        )

        carrier = sysfs.get("carrier")

        if carrier is None:
            # Fallback if /sys/class/net/<iface>/carrier
            # cannot be read.
            carrier = "LOWER_UP" in flags

        interface = NetworkInterface(
            name=name,
            ifindex=link.get(
                "ifindex",
                addr.get("ifindex", 0),
            ),
            mac_address=link.get(
                "address",
                addr.get("address"),
            ),
            mtu=link.get(
                "mtu",
                addr.get("mtu"),
            ),
            operstate=operstate,
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


def find_interface_by_ip(
    interfaces: list[NetworkInterface],
    ip_address: str,
) -> Optional[str]:
    for interface in interfaces:
        for cidr in interface.ipv4_addresses:
            if cidr.split("/", 1)[0] == ip_address:
                return interface.name

        for cidr in interface.ipv6_addresses:
            if cidr.split("/", 1)[0] == ip_address:
                return interface.name

    return None


def _looks_virtual(interface: NetworkInterface) -> bool:
    if interface.name == "lo":
        return True

    return interface.name.startswith(
        VIRTUAL_PREFIXES
    )


def classify_interfaces(
    interfaces: list[NetworkInterface],
    management_interface: Optional[str],
) -> None:
    """
    Assign one role to every Jetson network interface.

    Priority:

        MANAGEMENT
        IGNORED
        LIDAR_CANDIDATE
        AVAILABLE
    """

    for interface in interfaces:

        # --------------------------------------------------
        # Management NIC always wins.
        # Even if SSH goes through Tailscale, Wi-Fi, etc.
        # --------------------------------------------------

        if (
            management_interface
            and interface.name == management_interface
        ):
            interface.role = InterfaceRole.MANAGEMENT
            interface.protected = True
            interface.reason = (
                "Interface currently carries the SSH session"
            )
            continue

        # --------------------------------------------------
        # Loopback / virtual devices
        # --------------------------------------------------

        if _looks_virtual(interface):
            interface.role = InterfaceRole.IGNORED
            interface.reason = "Virtual or loopback interface"
            continue

        # --------------------------------------------------
        # Wi-Fi is not considered a LiDAR interface.
        # --------------------------------------------------

        if interface.wireless:
            interface.role = InterfaceRole.IGNORED
            interface.reason = "Wireless interface"
            continue

        # --------------------------------------------------
        # Only physical Ethernet devices should become
        # LiDAR candidates.
        # --------------------------------------------------

        if not interface.physical:
            interface.role = InterfaceRole.IGNORED
            interface.reason = (
                "Interface is not a physical network device"
            )
            continue

        # --------------------------------------------------
        # Physical Ethernet with carrier means cable/link.
        # --------------------------------------------------

        if interface.carrier:
            interface.role = InterfaceRole.LIDAR_CANDIDATE
            interface.reason = (
                "Physical Ethernet interface with active link"
            )
            continue

        # Physical Ethernet but no cable / link yet.
        interface.role = InterfaceRole.AVAILABLE
        interface.reason = (
            "Physical Ethernet interface without active link"
        )


def select_best_lidar_candidate(
    interfaces: list[NetworkInterface],
) -> Optional[str]:
    """
    For v0.1:
    choose the first active physical Ethernet interface
    which is not the SSH management interface.

    Later Livox discovery will confirm whether this is
    really the LiDAR NIC.
    """

    candidates = [
        interface
        for interface in interfaces
        if interface.role
        == InterfaceRole.LIDAR_CANDIDATE
    ]

    if not candidates:
        return None

    # Prefer interface without IPv4 configuration first,
    # because it is frequently the dedicated sensor NIC.
    candidates.sort(
        key=lambda interface: (
            bool(interface.ipv4_addresses),
            interface.name,
        )
    )

    return candidates[0].name
