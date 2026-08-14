import ipaddress
import json
import re
import shlex

from core.network.interface_detector import (
    classify_interfaces,
    find_interface_by_ip,
    parse_interfaces,
    select_best_lidar_candidate,
)
from core.network.models import (
    ManagementConnection,
    NetworkSnapshot,
    NetworkVerificationResult,
    NetworkVerificationStatus,
    PingResult,
)
from core.remote.ssh_manager import SSHManager


class NetworkManager:
    def __init__(self, ssh: SSHManager):
        self.ssh = ssh

    async def inspect(self) -> NetworkSnapshot:
        if not self.ssh.connected:
            raise RuntimeError("SSH connection must be established before inspecting network")

        link_result = await self.ssh.run("ip -json link", timeout=5)
        addr_result = await self.ssh.run("ip -json addr", timeout=5)
        ssh_connection_result = await self.ssh.run('printf "%s\\n" "$SSH_CONNECTION"', timeout=5)
        sysfs_result = await self.ssh.run(self._sysfs_command(), timeout=5)

        self._ensure_success(link_result.exit_status, "ip -json link", link_result.stderr)
        self._ensure_success(addr_result.exit_status, "ip -json addr", addr_result.stderr)

        try:
            link_data = json.loads(link_result.stdout)
            addr_data = json.loads(addr_result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Unable to parse iproute2 JSON: {exc}") from exc

        interfaces = parse_interfaces(
            link_data=link_data,
            addr_data=addr_data,
            sysfs_metadata=self._parse_sysfs_metadata(sysfs_result.stdout),
        )

        management = self._parse_ssh_connection(ssh_connection_result.stdout)
        if management.server_ip:
            management.interface = find_interface_by_ip(interfaces, management.server_ip)
            management.source_ip = management.server_ip

        if management.interface is None and management.client_ip:
            management.interface, management.source_ip = await self._route_to_client(management.client_ip)

        classify_interfaces(interfaces, management_interface=management.interface)
        return NetworkSnapshot(
            management=management,
            interfaces=interfaces,
            lidar_candidate=select_best_lidar_candidate(interfaces),
        )

    async def ping_lidar(
        self,
        interface_name: str,
        lidar_ip: str,
        count: int = 3,
        wait_seconds: int = 1,
    ) -> PingResult:
        if not self.ssh.connected:
            raise RuntimeError("SSH connection must be established before ping")
        if not interface_name or count < 1 or wait_seconds < 1:
            raise ValueError("Invalid LiDAR ping parameters")

        target = ipaddress.ip_address(lidar_ip)
        if target.version != 4:
            raise ValueError("LiDAR ping supports IPv4 only")
        command = (
            f"LC_ALL=C ping -I {shlex.quote(interface_name)} "
            f"-c {int(count)} -W {int(wait_seconds)} {shlex.quote(str(target))}"
        )
        result = await self.ssh.run(
            command,
            timeout=float(count * wait_seconds + 3),
        )
        return self.parse_ping_result(
            target_ip=str(target),
            interface_name=interface_name,
            command=command,
            exit_code=result.exit_status,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    @staticmethod
    def parse_ping_result(
        target_ip: str,
        interface_name: str,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> PingResult:
        output = "\n".join(part for part in (stdout, stderr) if part)
        packet_summary = re.search(
            r"(?P<transmitted>\d+)\s+packets transmitted,\s*"
            r"(?P<received>\d+)\s+(?:packets\s+)?received,\s*"
            r"(?P<loss>[\d.]+)%\s+packet loss",
            output,
            flags=re.IGNORECASE,
        )
        transmitted = (
            int(packet_summary.group("transmitted"))
            if packet_summary
            else None
        )
        received = (
            int(packet_summary.group("received"))
            if packet_summary
            else None
        )
        packet_loss = (
            float(packet_summary.group("loss"))
            if packet_summary
            else None
        )

        rtt_summary = re.search(
            r"(?:rtt|round-trip)[^=]*=\s*"
            r"(?P<minimum>[\d.]+)/(?P<average>[\d.]+)/",
            output,
            flags=re.IGNORECASE,
        )
        average_rtt = (
            float(rtt_summary.group("average")) if rtt_summary else None
        )
        return PingResult(
            reachable=received is not None and received > 0,
            target_ip=target_ip,
            interface=interface_name,
            transmitted=transmitted,
            received=received,
            packet_loss_percent=packet_loss,
            average_rtt_ms=average_rtt,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def verify_lidar_network(
        snapshot: NetworkSnapshot,
        interface_name: str,
        expected_jetson_cidr: str,
        expected_lidar_ip: str,
    ) -> NetworkVerificationResult:
        expected_jetson = ipaddress.ip_interface(expected_jetson_cidr)
        lidar_ip = ipaddress.ip_address(expected_lidar_ip)
        if expected_jetson.version != 4 or lidar_ip.version != 4:
            raise ValueError("LiDAR network verification supports IPv4 only")

        expected_network = expected_jetson.network
        interface = next(
            (
                item
                for item in snapshot.interfaces
                if item.name == interface_name
            ),
            None,
        )
        if interface is None or not interface.physical:
            return NetworkManager._verification_result(
                status=NetworkVerificationStatus.INTERFACE_NOT_FOUND,
                interface_name=interface_name,
                interface=interface,
                expected_jetson_cidr=str(expected_jetson),
                expected_lidar_ip=str(lidar_ip),
                expected_network=str(expected_network),
                reason=(
                    "Expected physical interface was not found"
                    if interface is None
                    else "Expected interface is not a physical network device"
                ),
            )

        flags = {str(flag).upper() for flag in interface.flags}
        state_up = (
            str(interface.operstate).upper() == "UP"
            or ("UP" in flags and "LOWER_UP" in flags)
        )
        if interface.carrier is not True or not state_up:
            return NetworkManager._verification_result(
                status=NetworkVerificationStatus.LINK_DOWN,
                interface_name=interface_name,
                interface=interface,
                expected_jetson_cidr=str(expected_jetson),
                expected_lidar_ip=str(lidar_ip),
                expected_network=str(expected_network),
                reason="Physical link requires carrier and UP/LOWER_UP state",
            )

        configured_addresses = []
        for value in interface.ipv4_addresses:
            try:
                configured_addresses.append(ipaddress.ip_interface(value))
            except ValueError:
                continue
        if expected_jetson not in configured_addresses:
            return NetworkManager._verification_result(
                status=NetworkVerificationStatus.JETSON_IP_MISMATCH,
                interface_name=interface_name,
                interface=interface,
                expected_jetson_cidr=str(expected_jetson),
                expected_lidar_ip=str(lidar_ip),
                expected_network=str(expected_network),
                reason="Expected Jetson LiDAR IPv4/CIDR is not configured",
            )

        if lidar_ip not in expected_network:
            return NetworkManager._verification_result(
                status=NetworkVerificationStatus.SUBNET_MISMATCH,
                interface_name=interface_name,
                interface=interface,
                expected_jetson_cidr=str(expected_jetson),
                expected_lidar_ip=str(lidar_ip),
                expected_network=str(expected_network),
                reason="Expected LiDAR IP is outside the Jetson LiDAR subnet",
            )

        return NetworkManager._verification_result(
            status=NetworkVerificationStatus.NETWORK_READY,
            interface_name=interface_name,
            interface=interface,
            expected_jetson_cidr=str(expected_jetson),
            expected_lidar_ip=str(lidar_ip),
            expected_network=str(expected_network),
            reason="Fixed LiDAR network profile verified",
        )

    @staticmethod
    def _verification_result(
        status: NetworkVerificationStatus,
        interface_name: str,
        interface,
        expected_jetson_cidr: str,
        expected_lidar_ip: str,
        expected_network: str,
        reason: str,
    ) -> NetworkVerificationResult:
        return NetworkVerificationResult(
            ready=status is NetworkVerificationStatus.NETWORK_READY,
            status=status,
            interface=interface_name,
            mac_address=(interface.mac_address if interface else None),
            operstate=(interface.operstate if interface else "NOT FOUND"),
            carrier=(interface.carrier if interface else None),
            physical=(interface.physical if interface else False),
            flags=list(interface.flags) if interface else [],
            ipv4_addresses=(
                list(interface.ipv4_addresses) if interface else []
            ),
            expected_jetson_cidr=expected_jetson_cidr,
            expected_lidar_ip=expected_lidar_ip,
            expected_network=expected_network,
            reason=reason,
        )

    @staticmethod
    def _parse_ssh_connection(value: str) -> ManagementConnection:
        parts = value.strip().split()
        if len(parts) < 4:
            return ManagementConnection()
        return ManagementConnection(client_ip=parts[0], server_ip=parts[2])

    async def _route_to_client(self, client_ip: str) -> tuple[str | None, str | None]:
        try:
            ipaddress.ip_address(client_ip)
        except ValueError:
            return None, None
        result = await self.ssh.run(f"ip route get {client_ip}", timeout=5)
        if result.exit_status != 0:
            return None, None
        interface_match = re.search(r"\bdev\s+(\S+)", result.stdout)
        source_match = re.search(r"\bsrc\s+(\S+)", result.stdout)
        return (
            interface_match.group(1) if interface_match else None,
            source_match.group(1) if source_match else None,
        )

    @staticmethod
    def _sysfs_command() -> str:
        return r'''
for p in /sys/class/net/*; do
    n=$(basename "$p")
    if [ -r "$p/carrier" ]; then c=$(cat "$p/carrier" 2>/dev/null || echo unknown); else c=unknown; fi
    if [ -e "$p/device" ]; then physical=1; else physical=0; fi
    if [ -d "$p/wireless" ]; then wireless=1; else wireless=0; fi
    printf '%s|%s|%s|%s\n' "$n" "$c" "$physical" "$wireless"
done
'''

    @staticmethod
    def _parse_sysfs_metadata(output: str) -> dict[str, dict]:
        metadata: dict[str, dict] = {}
        for raw_line in output.splitlines():
            parts = raw_line.strip().split("|")
            if len(parts) != 4:
                continue
            name, carrier_raw, physical_raw, wireless_raw = parts
            carrier = True if carrier_raw == "1" else False if carrier_raw == "0" else None
            metadata[name] = {
                "carrier": carrier,
                "physical": physical_raw == "1",
                "wireless": wireless_raw == "1",
            }
        return metadata

    @staticmethod
    def _ensure_success(exit_status: int, command: str, stderr: str) -> None:
        if exit_status != 0:
            raise RuntimeError(f"Remote command failed: {command}\n{stderr.strip()}")
