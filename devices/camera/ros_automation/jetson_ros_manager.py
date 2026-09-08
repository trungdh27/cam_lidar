"""Bounded ROS process/session manager executed on the Jetson over SSH."""

JETSON_ROS_MANAGER = r'''
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MARKER = "CAMERA_ROS_JSON="
ROOT = Path("/tmp/cam_lidar/ros_camera")
MAX_LOG_BYTES = 262144

LAUNCH_WORKER = r"""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
spec = json.loads(spec_path.read_text(encoding="utf-8"))
status_path = spec_path.parent / "process_status.json"
log_path = Path(spec["log_path"])
child = None

def write_status(state, exit_code=None, error=None):
    temporary = status_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "state": state,
        "exit_code": exit_code,
        "error": error,
    }, separators=(",", ":")), encoding="utf-8")
    os.replace(str(temporary), str(status_path))

def forward(signum, _frame):
    if child is not None and child.poll() is None:
        child.send_signal(signum)

signal.signal(signal.SIGINT, forward)
signal.signal(signal.SIGTERM, forward)
write_status("starting")
try:
    with log_path.open("ab", buffering=0) as log_file:
        child = subprocess.Popen(
            spec["command"],
            env=spec["environment"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        write_status("running")
        exit_code = child.wait()
    write_status("exited", exit_code=exit_code)
except BaseException as exc:
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
    write_status("failed", error=type(exc).__name__ + ": " + str(exc))
"""

COLLECTOR = r"""
import json
import math
import os
import sys
import time

import rclpy
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.utilities import get_message

MARKER = "CAMERA_ROS_COLLECT_JSON="
request = json.loads(sys.argv[1])
requirements = request.get("topics") or []
warmup_s = max(0.0, float(request.get("warmup_s", 0)))
timeout_s = max(0.1, float(request.get("timeout_s", 5)))
default_samples = max(1, int(request.get("sample_count", 1)))

rclpy.init(args=None)
node = rclpy.create_node("cam_lidar_bounded_probe_" + str(os.getpid()))
started = time.monotonic()
measurement_start = started + warmup_s
records = {}
subscriptions = []
subscribed_topics = set()

def stamp_value(message):
    header = getattr(message, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return None
    value = float(stamp.sec) + float(stamp.nanosec) / 1000000000.0
    return value if value > 0 else None

def callback_for(topic):
    def callback(message):
        received = time.monotonic()
        if received < measurement_start:
            return
        record = records[topic]
        record["sample_count"] += 1
        record["host_times"].append(received)
        stamp = stamp_value(message)
        if stamp is not None:
            record["message_times"].append(stamp)
        if hasattr(message, "width"):
            record["width"] = int(message.width)
        if hasattr(message, "height"):
            record["height"] = int(message.height)
        if hasattr(message, "encoding"):
            record["encoding"] = str(message.encoding)
        if hasattr(message, "step"):
            record["step"] = int(message.step)
        header = getattr(message, "header", None)
        if header is not None:
            record["frame_id"] = str(getattr(header, "frame_id", ""))
        if hasattr(message, "width"):
            valid = int(getattr(message, "width", 0)) > 0 and int(getattr(message, "height", 0)) > 0
            if hasattr(message, "encoding"):
                valid = valid and bool(getattr(message, "encoding", "")) and int(getattr(message, "step", 0)) > 0
            if valid:
                record["valid_sample_count"] += 1
            else:
                record["invalid_sample_count"] += 1
        else:
            record["valid_sample_count"] += 1
    return callback

try:
    for requirement in requirements:
        topic = str(requirement["topic"])
        expected_type = str(requirement["message_type"])
        records[topic] = {
            "exists": False,
            "expected_type": expected_type,
            "type": None,
            "types": [],
            "type_matches": False,
            "publisher_count": 0,
            "message_received": False,
            "sample_count": 0,
            "valid_sample_count": 0,
            "invalid_sample_count": 0,
            "host_times": [],
            "message_times": [],
            "width": None,
            "height": None,
            "encoding": None,
            "step": None,
            "frame_id": None,
        }

    deadline = measurement_start + timeout_s
    while time.monotonic() < deadline:
        # A camera node appears in the graph before it finishes opening the
        # hardware and advertising streams.  Refresh discovery during the
        # bounded wait and subscribe as soon as each required topic appears.
        graph = {name: list(types) for name, types in node.get_topic_names_and_types()}
        for requirement in requirements:
            topic = str(requirement["topic"])
            expected_type = str(requirement["message_type"])
            actual_types = graph.get(topic, [])
            record = records[topic]
            record["exists"] = bool(actual_types)
            record["type"] = actual_types[0] if actual_types else None
            record["types"] = actual_types
            record["type_matches"] = expected_type in actual_types
            record["publisher_count"] = int(node.count_publishers(topic))
            if record["type_matches"] and topic not in subscribed_topics:
                message_class = get_message(expected_type)
                subscriptions.append(node.create_subscription(
                    message_class, topic, callback_for(topic), qos_profile_sensor_data
                ))
                subscribed_topics.add(topic)
        rclpy.spin_once(node, timeout_sec=0.05)
        if time.monotonic() < measurement_start:
            continue
        complete = True
        for requirement in requirements:
            topic = str(requirement["topic"])
            target = int(requirement.get("sample_count") or default_samples)
            if records[topic]["valid_sample_count"] < target:
                complete = False
                break
        if complete:
            break

    finished = time.monotonic()
    final_graph = {name: list(types) for name, types in node.get_topic_names_and_types()}
    for topic, record in records.items():
        actual_types = final_graph.get(topic, [])
        record["exists"] = bool(actual_types)
        record["type"] = actual_types[0] if actual_types else None
        record["types"] = actual_types
        record["type_matches"] = record["expected_type"] in actual_types
        record["publisher_count"] = int(node.count_publishers(topic))
        message_times = record.pop("message_times")
        host_times = record.pop("host_times")
        record["message_received"] = record["valid_sample_count"] > 0
        rollbacks = sum(
            current <= previous
            for previous, current in zip(message_times, message_times[1:])
        )
        record["timestamp_rollback_count"] = rollbacks
        record["first_timestamp"] = message_times[0] if message_times else None
        record["last_timestamp"] = message_times[-1] if message_times else None
        message_duration = (
            message_times[-1] - message_times[0] if len(message_times) > 1 else 0.0
        )
        host_duration = host_times[-1] - host_times[0] if len(host_times) > 1 else 0.0
        record["duration_s"] = round(message_duration, 6)
        record["calculated_fps"] = round(
            (len(message_times) - 1) / message_duration
            if message_duration > 0 and rollbacks == 0
            else 0.0,
            3,
        )
        record["host_receive_fps"] = round(
            (len(host_times) - 1) / host_duration if host_duration > 0 else 0.0,
            3,
        )
    print(MARKER + json.dumps({
        "ok": True,
        "topics": records,
        "node_names": sorted(node_name for node_name, _namespace in node.get_node_names_and_namespaces()),
        "collection_duration_s": round(finished - started, 3),
    }, separators=(",", ":")))
finally:
    node.destroy_node()
    rclpy.shutdown()
"""


def emit(payload):
    print(MARKER + json.dumps(payload, separators=(",", ":")))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def shell_environment(setup_files):
    commands = ["source " + shlex.quote(path) for path in setup_files]
    commands.append("env -0")
    result = subprocess.run(
        ["bash", "-lc", "; ".join(commands)],
        capture_output=True,
        timeout=8,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    environment = {}
    for item in result.stdout.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            environment[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environment


def command_result(command, environment, timeout=6):
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def package_details(package, environment):
    code, output, error = command_result(["ros2", "pkg", "prefix", package], environment)
    prefix = output if code == 0 and output else None
    launch_files = []
    if prefix:
        launch_dir = Path(prefix) / "share" / package / "launch"
        if launch_dir.is_dir():
            launch_files = sorted(path.name for path in launch_dir.glob("*.py"))
    return {
        "installed": code == 0,
        "prefix": prefix,
        "launch_files": launch_files,
        "error": None if code == 0 else (error or output or "Package not found"),
    }


def workspace_candidates():
    home = Path.home()
    candidates = []
    for pattern in ("*/install/setup.bash", "*/*/install/setup.bash"):
        for path in home.glob(pattern):
            text = str(path)
            if text not in candidates:
                candidates.append(text)
    return sorted(candidates)


def resolve_environment(required_packages, preferred_setup_files=None, expected_distro="humble"):
    system_candidates = sorted(Path("/opt/ros").glob("*/setup.bash"))
    expected = Path("/opt/ros") / expected_distro / "setup.bash"
    if expected.is_file():
        system_candidates = [expected] + [item for item in system_candidates if item != expected]
    warnings = []
    if not system_candidates:
        return {
            "ros2_available": False,
            "ros_distro": None,
            "ros_environment_loaded": False,
            "environment_ready": False,
            "setup_files": [],
            "workspace_setup": None,
            "packages": {name: {"installed": False, "prefix": None, "launch_files": [], "error": "ROS setup unavailable"} for name in required_packages},
            "warnings": warnings,
            "errors": ["No /opt/ros/*/setup.bash was found"],
        }, None

    preferred = [
        str(path) for path in (preferred_setup_files or []) if Path(path).is_file()
    ]
    base = str(system_candidates[0])
    setup_options = []
    if preferred:
        setup_options.append(preferred)
    setup_options.append([base])
    setup_options.extend([base, candidate] for candidate in workspace_candidates())
    best = None
    best_environment = None
    best_rank = (-1, -1)
    seen = set()
    for setups in setup_options:
        setups = tuple(dict.fromkeys(setups))
        if setups in seen:
            continue
        seen.add(setups)
        try:
            environment = shell_environment(setups)
        except Exception as exc:
            warnings.append("Unable to source " + ", ".join(setups) + ": " + str(exc))
            continue
        if not environment.get("PATH") or not shutil_which("ros2", environment.get("PATH")):
            continue
        packages = {name: package_details(name, environment) for name in required_packages}
        score = sum(item["installed"] for item in packages.values())
        install_roots = [str(Path(item).parent) for item in setups[1:]]
        direct_packages = sum(
            bool(item.get("prefix"))
            and any(str(item["prefix"]).startswith(root + os.sep) for root in install_roots)
            for item in packages.values()
        )
        rank = (score, direct_packages)
        if rank > best_rank:
            best = (setups, packages)
            best_environment = environment
            best_rank = rank

    if best is None:
        return {
            "ros2_available": False,
            "ros_distro": None,
            "ros_environment_loaded": False,
            "environment_ready": False,
            "setup_files": [],
            "workspace_setup": None,
            "packages": {},
            "warnings": warnings,
            "errors": ["ROS setup files did not produce an executable ros2 CLI"],
        }, None
    setups, packages = best
    code, help_output, help_error = command_result(["ros2", "--help"], best_environment)
    distro = best_environment.get("ROS_DISTRO")
    errors = []
    if code != 0:
        errors.append(help_error or "ros2 --help failed")
    result = {
        "ros2_available": code == 0,
        "ros_distro": distro,
        "ros_version_output": help_output.splitlines()[0] if help_output else None,
        "ros_environment_loaded": bool(distro),
        "environment_ready": code == 0 and bool(distro) and not errors,
        "setup_files": list(setups),
        "workspace_setup": setups[-1] if len(setups) > 1 else None,
        "packages": packages,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": errors,
    }
    return result, best_environment


def shutil_which(executable, path):
    for directory in str(path or "").split(os.pathsep):
        candidate = Path(directory) / executable
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def process_start_ticks(pid):
    try:
        return Path("/proc").joinpath(str(pid), "stat").read_text().split()[21]
    except Exception:
        return None


def process_alive(metadata):
    pid = int(metadata.get("pid") or 0)
    if pid <= 0 or process_start_ticks(pid) != str(metadata.get("process_start_ticks")):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_group_members(metadata):
    """Return live members of this test-created process group.

    ros2 launch can exit before a component-container grandchild does.  Looking
    only at the launch-worker PID would then incorrectly report successful
    cleanup even though the owned process group still contains a ROS node.
    """
    pgid = int(metadata.get("process_group") or 0)
    original_ticks = int(metadata.get("process_start_ticks") or 0)
    if pgid <= 0 or original_ticks <= 0:
        return []
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if os.getpgid(pid) != pgid:
                continue
            stat = (entry / "stat").read_text().split()
            state = stat[2]
            start_ticks = int(stat[21])
            if state != "Z" and start_ticks >= original_ticks:
                members.append(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    return members


def process_group_alive(metadata):
    return bool(process_group_members(metadata))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def graph(environment):
    node_code, node_output, node_error = command_result(["ros2", "node", "list"], environment)
    topic_code, topic_output, topic_error = command_result(["ros2", "topic", "list", "-t"], environment)
    nodes = sorted(line.strip() for line in node_output.splitlines() if line.strip())
    topics = {}
    for line in topic_output.splitlines():
        match = re.match(r"^(\S+)\s+\[(.+)\]$", line.strip())
        if match:
            topics[match.group(1)] = [item.strip() for item in match.group(2).split(",")]
    return {
        "nodes": nodes,
        "topics": topics,
        "node_query_error": None if node_code == 0 else node_error,
        "topic_query_error": None if topic_code == 0 else topic_error,
    }


def bounded_log(path, limit=8192):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            text = stream.read().decode("utf-8", "replace")
        return re.sub(r"\x1b\[[0-9;]*m", "", text)[-limit:]
    except OSError:
        return ""


def trim_log(path):
    target = Path(path)
    try:
        if target.stat().st_size <= MAX_LOG_BYTES:
            return
        with target.open("rb") as stream:
            stream.seek(-MAX_LOG_BYTES, os.SEEK_END)
            data = stream.read()
        target.write_bytes(data)
    except OSError:
        pass


request = json.loads(sys.argv[1])
action = request.get("action")
try:
    if action == "environment":
        environment_info, _environment = resolve_environment(
            request.get("required_packages") or [],
            request.get("setup_files"),
            request.get("expected_distro") or "humble",
        )
        emit({"ok": True, "environment": environment_info})
        raise SystemExit(0)

    if action == "start":
        spec = request["launch_spec"]
        environment_info, environment = resolve_environment(
            [spec["package"]], request.get("setup_files")
        )
        package = environment_info.get("packages", {}).get(spec["package"], {})
        if not environment_info.get("environment_ready"):
            emit({"ok": False, "error_type": "ROS_ENVIRONMENT_UNAVAILABLE", "error": "; ".join(environment_info.get("errors") or ["ROS environment unavailable"]), "environment": environment_info})
            raise SystemExit(0)
        if not package.get("installed"):
            emit({"ok": False, "error_type": "ROS_DRIVER_MISSING", "error": spec["package"] + " is not installed", "environment": environment_info})
            raise SystemExit(0)
        if spec["launch_file"] not in package.get("launch_files", []):
            emit({"ok": False, "error_type": "ROS_LAUNCH_FAILED", "error": "Launch file not installed: " + spec["launch_file"], "environment": environment_info})
            raise SystemExit(0)

        existing_graph = graph(environment)
        if spec["expected_node"] in existing_graph["nodes"]:
            code, output, error = command_result(
                ["ros2", "param", "get", spec["expected_node"], spec["serial_parameter"]],
                environment,
                timeout=4,
            )
            digits = re.findall(r"\d+", output)
            verified = code == 0 and spec["selected_serial"] in digits
            if not verified:
                emit({"ok": False, "error_type": "CAMERA_BUSY", "error": "A matching ROS node exists but its physical serial cannot be verified", "nodes": existing_graph["nodes"], "serial_probe": (error or output)[-1000:]})
                raise SystemExit(0)
            emit({"ok": True, "session": {
                "session_id": "external",
                "device_uid": request["device_uid"],
                "driver": spec["driver"],
                "namespace": spec["namespace"],
                "expected_node": spec["expected_node"],
                "selected_serial": spec["selected_serial"],
                "owned_by_test": False,
                "setup_files": environment_info["setup_files"],
                "launch_command_summary": "Existing verified node",
            }, "environment": environment_info})
            raise SystemExit(0)

        ROOT.mkdir(parents=True, exist_ok=True)
        session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.get("session_id") or uuid.uuid4().hex)
        runtime = ROOT / session_id
        runtime.mkdir(parents=True, exist_ok=False)
        log_path = runtime / "launch.log"
        worker_path = runtime / "launch_worker.py"
        worker_path.write_text(LAUNCH_WORKER, encoding="utf-8")
        command = list(spec["command"])
        worker_spec = {
            "command": command,
            "environment": environment,
            "log_path": str(log_path),
        }
        worker_spec_path = runtime / "launch_spec.json"
        worker_spec_path.write_text(json.dumps(worker_spec), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(worker_path), str(worker_spec_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        metadata = {
            "session_id": session_id,
            "device_uid": request["device_uid"],
            "driver": spec["driver"],
            "namespace": spec["namespace"],
            "expected_node": spec["expected_node"],
            "selected_serial": spec["selected_serial"],
            "serial_parameter": spec["serial_parameter"],
            "owned_by_test": True,
            "pid": process.pid,
            "process_group": os.getpgid(process.pid),
            "process_start_ticks": process_start_ticks(process.pid),
            "log_path": str(log_path),
            "started_at": utc_now(),
            "setup_files": environment_info["setup_files"],
            "launch_command_summary": " ".join(shlex.quote(item) for item in command),
        }
        (runtime / "session.json").write_text(json.dumps(metadata), encoding="utf-8")
        emit({"ok": True, "session": metadata, "environment": environment_info})
        raise SystemExit(0)

    if action in ("status", "stop"):
        session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.get("session_id") or "")
        if session_id == "external":
            emit({"ok": True, "stopped": False, "external": True})
            raise SystemExit(0)
        runtime = ROOT / session_id
        metadata = read_json(runtime / "session.json")
        if not metadata:
            emit({"ok": False, "error_type": "ROS_NODE_EXITED", "error": "ROS session metadata was not found"})
            raise SystemExit(0)
        if action == "stop":
            alive = process_group_alive(metadata)
            if alive:
                pgid = int(metadata["process_group"])
                for sig, wait_s in ((signal.SIGINT, 5.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
                    try:
                        os.killpg(pgid, sig)
                    except ProcessLookupError:
                        break
                    deadline = time.monotonic() + wait_s
                    while process_group_alive(metadata) and time.monotonic() < deadline:
                        time.sleep(0.1)
                    if not process_group_alive(metadata):
                        break
            trim_log(metadata.get("log_path", ""))
            remaining_pids = process_group_members(metadata)
            emit({"ok": True, "stopped": not remaining_pids, "remaining_pids": remaining_pids, "session": metadata, "process_status": read_json(runtime / "process_status.json"), "stderr_summary": bounded_log(metadata.get("log_path", ""))})
            raise SystemExit(0)
        environment_info, environment = resolve_environment(
            [metadata["driver"]], metadata.get("setup_files")
        )
        current_graph = graph(environment) if environment is not None else {"nodes": [], "topics": {}}
        node_alive = metadata["expected_node"] in current_graph.get("nodes", [])
        detected_serial = None
        serial_verified = False
        serial_probe = None
        if node_alive and environment is not None:
            code, output, error = command_result(
                ["ros2", "param", "get", metadata["expected_node"], metadata["serial_parameter"]],
                environment,
                timeout=4,
            )
            digits = re.findall(r"\d+", output)
            if digits:
                detected_serial = digits[-1]
            serial_verified = code == 0 and metadata["selected_serial"] in digits
            if not serial_verified:
                serial_probe = (error or output or "Serial parameter unavailable")[-1000:]
        process_status = read_json(runtime / "process_status.json")
        emit({"ok": True, "status": {
            "process_alive": process_group_alive(metadata),
            "node_alive": node_alive,
            "node_names": current_graph.get("nodes", []),
            "detected_serial": detected_serial,
            "serial_verified": serial_verified,
            "serial_probe": serial_probe,
            "topics": current_graph.get("topics", {}),
            "process_exit_code": process_status.get("exit_code"),
            "process_state": process_status.get("state"),
            "process_error": process_status.get("error"),
            "stderr_summary": bounded_log(metadata.get("log_path", "")),
        }, "environment": environment_info})
        raise SystemExit(0)

    if action == "collect":
        environment_info, environment = resolve_environment(
            request.get("required_packages") or [], request.get("setup_files")
        )
        if environment is None or not environment_info.get("environment_ready"):
            emit({"ok": False, "error_type": "ROS_ENVIRONMENT_UNAVAILABLE", "error": "ROS environment unavailable for collector", "environment": environment_info})
            raise SystemExit(0)
        collector_request = {
            "topics": request.get("topics") or [],
            "warmup_s": request.get("warmup_s", 0),
            "timeout_s": request.get("timeout_s", 5),
            "sample_count": request.get("sample_count", 1),
        }
        timeout = float(collector_request["warmup_s"]) + float(collector_request["timeout_s"]) + 5
        result = subprocess.run(
            [sys.executable, "-c", COLLECTOR, json.dumps(collector_request, separators=(",", ":"))],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        payload = None
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("CAMERA_ROS_COLLECT_JSON="):
                payload = json.loads(line.split("=", 1)[1])
                break
        if result.returncode != 0 or payload is None:
            emit({"ok": False, "error_type": "ROS_TOPIC_TIMEOUT", "error": (result.stderr or result.stdout or "ROS collector returned no structured result")[-2000:]})
            raise SystemExit(0)
        emit({"ok": True, "collection": payload, "environment": environment_info})
        raise SystemExit(0)

    emit({"ok": False, "error_type": "ROS_PROBE_ERROR", "error": "Unsupported ROS manager action: " + str(action)})
except SystemExit:
    raise
except Exception as exc:
    emit({"ok": False, "error_type": "ROS_PROBE_ERROR", "error": type(exc).__name__ + ": " + str(exc)})
'''
