import ipaddress
import shlex

from core.network.models import TemporaryIPState
from core.network.network_manager import NetworkManager
from core.remote.ssh_manager import SSHManager


class TemporaryIPError(RuntimeError):
    pass


class ProtectedInterfaceError(TemporaryIPError):
    pass


class InvalidIPAddressError(TemporaryIPError):
    pass


class TemporaryIPManager:
    def __init__(self, ssh: SSHManager):
        self.ssh = ssh

    @staticmethod
    def normalize_cidr(ip_address: str, prefix: int) -> str:
        try:
            interface = ipaddress.ip_interface(f"{ip_address}/{prefix}")
        except ValueError as exc:
            raise InvalidIPAddressError(f"Invalid IPv4 address: {ip_address}/{prefix}") from exc
        if interface.version != 4:
            raise InvalidIPAddressError("Only IPv4 is supported in v0.1")
        ip = interface.ip
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
            raise InvalidIPAddressError(f"Address is not allowed for LiDAR configuration: {ip}")
        return str(interface)

    async def apply(self, interface_name: str, ip_address: str, prefix: int, sudo_password: str | None = None) -> TemporaryIPState:
        cidr = self.normalize_cidr(ip_address, prefix)
        snapshot = await NetworkManager(self.ssh).inspect()
        interface = self._find_interface(snapshot, interface_name)
        self._validate_target_interface(interface)
        requested_ip = ipaddress.ip_interface(cidr).ip

        for current_interface in snapshot.interfaces:
            for current_cidr in current_interface.ipv4_addresses:
                existing = ipaddress.ip_interface(current_cidr)
                if existing.ip != requested_ip:
                    continue
                if current_interface.name == interface_name:
                    return TemporaryIPState(
                        interface=interface_name,
                        cidr=current_cidr,
                        previous_ipv4=list(interface.ipv4_addresses),
                        added_by_app=False,
                        active=True,
                    )
                raise TemporaryIPError(
                    f"IP {requested_ip} is already configured on Jetson interface {current_interface.name}"
                )

        previous_ipv4 = list(interface.ipv4_addresses)
        await self._sudo_ip(["link", "set", interface_name, "up"], sudo_password)
        result = await self._sudo_ip(
            ["addr", "add", cidr, "dev", interface_name],
            sudo_password,
            raise_on_error=False,
        )
        if result.exit_status != 0:
            raise TemporaryIPError(f"Unable to configure temporary IP.\n{result.stderr.strip()}")
        if not await self._address_exists(interface_name, cidr):
            raise TemporaryIPError("Temporary IP command completed but verification failed")

        return TemporaryIPState(
            interface=interface_name,
            cidr=cidr,
            previous_ipv4=previous_ipv4,
            added_by_app=True,
            active=True,
        )

    async def restore(self, state: TemporaryIPState, sudo_password: str | None = None) -> TemporaryIPState:
        if not state.added_by_app:
            state.active = False
            return state
        snapshot = await NetworkManager(self.ssh).inspect()
        interface = self._find_interface(snapshot, state.interface)
        if interface.protected:
            raise ProtectedInterfaceError(
                f"Interface {interface.name} is now carrying the SSH session. Restore was blocked for safety."
            )
        if not await self._address_exists(state.interface, state.cidr):
            state.active = False
            return state
        result = await self._sudo_ip(
            ["addr", "del", state.cidr, "dev", state.interface],
            sudo_password,
            raise_on_error=False,
        )
        if result.exit_status != 0:
            raise TemporaryIPError(f"Unable to remove temporary IP.\n{result.stderr.strip()}")
        if await self._address_exists(state.interface, state.cidr):
            raise TemporaryIPError("Temporary IP removal verification failed")
        state.active = False
        return state

    @staticmethod
    def _find_interface(snapshot, interface_name: str):
        for interface in snapshot.interfaces:
            if interface.name == interface_name:
                return interface
        raise TemporaryIPError(f"Interface '{interface_name}' does not exist on Jetson")

    @staticmethod
    def _validate_target_interface(interface) -> None:
        if interface.protected:
            raise ProtectedInterfaceError(f"Interface {interface.name} is carrying the SSH session")
        if interface.wireless:
            raise TemporaryIPError(f"Interface {interface.name} is wireless and cannot be used as LiDAR Ethernet interface")
        if not interface.physical:
            raise TemporaryIPError(f"Interface {interface.name} is not a physical network interface")

    async def _address_exists(self, interface_name: str, cidr: str) -> bool:
        snapshot = await NetworkManager(self.ssh).inspect()
        requested = ipaddress.ip_interface(cidr)
        for interface in snapshot.interfaces:
            if interface.name != interface_name:
                continue
            for existing_cidr in interface.ipv4_addresses:
                existing = ipaddress.ip_interface(existing_cidr)
                if existing.ip == requested.ip and existing.network.prefixlen == requested.network.prefixlen:
                    return True
        return False

    async def _sudo_ip(self, args: list[str], sudo_password: str | None, raise_on_error: bool = True):
        command = "ip " + " ".join(shlex.quote(str(arg)) for arg in args)
        result = await self.ssh.run_sudo(command, sudo_password=sudo_password, timeout=10)
        if raise_on_error and result.exit_status != 0:
            message = result.stderr.strip()
            if "password is required" in message.lower():
                message = "sudo password is required"
            raise TemporaryIPError(f"Command failed: {command}\n{message}")
        return result
