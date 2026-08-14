from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal


class DeviceRegistry(QObject):
    """Application-scoped last-known device state."""

    devices_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices: dict[str, dict] = {}

    def devices(self) -> list[dict]:
        return [dict(device) for device in self._devices.values()]

    def device(self, name: str) -> dict | None:
        record = self._devices.get(self._key(name))
        return dict(record) if record is not None else None

    def update_device(self, device: dict) -> None:
        name = str(
            device.get("device")
            or device.get("name")
            or device.get("type")
            or ""
        ).strip()
        if not name:
            raise ValueError("Device registry updates require a device name")

        key = self._key(name)
        current = self._devices.get(
            key,
            {
                "device": name,
                "available": "unknown",
                "model": None,
                "serial": None,
                "status": "not configured",
                "last_update": None,
            },
        )
        updated = dict(current)
        updated["device"] = current.get("device") or name
        for field in (
            "available",
            "model",
            "serial",
            "status",
            "last_error",
        ):
            if field in device:
                updated[field] = device[field]
        updated["last_update"] = device.get("last_update") or self._timestamp()

        if updated == current:
            return
        self._devices[key] = updated
        self.devices_changed.emit(self.devices())

    def mark_unavailable(
        self,
        name: str,
        *,
        status: str = "warning",
        reason: str | None = None,
    ) -> None:
        if self._key(name) not in self._devices:
            return
        update = {
            "device": name,
            "available": "not detected",
            "status": status,
        }
        if reason is not None:
            update["last_error"] = reason
        self.update_device(update)

    @staticmethod
    def _key(name: str) -> str:
        return str(name).strip().casefold()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
