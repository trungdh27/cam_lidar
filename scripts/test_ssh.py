import asyncio
import getpass
from core.remote.ssh_manager import SSHConfig, SSHManager


async def main():
    host = input("Jetson IP: ").strip()
    username = input("Username: ").strip()
    password = getpass.getpass("SSH password (Enter if using SSH key): ")
    ssh = SSHManager(SSHConfig(host=host, username=username, password=password or None))
    try:
        await ssh.connect()
        print("SSH CONNECTED")
        info = await ssh.probe_jetson()
        print("Hostname     :", info["hostname"])
        print("Architecture :", info["architecture"])
        print("Kernel       :", info["kernel"])
        print(info["network"])
    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
