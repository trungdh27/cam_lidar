"""Bounded ROS Image sampler used by the monitor's read-only mode."""

JETSON_ROS_CAMERA_MONITOR = r'''
import base64
import json
import sys
import time

request = json.loads(sys.argv[1])
marker = "CAMERA_ROS_MONITOR_JSON="

try:
    import cv2
    import numpy as np
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from rosidl_runtime_py.utilities import get_message

    result = {"received": False, "frame_count": 0, "first_received_at": None, "last_received_at": None}
    rclpy.init(args=None)
    node = rclpy.create_node("cam_lidar_ros_monitor_" + str(__import__("os").getpid()))
    message_type = get_message(request["message_type"])

    def callback(message):
        received_at = time.monotonic()
        width, height = int(message.width), int(message.height)
        encoding = str(message.encoding or "").lower()
        channels = 3 if encoding in ("rgb8", "bgr8") else 4 if encoding in ("rgba8", "bgra8") else 1
        data = np.frombuffer(bytes(message.data), dtype=np.uint8)
        expected = height * int(message.step)
        if width <= 0 or height <= 0 or len(data) < expected:
            return
        image = data[:expected].reshape((height, int(message.step)))[:, :width * channels]
        image = image.reshape((height, width, channels)) if channels > 1 else image.reshape((height, width))
        if encoding == "rgb8": image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif encoding == "rgba8": image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        elif encoding == "bgra8": image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        elif channels == 1: image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        stamp = message.header.stamp
        result.update({
            "received": True, "width": width, "height": height,
            "encoding": str(message.encoding),
            "timestamp": "%d.%09d" % (stamp.sec, stamp.nanosec),
            "jpeg_base64": base64.b64encode(jpeg.tobytes()).decode("ascii"),
        })
        result["frame_count"] += 1
        result["first_received_at"] = result["first_received_at"] or received_at
        result["last_received_at"] = received_at

    sub = node.create_subscription(message_type, request["rgb_topic"], callback, qos_profile_sensor_data)
    deadline = time.monotonic() + float(request.get("sample_timeout", 1.5))
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    if result["frame_count"] > 1:
        elapsed = result["last_received_at"] - result["first_received_at"]
        result["observed_fps"] = (result["frame_count"] - 1) / elapsed if elapsed > 0 else None
    result.pop("first_received_at", None)
    result.pop("last_received_at", None)
    node.destroy_subscription(sub)
    node.destroy_node()
    rclpy.shutdown()
    print(marker + json.dumps({"ok": True, **result}, separators=(",", ":")))
except Exception as exc:
    try:
        if rclpy.ok(): rclpy.shutdown()
    except Exception:
        pass
    print(marker + json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")))
'''
