import ipaddress
import json
import re

from core.network.interface_detector import (
    classify_interfaces,
    find_interface_by_ip,
    parse_interfaces,
    select_best_lidar_candidate,
)

from core.network.models import (
    ManagementConnection,
    NetworkSnapshot,
)

from core.remote.ssh_manager import SSHManager


class NetworkManager:
    def __init__(self, ssh: SSHManager):
        self.ssh = ssh

    async def inspect(self) -> NetworkSnapshot:
        """
        Inspect Jetson network state without modifying anything.
        """

        if not self.ssh.connected:
            raise RuntimeError(
                "SSH connection must be established "
                "before inspecting network"
            )

        link_result = await self.ssh.run(
            "ip -json link",
            timeout=5,
        )

        addr_result = await self.ssh.run(
            "ip -json addr",
            timeout=5,
        )

        ssh_connection_result = await self.ssh.run(
            'printf "%s\\n" "$SSH_CONNECTION"',
            timeout=5,
        )

        sysfs_result = await self.ssh.run(
            self._sysfs_command(),
            timeout=5,
        )

        self._ensure_success(
            link_result.exit_status,
            "ip -json link",
            link_result.stderr,
        )

        self._ensure_success(
            addr_result.exit_status,
            "ip -json addr",
            addr_result.stderr,
        )

        try:
            link_data = json.loads(
                link_result.stdout
            )

            addr_data = json.loads(
                addr_result.stdout
            )

        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Unable to parse iproute2 JSON: {exc}"
            ) from exc

        sysfs_metadata = self._parse_sysfs_metadata(
            sysfs_result.stdout
        )

        interfaces = parse_interfaces(
            link_data=link_data,
            addr_data=addr_data,
            sysfs_metadata=sysfs_metadata,
        )

        management = self._parse_ssh_connection(
            ssh_connection_result.stdout
        )

        # Best case:
        # SSH_CONNECTION gives the exact local Jetson IP
        # used for this session.
        if management.server_ip:
            management.interface = find_interface_by_ip(
                interfaces,
                management.server_ip,
            )

            management.source_ip = (
                management.server_ip
            )

        # Fallback if the local SSH address could not
        # be matched directly.
        if (
            management.interface is None
            and management.client_ip
        ):
            (
                management.interface,
                management.source_ip,
            ) = await self._route_to_client(
                management.client_ip
            )

        classify_interfaces(
            interfaces,
            management_interface=management.interface,
        )

        lidar_candidate = select_best_lidar_candidate(
            interfaces
        )

        return NetworkSnapshot(
            management=management,
            interfaces=interfaces,
            lidar_candidate=lidar_candidate,
        )

    # ------------------------------------------------------
    # SSH management connection
    # ------------------------------------------------------

    @staticmethod
    def _parse_ssh_connection(
        value: str,
    ) -> ManagementConnection:
        """
        SSH_CONNECTION normally contains:

        <client_ip> <client_port> <server_ip> <server_port>

        Example:

        192.168.9.10 50382 192.168.9.169 22
        """

        parts = value.strip().split()

        if len(parts) < 4:
            return ManagementConnection()

        return ManagementConnection(
            client_ip=parts[0],
            server_ip=parts[2],
        )

    async def _route_to_client(
        self,
        client_ip: str,
    ) -> tuple[str | None, str | None]:

        # Validate before inserting it in the shell command.
        try:
            ipaddress.ip_address(client_ip)
        except ValueError:
            return None, None

        result = await self.ssh.run(
            f"ip route get {client_ip}",
            timeout=5,
        )

        if result.exit_status != 0:
            return None, None

        interface_match = re.search(
            r"\bdev\s+(\S+)",
            result.stdout,
        )

        source_match = re.search(
            r"\bsrc\s+(\S+)",
            result.stdout,
        )

        interface = (
            interface_match.group(1)
            if interface_match
            else None
        )

        source_ip = (
            source_match.group(1)
            if source_match
            else None
        )

        return interface, source_ip

    # ------------------------------------------------------
    # sysfs
    # ------------------------------------------------------

    @staticmethod
    def _sysfs_command() -> str:
        return r"""
for p in /sys/class/net/*; do
    n=$(basename "$p")

    if [ -r "$p/carrier" ]; then
        c=$(cat "$p/carrier" 2>/dev/null || echo unknown)
    else
        c=unknown
    fi

    if [ -e "$p/device" ]; then
        physical=1
    else
        physical=0
    fi

    if [ -d "$p/wireless" ]; then
        wireless=1
    else
        wireless=0
    fi

    printf '%s|%s|%s|%s\n' \
        "$n" "$c" "$physical" "$wireless"
done
"""

    @staticmethod
    def _parse_sysfs_metadata(
        output: str,
    ) -> dict[str, dict]:

        metadata: dict[str, dict] = {}

        for raw_line in output.splitlines():
            line = raw_line.strip()

            if not line:
                continue

            parts = line.split("|")

            if len(parts) != 4:
                continue

            name, carrier_raw, physical_raw, wireless_raw = (
                parts
            )

            if carrier_raw == "1":
                carrier = True
            elif carrier_raw == "0":
                carrier = False
            else:
                carrier = None

            metadata[name] = {
                "carrier": carrier,
                "physical": physical_raw == "1",
                "wireless": wireless_raw == "1",
            }

        return metadata

    # ------------------------------------------------------
    # utilities
    # ------------------------------------------------------

    @staticmethod
    def _ensure_success(
        exit_status: int,
        command: str,
        stderr: str,
    ) -> None:

        if exit_status == 0:
            return

        raise RuntimeError(
            f"Remote command failed: {command}\n"
            f"{stderr.strip()}"
        )
