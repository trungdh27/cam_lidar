import importlib
import time
from threading import RLock

from devices.camera.base_adapter import (
    BaseCameraAdapter,
    CameraAlreadyInUseError,
    CameraNotDetectedError,
    CameraOpenError,
    CameraOperationTimeout,
    CameraSDKUnavailableError,
)
from devices.camera.profiles import get_camera_profile
from devices.camera.zed_models import (
    is_camera_one_profile,
    model_matches_profile,
    normalize_zed_model,
    zed_model_family,
)


class ZedAdapter(BaseCameraAdapter):
    """Stereolabs ZED SDK adapter. Importing this module does not require pyzed."""

    def __init__(self, sdk_module=None, open_timeout_seconds: float = 10.0):
        self._sdk_module = sdk_module
        self.open_timeout_seconds = open_timeout_seconds
        self._camera = None
        self._connected_device = None
        self._lock = RLock()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._camera is not None

    def _sdk(self):
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            self._sdk_module = importlib.import_module("pyzed.sl")
        except (ImportError, OSError) as exc:
            raise CameraSDKUnavailableError(
                "ZED SDK Python API (pyzed.sl) is not installed or cannot be loaded."
            ) from exc
        return self._sdk_module

    def discover(self, payload: dict) -> dict:
        self._ensure_local_execution(payload)
        sl = self._sdk()
        started = time.monotonic()
        try:
            camera_class, _ = self._api_classes(sl, payload["profile_id"])
            properties = camera_class.get_device_list()
        except Exception as exc:
            self._raise_sdk_error("ZED discovery failed", exc)

        if time.monotonic() - started > self.open_timeout_seconds:
            raise CameraOperationTimeout("ZED discovery timed out.")

        devices = [self._device_payload(item, sl) for item in properties]
        requested_profile = get_camera_profile(payload["profile_id"])
        matching = [item for item in devices if self._matches_profile(item, requested_profile.profile_id)]
        if matching:
            devices = matching
        if not devices:
            raise CameraNotDetectedError(
                f"No {requested_profile.display_name} camera was detected."
            )
        return {
            "devices": devices,
            "sdk_available": True,
            "sdk_version": self._sdk_version(sl),
        }

    def connect(self, payload: dict) -> dict:
        self._ensure_local_execution(payload)
        with self._lock:
            if self._camera is not None:
                serial = self._connected_device.get("serial_number", "-")
                raise CameraAlreadyInUseError(f"A ZED camera is already open (SN: {serial}).")

            serial_raw = payload.get("device_id")
            if serial_raw in (None, ""):
                raise CameraNotDetectedError("Select a discovered ZED camera before connecting.")
            try:
                serial = int(serial_raw)
            except (TypeError, ValueError) as exc:
                raise CameraOpenError(f"Invalid ZED serial number: {serial_raw}") from exc

            sl = self._sdk()
            camera_class, init_class = self._api_classes(sl, payload["profile_id"])
            camera = camera_class()
            init = init_class()
            self._select_serial(init, serial)
            resolution_key, fps = self._validate_configuration(payload)
            self._apply_configuration(sl, init, resolution_key, fps)
            if hasattr(init, "open_timeout_sec"):
                init.open_timeout_sec = float(self.open_timeout_seconds)

            started = time.monotonic()
            try:
                status = camera.open(init)
            except Exception as exc:
                self._close_quietly(camera)
                self._raise_sdk_error("Unable to open ZED camera", exc)
            elapsed = time.monotonic() - started
            if elapsed > self.open_timeout_seconds + 1.0:
                self._close_quietly(camera)
                raise CameraOperationTimeout("Opening the ZED camera timed out.")
            if status != sl.ERROR_CODE.SUCCESS:
                self._close_quietly(camera)
                self._raise_open_status(status)

            try:
                info = camera.get_camera_information()
                result = self._connected_payload(info, serial, sl)
            except Exception as exc:
                self._close_quietly(camera)
                raise CameraOpenError(f"ZED camera opened but device information failed: {exc}") from exc

            self._camera = camera
            self._connected_device = result
            stream = self._stream_profile(payload["profile_id"], resolution_key)
            return {
                "device": result,
                "sdk_available": True,
                "sdk_version": self._sdk_version(sl),
                "resolution": stream.display_name,
                "resolution_key": resolution_key,
                "fps": fps,
            }

    def disconnect(self, payload: dict | None = None) -> dict:
        with self._lock:
            camera = self._camera
            self._camera = None
            self._connected_device = None
        if camera is not None:
            try:
                camera.close()
            except Exception as exc:
                raise CameraOpenError(f"ZED camera close failed: {exc}") from exc
        return {"disconnected": True}

    @staticmethod
    def _ensure_local_execution(payload):
        host = payload.get("execution_host", "Local Host")
        if host != "Local Host":
            raise CameraSDKUnavailableError(
                "Remote Jetson ZED operations require a camera agent and are not available in Phase 2. "
                "Run this application on the Jetson or select Local Host."
            )

    @staticmethod
    def _api_classes(sl, profile_id):
        if is_camera_one_profile(profile_id):
            camera_class = getattr(sl, "CameraOne", None)
            init_class = getattr(sl, "InitParametersOne", None)
            if camera_class is None or init_class is None:
                raise CameraSDKUnavailableError(
                    "Installed ZED SDK Python API does not expose CameraOne/InitParametersOne."
                )
            return camera_class, init_class
        return sl.Camera, sl.InitParameters

    @staticmethod
    def _select_serial(init, serial):
        if hasattr(init, "set_from_serial_number"):
            init.set_from_serial_number(serial)
        elif hasattr(init, "input") and hasattr(init.input, "set_from_serial_number"):
            init.input.set_from_serial_number(serial)
        else:
            raise CameraSDKUnavailableError("Installed ZED SDK cannot select a camera by serial number.")

    @staticmethod
    def _stream_profile(profile_id, resolution_key):
        for stream in get_camera_profile(profile_id).stream_profiles:
            if stream.key == resolution_key:
                return stream
        raise CameraOpenError(f"Unsupported resolution '{resolution_key}' for {profile_id}.")

    @classmethod
    def _validate_configuration(cls, payload):
        resolution_key = str(payload.get("resolution_key") or "")
        stream = cls._stream_profile(payload["profile_id"], resolution_key)
        try:
            fps = int(payload.get("fps"))
        except (TypeError, ValueError) as exc:
            raise CameraOpenError(f"Unsupported FPS: {payload.get('fps')}") from exc
        if fps not in stream.fps:
            raise CameraOpenError(
                f"Unsupported configuration: {stream.display_name} @ {fps} FPS. "
                f"Allowed FPS: {list(stream.fps)}"
            )
        return resolution_key, fps

    @staticmethod
    def _apply_configuration(sl, init, resolution_key, fps):
        resolution_enum = None
        checked = []
        for enum_name in ("RESOLUTION", "RESOLUTION_ONE"):
            enum_class = getattr(sl, enum_name, None)
            if enum_class is None:
                continue
            checked.append(enum_name)
            resolution_enum = getattr(enum_class, resolution_key, None)
            if resolution_enum is not None:
                break
        if resolution_enum is None:
            raise CameraSDKUnavailableError(
                f"Installed ZED SDK does not expose resolution {resolution_key} "
                f"in {checked or ['RESOLUTION', 'RESOLUTION_ONE']}."
            )
        if not hasattr(init, "camera_resolution") or not hasattr(init, "camera_fps"):
            raise CameraSDKUnavailableError(
                "Installed ZED initialization API cannot set camera_resolution/camera_fps."
            )
        init.camera_resolution = resolution_enum
        init.camera_fps = fps

    def _device_payload(self, properties, sl):
        serial = str(getattr(properties, "serial_number", "-"))
        model = self._enum_text(getattr(properties, "camera_model", "Unknown ZED"))
        path = str(getattr(properties, "path", "-"))
        input_type = self._enum_text(getattr(properties, "input_type", "GMSL2"))
        return {
            "model": model,
            "normalized_model": normalize_zed_model(model),
            "model_family": zed_model_family(model),
            "serial_number": serial,
            "firmware": "-",
            "interface": input_type or "GMSL2",
            "device_path": path,
            "sdk_driver": f"ZED SDK {self._sdk_version(sl)}",
        }

    def _connected_payload(self, info, fallback_serial, sl):
        config = getattr(info, "camera_configuration", None)
        firmware = getattr(config, "firmware_version", "-") if config is not None else "-"
        model = self._enum_text(getattr(info, "camera_model", "Unknown ZED"))
        serial = str(getattr(info, "serial_number", fallback_serial))
        discovered = self._find_discovered_device(sl, serial)
        return {
            "model": model,
            "serial_number": serial,
            "firmware": str(firmware),
            "interface": discovered.get("interface", "GMSL2"),
            "device_path": discovered.get("device_path", "-"),
            "sdk_driver": f"ZED SDK {self._sdk_version(sl)}",
        }

    def _find_discovered_device(self, sl, serial):
        try:
            camera_class = type(self._camera) if self._camera is not None else sl.Camera
            for properties in camera_class.get_device_list():
                item = self._device_payload(properties, sl)
                if item["serial_number"] == serial:
                    return item
        except Exception:
            pass
        return {}

    @staticmethod
    def _matches_profile(device, profile_id):
        return model_matches_profile(device.get("model", ""), profile_id)

    @staticmethod
    def _sdk_version(sl):
        try:
            return str(sl.Camera.get_sdk_version())
        except Exception:
            return "Unknown"

    @staticmethod
    def _enum_text(value):
        text = getattr(value, "name", None) or str(value)
        return text.replace("CAMERA_MODEL.", "").replace("INPUT_TYPE.", "").replace("_", " ")

    @staticmethod
    def _close_quietly(camera):
        try:
            camera.close()
        except Exception:
            pass

    @staticmethod
    def _raise_sdk_error(prefix, exc):
        message = str(exc)
        upper = message.upper()
        if "TIMEOUT" in upper:
            raise CameraOperationTimeout(f"{prefix}: {message}") from exc
        if "NOT DETECTED" in upper or "NO CAMERA" in upper:
            raise CameraNotDetectedError(f"{prefix}: {message}") from exc
        if "BUSY" in upper or "ALREADY" in upper or "IN USE" in upper:
            raise CameraAlreadyInUseError(f"{prefix}: {message}") from exc
        raise CameraOpenError(f"{prefix}: {message}") from exc

    @staticmethod
    def _raise_open_status(status):
        message = getattr(status, "name", None) or str(status)
        upper = message.upper().replace("_", " ")
        if "TIMEOUT" in upper:
            raise CameraOperationTimeout(f"Opening the ZED camera timed out: {message}")
        if "NOT DETECTED" in upper or "NO CAMERA" in upper:
            raise CameraNotDetectedError(f"ZED camera not detected: {message}")
        if "BUSY" in upper or "ALREADY" in upper or "IN USE" in upper:
            raise CameraAlreadyInUseError(f"ZED camera is already in use: {message}")
        raise CameraOpenError(f"Unable to open ZED camera: {message}")
