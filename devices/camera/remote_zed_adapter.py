import asyncio
import json
import shlex
from threading import RLock

import asyncssh

from devices.camera.base_adapter import (
    BaseCameraAdapter,
    CameraAlreadyInUseError,
    CameraNotDetectedError,
    CameraOpenError,
    CameraOperationTimeout,
    CameraSDKUnavailableError,
)
from devices.camera.jetson_zed_probe import JETSON_ZED_PROBE
from devices.camera.jetson_zed_stream import JETSON_ZED_STREAM_MANAGER
from devices.camera.profiles import get_camera_profile
from devices.camera.preview_config import (
    H264_BITRATE_KBPS, H264_GOP_FRAMES, H264_PREVIEW_PORT, H264_PREVIEW_TARGET_FPS,
    PREVIEW_HEIGHT, PREVIEW_JPEG_QUALITY, PREVIEW_MODE, PREVIEW_PORT,
    PREVIEW_TARGET_FPS, PREVIEW_WIDTH,
)
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
        raise CameraOpenError(
            "Remote ZED discovery requires the shared Jetson connection."
        )

    def connect(self, payload: dict) -> dict:
        raise CameraOpenError(
            "Remote ZED connection requires the shared Jetson connection."
        )

    async def execute_with_ssh(self, ssh, action: str, payload: dict) -> dict:
        if action == "disconnect":
            return self.disconnect(payload)
        if action == "connect":
            return await self._connect_with_ssh(ssh, payload)
        if action == "discover":
            return await self._discover_with_ssh(ssh, payload)
        if action in ("start_stream", "stream_status", "stop_stream", "preview_fallback"):
            return await self._stream_with_ssh(ssh, action, payload)
        raise CameraOpenError(f"Remote ZED operation '{action}' is not supported.")

    async def _stream_with_ssh(self, ssh, action: str, payload: dict) -> dict:
        if action == "start_stream":
            if not self.connected:
                raise CameraOpenError("Connect and validate the camera before starting a stream.")
            self._validate_configuration(payload)
        requested_mode = payload.get("preview_mode", PREVIEW_MODE).upper()
        host_gstreamer = bool(payload.get("host_gstreamer_available"))
        preview_backend = (
            "off" if requested_mode == "OFF" else
            "gstreamer_h264"
            if requested_mode == "GSTREAMER_H264" or (requested_mode == "AUTO" and host_gstreamer)
            else "jpeg_tcp"
        )
        request = {
            "action": {"start_stream": "start", "stream_status": "status", "stop_stream": "stop", "preview_fallback": "preview_fallback"}[action],
            "profile_id": payload["profile_id"],
            "serial_number": payload.get("device_id"),
            "resolution_key": payload.get("resolution_key"),
            "resolution": payload.get("resolution"),
            "fps": payload.get("fps"),
            "pixel_format": payload.get("pixel_format"),
            "startup_timeout": 10,
            "stop_timeout": 5,
            "preview_bind": "0.0.0.0",
            "preview_port": PREVIEW_PORT,
            "preview_width": PREVIEW_WIDTH,
            "preview_height": PREVIEW_HEIGHT,
            "preview_fps": H264_PREVIEW_TARGET_FPS if preview_backend == "gstreamer_h264" else PREVIEW_TARGET_FPS,
            "jpeg_preview_fps": PREVIEW_TARGET_FPS,
            "preview_quality": PREVIEW_JPEG_QUALITY,
            "preview_mode": requested_mode,
            "preview_backend": preview_backend,
            "preview_host": ssh.local_address,
            "h264_preview_port": H264_PREVIEW_PORT,
            "h264_bitrate_kbps": H264_BITRATE_KBPS,
            "h264_gop_frames": H264_GOP_FRAMES,
            "automation_validation": bool(payload.get("automation_validation")),
        }
        return await self._execute_script(
            ssh, JETSON_ZED_STREAM_MANAGER, request, "CAMERA_STREAM_JSON=", 15.0
        )

    async def _discover_with_ssh(self, ssh, payload: dict) -> dict:
        result = await self._execute_remote(ssh, "discover", payload)
        profile = get_camera_profile(payload["profile_id"])
        devices = result.get("devices", [])
        matching = [item for item in devices if self._matches_profile(item, profile.profile_id)]
        if matching:
            devices = matching
        if not devices:
            raise CameraNotDetectedError(f"No {profile.display_name} camera was detected on Jetson.")
        result["devices"] = devices
        result["execution_host"] = self._target_text(ssh)
        return result

    async def _connect_with_ssh(self, ssh, payload: dict) -> dict:
        with self._lock:
            if self._connected_device is not None:
                serial = self._connected_device.get("serial_number", "-")
                raise CameraAlreadyInUseError(
                    f"A remote ZED camera is already connected (SN: {serial})."
                )
        if not payload.get("device_id"):
            raise CameraNotDetectedError("Select a discovered Jetson ZED camera before connecting.")
        self._validate_configuration(payload)
        result = await self._execute_remote(ssh, "connect", payload)
        with self._lock:
            self._connected_device = result["device"]
        result["execution_host"] = self._target_text(ssh)
        result["access_validated"] = True
        return result

    def disconnect(self, payload: dict | None = None) -> dict:
        with self._lock:
            self._connected_device = None
        return {"disconnected": True, "remote_validation_session": True}

    async def _execute_remote(self, ssh, action: str, payload: dict) -> dict:
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
            return await self._execute_script(
                ssh, JETSON_ZED_PROBE, request, self.MARKER, self.command_timeout
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise CameraOperationTimeout(
                f"Jetson ZED {action} command timed out after {self.command_timeout:.0f} seconds."
            ) from exc
        except asyncssh.PermissionDenied as exc:
            config = ssh.config
            raise CameraOpenError(
                f"Jetson SSH authentication failed for "
                f"{config.username}@{config.host}."
            ) from exc
        except (asyncssh.Error, OSError) as exc:
            raise CameraOpenError(
                f"Jetson {ssh.config.host} remote operation failed: {exc}"
            ) from exc

    async def _execute_script(self, ssh, script, request, marker, timeout):
        try:
            command = "python3 -c " + shlex.quote(script) + " " + shlex.quote(
                json.dumps(request, separators=(",", ":"))
            )
            result = await ssh.run(command, timeout=timeout)
            response = self._parse_payload(result.stdout, marker)
            if not response.get("ok"):
                self._raise_remote_error(response)
            return response
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise CameraOperationTimeout(
                f"Jetson camera command timed out after {timeout:.0f} seconds."
            ) from exc
        except (asyncssh.Error, OSError) as exc:
            raise CameraOpenError(f"Jetson remote camera operation failed: {exc}") from exc

    @classmethod
    def _parse_payload(cls, output: str, marker: str | None = None) -> dict:
        marker = marker or cls.MARKER
        for line in reversed(output.splitlines()):
            if line.startswith(marker):
                try:
                    return json.loads(line[len(marker):])
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
    def _target_text(ssh):
        config = ssh.config
        return f"{config.username}@{config.host}:{config.port}"

    @staticmethod
    def _validate_configuration(payload):
        profile = get_camera_profile(payload["profile_id"])
        resolution_key = payload.get("resolution_key")
        if resolution_key in (None, ""):
            raise CameraOpenError("Missing required parameter: resolution_key.")
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
