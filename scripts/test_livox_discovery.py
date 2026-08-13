import asyncio
import getpass
import json
from core.remote.ssh_manager import SSHConfig, SSHManager
from devices.livox.sdk2_backend import LivoxSDK2Backend


async def main():
    jetson_ip = input("Jetson SSH IP: ").strip()
    username = input("SSH username: ").strip()
    password = getpass.getpass("SSH password (Enter if using key): ")
    host_ip = input("Jetson LiDAR-side IP: ").strip()
    model = input("Model [MID360/MID360S]: ").strip().upper()
    ssh = SSHManager(SSHConfig(host=jetson_ip, username=username, password=password or None))
    try:
        await ssh.connect()
        backend = LivoxSDK2Backend(ssh)
        print("SDK:", await backend.get_sdk_version())
        device = await backend.discover(host_ip=host_ip, expected_model=model, timeout=8)
        print(json.dumps(device.to_dict(), indent=2))
    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
