import asyncio
import getpass
from core.network.network_manager import NetworkManager
from core.remote.ssh_manager import SSHConfig, SSHManager


async def main():
    host = input("Jetson IP: ").strip()
    username = input("Username: ").strip()
    password = getpass.getpass("SSH password (Enter if using SSH key): ")
    ssh = SSHManager(SSHConfig(host=host, username=username, password=password or None))
    try:
        await ssh.connect()
        snapshot = await NetworkManager(ssh).inspect()
        print("Host IP        :", snapshot.management.client_ip)
        print("Jetson SSH IP  :", snapshot.management.server_ip)
        print("SSH Interface  :", snapshot.management.interface)
        print("LiDAR candidate:", snapshot.lidar_candidate)
        for interface in snapshot.interfaces:
            print(interface.to_dict())
    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
