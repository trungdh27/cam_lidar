"""Non-invasive RealSense inventory probe executed by Jetson Python."""

JETSON_REALSENSE_PROBE = r'''
import json
import pathlib
import re
import shutil
import subprocess


MARKER = "CAMERA_REALSENSE_JSON="


def emit(payload):
    print(MARKER + json.dumps(payload, separators=(",", ":")))


def text_enum(value):
    return str(value).split(".")[-1].lower()


def safe_info(item, key):
    try:
        return str(item.get_info(key)) if item.supports(key) else None
    except Exception:
        return None


def profile_payload(profile):
    payload = {
        "stream": text_enum(profile.stream_type()),
        "format": text_enum(profile.format()),
        "width": None,
        "height": None,
        "fps": int(profile.fps()),
    }
    try:
        video = profile.as_video_stream_profile()
        payload["width"] = int(video.width())
        payload["height"] = int(video.height())
    except Exception:
        pass
    return payload


def pyrealsense_devices():
    try:
        import pyrealsense2 as rs
    except Exception as exc:
        return [], "pyrealsense2 unavailable: " + str(exc)
    devices = []
    try:
        queried = rs.context().query_devices()
    except Exception as exc:
        return [], "pyrealsense2 device enumeration failed: " + str(exc)
    for device in queried:
        sensors = []
        profiles = []
        warnings = []
        try:
            for sensor in device.query_sensors():
                name = safe_info(sensor, rs.camera_info.name) or "Unknown Sensor"
                sensors.append(name)
                try:
                    profiles.extend(profile_payload(item) for item in sensor.get_stream_profiles())
                except Exception as exc:
                    warnings.append(name + " profile enumeration failed: " + str(exc))
        except Exception as exc:
            warnings.append("Sensor enumeration failed: " + str(exc))
        devices.append({
            "model": safe_info(device, rs.camera_info.name) or "Intel RealSense",
            "serial": safe_info(device, rs.camera_info.serial_number),
            "firmware": safe_info(device, rs.camera_info.firmware_version),
            "physical_port": safe_info(device, rs.camera_info.physical_port),
            "usb_type_descriptor": safe_info(device, rs.camera_info.usb_type_descriptor),
            "product_id": safe_info(device, rs.camera_info.product_id),
            "sensors": sensors,
            "stream_profiles": profiles,
            "profile_status": "AVAILABLE",
            "backend": "pyrealsense2",
            "warnings": warnings,
        })
    return devices, None


def cli_devices():
    executable = shutil.which("rs-enumerate-devices")
    if not executable:
        return [], "rs-enumerate-devices unavailable"
    try:
        result = subprocess.run(
            [executable], capture_output=True, text=True,
            timeout=8, check=False,
        )
    except Exception as exc:
        return [], "rs-enumerate-devices failed: " + str(exc)
    output = result.stdout + "\n" + result.stderr
    if result.returncode != 0:
        return [], "rs-enumerate-devices returned " + str(result.returncode)
    aliases = {
        "model": ("device name", "name"),
        "serial": ("device serial no", "serial number", "serial"),
        "firmware": ("firmware version",),
        "physical_port": ("physical port",),
        "usb_type_descriptor": ("usb type descriptor",),
        "product_id": ("product id",),
    }
    devices = []
    current = None
    current_sensor = None
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("device info"):
            if current and (current.get("serial") or current.get("model")):
                devices.append(current)
            current = {"sensors": [], "stream_profiles": []}
            current_sensor = None
            continue
        if current is None:
            continue
        sensor_match = re.match(r"stream profiles supported by\s+(.+)", line, re.I)
        if sensor_match:
            current_sensor = sensor_match.group(1).strip()
            if current_sensor not in current["sensors"]:
                current["sensors"].append(current_sensor)
            continue
        if current_sensor:
            video_profile = re.match(
                r"(.+?)\s+(\d+)\s*x\s*(\d+)\s+(\S+)\s+@\s*([\d/]+)\s*Hz",
                line, re.I,
            )
            motion_profile = re.match(
                r"(.+?)\s+(\S+)\s+@\s*([\d/]+)\s*Hz", line, re.I
            )
            if video_profile:
                stream, width, height, fmt, rates = video_profile.groups()
                for rate in rates.split("/"):
                    current["stream_profiles"].append({
                        "stream": stream.strip(), "format": fmt,
                        "width": int(width), "height": int(height),
                        "fps": int(rate),
                    })
                continue
            if motion_profile and "supported modes" not in line.lower():
                stream, fmt, rates = motion_profile.groups()
                for rate in rates.split("/"):
                    current["stream_profiles"].append({
                        "stream": stream.strip(), "format": fmt,
                        "width": None, "height": None, "fps": int(rate),
                    })
                continue
        match = re.match(r"([^:]+?)\s*:\s*(.+)$", line)
        if not match:
            match = re.match(r"(.+?)\s{2,}(.+)$", line)
        if not match:
            continue
        label, value = match.group(1).strip().lower(), match.group(2).strip()
        key = next((key for key, names in aliases.items() if label in names), None)
        if key:
            current[key] = value
    if current and (current.get("serial") or "realsense" in current.get("model", "").lower()):
        devices.append(current)
    for item in devices:
        profiles_available = bool(item.get("stream_profiles"))
        item.update({
            "profile_status": "AVAILABLE" if profiles_available else "UNAVAILABLE",
            "backend": "librealsense_cli",
            "warnings": [] if profiles_available else [
                "Detailed stream profiles unavailable from librealsense CLI enumeration."
            ],
        })
    return devices, None


def sysfs_devices():
    devices = []
    root = pathlib.Path("/sys/bus/usb/devices")
    if not root.exists():
        return devices, "USB sysfs unavailable"
    for path in root.iterdir():
        try:
            vendor = (path / "idVendor").read_text().strip().lower()
            product_name = (path / "product").read_text().strip()
        except Exception:
            continue
        if vendor != "8086" or "realsense" not in product_name.lower():
            continue
        def read(name):
            try:
                return (path / name).read_text().strip()
            except Exception:
                return None
        devices.append({
            "model": product_name,
            "serial": read("serial"),
            "firmware": None,
            "physical_port": path.name,
            "usb_type_descriptor": None,
            "usb_sysfs_speed": read("speed"),
            "product_id": read("idProduct"),
            "sensors": [], "stream_profiles": [],
            "profile_status": "UNAVAILABLE", "backend": "usb_sysfs",
            "warnings": [
                "Detailed stream profiles unavailable because librealsense enumeration is not available."
            ],
        })
    return devices, None


def merge_devices(primary, fallback):
    merged = list(primary)
    known = {str(item.get("serial") or "") for item in merged}
    for item in fallback:
        serial = str(item.get("serial") or "")
        existing = next((value for value in merged if serial and str(value.get("serial")) == serial), None)
        if existing is None:
            item_model = re.sub(r"[^a-z0-9]", "", str(item.get("model") or "").lower())
            existing = next((
                value for value in merged
                if not value.get("serial") and item_model and (
                    item_model in re.sub(r"[^a-z0-9]", "", str(value.get("model") or "").lower())
                    or re.sub(r"[^a-z0-9]", "", str(value.get("model") or "").lower()) in item_model
                )
            ), None)
        if existing is None and not serial and len(merged) == 1:
            existing = merged[0]
        if existing:
            for key in ("serial", "physical_port", "usb_sysfs_speed", "product_id"):
                if not existing.get(key) and item.get(key):
                    existing[key] = item[key]
            continue
        if not serial or serial not in known:
            merged.append(item)
            known.add(serial)
    return merged


def main():
    warnings = []
    devices, error = pyrealsense_devices()
    if error:
        warnings.append(error)
        devices, cli_error = cli_devices()
        if cli_error:
            warnings.append(cli_error)
    sysfs, sysfs_error = sysfs_devices()
    if sysfs_error:
        warnings.append(sysfs_error)
    devices = merge_devices(devices, sysfs)
    emit({"ok": True, "devices": devices, "warnings": warnings})


if __name__ == "__main__":
    main()
'''
