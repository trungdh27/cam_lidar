import asyncio
import getpass

from core.remote.ssh_manager import SSHConfig, SSHManager


async def main():
    host = input("Jetson IP: ").strip()
    username = input("Username: ").strip()

    password = getpass.getpass(
        "SSH password (Enter if using SSH key): "
    )

    config = SSHConfig(
        host=host,
        username=username,
        password=password or None,
    )

    ssh = SSHManager(config)

    try:
        print("\nConnecting...")

        await ssh.connect()

        print("SSH CONNECTED\n")

        info = await ssh.probe_jetson()

        print("Hostname:")
        print(info["hostname"])

        print("\nArchitecture:")
        print(info["architecture"])

        print("\nKernel:")
        print(info["kernel"])

        print("\nNetwork:")
        print(info["network"])

    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
