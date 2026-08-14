import asyncio
import getpass
import json
from core.remote.ssh_manager import SSHConfig, SSHManager
from devices.livox.profile import load_default_livox_profile
from devices.livox.sdk2_backend import LivoxSDK2Backend


async def main():
    jetson_ip = input("Jetson SSH IP: ").strip()
    username = input("SSH username: ").strip()
    password = getpass.getpass("SSH password (Enter if using key): ")
    model = input("Model [MID360/MID360S]: ").strip().upper()
    profile = load_default_livox_profile()
    ssh = SSHManager(SSHConfig(host=jetson_ip, username=username, password=password or None))
    try:
        await ssh.connect()
        backend = LivoxSDK2Backend(ssh, network_profile=profile)
        print("Environment:", json.dumps(await backend.check_environment(), indent=2))
        print("SDK:", await backend.get_sdk_version())
        device = await backend.discover(
            expected_model=model,
            timeout=8,
        )
        print(json.dumps(device.to_dict(), indent=2))
    finally:
        await ssh.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
