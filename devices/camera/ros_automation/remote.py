import json
import shlex
import uuid

from devices.camera.ros_automation.jetson_ros_manager import JETSON_ROS_MANAGER
from devices.camera.ros_automation.models import RosBagSession, RosNodeSession


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
            try:
                response = await self.service.execute_with_ssh(ssh, action, payload)
                return {"remote_ros_response": response}
            except RosRemoteError as exc:
                return {
                    "remote_ros_error": {
                        "code": exc.code,
                        "message": str(exc),
                        "payload": exc.payload,
                    }
                }

        envelope = self.client.call(
            "camera_ros_" + action,
            operation,
            timeout,
            ignore_cancel=cleanup,
        )
        error = envelope.get("remote_ros_error") or {}
        if error:
            raise RosRemoteError(
                str(error.get("code") or "ROS_PROBE_ERROR"),
                str(error.get("message") or "Remote ROS operation failed"),
                error.get("payload") or {},
            )
        return envelope.get("remote_ros_response") or {}

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
            cleanup=True,
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

    def collect_capabilities(
        self, session, launch_spec, topics, warmup_s, timeout_s, sample_count,
        *, equal_timestamps_valid=False, include_message_timestamps=False,
    ):
        payload_topics = []
        for index, item in enumerate(topics):
            candidates = item.candidate_topics(launch_spec.namespace)
            payload_topics.append({
                "result_key": f"{item.capability}:{index}",
                "topic": candidates[0],
                "candidate_topics": list(candidates),
                "message_type": item.message_type,
                "capability": item.capability,
                "availability": item.availability,
                "expected_rate_hz": item.expected_rate_hz,
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
                "equal_timestamps_valid": equal_timestamps_valid,
                "include_message_timestamps": include_message_timestamps,
                "remote_timeout_s": warmup_s + timeout_s + 8,
            },
            warmup_s + timeout_s + 11,
        )
        return response["collection"]

    @staticmethod
    def _capability_payload(launch_spec, topics):
        return [
            {
                "result_key": f"{item.capability}:{index}",
                "topic": item.topic(launch_spec.namespace),
                "candidate_topics": list(item.candidate_topics(launch_spec.namespace)),
                "message_type": item.message_type,
                "capability": item.capability,
                "availability": item.availability,
            }
            for index, item in enumerate(topics)
        ]

    def qos_matrix(
        self, session, launch_spec, topics, graph_timeout_s, timeout_s,
        sample_count,
    ):
        topic_count = max(1, len(topics))
        operation_timeout = (
            graph_timeout_s * topic_count + timeout_s + 4 * topic_count + 8
        )
        response = self._call(
            "qos",
            {
                "required_packages": [launch_spec.package],
                "setup_files": list(session.setup_files),
                "topics": self._capability_payload(launch_spec, topics),
                "graph_timeout_s": graph_timeout_s,
                "timeout_s": timeout_s,
                "sample_count": sample_count,
                "remote_timeout_s": operation_timeout - 3,
            },
            operation_timeout,
        )
        return response["qos"]

    def bag_preflight(self, setup_files, timeout_s=10):
        response = self._call(
            "bag_preflight",
            {"setup_files": list(setup_files), "remote_timeout_s": timeout_s},
            timeout_s + 3,
        )
        return response["preflight"]

    def start_bag_record(self, setup_files, topics, timeout_s=10):
        response = self._call(
            "bag_record_start",
            {
                "session_id": uuid.uuid4().hex,
                "setup_files": list(setup_files),
                "topics": list(topics),
                "remote_timeout_s": timeout_s,
            },
            timeout_s + 3,
            cleanup=True,
        )
        return RosBagSession.from_dict(response["session"])

    def bag_status(self, session, timeout_s=8):
        response = self._call(
            "bag_status",
            {"session_id": session.session_id, "remote_timeout_s": timeout_s},
            timeout_s + 3,
        )
        return response["status"]

    def stop_bag_process(self, session, timeout_s=10):
        if not session.owned_by_test:
            return {"stopped": False, "external": True}
        return self._call(
            "bag_stop",
            {"session_id": session.session_id, "remote_timeout_s": timeout_s},
            timeout_s + 3,
            cleanup=True,
        )

    def inspect_bag(self, session, timeout_s=10):
        response = self._call(
            "bag_inspect",
            {"bag_path": session.bag_path, "remote_timeout_s": timeout_s},
            timeout_s + 3,
        )
        return response["inspection"]

    def start_bag_replay(
        self, record_session, topics, remappings, timeout_s=10
    ):
        response = self._call(
            "bag_replay_start",
            {
                "session_id": uuid.uuid4().hex,
                "setup_files": list(record_session.setup_files),
                "bag_path": record_session.bag_path,
                "topics": list(topics),
                "remappings": dict(remappings),
                "remote_timeout_s": timeout_s,
            },
            timeout_s + 3,
            cleanup=True,
        )
        return RosBagSession.from_dict(response["session"])

    def collect_topic_names(self, setup_files, topics, timeout_s, sample_count):
        payload_topics = [
            {
                "result_key": name,
                "topic": name,
                "candidate_topics": [name],
                "message_type": message_type,
                "sample_count": sample_count,
            }
            for name, message_type in topics.items()
        ]
        response = self._call(
            "collect",
            {
                "required_packages": [],
                "setup_files": list(setup_files),
                "topics": payload_topics,
                "warmup_s": 0,
                "timeout_s": timeout_s,
                "sample_count": sample_count,
                "remote_timeout_s": timeout_s + 8,
            },
            timeout_s + 11,
        )
        return response["collection"]
