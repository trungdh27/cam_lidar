"""Bounded ROS process/session manager executed on the Jetson over SSH."""

JETSON_ROS_MANAGER = r'''
import json
import os
import re
import shlex
import shutil
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
equal_timestamps_valid = bool(request.get("equal_timestamps_valid", False))
include_message_timestamps = bool(request.get("include_message_timestamps", False))

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

def finite_list(values):
    return [float(value) if math.isfinite(float(value)) else None for value in values]

def numeric_values(value, field_name=""):
    if field_name in {"data"}:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        yield float(value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from numeric_values(item, field_name)
        return
    fields = getattr(value, "get_fields_and_field_types", None)
    if callable(fields):
        for name in fields():
            if name == "data":
                continue
            yield from numeric_values(getattr(value, name, None), name)

def update_extrema(record, name, values):
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return
    low, high = min(finite), max(finite)
    record[name + "_min"] = low if record.get(name + "_min") is None else min(record[name + "_min"], low)
    record[name + "_max"] = high if record.get(name + "_max") is None else max(record[name + "_max"], high)

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
        numbers = list(numeric_values(message))
        record["nan_count"] += sum(math.isnan(value) for value in numbers)
        record["inf_count"] += sum(math.isinf(value) for value in numbers)
        record["non_finite_value_count"] = record["nan_count"] + record["inf_count"]
        if hasattr(message, "k"):
            record["K"] = finite_list(message.k)
            record["D"] = finite_list(message.d)
            record["R"] = finite_list(message.r)
            record["P"] = finite_list(message.p)
            record["distortion_model"] = str(message.distortion_model)
            record["fx"] = record["K"][0] if len(record["K"]) > 0 else None
            record["fy"] = record["K"][4] if len(record["K"]) > 4 else None
            record["cx"] = record["K"][2] if len(record["K"]) > 2 else None
            record["cy"] = record["K"][5] if len(record["K"]) > 5 else None
        angular = getattr(message, "angular_velocity", None)
        if angular is not None:
            update_extrema(record, "angular_velocity", (angular.x, angular.y, angular.z))
        acceleration = getattr(message, "linear_acceleration", None)
        if acceleration is not None:
            update_extrema(record, "linear_acceleration", (acceleration.x, acceleration.y, acceleration.z))
        if hasattr(message, "orientation_covariance"):
            covariance = list(message.orientation_covariance)
            record["orientation_available"] = bool(covariance and covariance[0] != -1.0)
        if hasattr(message, "temperature"):
            update_extrema(record, "temperature", (message.temperature,))
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
        key = str(requirement.get("result_key") or topic)
        expected_type = str(requirement["message_type"])
        records[key] = {
            "topic_name": topic,
            "candidate_topics": list(requirement.get("candidate_topics") or [topic]),
            "capability": requirement.get("capability"),
            "availability": requirement.get("availability", "MANDATORY"),
            "expected_rate_hz": requirement.get("expected_rate_hz"),
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
            "K": None,
            "D": None,
            "R": None,
            "P": None,
            "distortion_model": None,
            "fx": None,
            "fy": None,
            "cx": None,
            "cy": None,
            "nan_count": 0,
            "inf_count": 0,
            "non_finite_value_count": 0,
            "angular_velocity_min": None,
            "angular_velocity_max": None,
            "linear_acceleration_min": None,
            "linear_acceleration_max": None,
            "orientation_available": None,
            "temperature_min": None,
            "temperature_max": None,
        }

    deadline = measurement_start + timeout_s
    while time.monotonic() < deadline:
        # A camera node appears in the graph before it finishes opening the
        # hardware and advertising streams.  Refresh discovery during the
        # bounded wait and subscribe as soon as each required topic appears.
        graph = {name: list(types) for name, types in node.get_topic_names_and_types()}
        for requirement in requirements:
            topic = str(requirement["topic"])
            key = str(requirement.get("result_key") or topic)
            expected_type = str(requirement["message_type"])
            candidates = list(requirement.get("candidate_topics") or [topic])
            resolved = next(
                (name for name in candidates if expected_type in graph.get(name, [])),
                next((name for name in candidates if graph.get(name)), topic),
            )
            actual_types = graph.get(resolved, [])
            record = records[key]
            record["topic_name"] = resolved
            record["exists"] = bool(actual_types)
            record["type"] = actual_types[0] if actual_types else None
            record["types"] = actual_types
            record["type_matches"] = expected_type in actual_types
            record["publisher_count"] = int(node.count_publishers(resolved))
            subscription_key = key + "\0" + resolved
            if record["type_matches"] and subscription_key not in subscribed_topics:
                message_class = get_message(expected_type)
                subscriptions.append(node.create_subscription(
                    message_class, resolved, callback_for(key), qos_profile_sensor_data
                ))
                subscribed_topics.add(subscription_key)
        rclpy.spin_once(node, timeout_sec=0.05)
        if time.monotonic() < measurement_start:
            continue
        complete = True
        for requirement in requirements:
            topic = str(requirement.get("result_key") or requirement["topic"])
            target = int(requirement.get("sample_count") or default_samples)
            if records[topic]["valid_sample_count"] < target:
                complete = False
                break
        if complete:
            break

    finished = time.monotonic()
    final_graph = {name: list(types) for name, types in node.get_topic_names_and_types()}
    for topic, record in records.items():
        resolved = record["topic_name"]
        actual_types = final_graph.get(resolved, [])
        record["exists"] = bool(actual_types)
        record["type"] = actual_types[0] if actual_types else None
        record["types"] = actual_types
        record["type_matches"] = record["expected_type"] in actual_types
        record["publisher_count"] = int(node.count_publishers(resolved))
        message_times = record.pop("message_times")
        host_times = record.pop("host_times")
        record["message_received"] = record["valid_sample_count"] > 0
        record["message_timeout_count"] = 0 if record["message_received"] else 1
        rollbacks = sum(
            current < previous if equal_timestamps_valid else current <= previous
            for previous, current in zip(message_times, message_times[1:])
        )
        record["timestamp_rollback_count"] = rollbacks
        if include_message_timestamps:
            record["message_timestamps"] = message_times
        record["first_timestamp"] = message_times[0] if message_times else None
        record["last_timestamp"] = message_times[-1] if message_times else None
        message_duration = (
            message_times[-1] - message_times[0] if len(message_times) > 1 else 0.0
        )
        host_duration = host_times[-1] - host_times[0] if len(host_times) > 1 else 0.0
        record["duration_s"] = round(message_duration, 6)
        record["measurement_duration_s"] = round(host_duration, 6)
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
        record["measured_rate_hz"] = record["host_receive_fps"]
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

QOS_PROBE = r"""
import json
import os
import subprocess
import sys
import time

import rclpy
from rclpy.qos import (
    DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
)
from rosidl_runtime_py.utilities import get_message

MARKER = "CAMERA_ROS_QOS_JSON="
request = json.loads(sys.argv[1])
requirements = request.get("topics") or []
timeout_s = max(0.1, float(request.get("timeout_s", 5)))
graph_timeout_s = max(0.1, float(request.get("graph_timeout_s", 5)))
sample_count = max(1, int(request.get("sample_count", 3)))

def enum_name(value):
    return getattr(value, "name", str(value).split(".")[-1])

def qos_dict(profile):
    if profile is None:
        return None
    def nanoseconds(value):
        return int(getattr(value, "nanoseconds", 0) or 0)
    return {
        "reliability": enum_name(profile.reliability),
        "durability": enum_name(profile.durability),
        "history": enum_name(profile.history),
        "depth": int(profile.depth),
        "liveliness": enum_name(profile.liveliness),
        "deadline_ns": nanoseconds(profile.deadline),
        "lifespan_ns": nanoseconds(profile.lifespan),
        "liveliness_lease_duration_ns": nanoseconds(profile.liveliness_lease_duration),
    }

def resolved_topic(requirement, graph):
    expected_type = str(requirement["message_type"])
    candidates = list(requirement.get("candidate_topics") or [requirement["topic"]])
    return next(
        (name for name in candidates if expected_type in graph.get(name, [])),
        next((name for name in candidates if graph.get(name)), candidates[0]),
    )

def compatible_profile(endpoints, warnings):
    offered_reliability = [item.qos_profile.reliability for item in endpoints]
    offered_durability = [item.qos_profile.durability for item in endpoints]
    if any(value == ReliabilityPolicy.BEST_EFFORT for value in offered_reliability):
        reliability = ReliabilityPolicy.BEST_EFFORT
    elif all(value == ReliabilityPolicy.RELIABLE for value in offered_reliability):
        reliability = ReliabilityPolicy.RELIABLE
    else:
        reliability = ReliabilityPolicy.BEST_EFFORT
        warnings.append("Publisher reliability contains SYSTEM_DEFAULT/UNKNOWN; using compatible conservative BEST_EFFORT request.")
    if any(value == DurabilityPolicy.VOLATILE for value in offered_durability):
        durability = DurabilityPolicy.VOLATILE
    elif all(value == DurabilityPolicy.TRANSIENT_LOCAL for value in offered_durability):
        durability = DurabilityPolicy.TRANSIENT_LOCAL
    else:
        durability = DurabilityPolicy.VOLATILE
        warnings.append("Publisher durability contains SYSTEM_DEFAULT/UNKNOWN; using compatible conservative VOLATILE request.")
    depths = [int(item.qos_profile.depth or 0) for item in endpoints]
    depth = max(1, min([value for value in depths if value > 0] or [10]))
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=reliability,
        durability=durability,
    )

def diagnostic_command(command):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=2, check=False,
        )
        return {
            "command": " ".join(command),
            "exit_code": result.returncode,
            "output": (result.stdout or result.stderr or "")[-2000:],
        }
    except Exception as exc:
        return {"command": " ".join(command), "error": type(exc).__name__ + ": " + str(exc)}

rclpy.init(args=None)
node = rclpy.create_node("cam_lidar_qos_probe_" + str(os.getpid()))
subscriptions = []
results = {}
try:
    for index, requirement in enumerate(requirements):
        key = str(requirement.get("result_key") or index)
        expected_type = str(requirement["message_type"])
        candidates = list(requirement.get("candidate_topics") or [requirement["topic"]])
        topic = candidates[0]
        graph = {}
        endpoints = []
        discovery_spin_count = 0
        discovery_started = time.monotonic()
        discovery_deadline = discovery_started + graph_timeout_s
        while time.monotonic() < discovery_deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            discovery_spin_count += 1
            graph = {
                name: list(types)
                for name, types in node.get_topic_names_and_types()
            }
            topic = resolved_topic(requirement, graph)
            endpoints = list(node.get_publishers_info_by_topic(topic))
            if endpoints:
                break
        publisher_qos = [qos_dict(item.qos_profile) for item in endpoints]
        warnings = []
        compatible = compatible_profile(endpoints, warnings) if endpoints else None
        negative = None
        negative_expected = "NOT_APPLICABLE"
        if endpoints and all(item.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT for item in endpoints):
            negative = QoSProfile(
                history=HistoryPolicy.KEEP_LAST, depth=10,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=compatible.durability,
            )
            negative_expected = "INCOMPATIBLE_NO_MESSAGES"
        elif endpoints and all(item.qos_profile.durability == DurabilityPolicy.VOLATILE for item in endpoints):
            negative = QoSProfile(
                history=HistoryPolicy.KEEP_LAST, depth=10,
                reliability=compatible.reliability,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            )
            negative_expected = "INCOMPATIBLE_NO_MESSAGES"
        record = {
            "topic": topic,
            "message_type": expected_type,
            "publisher_count": len(endpoints),
            "publisher_qos": publisher_qos,
            "qos_metadata_discovered": bool(endpoints),
            "compatible_subscriber_qos": qos_dict(compatible),
            "compatible_message_count": 0,
            "compatible_timeout": True,
            "compatible_result": "TIMEOUT",
            "negative_test_attempted": negative is not None,
            "incompatible_subscriber_qos": qos_dict(negative) if negative else None,
            "incompatible_message_count": 0,
            "negative_expected_result": negative_expected,
            "negative_actual_result": "NOT_APPLICABLE" if negative is None else "PENDING",
            "warnings": warnings,
            "graph_timeout_s": graph_timeout_s,
            "graph_discovery_duration_s": round(time.monotonic() - discovery_started, 3),
            "graph_discovery_spin_count": discovery_spin_count,
            "graph_topic_types": graph.get(topic, []),
            "probe_environment": {
                "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0"),
                "RMW_IMPLEMENTATION": os.environ.get("RMW_IMPLEMENTATION"),
                "ROS_LOCALHOST_ONLY": os.environ.get("ROS_LOCALHOST_ONLY", "0"),
            },
            "diagnostic_summary": None,
        }
        results[key] = record
        if expected_type in graph.get(topic, []) and endpoints:
            message_class = get_message(expected_type)
            subscriptions.append(node.create_subscription(
                message_class, topic,
                lambda _message, item=record: item.__setitem__(
                    "compatible_message_count", item["compatible_message_count"] + 1
                ), compatible,
            ))
            if negative is not None:
                subscriptions.append(node.create_subscription(
                    message_class, topic,
                    lambda _message, item=record: item.__setitem__(
                        "incompatible_message_count", item["incompatible_message_count"] + 1
                    ), negative,
                ))

    missing = [item for item in results.values() if not item["qos_metadata_discovered"]]
    if missing:
        topic_list_diagnostic = diagnostic_command(["ros2", "topic", "list", "-t"])
        for item in missing:
            item["diagnostic_summary"] = {
                "topic_list": topic_list_diagnostic,
                "topic_info": diagnostic_command(
                    ["ros2", "topic", "info", "-v", item["topic"]]
                ),
                "reason": "No rclpy publisher endpoints discovered before graph timeout.",
            }

    deadline = time.monotonic() + timeout_s
    while subscriptions and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
        compatible_complete = all(
            item["compatible_message_count"] >= sample_count
            for item in results.values() if item["qos_metadata_discovered"]
        )
        if compatible_complete:
            break
    for item in results.values():
        count = item["compatible_message_count"]
        item["compatible_timeout"] = count == 0
        item["compatible_result"] = "PASS" if count > 0 else "TIMEOUT"
        item["compatible_sample_target"] = sample_count
        item["compatible_sample_target_met"] = count >= sample_count
        if item["negative_test_attempted"]:
            item["negative_actual_result"] = (
                "INCOMPATIBLE_NO_MESSAGES"
                if item["incompatible_message_count"] == 0
                else "UNEXPECTED_MESSAGES"
            )
    print(MARKER + json.dumps({
        "topics": results,
        "graph_timeout_s": graph_timeout_s,
        "subscriber_timeout_s": timeout_s,
        "probe_environment": {
            "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0"),
            "RMW_IMPLEMENTATION": os.environ.get("RMW_IMPLEMENTATION"),
            "ROS_LOCALHOST_ONLY": os.environ.get("ROS_LOCALHOST_ONLY", "0"),
        },
    }, separators=(",", ":")))
finally:
    node.destroy_node()
    rclpy.shutdown()
"""


GRAPH_PROBE = r"""
import json
import os
import sys

import rclpy

MARKER = "CAMERA_ROS_GRAPH_JSON="
request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
topics = [str(item) for item in request.get("topics") or []]
rclpy.init(args=None)
node = rclpy.create_node("cam_lidar_graph_probe_" + str(os.getpid()))
try:
    # Discovery callbacks are processed directly by this probe; this does not
    # depend on the ros2 CLI daemon cache.
    for _ in range(3):
        rclpy.spin_once(node, timeout_sec=0.05)
    names = []
    for name, namespace in node.get_node_names_and_namespaces():
        full_name = name if str(name).startswith("/") else (
            str(namespace).rstrip("/") + "/" + str(name)
        )
        names.append(full_name if full_name.startswith("/") else "/" + full_name)
    publisher_counts = {
        topic: len(node.get_publishers_info_by_topic(topic)) for topic in topics
    }
    print(MARKER + json.dumps({
        "nodes": sorted(set(names)), "publisher_counts": publisher_counts,
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


def verify_owned_identity(metadata, request):
    if not metadata or metadata.get("owned_by_test") is not True:
        return False
    for key in ("session_id", "device_uid", "pid", "process_group"):
        requested = request.get(key)
        actual = metadata.get(key)
        if requested is None or str(requested) != str(actual):
            return False
    return True


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


def direct_graph_probe(environment, topics=()):
    """One direct rclpy graph sample for recovery-critical node loss checks."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", GRAPH_PROBE, json.dumps({"topics": list(topics)})], env=environment,
            capture_output=True, text=True, timeout=4, check=False,
        )
    except Exception as exc:
        return {"nodes": [], "publisher_counts": {}, "ok": False, "method": "rclpy_direct", "error": str(exc)}
    payload = None
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("CAMERA_ROS_GRAPH_JSON="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                pass
            break
    if result.returncode != 0 or payload is None:
        return {
            "nodes": [], "publisher_counts": {}, "ok": False, "method": "rclpy_direct",
            "error": (result.stderr or result.stdout or "rclpy graph probe failed")[-2000:],
        }
    return {
        "nodes": list(payload.get("nodes") or ()),
        "publisher_counts": dict(payload.get("publisher_counts") or {}), "ok": True,
        "method": "rclpy_direct", "error": None,
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


def background_runtime(session_id):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id or "")
    return ROOT / ("bag_" + safe)


def start_background(session_id, kind, command, environment, setup_files, **extra):
    ROOT.mkdir(parents=True, exist_ok=True)
    runtime = background_runtime(session_id)
    runtime.mkdir(parents=True, exist_ok=False)
    log_path = runtime / (kind + ".log")
    worker_path = runtime / "process_worker.py"
    worker_path.write_text(LAUNCH_WORKER, encoding="utf-8")
    worker_spec_path = runtime / "process_spec.json"
    worker_spec_path.write_text(json.dumps({
        "command": command, "environment": environment, "log_path": str(log_path),
    }), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(worker_path), str(worker_spec_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(0.2)
    metadata = {
        "session_id": session_id,
        "kind": kind,
        "owned_by_test": True,
        "pid": process.pid,
        "process_group": os.getpgid(process.pid),
        "process_start_ticks": process_start_ticks(process.pid),
        "log_path": str(log_path),
        "started_at": utc_now(),
        "setup_files": list(setup_files),
        "command_summary": " ".join(shlex.quote(item) for item in command),
        **extra,
    }
    (runtime / "session.json").write_text(json.dumps(metadata), encoding="utf-8")
    return metadata


def stop_background(metadata):
    if process_group_alive(metadata):
        pgid = int(metadata["process_group"])
        for sig, wait_s in ((signal.SIGINT, 4.0), (signal.SIGTERM, 1.5), (signal.SIGKILL, 0.5)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + wait_s
            while process_group_alive(metadata) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not process_group_alive(metadata):
                break
    runtime = background_runtime(metadata["session_id"])
    status = read_json(runtime / "process_status.json")
    remaining = process_group_members(metadata)
    return {
        "stopped": not remaining,
        "remaining_pids": remaining,
        "process_exit_code": status.get("exit_code"),
        "process_state": status.get("state"),
        "process_error": status.get("error"),
        "stderr_summary": bounded_log(metadata.get("log_path", "")),
    }


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

    if action == "launch_arguments":
        package_name = str(request.get("package") or "")
        launch_file = str(request.get("launch_file") or "")
        environment_info, environment = resolve_environment(
            [package_name], request.get("setup_files")
        )
        package = environment_info.get("packages", {}).get(package_name, {})
        if not environment_info.get("environment_ready") or not package.get("installed"):
            emit({"ok": False, "error_type": "ROS_DRIVER_MISSING", "error": "Launch package is unavailable", "environment": environment_info})
            raise SystemExit(0)
        code, output, error = command_result(
            ["ros2", "launch", package_name, launch_file, "--show-args"],
            environment, timeout=8,
        )
        if code != 0:
            emit({"ok": False, "error_type": "ROS_LAUNCH_ARGUMENTS_UNAVAILABLE", "error": (error or output or "ros2 launch --show-args failed")[-2000:]})
            raise SystemExit(0)
        emit({"ok": True, "launch_arguments": {
            "package": package_name, "launch_file": launch_file,
            "output": output[-8000:],
        }})
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
        stale_cli_node = False
        if spec["expected_node"] in existing_graph["nodes"]:
            direct_graph = direct_graph_probe(environment)
            # The daemon-backed CLI can retain a terminated test node.  Only
            # disregard that one CLI entry when a fresh direct rclpy query
            # proves the exact expected node is absent.  A live direct-graph
            # match continues through strict serial/external-node protection.
            if direct_graph.get("ok") and spec["expected_node"] not in direct_graph.get("nodes", []):
                stale_cli_node = True
            else:
                code, output, error = command_result(
                    ["ros2", "param", "get", spec["expected_node"], spec["serial_parameter"]],
                    environment,
                    timeout=4,
                )
                digits = re.findall(r"\d+", output)
                verified = code == 0 and spec["selected_serial"] in digits
                if not verified:
                    emit({"ok": False, "error_type": "CAMERA_BUSY", "error": "A matching ROS node exists but its physical serial cannot be verified", "nodes": existing_graph["nodes"], "direct_graph": direct_graph, "serial_probe": (error or output)[-1000:]})
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
            "stale_cli_node_ignored": stale_cli_node,
        }
        (runtime / "session.json").write_text(json.dumps(metadata), encoding="utf-8")
        emit({"ok": True, "session": metadata, "environment": environment_info})
        raise SystemExit(0)

    if action in ("status", "stop", "terminate_owned", "release_owned"):
        session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.get("session_id") or "")
        if session_id == "external":
            emit({"ok": True, "stopped": False, "external": True})
            raise SystemExit(0)
        runtime = ROOT / session_id
        metadata = read_json(runtime / "session.json")
        if not metadata:
            if action == "release_owned":
                # Release is intentionally idempotent for a session that this
                # framework already reconciled.  There is no process or file
                # left to affect, and external sessions use the early branch.
                emit({"ok": True, "released": True, "already_released": True})
                raise SystemExit(0)
            emit({"ok": False, "error_type": "ROS_NODE_EXITED", "error": "ROS session metadata was not found"})
            raise SystemExit(0)
        if not verify_owned_identity(metadata, request):
            emit({"ok": False, "error_type": "ROS_SESSION_IDENTITY_MISMATCH", "error": "The requested operation does not match the exact test-owned session identity"})
            raise SystemExit(0)
        if action == "terminate_owned":
            signal_sent = False
            if process_group_alive(metadata):
                try:
                    os.killpg(int(metadata["process_group"]), signal.SIGTERM)
                    signal_sent = True
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + min(3.0, float(request.get("remote_timeout_s") or 8))
            while process_group_alive(metadata) and time.monotonic() < deadline:
                time.sleep(0.1)
            process_status = read_json(runtime / "process_status.json")
            emit({"ok": True, "signal": "SIGTERM", "signal_sent": signal_sent, "process_alive": process_group_alive(metadata), "remaining_pids": process_group_members(metadata), "process_exit_code": process_status.get("exit_code"), "stderr_summary": bounded_log(metadata.get("log_path", "")), "session": metadata})
            raise SystemExit(0)
        if action == "release_owned":
            remaining = process_group_members(metadata)
            if remaining:
                emit({"ok": False, "error_type": "ROS_ORPHAN_PROCESS_DETECTED", "error": "Cannot release an owned session while its process group is alive", "remaining_pids": remaining})
                raise SystemExit(0)
            diagnostics = {"session": metadata, "process_status": read_json(runtime / "process_status.json"), "stderr_summary": bounded_log(metadata.get("log_path", ""))}
            shutil.rmtree(runtime)
            emit({"ok": True, "released": True, **diagnostics})
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

    if action == "audit_owned":
        environment_info, environment = resolve_environment([], request.get("setup_files"))
        expected_topics = [str(item) for item in request.get("expected_topics") or []]
        current_graph = direct_graph_probe(environment, expected_topics) if environment is not None else {
            "nodes": [], "ok": False, "method": "rclpy_direct",
            "error": "ROS environment unavailable for graph probe",
        }
        requested_sessions = request.get("sessions") or []
        session_states = {}
        for identity in requested_sessions:
            session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", identity.get("session_id") or "")
            runtime = ROOT / session_id
            metadata = read_json(runtime / "session.json")
            matches = verify_owned_identity(metadata, identity)
            members = process_group_members(metadata) if matches else []
            session_states[session_id] = {
                "metadata_present": bool(metadata),
                "identity_matches": matches,
                "process_alive": bool(members),
                "remaining_pids": members,
            }
        active_records = []
        runtime_count = 0
        if ROOT.exists():
            for runtime in ROOT.iterdir():
                if not runtime.is_dir() or runtime.name.startswith("bag_"):
                    continue
                metadata = read_json(runtime / "session.json")
                if metadata.get("owned_by_test") is True:
                    runtime_count += 1
                    if process_group_alive(metadata):
                        active_records.append(metadata.get("session_id"))
        expected_nodes = [str(item) for item in request.get("expected_nodes") or []]
        nodes = current_graph.get("nodes", [])
        emit({"ok": True, "audit": {
            "session_states": session_states,
            "active_owned_session_ids": sorted(active_records),
            "active_owned_process_count": len(active_records),
            "owned_runtime_directory_count": runtime_count,
            "node_names": nodes,
            "expected_nodes_present": [item for item in expected_nodes if item in nodes],
            "publisher_counts": current_graph.get("publisher_counts") or {},
            "graph_probe_ok": bool(current_graph.get("ok")),
            "graph_probe_method": current_graph.get("method"),
            "graph_probe_error": current_graph.get("error"),
            "environment": environment_info,
        }})
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
            "equal_timestamps_valid": request.get("equal_timestamps_valid", False),
            "include_message_timestamps": request.get("include_message_timestamps", False),
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

    if action == "qos":
        environment_info, environment = resolve_environment(
            request.get("required_packages") or [], request.get("setup_files")
        )
        if environment is None or not environment_info.get("environment_ready"):
            emit({"ok": False, "error_type": "ROS_ENVIRONMENT_UNAVAILABLE", "error": "ROS environment unavailable for QoS probe"})
            raise SystemExit(0)
        probe_request = {
            "topics": request.get("topics") or [],
            "timeout_s": request.get("timeout_s", 5),
            "graph_timeout_s": request.get("graph_timeout_s", 5),
            "sample_count": request.get("sample_count", 3),
        }
        topic_count = max(1, len(probe_request["topics"]))
        timeout = (
            float(probe_request["timeout_s"])
            + float(probe_request["graph_timeout_s"]) * topic_count
            + 4 * topic_count
            + 5
        )
        result = subprocess.run(
            [sys.executable, "-c", QOS_PROBE, json.dumps(probe_request, separators=(",", ":"))],
            env=environment, capture_output=True, text=True, timeout=timeout, check=False,
        )
        payload = None
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("CAMERA_ROS_QOS_JSON="):
                payload = json.loads(line.split("=", 1)[1])
                break
        if result.returncode != 0 or payload is None:
            emit({"ok": False, "error_type": "ROS_QOS_DISCOVERY_FAILED", "error": (result.stderr or result.stdout or "QoS probe returned no structured result")[-2000:]})
            raise SystemExit(0)
        emit({"ok": True, "qos": payload, "environment": environment_info})
        raise SystemExit(0)

    if action == "bag_preflight":
        environment_info, environment = resolve_environment([], request.get("setup_files"))
        if environment is None or not environment_info.get("environment_ready"):
            emit({"ok": False, "error_type": "ROS_ENVIRONMENT_UNAVAILABLE", "error": "ROS environment unavailable for rosbag"})
            raise SystemExit(0)
        record_code, record_help, record_error = command_result(
            ["ros2", "bag", "record", "--help"], environment, timeout=6
        )
        play_code, play_help, play_error = command_result(
            ["ros2", "bag", "play", "--help"], environment, timeout=6
        )
        available = record_code == 0 and play_code == 0
        emit({"ok": True, "preflight": {
            "available": available,
            "record_available": record_code == 0,
            "play_available": play_code == 0,
            "supports_remap": "--remap" in play_help,
            "default_storage": "installed_default",
            "errors": [item for item in (record_error, play_error) if item] if not available else [],
        }, "environment": environment_info})
        raise SystemExit(0)

    if action == "bag_record_start":
        environment_info, environment = resolve_environment([], request.get("setup_files"))
        if environment is None or not environment_info.get("environment_ready"):
            emit({"ok": False, "error_type": "ROSBAG_UNAVAILABLE", "error": "ROS environment unavailable for recorder"})
            raise SystemExit(0)
        topics = [str(item) for item in request.get("topics") or [] if str(item).startswith("/")]
        if not topics:
            emit({"ok": False, "error_type": "ROSBAG_RECORD_FAILED", "error": "No resolved ROS topics were supplied"})
            raise SystemExit(0)
        session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.get("session_id") or uuid.uuid4().hex)
        bag_path = background_runtime(session_id) / "bag"
        command = ["ros2", "bag", "record", "--output", str(bag_path), *topics]
        metadata = start_background(
            session_id, "record", command, environment,
            environment_info.get("setup_files") or (), bag_path=str(bag_path), topics=topics,
        )
        emit({"ok": True, "session": metadata, "environment": environment_info})
        raise SystemExit(0)

    if action in ("bag_status", "bag_stop"):
        runtime = background_runtime(request.get("session_id") or "")
        metadata = read_json(runtime / "session.json")
        if not metadata:
            emit({"ok": False, "error_type": "ROSBAG_RECORD_FAILED", "error": "Owned rosbag session metadata was not found"})
            raise SystemExit(0)
        if action == "bag_stop":
            emit({"ok": True, **stop_background(metadata), "session": metadata})
        else:
            status = read_json(runtime / "process_status.json")
            emit({"ok": True, "status": {
                "process_alive": process_group_alive(metadata),
                "process_exit_code": status.get("exit_code"),
                "process_state": status.get("state"),
                "process_error": status.get("error"),
                "stderr_summary": bounded_log(metadata.get("log_path", "")),
            }, "session": metadata})
        raise SystemExit(0)

    if action == "bag_inspect":
        bag_path = Path(str(request.get("bag_path") or ""))
        metadata_path = bag_path / "metadata.yaml"
        readable = False
        metadata_error = None
        metadata = {}
        try:
            import yaml
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
            readable = True
        except Exception as exc:
            metadata_error = type(exc).__name__ + ": " + str(exc)
        info = metadata.get("rosbag2_bagfile_information") or metadata
        topic_rows = info.get("topics_with_message_count") or []
        counts = {}
        types = {}
        for row in topic_rows:
            topic_metadata = row.get("topic_metadata") or {}
            name = str(topic_metadata.get("name") or "")
            if name:
                counts[name] = int(row.get("message_count") or 0)
                types[name] = topic_metadata.get("type")
        duration = info.get("duration") or {}
        duration_ns = duration.get("nanoseconds", 0) if isinstance(duration, dict) else 0
        storage_files = sorted(
            str(path) for path in bag_path.glob("*")
            if path.is_file() and path.name != "metadata.yaml"
        ) if bag_path.is_dir() else []
        bag_size = sum(
            path.stat().st_size for path in bag_path.rglob("*") if path.is_file()
        ) if bag_path.is_dir() else 0
        emit({"ok": True, "inspection": {
            "bag_path": str(bag_path),
            "bag_directory_exists": bag_path.is_dir(),
            "metadata_path": str(metadata_path),
            "metadata_exists": metadata_path.is_file(),
            "metadata_readable": readable,
            "metadata_error": metadata_error,
            "storage_identifier": info.get("storage_identifier"),
            "storage_files": storage_files,
            "bag_size_bytes": bag_size,
            "record_duration_s": round(float(duration_ns) / 1000000000.0, 6),
            "topic_count": len(counts),
            "message_count_by_topic": counts,
            "message_type_by_topic": types,
            "recorded_topics": sorted(counts),
        }})
        raise SystemExit(0)

    if action == "bag_replay_start":
        environment_info, environment = resolve_environment([], request.get("setup_files"))
        if environment is None or not environment_info.get("environment_ready"):
            emit({"ok": False, "error_type": "ROSBAG_REPLAY_FAILED", "error": "ROS environment unavailable for replay"})
            raise SystemExit(0)
        bag_path = str(request.get("bag_path") or "")
        remappings = dict(request.get("remappings") or {})
        session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.get("session_id") or uuid.uuid4().hex)
        command = ["ros2", "bag", "play", bag_path]
        if remappings:
            command.extend(["--remap", *[source + ":=" + target for source, target in remappings.items()]])
        metadata = start_background(
            session_id, "replay", command, environment,
            environment_info.get("setup_files") or (), bag_path=bag_path,
            topics=list(request.get("topics") or ()), remappings=remappings,
        )
        emit({"ok": True, "session": metadata, "environment": environment_info})
        raise SystemExit(0)

    emit({"ok": False, "error_type": "ROS_PROBE_ERROR", "error": "Unsupported ROS manager action: " + str(action)})
except SystemExit:
    raise
except Exception as exc:
    emit({"ok": False, "error_type": "ROS_PROBE_ERROR", "error": type(exc).__name__ + ": " + str(exc)})
'''
