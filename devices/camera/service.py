from devices.camera.backend import CameraBackendUnavailable
from devices.camera.profiles import CAMERA_PROFILES, CameraProfile, get_camera_profile
from devices.camera.remote_zed_adapter import RemoteZedAdapter
from devices.camera.zed_adapter import ZedAdapter


class CameraService:
    """Qt-free camera application service and future backend registry."""

    def __init__(self):
        self._backends = {"zed": ZedAdapter()}
        self._remote_backends = {"zed": RemoteZedAdapter()}

    def profiles(self) -> tuple[CameraProfile, ...]:
        return CAMERA_PROFILES

    def profile(self, profile_id: str) -> CameraProfile:
        return get_camera_profile(profile_id)

    def register_backend(self, backend_name: str, backend) -> None:
        self._backends[backend_name] = backend

    def is_connected(self, backend_name: str) -> bool:
        return any(
            backend and backend.connected
            for backend in (
                self._backends.get(backend_name),
                self._remote_backends.get(backend_name),
            )
        )

    def execute(self, action: str, payload: dict) -> dict:
        profile = self.profile(payload["profile_id"])
        remote = payload.get("execution_host") == "Jetson"
        registry = self._remote_backends if remote else self._backends
        backend = registry.get(profile.backend)

        if backend is None:
            raise CameraBackendUnavailable(
                f"{profile.sdk_driver} communication is not implemented for this camera family."
            )

        handler = getattr(backend, action, None)
        if handler is None:
            raise CameraBackendUnavailable(
                f"Camera backend '{profile.backend}' does not support '{action}'."
            )
        return handler(payload)
