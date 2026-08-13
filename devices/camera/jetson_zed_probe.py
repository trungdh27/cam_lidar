"""Source sent to Jetson and executed by its Python interpreter."""

JETSON_ZED_PROBE = r'''
import json
import re
import shutil
import subprocess
import sys


def enum_text(value):
    text = getattr(value, "name", None) or str(value)
    return text.replace("CAMERA_MODEL_ONE.", "").replace("CAMERA_MODEL.", "").replace("INPUT_TYPE.", "").replace("_", " ")


def normalize_model(value):
    return " ".join(re.findall(r"[A-Z]+|\d+", enum_text(value).upper()))


def model_family(value):
    compact = normalize_model(value).replace(" ", "")
    if "XONE" in compact:
        if "GS" in compact or "GLOBALSHUTTER" in compact:
            return "zed_x_one_gs"
        if "UHD" in compact or "4K" in compact:
            return "zed_x_one_4k"
        return "zed_x_one"
    if "XMINI" in compact or "ZEDMINI" in compact:
        return "zed_x_mini"
    if "ZEDX" in compact:
        return "zed_x"
    return "unknown_zed"


def emit(payload):
    print("CAMERA_ZED_JSON=" + json.dumps(payload, separators=(",", ":")))


def value(item, names, default="-"):
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def device_payload(item, sdk_version, api):
    model = enum_text(value(item, ("camera_model", "model"), "Unknown ZED"))
    return {
        "model": model,
        "raw_model": str(value(item, ("camera_model", "model"), "Unknown ZED")),
        "normalized_model": normalize_model(model),
        "model_family": model_family(model),
        "serial_number": str(value(item, ("serial_number", "serial"))),
        "camera_id": str(value(item, ("id", "camera_id"))),
        "state": enum_text(value(item, ("camera_state", "state"))),
        "firmware": "-",
        "interface": enum_text(value(item, ("input_type", "connection_type"), "GMSL2")),
        "device_path": str(value(item, ("path", "device_path"))),
        "sdk_driver": "ZED SDK " + sdk_version,
        "api": api,
    }


def camera_one_classes(sl):
    camera = getattr(sl, "CameraOne", None)
    init = getattr(sl, "InitParametersOne", None)
    return camera, init


def resolution_value(sl, key):
    checked = []
    for enum_name in ("RESOLUTION", "RESOLUTION_ONE"):
        enum_class = getattr(sl, enum_name, None)
        if enum_class is None: continue
        checked.append(enum_name)
        value = getattr(enum_class, key, None)
        if value is not None: return value
    raise RuntimeError("Installed ZED SDK does not expose resolution " + key + " in " + str(checked))


def explorer_devices(sdk_version):
    executable = shutil.which("ZED_Explorer")
    if executable is None and shutil.which("/usr/local/zed/tools/ZED_Explorer"):
        executable = "/usr/local/zed/tools/ZED_Explorer"
    if executable is None:
        return [], "ZED_Explorer was not found"
    try:
        result = subprocess.run([executable, "--all"], capture_output=True, text=True, timeout=10, check=False)
    except Exception as exc:
        return [], "ZED_Explorer fallback failed: " + str(exc)
    output = result.stdout + "\n" + result.stderr
    devices = []
    current = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        model_match = re.search(r"(?:camera\s+model|model)\s*[:=]\s*(.+)", line, re.I)
        serial_match = re.search(r"(?:serial(?:\s+number)?|sn)\s*[:=]\s*(\d+)", line, re.I)
        id_match = re.search(r"camera\s+id\s*[:=]\s*([^\s]+)", line, re.I)
        state_match = re.search(r"state\s*[:=]\s*(.+)", line, re.I)
        if model_match and current.get("model"):
            devices.append(current)
            current = {}
        if model_match: current["model"] = model_match.group(1).strip()
        if serial_match: current["serial_number"] = serial_match.group(1)
        if id_match: current["camera_id"] = id_match.group(1)
        if state_match: current["state"] = state_match.group(1).strip()
    if current.get("model") or current.get("serial_number"):
        devices.append(current)
    normalized = []
    for item in devices:
        model = item.get("model", "Unknown ZED")
        normalized.append({
            "model": model, "raw_model": model, "normalized_model": normalize_model(model),
            "model_family": model_family(model), "serial_number": item.get("serial_number", "-"),
            "camera_id": item.get("camera_id", "-"), "state": item.get("state", "-"),
            "firmware": "-", "interface": "GMSL2", "device_path": "-",
            "sdk_driver": "ZED SDK " + sdk_version, "api": "ZED_Explorer --all",
        })
    return normalized, None


def main():
    request = json.loads(sys.argv[1])
    profile_id = request.get("profile_id", "")
    mono = profile_id in ("zed_x_one_4k", "zed_x_one_gs")
    try:
        import pyzed.sl as sl
    except Exception as exc:
        emit({"ok": False, "error_type": "sdk_unavailable", "error": "pyzed.sl unavailable: " + str(exc), "sdk_available": False})
        return 20

    try: sdk_version = str(sl.Camera.get_sdk_version())
    except Exception: sdk_version = "Unknown"

    camera_class, init_class = (camera_one_classes(sl) if mono else (sl.Camera, sl.InitParameters))
    fallback_note = None
    devices = []
    if camera_class is not None and hasattr(camera_class, "get_device_list"):
        try:
            devices = [device_payload(item, sdk_version, "CameraOne" if mono else "Camera") for item in camera_class.get_device_list()]
        except Exception as exc:
            fallback_note = "CameraOne device list failed: " + str(exc) if mono else None
            if not mono:
                emit({"ok": False, "error_type": "discovery", "error": str(exc), "sdk_available": True, "sdk_version": sdk_version})
                return 21
    elif mono:
        fallback_note = "CameraOne.get_device_list is unavailable in this ZED SDK"

    if mono and not devices:
        if fallback_note is None:
            fallback_note = "CameraOne device list returned no devices"
        devices, explorer_error = explorer_devices(sdk_version)
        if explorer_error:
            fallback_note += "; " + explorer_error

    raw_devices = list(devices)
    action = request.get("action")
    if action == "discover":
        emit({"ok": True, "detected": bool(devices), "devices": devices, "raw_devices": raw_devices, "sdk_available": True, "sdk_version": sdk_version, "discovery_note": fallback_note})
        return 0

    if mono and (camera_class is None or init_class is None):
        emit({"ok": False, "error_type": "sdk_unavailable", "error": "Installed pyzed.sl does not expose CameraOne/InitParametersOne required to open ZED X One.", "sdk_available": True, "sdk_version": sdk_version})
        return 25

    serial_text = str(request.get("serial_number") or "")
    selected = next((item for item in devices if item["serial_number"] == serial_text), None)
    if selected is None:
        emit({"ok": False, "error_type": "not_detected", "error": "Selected ZED camera was not detected.", "sdk_available": True, "sdk_version": sdk_version, "raw_devices": raw_devices})
        return 22

    camera = camera_class()
    init = init_class()
    serial = int(serial_text)
    if hasattr(init, "set_from_serial_number"): init.set_from_serial_number(serial)
    elif hasattr(init, "input") and hasattr(init.input, "set_from_serial_number"): init.input.set_from_serial_number(serial)
    else:
        emit({"ok": False, "error_type": "sdk_unavailable", "error": "Selected ZED API cannot open by serial number.", "sdk_available": True, "sdk_version": sdk_version})
        return 26
    resolution_key = str(request.get("resolution_key") or "")
    try:
        init.camera_resolution = resolution_value(sl, resolution_key)
        init.camera_fps = int(request.get("fps"))
    except Exception as exc:
        emit({"ok": False, "error_type": "configuration", "error": str(exc), "sdk_available": True, "sdk_version": sdk_version})
        return 27
    if hasattr(init, "open_timeout_sec"): init.open_timeout_sec = float(request.get("open_timeout", 10))
    try:
        status = camera.open(init)
        if status != sl.ERROR_CODE.SUCCESS:
            message = getattr(status, "name", None) or str(status)
            upper = message.upper().replace("_", " ")
            error_type = "already_in_use" if any(word in upper for word in ("BUSY", "ALREADY", "IN USE")) else "not_detected" if "NOT DETECTED" in upper else "timeout" if "TIMEOUT" in upper else "open_failure"
            emit({"ok": False, "error_type": error_type, "error": message, "sdk_available": True, "sdk_version": sdk_version})
            return 23
        info = camera.get_camera_information()
        config = getattr(info, "camera_configuration", None)
        selected["model"] = enum_text(getattr(info, "camera_model", selected["model"]))
        selected["firmware"] = str(getattr(config, "firmware_version", "-")) if config is not None else "-"
        emit({"ok": True, "validated": True, "device": selected, "raw_devices": raw_devices, "sdk_available": True, "sdk_version": sdk_version, "resolution": request.get("resolution"), "resolution_key": resolution_key, "fps": int(request.get("fps"))})
        return 0
    except Exception as exc:
        message = str(exc); upper = message.upper()
        error_type = "already_in_use" if any(word in upper for word in ("BUSY", "ALREADY", "IN USE")) else "timeout" if "TIMEOUT" in upper else "open_failure"
        emit({"ok": False, "error_type": error_type, "error": message, "sdk_available": True, "sdk_version": sdk_version})
        return 24
    finally:
        try: camera.close()
        except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())
'''
