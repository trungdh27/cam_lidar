import asyncio
import getpass

from core.network.network_manager import NetworkManager
from core.network.temporary_ip_manager import TemporaryIPManager
from core.remote.ssh_manager import SSHConfig, SSHManager


async def main():
    host = input("Jetson SSH IP: ").strip()
    username = input("Username: ").strip()
    ssh_password = getpass.getpass("SSH password (Enter if using key): ")
    sudo_password = getpass.getpass("sudo password (Enter for NOPASSWD): ")

    ssh = SSHManager(
        SSHConfig(
            host=host,
            username=username,
            password=ssh_password or None,
        )
    )

    try:
        await ssh.connect()
        snapshot = await NetworkManager(ssh).inspect()
        print("Management:", snapshot.management.interface)
        print("Candidate :", snapshot.lidar_candidate)

        if not snapshot.lidar_candidate:
            return

        interface = input(f"Interface [{snapshot.lidar_candidate}]: ").strip() or snapshot.lidar_candidate
        ip_address = input("Temporary Jetson LiDAR IP: ").strip()
        prefix = int(input("Prefix [24]: ").strip() or "24")

        manager = TemporaryIPManager(ssh)
        state = await manager.apply(
            interface_name=interface,
            ip_address=ip_address,
            prefix=prefix,
            sudo_password=sudo_password or None,
        )
        print("ACTIVE:", state.to_dict())

        input("Press Enter to RESTORE...")
        state = await manager.restore(
            state,
            sudo_password=sudo_password or None,
        )
        print("RESTORED:", state.to_dict())
    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
