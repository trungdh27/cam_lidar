import json
import shlex
import uuid

from devices.camera.ros_automation.jetson_ros_manager import JETSON_ROS_MANAGER
from devices.camera.ros_automation.models import RosNodeSession


class RosRemoteError(RuntimeError):
    def __init__(self, code: str, message: str, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class RemoteRosCameraService:
    marker = "CAMERA_ROS_JSON="

    async def execute_with_ssh(self, ssh, action: str, payload: dict) -> dict:
        request = dict(payload)
        request["action"] = action
        command = "python3 -c " + shlex.quote(JETSON_ROS_MANAGER) + " " + shlex.quote(
            json.dumps(request, separators=(",", ":"))
        )
        timeout = float(payload.get("remote_timeout_s") or 15)
        result = await ssh.run(command, timeout=timeout)
        response = self._parse(result.stdout)
        if not response.get("ok"):
            raise RosRemoteError(
                str(response.get("error_type") or "ROS_PROBE_ERROR"),
                str(response.get("error") or "Remote ROS operation failed"),
                response,
            )
        return response

    def _parse(self, output: str) -> dict:
        for line in reversed(str(output).splitlines()):
            if line.startswith(self.marker):
                return json.loads(line[len(self.marker):])
        raise RosRemoteError("ROS_PROBE_ERROR", "Jetson ROS manager returned no structured result")


class RosRemoteProcessManager:
    """Host-side facade; every shared SSH operation is explicitly bounded."""

    def __init__(self, operation_client, service=None):
        self.client = operation_client
        self.service = service or RemoteRosCameraService()

    def _call(self, action, payload, timeout, cleanup=False):
        async def operation(ssh):
            return await self.service.execute_with_ssh(ssh, action, payload)

        return self.client.call(
            "camera_ros_" + action,
            operation,
            timeout,
            ignore_cancel=cleanup,
        )

    def probe_environment(self, required_packages, expected_distro="humble"):
        response = self._call(
            "environment",
            {
                "required_packages": list(required_packages),
                "expected_distro": expected_distro,
                "remote_timeout_s": 12,
            },
            15,
        )
        return response["environment"]

    def start_node(self, device, launch_spec, setup_files):
        response = self._call(
            "start",
            {
                "session_id": uuid.uuid4().hex,
                "device_uid": device.device_uid,
                "launch_spec": launch_spec.to_dict(),
                "setup_files": list(setup_files),
                "remote_timeout_s": 12,
            },
            15,
        )
        return RosNodeSession.from_dict(response["session"])

    def status(self, session):
        response = self._call(
            "status",
            {
                "session_id": session.session_id,
                "remote_timeout_s": 10,
            },
            13,
        )
        return response["status"]

    def stop_node(self, session):
        if not session.owned_by_test:
            return {"stopped": False, "external": True}
        return self._call(
            "stop",
            {
                "session_id": session.session_id,
                "remote_timeout_s": 10,
            },
            13,
            cleanup=True,
        )

    def collect(self, session, launch_spec, topics, warmup_s, timeout_s, sample_count):
        payload_topics = []
        for item in topics:
            payload_topics.append({
                "topic": item.topic(launch_spec.namespace),
                "message_type": item.message_type,
                "sample_count": sample_count,
            })
        response = self._call(
            "collect",
            {
                "required_packages": [launch_spec.package],
                "setup_files": list(session.setup_files),
                "topics": payload_topics,
                "warmup_s": warmup_s,
                "timeout_s": timeout_s,
                "sample_count": sample_count,
                "remote_timeout_s": warmup_s + timeout_s + 8,
            },
            warmup_s + timeout_s + 11,
        )
        return response["collection"]
