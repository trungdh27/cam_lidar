"""Read-only production ROS camera discovery script executed on the Jetson."""

JETSON_ROS_CAMERA_PROBE = r'''
import json
import re
import subprocess

MARKER = "CAMERA_ROS_PRODUCTION_JSON="


def run(args, timeout=4):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def topic_rows():
    output = run(["ros2", "topic", "list", "-t"], 5)
    rows = []
    for line in output.splitlines():
        match = re.match(r"^(\S+)\s+\[([^]]+)\]$", line.strip())
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


def scalar_fields(text):
    # ``ros2 topic echo --once`` uses YAML. Device info is intentionally parsed
    # as scalar fields only, keeping this probe dependency-free and bounded.
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip().strip("\\\"").strip("'")
        if value and value not in {"[]", "{}"}:
            values[key] = value
    return values


def score_owner(node, topic):
    node_tokens = set(re.findall(r"[a-z0-9]+", node.lower()))
    topic_tokens = set(re.findall(r"[a-z0-9]+", topic.lower()))
    return len(node_tokens & topic_tokens)


def choose_rgb(info_topic, topics):
    base = info_topic.rsplit("/", 1)[0]
    images = [name for name, kind in topics if kind == "sensor_msgs/msg/Image"]
    preferred = ("/rgb", "/image_raw", "/image", "/color/image_raw")
    for suffix in preferred:
        candidate = base + suffix
        if candidate in images:
            return candidate
    related = [name for name in images if name.startswith(base + "/")]
    return related[0] if len(related) == 1 else (related[0] if related else None)


def image_health(topic):
    # A topic name alone is not a live stream.  This is a bounded message sample.
    output = echo_once(topic, "header")
    return bool(output.strip())


def echo_once(topic, field=None):
    command = ["timeout", "3", "ros2", "topic", "echo", topic, "--once"]
    if field:
        command.extend(["--field", field])
    output = run(command, 4)
    if output.strip():
        return output
    # Camera publishers commonly use sensor-data QoS. Best effort remains
    # compatible with reliable publishers and lets graph evidence survive a
    # default-reliability mismatch.
    command.extend(["--qos-reliability", "best_effort"])
    return run(command, 4)


def main():
    nodes = [line.strip() for line in run(["ros2", "node", "list"], 5).splitlines() if line.strip()]
    topics = topic_rows()
    device_topics = [name for name, _kind in topics if name.endswith("/device_info")]
    devices, warnings = [], []
    for topic in device_topics[:8]:
        fields = scalar_fields(echo_once(topic))
        rgb_topic = choose_rgb(topic, topics)
        # Retain incomplete candidates if graph evidence still strongly says camera.
        if not fields and not rgb_topic:
            continue
        owner = max(nodes, key=lambda node: score_owner(node, topic), default="")
        if owner and score_owner(owner, topic) == 0:
            owner = ""
        device_type = fields.get("device_type", "").lower()
        if device_type and device_type != "camera":
            continue
        active = image_health(rgb_topic) if rgb_topic else False
        if rgb_topic and not active:
            warnings.append("ROS RGB topic exists but no message was received: " + rgb_topic)
        devices.append({
            "vendor": fields.get("vendor", "Unknown"),
            "model": fields.get("model", "ROS Camera"),
            "serial_number": fields.get("serial_number", ""),
            "driver_name": fields.get("driver_name"),
            "driver_version": fields.get("driver_version"),
            "input_type": fields.get("input_type"),
            "resolution": fields.get("resolution"),
            "target_fps": fields.get("target_fps"),
            "firmware_version": fields.get("firmware_version"),
            "sensors_firmware_version": fields.get("sensors_firmware_version"),
            "variant": fields.get("variant"),
            "capabilities": fields.get("capabilities"),
            "device_info_topic": topic,
            "rgb_topic": rgb_topic,
            "ros_node": owner or None,
            "ros_camera_name": topic.rsplit("/", 1)[0].rsplit("/", 1)[-1],
            "stream_active": active,
            "metadata_complete": bool(fields),
            "warnings": ([] if fields else ["ROS device_info metadata unavailable; graph evidence retained."]),
        })
    print(MARKER + json.dumps({"ok": True, "devices": devices, "warnings": warnings}, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(MARKER + json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")))
'''
