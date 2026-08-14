"""Self-contained ZED stream manager executed on the Jetson over SSH."""

JETSON_ZED_STREAM_MANAGER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path

MARKER = "CAMERA_STREAM_JSON="
ROOT = Path("/tmp/cam_lidar/camera")

WORKER = r"""
import json, os, signal, socket, struct, sys, threading, time
from collections import deque
from pathlib import Path

request = json.loads(sys.argv[1])
runtime = Path(request["runtime"])
status_path = runtime / "stream_status.json"
stop_path = runtime / "stop.request"
pid_path = runtime / "stream.pid"
preview_fallback_path = runtime / "preview_fallback.request"
terminate_requested = False

def request_termination(_signum, _frame):
    global terminate_requested
    terminate_requested = True

signal.signal(signal.SIGTERM, request_termination)

def atomic_status(data):
    temporary = status_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(str(temporary), str(status_path))

def resolution_value(sl, key):
    for enum_name in ("RESOLUTION", "RESOLUTION_ONE"):
        enum_class = getattr(sl, enum_name, None)
        value = getattr(enum_class, key, None) if enum_class else None
        if value is not None: return value
    raise RuntimeError("Installed ZED SDK does not expose resolution " + key)

base = {
    "running": False, "state": "starting", "pid": os.getpid(),
    "serial_number": request["serial_number"], "profile_id": request["profile_id"],
    "resolution_key": request["resolution_key"], "resolution": request["resolution"],
    "configured_fps": request["fps"], "pixel_format": request.get("pixel_format"),
    "actual_fps": None, "frame_interval_ms": None, "frame_count": 0,
    "dropped_frames": 0, "dropped_frames_estimated": True, "timestamp": None,
    "exposure": None, "gain": None, "temperature": None, "last_error": None,
    "stream_duration_s": 0.0, "grab_errors": 0,
    "valid_frame_count": 0, "invalid_frame_count": 0, "corrupted_frame_count": 0,
    "duplicate_timestamp_count": 0, "timestamp_rollback_count": 0,
    "actual_width": None, "actual_height": None,
    "preview_state": "starting", "preview_backend": request["preview_backend"],
    "preview_port": request["preview_port"], "h264_preview_port": request["h264_preview_port"],
    "preview_width": request["preview_width"], "preview_height": request["preview_height"],
    "preview_target_fps": request["preview_fps"], "preview_jpeg_quality": request["preview_quality"],
    "preview_frames_produced": 0, "preview_frames_sent": 0,
    "preview_jpeg_size": None, "preview_client_connected": False, "preview_error": None,
    "preview_encoder": None, "preview_encoder_hardware": None,
    "preview_frames_submitted": 0, "preview_frames_dropped": 0,
}
runtime.mkdir(parents=True, exist_ok=True)
pid_path.write_text(str(os.getpid()), encoding="ascii")
atomic_status(base)
camera = None
preview_stop = threading.Event()
preview_lock = threading.Lock()
preview_slot = {"sequence": 0, "image": None}

def jpeg_preview_server():
    server = client = None
    last_sequence = 0
    try:
        import cv2
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((request.get("preview_bind", "0.0.0.0"), int(request["preview_port"])))
        server.listen(1)
        server.settimeout(0.5)
        base["preview_state"] = "listening"
        while not preview_stop.is_set():
            if client is None:
                try:
                    client, _address = server.accept()
                    client.settimeout(0.75)
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
                    base.update(preview_state="connected", preview_client_connected=True, preview_error=None)
                except socket.timeout:
                    continue
            with preview_lock:
                sequence, image = preview_slot["sequence"], preview_slot["image"]
            if image is None or sequence == last_sequence:
                preview_stop.wait(0.005); continue
            last_sequence = sequence
            height, width = image.shape[:2]
            scale = min(float(request["preview_width"])/width, float(request["preview_height"])/height)
            target = (max(1, int(width*scale)), max(1, int(height*scale)))
            resized = cv2.resize(image, target, interpolation=cv2.INTER_AREA)
            if resized.ndim == 3 and resized.shape[2] == 4:
                resized = cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)
            ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, int(request["preview_quality"])])
            if not ok: raise RuntimeError("OpenCV JPEG encoding failed")
            payload = encoded.tobytes()
            try:
                client.sendall(struct.pack("!I", len(payload)) + payload)
                base["preview_frames_sent"] += 1
                base["preview_jpeg_size"] = len(payload)
                base["preview_state"] = "live"
            except (OSError, socket.timeout) as exc:
                try: client.close()
                except OSError: pass
                client = None
                base.update(preview_state="listening", preview_client_connected=False, preview_error=str(exc))
    except Exception as exc:
        base.update(preview_state="failed", preview_client_connected=False, preview_error=str(exc))
    finally:
        for item in (client, server):
            if item is not None:
                try: item.close()
                except OSError: pass
        base.update(preview_client_connected=False, preview_state="stopped" if preview_stop.is_set() else base["preview_state"])

def find_h264_encoder(Gst):
    factories = Gst.ElementFactory.list_get_elements(Gst.ELEMENT_FACTORY_TYPE_ENCODER, Gst.Rank.NONE)
    matches = []
    for factory in factories:
        if "Video" not in factory.get_klass(): continue
        for template in factory.get_static_pad_templates():
            if template.direction == Gst.PadDirection.SRC and "video/x-h264" in template.get_caps().to_string():
                hardware = "Hardware" in factory.get_klass()
                matches.append((hardware, factory.get_rank(), factory.get_name()))
                break
    matches.sort(reverse=True)
    return matches[0] if matches else None

def set_if_present(element, name, value):
    if element.find_property(name) is not None: element.set_property(name, value)

def gstreamer_preview_sender():
    pipeline = None
    try:
        import cv2, gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        required = ("appsrc", "videoconvert", "h264parse", "rtph264pay", "udpsink")
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        selected = find_h264_encoder(Gst)
        if missing or selected is None:
            raise RuntimeError("Missing Jetson GStreamer component: " + (", ".join(missing) or "H.264 encoder"))
        hardware, _rank, encoder_name = selected
        pipeline = Gst.Pipeline.new("camera-h264-preview")
        source = Gst.ElementFactory.make("appsrc", "preview_source")
        convert = Gst.ElementFactory.make("videoconvert", None)
        encoder = Gst.ElementFactory.make(encoder_name, "preview_encoder")
        parser = Gst.ElementFactory.make("h264parse", None)
        pay = Gst.ElementFactory.make("rtph264pay", None)
        sink = Gst.ElementFactory.make("udpsink", None)
        if any(item is None for item in (pipeline, source, convert, encoder, parser, pay, sink)):
            raise RuntimeError("Unable to create Jetson H.264 pipeline elements")
        caps = Gst.Caps.from_string("video/x-raw,format=BGR,width=%d,height=%d,framerate=%d/1" %
            (request["preview_width"], request["preview_height"], request["preview_fps"]))
        source.set_property("caps", caps); source.set_property("is-live", True)
        source.set_property("format", Gst.Format.TIME); source.set_property("block", False)
        set_if_present(source, "max-bytes", request["preview_width"] * request["preview_height"] * 3)
        set_if_present(source, "max-buffers", 1)
        set_if_present(source, "leaky-type", 2)
        bitrate_prop = encoder.find_property("bitrate")
        if bitrate_prop is not None:
            bitrate = request["h264_bitrate_kbps"]
            encoder.set_property("bitrate", bitrate * 1000 if getattr(bitrate_prop, "maximum", 0) > 100000 else bitrate)
        set_if_present(encoder, "key-int-max", request["h264_gop_frames"])
        set_if_present(encoder, "iframeinterval", request["h264_gop_frames"])
        set_if_present(encoder, "bframes", 0)
        set_if_present(pay, "config-interval", 1); set_if_present(pay, "pt", 96)
        sink.set_property("host", request["preview_host"]); sink.set_property("port", request["h264_preview_port"])
        sink.set_property("sync", False); sink.set_property("async", False)
        for item in (source, convert, encoder, parser, pay, sink): pipeline.add(item)
        if not source.link(convert) or not convert.link(encoder) or not encoder.link(parser) or not parser.link(pay) or not pay.link(sink):
            raise RuntimeError("Unable to link Jetson H.264 pipeline")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Jetson H.264 pipeline failed to enter PLAYING")
        base.update(preview_backend="gstreamer_h264", preview_state="playing", preview_encoder=encoder_name,
            preview_encoder_hardware=hardware, preview_error=None)
        last_sequence = 0; frame_number = 0
        while not preview_stop.is_set():
            if preview_fallback_path.exists():
                raise RuntimeError("Host requested JPEG fallback")
            with preview_lock:
                sequence, image = preview_slot["sequence"], preview_slot["image"]
            if image is None or sequence == last_sequence:
                preview_stop.wait(0.003); continue
            if sequence > last_sequence + 1: base["preview_frames_dropped"] += sequence - last_sequence - 1
            last_sequence = sequence
            height, width = image.shape[:2]
            scale = min(float(request["preview_width"])/width, float(request["preview_height"])/height)
            resized = cv2.resize(image, (max(1, int(width*scale)), max(1, int(height*scale))), interpolation=cv2.INTER_AREA)
            if resized.shape[1] != request["preview_width"] or resized.shape[0] != request["preview_height"]:
                canvas = __import__("numpy").zeros((request["preview_height"], request["preview_width"], 3), dtype="uint8")
                bgr = cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR) if resized.shape[2] == 4 else resized
                y=(canvas.shape[0]-bgr.shape[0])//2; x=(canvas.shape[1]-bgr.shape[1])//2; canvas[y:y+bgr.shape[0],x:x+bgr.shape[1]]=bgr
                resized = canvas
            elif resized.shape[2] == 4: resized = cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)
            payload = resized.tobytes(); buffer = Gst.Buffer.new_allocate(None, len(payload), None); buffer.fill(0, payload)
            duration = Gst.SECOND // int(request["preview_fps"]); buffer.pts = frame_number * duration; buffer.duration = duration
            frame_number += 1
            if source.emit("push-buffer", buffer) != Gst.FlowReturn.OK: raise RuntimeError("GStreamer appsrc rejected preview frame")
            base["preview_frames_submitted"] += 1; base["preview_frames_sent"] += 1; base["preview_state"] = "live"
    except Exception as exc:
        base.update(preview_error=str(exc), preview_state="fallback", preview_backend="jpeg_tcp",
            preview_target_fps=request["jpeg_preview_fps"])
        try: preview_fallback_path.unlink()
        except FileNotFoundError: pass
        if pipeline is not None:
            try: pipeline.set_state(Gst.State.NULL)
            except Exception: pass
        jpeg_preview_server()
        return
    finally:
        if pipeline is not None:
            try: pipeline.set_state(Gst.State.NULL)
            except Exception: pass

def preview_worker():
    if request.get("preview_backend") == "off":
        base["preview_state"] = "off"
    elif request.get("preview_backend") == "gstreamer_h264": gstreamer_preview_sender()
    else: jpeg_preview_server()

try:
    import pyzed.sl as sl
    mono = request["profile_id"] in ("zed_x_one_4k", "zed_x_one_gs")
    camera_class = getattr(sl, "CameraOne", None) if mono else sl.Camera
    init_class = getattr(sl, "InitParametersOne", None) if mono else sl.InitParameters
    if camera_class is None or init_class is None:
        raise RuntimeError("Installed pyzed.sl does not expose CameraOne/InitParametersOne")
    camera, init = camera_class(), init_class()
    serial = int(request["serial_number"])
    if hasattr(init, "set_from_serial_number"): init.set_from_serial_number(serial)
    elif hasattr(init, "input") and hasattr(init.input, "set_from_serial_number"):
        init.input.set_from_serial_number(serial)
    else: raise RuntimeError("Selected ZED API cannot select a camera by serial number")
    init.camera_resolution = resolution_value(sl, request["resolution_key"])
    init.camera_fps = int(request["fps"])
    if hasattr(init, "open_timeout_sec"): init.open_timeout_sec = 10.0
    result = camera.open(init)
    if result != sl.ERROR_CODE.SUCCESS:
        raise RuntimeError("Camera open failed: " + (getattr(result, "name", None) or str(result)))
    info = camera.get_camera_information()
    configuration = getattr(info, "camera_configuration", None)
    camera_resolution = getattr(configuration, "resolution", None)
    base.update(running=True, state="streaming",
        actual_width=getattr(camera_resolution, "width", None),
        actual_height=getattr(camera_resolution, "height", None))
    atomic_status(base)
    preview_thread = threading.Thread(target=preview_worker, name="zed-preview", daemon=True)
    preview_thread.start()
    frame_times, frame_intervals, previous = deque(), deque(maxlen=max(5, int(request["fps"]))), None
    expected = 1.0 / float(request["fps"])
    stream_started = last_write = time.monotonic()
    consecutive_errors = 0
    next_preview = stream_started
    preview_mat = sl.Mat()
    validation_mat = sl.Mat() if request.get("automation_validation") else None
    previous_camera_timestamp = None
    while not stop_path.exists() and not terminate_requested:
        result = camera.grab()
        current = time.monotonic()
        if result == sl.ERROR_CODE.SUCCESS:
            consecutive_errors = 0
            base["frame_count"] += 1
            try:
                camera_timestamp = int(camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).data_ns)
            except Exception:
                camera_timestamp = time.monotonic_ns()
            base["timestamp"] = camera_timestamp
            if previous_camera_timestamp is not None:
                if camera_timestamp == previous_camera_timestamp: base["duplicate_timestamp_count"] += 1
                elif camera_timestamp < previous_camera_timestamp: base["timestamp_rollback_count"] += 1
            previous_camera_timestamp = camera_timestamp
            if previous is not None:
                interval = current - previous
                frame_intervals.append(interval)
                base["frame_interval_ms"] = round(
                    sum(frame_intervals) * 1000.0 / len(frame_intervals), 2
                )
                # A 1.5-frame threshold avoids counting ordinary scheduling jitter.
                if interval > expected * 1.5:
                    base["dropped_frames"] += max(1, round(interval / expected) - 1)
            previous = current
            frame_times.append(current)
            while frame_times and current - frame_times[0] > 1.0: frame_times.popleft()
            if len(frame_times) > 1:
                base["actual_fps"] = round((len(frame_times)-1)/(frame_times[-1]-frame_times[0]), 2)
            base["last_error"] = None
            if validation_mat is not None:
                try:
                    validation_status = camera.retrieve_image(validation_mat, sl.VIEW.LEFT)
                    data = validation_mat.get_data() if validation_status == sl.ERROR_CODE.SUCCESS else None
                    if data is None or getattr(data, "size", 0) == 0:
                        base["invalid_frame_count"] += 1
                    else:
                        height, width = data.shape[:2]
                        base["actual_width"], base["actual_height"] = int(width), int(height)
                        base["valid_frame_count"] += 1
                except Exception:
                    base["invalid_frame_count"] += 1
                    base["corrupted_frame_count"] += 1
            if request.get("preview_backend") != "off" and current >= next_preview:
                sampling_fps = request["jpeg_preview_fps"] if base.get("preview_backend") == "jpeg_tcp" else request["preview_fps"]
                next_preview = current + 1.0 / float(sampling_fps)
                try:
                    retrieve_status = camera.retrieve_image(preview_mat, sl.VIEW.LEFT)
                    if retrieve_status == sl.ERROR_CODE.SUCCESS:
                        newest = preview_mat.get_data().copy()
                        with preview_lock:
                            preview_slot["sequence"] += 1
                            preview_slot["image"] = newest
                        base["preview_frames_produced"] += 1
                except Exception as exc:
                    if base.get("preview_error") is None:
                        base["preview_error"] = "Image retrieval failed: " + str(exc)
        else:
            consecutive_errors += 1
            base["grab_errors"] += 1
            base["last_error"] = "grab failed: " + (getattr(result, "name", None) or str(result))
            if consecutive_errors > max(30, int(request["fps"]) * 5):
                raise RuntimeError(base["last_error"])
            time.sleep(0.005)
        if current - last_write >= 0.25:
            base["stream_duration_s"] = round(current - stream_started, 3)
            atomic_status(base); last_write = current
    base["stream_duration_s"] = round(time.monotonic() - stream_started, 3)
    base.update(running=False, state="stopping")
    atomic_status(base)
except Exception as exc:
    base.update(running=False, state="failed", last_error=str(exc))
    atomic_status(base)
finally:
    preview_stop.set()
    thread = locals().get("preview_thread")
    if thread is not None: thread.join(timeout=2.0)
    if camera is not None:
        try: camera.close()
        except Exception as exc:
            base["last_error"] = (base.get("last_error") + "; " if base.get("last_error") else "") + "close failed: " + str(exc)
    if base["state"] != "failed": base["state"] = "stopped"
    base.update(running=False)
    atomic_status(base)
    try: pid_path.unlink()
    except FileNotFoundError: pass
"""

def emit(data): print(MARKER + json.dumps(data, separators=(",", ":")))
def alive(pid):
    try: os.kill(pid, 0); return True
    except (OSError, ValueError): return False
def read_json(path):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return None
def pid_of(path):
    try: return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError): return None

request = json.loads(sys.argv[1])
serial = str(request.get("serial_number") or "")
if not serial.isdigit():
    emit({"ok": False, "error": "A valid camera serial number is required."}); raise SystemExit(2)
runtime = ROOT / serial
pid_path, status_path, stop_path = runtime/"stream.pid", runtime/"stream_status.json", runtime/"stop.request"
action = request["action"]
pid = pid_of(pid_path)
if action == "start":
    if pid and alive(pid):
        emit({"ok": False, "error": "Camera stream is already running.", "pid": pid}); raise SystemExit(3)
    runtime.mkdir(parents=True, exist_ok=True)
    for path in (pid_path, status_path, stop_path, runtime/"preview_fallback.request"):
        try: path.unlink()
        except FileNotFoundError: pass
    request["runtime"] = str(runtime)
    log = open(runtime/"stream.log", "ab", buffering=0)
    process = subprocess.Popen([sys.executable, "-c", WORKER, json.dumps(request, separators=(",", ":"))],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + float(request.get("startup_timeout", 10))
    status = None
    while time.monotonic() < deadline:
        status = read_json(status_path)
        if status and status.get("state") == "failed": break
        if status and status.get("state") == "streaming" and status.get("preview_state") != "starting": break
        if process.poll() is not None: break
        time.sleep(.1)
    if status and status.get("state") == "streaming":
        emit({"ok": True, "pid": process.pid, "status": status}); raise SystemExit(0)
    error = (status or {}).get("last_error") or "Stream worker did not confirm camera open."
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: pass
    emit({"ok": False, "error": error, "pid": process.pid, "status": status}); raise SystemExit(4)
elif action == "status":
    status = read_json(status_path)
    if status is None:
        emit({"ok": False, "error": "Stream status is unavailable.", "running": bool(pid and alive(pid))})
    else:
        status["process_alive"] = bool(pid and alive(pid)); emit({"ok": True, "status": status})
elif action == "preview_fallback":
    if not pid or not alive(pid):
        emit({"ok": False, "error": "Camera stream is not active."})
    else:
        (runtime/"preview_fallback.request").touch()
        emit({"ok": True, "requested": True})
elif action == "stop":
    if not pid or not alive(pid):
        status = read_json(status_path) or {"running": False, "state": "stopped"}
        for path in (pid_path, stop_path):
            try: path.unlink()
            except FileNotFoundError: pass
        emit({"ok": True, "already_stopped": True, "status": status}); raise SystemExit(0)
    stop_path.touch()
    deadline = time.monotonic() + float(request.get("stop_timeout", 5))
    while alive(pid) and time.monotonic() < deadline: time.sleep(.1)
    forced = False
    if alive(pid):
        os.kill(pid, signal.SIGTERM); forced = True
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline: time.sleep(.1)
    status = read_json(status_path) or {"running": False, "state": "stopped"}
    if not alive(pid):
        for path in (pid_path, stop_path):
            try: path.unlink()
            except FileNotFoundError: pass
    emit({"ok": not alive(pid), "pid": pid, "sigterm_used": forced, "status": status,
          "error": "Stream worker did not stop after SIGTERM." if alive(pid) else None})
else: emit({"ok": False, "error": "Unknown stream action."})
'''
