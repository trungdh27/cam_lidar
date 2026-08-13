import ipaddress
import json
import shlex

from core.remote.ssh_manager import SSHManager
from devices.livox.models import LivoxDeviceInfo


class LivoxSDK2Error(RuntimeError):
    pass


class LivoxSDK2Backend:
    HELPER = "$HOME/.cam_lidar/bin/livox_discover"
    MODEL_PROFILE = {"MID360": "mid360", "MID360S": "mid360s"}

    def __init__(self, ssh: SSHManager):
        self.ssh = ssh

    async def check_helper(self) -> None:
        result = await self.ssh.run('test -x "$HOME/.cam_lidar/bin/livox_discover"', timeout=5)
        if result.exit_status != 0:
            raise LivoxSDK2Error(
                "Livox discovery helper is not installed on Jetson. Run scripts/deploy_livox_helper.sh from the Host."
            )

    async def get_sdk_version(self) -> str:
        await self.check_helper()
        result = await self.ssh.run(f"{self.HELPER} --version", timeout=5)
        payload = self._parse_payload(result.stdout)
        version = payload.get("sdk_version")
        if not version:
            raise LivoxSDK2Error("Unable to read Livox SDK2 version")
        return version

    async def discover(self, host_ip: str, expected_model: str, timeout: int = 8) -> LivoxDeviceInfo:
        await self.check_helper()
        try:
            ip = ipaddress.ip_address(host_ip)
        except ValueError as exc:
            raise LivoxSDK2Error(f"Invalid Jetson LiDAR IP: {host_ip}") from exc
        if ip.version != 4:
            raise LivoxSDK2Error("Only IPv4 is supported")

        normalized_model = expected_model.strip().upper()
        profile = self.MODEL_PROFILE.get(normalized_model)
        if profile is None:
            raise LivoxSDK2Error(f"Unsupported Livox model: {expected_model}")
        if timeout < 1 or timeout > 60:
            raise LivoxSDK2Error("Invalid discovery timeout")

        command = f"{self.HELPER} {shlex.quote(host_ip)} {shlex.quote(profile)} {int(timeout)}"
        result = await self.ssh.run(command, timeout=float(timeout + 5))

        try:
            payload = self._parse_payload(result.stdout)
        except LivoxSDK2Error as exc:
            raise LivoxSDK2Error(
                f"Livox discovery helper failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            ) from exc

        if not payload.get("ok", False):
            raise LivoxSDK2Error("Livox discovery helper returned an error")

        return LivoxDeviceInfo(
            found=payload.get("found", False),
            model=payload.get("model"),
            serial=payload.get("serial"),
            lidar_ip=payload.get("lidar_ip"),
            dev_type=payload.get("dev_type"),
            handle=payload.get("handle"),
            sdk_version=payload.get("sdk_version"),
            host_ip=payload.get("host_ip", host_ip),
            profile=payload.get("profile", profile),
            raw_output=result.stdout,
        )

    @staticmethod
    def _parse_payload(output: str) -> dict:
        marker = "LIVOX_DISCOVERY_JSON="
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line.startswith(marker):
                continue
            try:
                return json.loads(line[len(marker):])
            except json.JSONDecodeError as exc:
                raise LivoxSDK2Error("Invalid JSON returned by Livox helper") from exc
        raise LivoxSDK2Error("Livox helper did not return a discovery payload")
