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
    current = None

    def clean_explorer_value(raw_value):
        cleaned = raw_value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("\"", "'"):
            cleaned = cleaned[1:-1].strip()
        return cleaned

    field_names = {
        "model": "model",
        "camera model": "model",
        "s/n": "serial_number",
        "sn": "serial_number",
        "serial": "serial_number",
        "serial number": "serial_number",
        "state": "state",
        "path": "device_path",
        "id": "camera_id",
        "camera id": "camera_id",
        "port": "port",
        "type": "interface",
        "interface": "interface",
    }
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if re.match(r"^##\s*Cam\s+\d+\s*##$", line, re.I):
            if current:
                devices.append(current)
            current = {}
            continue
        if current is None:
            continue
        if re.match(r"^\*+$", line):
            if current:
                devices.append(current)
            current = None
            continue
        field_match = re.match(r"^([^:=]+?)\s*[:=]\s*(.*?)\s*$", line)
        if not field_match:
            continue
        key = " ".join(field_match.group(1).strip().lower().split())
        field = field_names.get(key)
        if field:
            current[field] = clean_explorer_value(field_match.group(2))
    if current:
        devices.append(current)
    normalized = []
    for item in devices:
        model = item.get("model", "Unknown ZED")
        normalized.append({
            "model": model, "raw_model": model, "normalized_model": normalize_model(model),
            "model_family": model_family(model), "serial_number": item.get("serial_number", "-"),
            "camera_id": item.get("camera_id", "-"), "state": item.get("state", "-"),
            "firmware": "-", "interface": item.get("interface", "-"),
            "device_path": item.get("device_path", "-"), "port": item.get("port", "-"),
            "sdk_driver": "ZED SDK " + sdk_version, "api": "ZED_Explorer --all",
        })
    return normalized, None


def deduplicate_devices(devices):
    specificity = {
        "unknown_zed": 0, "zed_x": 1, "zed_x_one": 2,
        "zed_x_mini": 3, "zed_x_one_gs": 3, "zed_x_one_4k": 3,
    }
    missing_values = {"", "-", "none", "unknown", "n/a"}

    def clean_identity_value(value):
        text = str(value or "").strip()
        return "" if text.casefold() in missing_values else text

    def serial_of(item):
        return clean_identity_value(item.get("serial_number") or item.get("serial"))

    def record_family(item):
        family = clean_identity_value(item.get("model_family"))
        return family or model_family(item.get("model", "Unknown ZED"))

    def physical_match(first, second):
        locator_fields = ("device_path", "camera_id", "port")
        shared = []
        for field in locator_fields:
            first_value = clean_identity_value(first.get(field))
            second_value = clean_identity_value(second.get(field))
            if first_value and second_value:
                if first_value != second_value:
                    return False
                shared.append(field)
        first_transport = clean_identity_value(first.get("interface")).upper()
        second_transport = clean_identity_value(second.get("interface")).upper()
        if first_transport and second_transport and first_transport != second_transport:
            if not (first_transport.startswith("GMSL") and second_transport.startswith("GMSL")):
                return False
        return bool(shared)

    def merge_records(current, incoming):
        current_family = record_family(current)
        incoming_family = record_family(incoming)
        current_rank = specificity.get(current_family, 0)
        incoming_rank = specificity.get(incoming_family, 0)
        merged = dict(current)
        for key, value in incoming.items():
            if not clean_identity_value(merged.get(key)) and clean_identity_value(value):
                merged[key] = value

        preferred = incoming if incoming_rank > current_rank else current
        preferred_family = incoming_family if incoming_rank > current_rank else current_family
        preferred_model = clean_identity_value(preferred.get("model"))
        if preferred_model:
            merged["model"] = preferred_model
            merged["raw_model"] = clean_identity_value(preferred.get("raw_model")) or preferred_model
            merged["normalized_model"] = (
                clean_identity_value(preferred.get("normalized_model"))
                or normalize_model(preferred_model)
            )
            merged["model_family"] = preferred_family

        serial = serial_of(current) or serial_of(incoming)
        if serial:
            merged["serial_number"] = serial

        # Explorer reports the physical connection fields more precisely than
        # the SDK device-list APIs, so retain those values during reconciliation.
        explorer = next(
            (
                item for item in (incoming, current)
                if "ZED_Explorer" in str(item.get("api", ""))
            ),
            None,
        )
        if explorer is not None:
            for key in ("camera_id", "state", "interface", "device_path", "port"):
                if clean_identity_value(explorer.get(key)):
                    merged[key] = explorer[key]

        sources = []
        for item in (current, incoming):
            for source in str(item.get("api") or "").split(" + "):
                if source and source not in sources:
                    sources.append(source)
        if sources:
            merged["api"] = " + ".join(sources)
        return merged

    # Reconcile serial-bearing observations first. This lets a later no-serial
    # fallback attach only when its physical locators select one unique camera.
    ordered = sorted(devices, key=lambda item: not bool(serial_of(item)))
    reconciled = []
    for item in ordered:
        incoming = dict(item)
        serial = serial_of(incoming)
        serial_matches = [
            index for index, current in enumerate(reconciled)
            if serial and serial_of(current) == serial
        ]
        if serial_matches:
            index = serial_matches[0]
            reconciled[index] = merge_records(reconciled[index], incoming)
            continue

        physical_matches = [
            index for index, current in enumerate(reconciled)
            if not (serial and serial_of(current)) and physical_match(current, incoming)
        ]
        if len(physical_matches) == 1:
            index = physical_matches[0]
            reconciled[index] = merge_records(reconciled[index], incoming)
        else:
            reconciled.append(incoming)
    return reconciled


def all_zed_devices(sl, sdk_version):
    devices = []
    notes = []
    camera_classes = [
        (getattr(sl, "Camera", None), "Camera"),
        (getattr(sl, "CameraOne", None), "CameraOne"),
    ]
    camera_one_available = camera_classes[1][0] is not None
    for camera_class, api in camera_classes:
        if camera_class is None or not hasattr(camera_class, "get_device_list"):
            notes.append(api + ".get_device_list is unavailable")
            continue
        try:
            devices.extend(
                device_payload(item, sdk_version, api)
                for item in camera_class.get_device_list()
            )
        except Exception as exc:
            notes.append(api + " device list failed: " + str(exc))
    # Explorer is a supplemental *physical* source, not merely a CameraOne
    # fallback. A production-owned camera may be omitted by a SDK device list
    # while Explorer still reports its serial, I2C path and busy state.
    explorer, explorer_error = explorer_devices(sdk_version)
    devices.extend(explorer)
    if explorer_error:
        notes.append(explorer_error)
    return deduplicate_devices(devices), "; ".join(notes) or None


def main():
    request = json.loads(sys.argv[1])
    profile_id = request.get("profile_id", "")
    mono = profile_id in ("zed_x_one_4k", "zed_x_one_gs")
    action = request.get("action")
    try:
        import pyzed.sl as sl
    except Exception as exc:
        if action == "discover_all":
            devices, explorer_error = explorer_devices("Unknown")
            emit({
                "ok": True, "detected": bool(devices), "devices": devices,
                "raw_devices": list(devices), "sdk_available": False,
                "sdk_version": "Unknown",
                "discovery_note": "pyzed.sl unavailable: " + str(exc)
                + ("; " + explorer_error if explorer_error else ""),
            })
            return 0
        emit({"ok": False, "error_type": "sdk_unavailable", "error": "pyzed.sl unavailable: " + str(exc), "sdk_available": False})
        return 20

    try: sdk_version = str(sl.Camera.get_sdk_version())
    except Exception: sdk_version = "Unknown"

    if action == "discover_all":
        devices, note = all_zed_devices(sl, sdk_version)
        emit({
            "ok": True, "detected": bool(devices), "devices": devices,
            "raw_devices": list(devices), "sdk_available": True,
            "sdk_version": sdk_version, "discovery_note": note,
        })
        return 0

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
