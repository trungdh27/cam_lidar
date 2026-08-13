import asyncio
import json
import shlex
from threading import RLock

import asyncssh

from core.remote.ssh_manager import SSHConfig, SSHManager
from devices.camera.base_adapter import (
    BaseCameraAdapter,
    CameraAlreadyInUseError,
    CameraNotDetectedError,
    CameraOpenError,
    CameraOperationTimeout,
    CameraSDKUnavailableError,
)
from devices.camera.jetson_zed_probe import JETSON_ZED_PROBE
from devices.camera.profiles import get_camera_profile
from devices.camera.zed_models import model_matches_profile


class RemoteZedAdapter(BaseCameraAdapter):
    """Runs ZED SDK operations on Jetson through the shared SSH manager."""

    MARKER = "CAMERA_ZED_JSON="

    def __init__(self, command_timeout: float = 15.0):
        self.command_timeout = command_timeout
        self._connected_device = None
        self._lock = RLock()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected_device is not None

    def discover(self, payload: dict) -> dict:
        result = self._execute_remote("discover", payload)
        profile = get_camera_profile(payload["profile_id"])
        devices = result.get("devices", [])
        matching = [item for item in devices if self._matches_profile(item, profile.profile_id)]
        if matching:
            devices = matching
        if not devices:
            raise CameraNotDetectedError(f"No {profile.display_name} camera was detected on Jetson.")
        result["devices"] = devices
        result["execution_host"] = self._target_text(payload)
        return result

    def connect(self, payload: dict) -> dict:
        with self._lock:
            if self._connected_device is not None:
                serial = self._connected_device.get("serial_number", "-")
                raise CameraAlreadyInUseError(
                    f"A remote ZED camera is already connected (SN: {serial})."
                )
        if not payload.get("device_id"):
            raise CameraNotDetectedError("Select a discovered Jetson ZED camera before connecting.")
        self._validate_configuration(payload)
        result = self._execute_remote("connect", payload)
        with self._lock:
            self._connected_device = result["device"]
        result["execution_host"] = self._target_text(payload)
        result["access_validated"] = True
        return result

    def disconnect(self, payload: dict | None = None) -> dict:
        with self._lock:
            self._connected_device = None
        return {"disconnected": True, "remote_validation_session": True}

    def _execute_remote(self, action: str, payload: dict) -> dict:
        config = self._ssh_config(payload)
        request = {
            "action": action,
            "profile_id": payload["profile_id"],
            "serial_number": payload.get("device_id"),
            "resolution_key": payload.get("resolution_key"),
            "resolution": payload.get("resolution"),
            "fps": payload.get("fps"),
            "open_timeout": min(self.command_timeout, 10.0),
        }
        try:
            return asyncio.run(self._run(config, request))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise CameraOperationTimeout(
                f"Jetson ZED {action} command timed out after {self.command_timeout:.0f} seconds."
            ) from exc
        except asyncssh.PermissionDenied as exc:
            raise CameraOpenError(
                f"Jetson SSH authentication failed for {config.username}@{config.host}."
            ) from exc
        except (asyncssh.Error, OSError) as exc:
            raise CameraOpenError(f"Jetson {config.host} is unreachable over SSH: {exc}") from exc

    async def _run(self, config: SSHConfig, request: dict) -> dict:
        ssh = SSHManager(config)
        try:
            await asyncio.wait_for(ssh.connect(), timeout=self.command_timeout)
            command = "python3 -c " + shlex.quote(JETSON_ZED_PROBE) + " " + shlex.quote(
                json.dumps(request, separators=(",", ":"))
            )
            result = await ssh.run(command, timeout=self.command_timeout)
            payload = self._parse_payload(result.stdout)
            if not payload.get("ok"):
                self._raise_remote_error(payload)
            return payload
        finally:
            await ssh.disconnect()

    @classmethod
    def _parse_payload(cls, output: str) -> dict:
        for line in reversed(output.splitlines()):
            if line.startswith(cls.MARKER):
                try:
                    return json.loads(line[len(cls.MARKER):])
                except json.JSONDecodeError as exc:
                    raise CameraOpenError("Jetson returned invalid ZED discovery JSON.") from exc
        raise CameraOpenError("Jetson ZED probe returned no structured result.")

    @staticmethod
    def _raise_remote_error(payload):
        message = payload.get("error") or "Unknown remote ZED error"
        error_type = payload.get("error_type")
        if error_type == "sdk_unavailable":
            raise CameraSDKUnavailableError(message)
        if error_type == "not_detected":
            raise CameraNotDetectedError(message)
        if error_type == "already_in_use":
            raise CameraAlreadyInUseError(message)
        if error_type == "timeout":
            raise CameraOperationTimeout(message)
        raise CameraOpenError(message)

    @staticmethod
    def _ssh_config(payload):
        config = payload.get("ssh_config")
        if not isinstance(config, SSHConfig) or not config.host or not config.username:
            raise CameraOpenError("Jetson SSH configuration is required.")
        return config

    @staticmethod
    def _target_text(payload):
        config = payload["ssh_config"]
        return f"{config.username}@{config.host}:{config.port}"

    @staticmethod
    def _validate_configuration(payload):
        profile = get_camera_profile(payload["profile_id"])
        resolution_key = payload.get("resolution_key")
        stream = next(
            (item for item in profile.stream_profiles if item.key == resolution_key),
            None,
        )
        if stream is None:
            raise CameraOpenError(
                f"Unsupported resolution '{resolution_key}' for {profile.display_name}."
            )
        try:
            fps = int(payload.get("fps"))
        except (TypeError, ValueError) as exc:
            raise CameraOpenError(f"Unsupported FPS: {payload.get('fps')}") from exc
        if fps not in stream.fps:
            raise CameraOpenError(
                f"Unsupported configuration: {stream.display_name} @ {fps} FPS. "
                f"Allowed FPS: {list(stream.fps)}"
            )

    @staticmethod
    def _matches_profile(device, profile_id):
        return model_matches_profile(device.get("model", ""), profile_id)
