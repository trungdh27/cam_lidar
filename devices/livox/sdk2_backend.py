from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from typing import Any

from core.remote.ssh_manager import SSHManager
from devices.livox.models import LivoxDeviceInfo, LivoxDiscoveryStatus
from devices.livox.profile import (
    LivoxNetworkProfile,
    load_default_livox_profile,
)


class LivoxSDK2Error(RuntimeError):
    pass


class LivoxSDK2EnvironmentError(LivoxSDK2Error):
    pass


class LivoxSDK2InitializationError(LivoxSDK2Error):
    pass


class LivoxSDK2Backend:
    HELPER = "$HOME/.cam_lidar/bin/livox_discover"
    HELPER_TEST_COMMAND = 'test -x "$HOME/.cam_lidar/bin/livox_discover"'
    PAYLOAD_MARKER = "LIVOX_DISCOVERY_JSON="
    MISSING_HELPER_MESSAGE = (
        "Livox discovery helper is missing on Jetson.\n"
        "Deploy it with:\n"
        "./scripts/deploy_livox_helper.sh <user>@<jetson-ip>"
    )

    def __init__(
        self,
        ssh: SSHManager,
        network_profile: LivoxNetworkProfile | None = None,
    ):
        self.ssh = ssh
        self.network_profile = network_profile or load_default_livox_profile()

    async def check_environment(self) -> dict[str, Any]:
        helper_installed = await self._helper_is_installed()
        sdk_version = None
        if helper_installed:
            sdk_version = await self._read_sdk_version()
        return {
            "helper_installed": helper_installed,
            "helper_path": self.HELPER,
            "sdk_version": sdk_version,
            "host_ip": str(self.network_profile.jetson.ip),
            "expected_lidar_ip": str(self.network_profile.lidar.ip),
        }

    async def check_helper(self) -> None:
        if not await self._helper_is_installed():
            raise LivoxSDK2EnvironmentError(self.MISSING_HELPER_MESSAGE)

    async def get_sdk_version(self) -> str:
        await self.check_helper()
        return await self._read_sdk_version()

    async def discover(
        self,
        expected_model: str,
        timeout: int = 8,
    ) -> LivoxDeviceInfo:
        await self.check_helper()

        normalized_model = expected_model.strip().upper()
        model_profile = self.network_profile.model(normalized_model)
        if timeout < 1 or timeout > 60:
            raise LivoxSDK2Error("Invalid discovery timeout")

        host_ip = str(self.network_profile.jetson.ip)
        expected_lidar_ip = str(self.network_profile.lidar.ip)
        command = (
            f"{self.HELPER} --host-ip {shlex.quote(host_ip)} "
            f"--expected-lidar-ip {shlex.quote(expected_lidar_ip)} "
            f"--model {shlex.quote(normalized_model)} --timeout {int(timeout)}"
        )
        result = await self.ssh.run(command, timeout=float(timeout + 5))
        evidence = self._evidence(
            result=result,
            request={
                "host_ip": host_ip,
                "expected_lidar_ip": expected_lidar_ip,
                "model": normalized_model,
                "timeout": int(timeout),
            },
        )

        try:
            payload = self._parse_payload(result.stdout)
        except LivoxSDK2Error as exc:
            raise LivoxSDK2Error(
                "Livox discovery helper did not return a valid machine-readable "
                f"result (exit code {result.exit_status})."
            ) from exc
        evidence["payload"] = payload

        if result.exit_status == 2:
            return self._not_found_result(
                payload=payload,
                evidence=evidence,
                normalized_model=normalized_model,
                sdk_profile=model_profile.sdk_profile,
            )
        if result.exit_status == 3:
            detail = payload.get("error") or "LivoxLidarSdkInit failed"
            raise LivoxSDK2InitializationError(
                f"Livox SDK2 initialization failed: {detail}"
            )
        if result.exit_status != 0:
            detail = payload.get("error") or "unknown helper error"
            raise LivoxSDK2Error(
                f"Livox discovery helper failed with exit code "
                f"{result.exit_status}: {detail}"
            )
        if not payload.get("ok", False):
            detail = payload.get("error") or "unknown helper error"
            raise LivoxSDK2Error(f"Livox discovery helper returned an error: {detail}")
        if not payload.get("found", False):
            return self._device_info(
                status=LivoxDiscoveryStatus.DEVICE_NOT_FOUND,
                payload=payload,
                evidence=evidence,
                sdk_profile=model_profile.sdk_profile,
                reason=payload.get("reason") or "device_not_found",
            )

        detected_lidar_ip = self._optional_text(payload.get("lidar_ip"))
        detected_model = self._optional_text(payload.get("model"))
        detected_serial = self._optional_text(payload.get("serial"))
        status = LivoxDiscoveryStatus.FOUND
        reason = None
        if detected_lidar_ip != expected_lidar_ip:
            status = LivoxDiscoveryStatus.IP_MISMATCH
            reason = "detected_lidar_ip_does_not_match_profile"
        elif detected_model != normalized_model:
            status = LivoxDiscoveryStatus.MODEL_MISMATCH
            reason = "detected_model_does_not_match_requested_model"
        elif self._strict_serial_mismatch(detected_serial):
            status = LivoxDiscoveryStatus.SERIAL_MISMATCH
            reason = "detected_serial_does_not_match_profile"

        return self._device_info(
            status=status,
            payload=payload,
            evidence=evidence,
            sdk_profile=model_profile.sdk_profile,
            reason=reason,
        )

    async def _helper_is_installed(self) -> bool:
        result = await self.ssh.run(self.HELPER_TEST_COMMAND, timeout=5)
        return result.exit_status == 0

    async def _read_sdk_version(self) -> str:
        result = await self.ssh.run(f"{self.HELPER} --version", timeout=5)
        try:
            payload = self._parse_payload(result.stdout)
        except LivoxSDK2Error as exc:
            raise LivoxSDK2EnvironmentError(
                "Unable to read Livox SDK2 version from the discovery helper"
            ) from exc
        version = self._optional_text(payload.get("sdk_version"))
        if result.exit_status != 0 or not payload.get("ok", False) or not version:
            detail = payload.get("error") or "missing sdk_version"
            raise LivoxSDK2EnvironmentError(
                f"Unable to read Livox SDK2 version: {detail}"
            )
        return version

    def _not_found_result(
        self,
        payload: dict[str, Any],
        evidence: dict[str, Any],
        normalized_model: str,
        sdk_profile: str,
    ) -> LivoxDeviceInfo:
        detected_ip = self._optional_text(payload.get("detected_lidar_ip"))
        detected_model = self._optional_text(payload.get("detected_model"))
        if detected_ip and detected_ip != str(self.network_profile.lidar.ip):
            status = LivoxDiscoveryStatus.IP_MISMATCH
        elif detected_model and detected_model != normalized_model:
            status = LivoxDiscoveryStatus.MODEL_MISMATCH
        else:
            status = LivoxDiscoveryStatus.DEVICE_NOT_FOUND

        detected_payload = {
            **payload,
            "model": detected_model,
            "dev_type": payload.get("detected_dev_type"),
            "serial": self._optional_text(payload.get("detected_serial")),
            "lidar_ip": detected_ip,
            "handle": payload.get("detected_handle"),
        }
        return self._device_info(
            status=status,
            payload=detected_payload,
            evidence=evidence,
            sdk_profile=sdk_profile,
            reason=payload.get("reason") or "device_not_found",
        )

    def _device_info(
        self,
        status: LivoxDiscoveryStatus,
        payload: dict[str, Any],
        evidence: dict[str, Any],
        sdk_profile: str,
        reason: str | None,
    ) -> LivoxDeviceInfo:
        serial = self._optional_text(payload.get("serial"))
        expected_serial = self.network_profile.lidar.expected_serial
        serial_match = serial == expected_serial if serial else None
        return LivoxDeviceInfo(
            found=status is LivoxDiscoveryStatus.FOUND,
            status=status,
            model=self._optional_text(payload.get("model")),
            serial=serial,
            lidar_ip=self._optional_text(payload.get("lidar_ip")),
            dev_type=payload.get("dev_type"),
            handle=payload.get("handle"),
            sdk_version=self._optional_text(payload.get("sdk_version")),
            host_ip=self._optional_text(payload.get("host_ip"))
            or str(self.network_profile.jetson.ip),
            profile=self._optional_text(payload.get("profile")) or sdk_profile,
            expected_lidar_ip=str(self.network_profile.lidar.ip),
            expected_serial=expected_serial,
            serial_match=serial_match,
            strict_serial_verification=(
                self.network_profile.lidar.strict_serial_verification
            ),
            reason=reason,
            exit_code=evidence["exit_code"],
            raw_result=evidence,
            raw_output=evidence["stdout"],
        )

    def _strict_serial_mismatch(self, detected_serial: str | None) -> bool:
        return (
            self.network_profile.lidar.strict_serial_verification
            and detected_serial != self.network_profile.lidar.expected_serial
        )

    def _evidence(self, result, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "helper_path": self.HELPER,
            "request": request,
            "exit_code": result.exit_status,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @classmethod
    def _parse_payload(cls, output: str) -> dict[str, Any]:
        marked_lines = [
            line.strip()[len(cls.PAYLOAD_MARKER) :]
            for line in output.splitlines()
            if line.strip().startswith(cls.PAYLOAD_MARKER)
        ]
        if not marked_lines:
            raise LivoxSDK2Error("Livox helper did not return a discovery payload")
        if len(marked_lines) != 1:
            raise LivoxSDK2Error("Livox helper returned multiple discovery payloads")
        try:
            payload = json.loads(marked_lines[0])
        except json.JSONDecodeError as exc:
            raise LivoxSDK2Error("Invalid JSON returned by Livox helper") from exc
        if not isinstance(payload, dict):
            raise LivoxSDK2Error("Livox helper payload must be a JSON object")
        return payload

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
