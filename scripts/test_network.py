import asyncio
import getpass

from core.network.network_manager import NetworkManager
from core.remote.ssh_manager import (
    SSHConfig,
    SSHManager,
)


async def main():
    host = input("Jetson IP: ").strip()
    username = input("Username: ").strip()

    password = getpass.getpass(
        "Password (Enter if using SSH key): "
    )

    ssh = SSHManager(
        SSHConfig(
            host=host,
            username=username,
            password=password or None,
        )
    )

    try:
        print("\nConnecting Jetson...")

        await ssh.connect()

        print("SSH CONNECTED")

        network = NetworkManager(ssh)

        snapshot = await network.inspect()

        print("\n================================")
        print("SSH MANAGEMENT")
        print("================================")

        print(
            "Host Ubuntu IP :",
            snapshot.management.client_ip,
        )

        print(
            "Jetson SSH IP  :",
            snapshot.management.server_ip,
        )

        print(
            "SSH Interface  :",
            snapshot.management.interface,
        )

        print(
            "Source IP      :",
            snapshot.management.source_ip,
        )

        print("\n================================")
        print("JETSON INTERFACES")
        print("================================")

        for interface in snapshot.interfaces:

            print()
            print(f"Interface : {interface.name}")
            print(
                f"IPv4      : "
                f"{', '.join(interface.ipv4_addresses) or '-'}"
            )

            print(
                f"State     : {interface.operstate}"
            )

            print(
                f"Carrier   : {interface.carrier}"
            )

            print(
                f"Physical  : {interface.physical}"
            )

            print(
                f"Wireless  : {interface.wireless}"
            )

            print(
                f"Role      : {interface.role.value}"
            )

            print(
                f"Protected : {interface.protected}"
            )

            print(
                f"Reason    : {interface.reason}"
            )

        print("\n================================")

        print(
            "LiDAR candidate:",
            snapshot.lidar_candidate or "NOT FOUND",
        )

    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
