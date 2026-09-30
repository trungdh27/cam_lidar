import json
import shlex

from devices.camera.base_adapter import CameraOpenError
from devices.camera.jetson_ros_camera_monitor import JETSON_ROS_CAMERA_MONITOR


class RemoteRosMonitorAdapter:
    """Read-only ROS sampler; it never opens or manages camera hardware."""

    marker = "CAMERA_ROS_MONITOR_JSON="

    async def sample_with_ssh(self, ssh, payload: dict) -> dict:
        request = {
            "rgb_topic": payload["rgb_topic"],
            "message_type": payload.get("rgb_message_type", "sensor_msgs/msg/Image"),
            "sample_timeout": 1.5,
        }
        shell = (
            "source /opt/ros/humble/setup.bash >/dev/null 2>&1; "
            "for setup in /opt/vindynamics/setup.bash /opt/vindynamics/local_setup.bash "
            "/opt/vindynamics/sensors/*/setup.bash /opt/vindynamics/sensors/*/local_setup.bash; do "
            "[ -r \"$setup\" ] && source \"$setup\" >/dev/null 2>&1; done; python3 -c "
            + shlex.quote(JETSON_ROS_CAMERA_MONITOR) + " "
            + shlex.quote(json.dumps(request, separators=(",", ":")))
        )
        result = await ssh.run("bash -lc " + shlex.quote(shell), timeout=4.0)
        for line in reversed(str(result.stdout).splitlines()):
            if line.startswith(self.marker):
                payload = json.loads(line[len(self.marker):])
                if payload.get("ok"):
                    return payload
                raise CameraOpenError(payload.get("error") or "ROS image sample failed")
        raise CameraOpenError("ROS image sampler returned no structured result")
