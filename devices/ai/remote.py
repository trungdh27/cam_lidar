import json
import shlex

from devices.ai.jetson_ai_manager import JETSON_AI_MANAGER
from devices.ai.models import AiSession


class AiRemoteError(RuntimeError):
    def __init__(self, code, message, payload=None):
        super().__init__(message)
        self.code, self.payload = code, payload or {}


class AiRemoteProcessManager:
    marker = "CAMERA_AI_JSON="

    def __init__(self, operation_client):
        self.client = operation_client

    def _call(self, action, payload, timeout, cleanup=False):
        async def operation(ssh):
            try:
                return {"ai_response": await AiRemoteService().execute_with_ssh(ssh, action, payload, timeout)}
            except AiRemoteError as exc:
                return {"ai_error": {"code": exc.code, "message": str(exc), "payload": exc.payload}}
        result = self.client.call("ai_" + action, operation, timeout + 3, ignore_cancel=cleanup)
        error = result.get("ai_error") or {}
        if error:
            raise AiRemoteError(str(error.get("code")), str(error.get("message")), error.get("payload"))
        return result.get("ai_response") or {}

    def discover_environment(self):
        return self._call("discover", {}, 10)["environment"]

    @staticmethod
    def _identity(session):
        return {"session_id": session.session_id, "module_uid": session.module_uid,
                "pid": session.pid, "process_group": session.process_group}

    def start(self, module, setup_files=()):
        return AiSession.from_dict(self._call("start", {"module": module.to_dict(), "setup_files": list(setup_files)}, 15, cleanup=True)["session"])

    def status(self, session):
        return self._call("status", self._identity(session), 10)["status"]

    def stop(self, session):
        if not session.owned_by_test:
            return {"external": True, "stopped": False}
        return self._call("stop", {**self._identity(session), "timeout_s": 8}, 12, cleanup=True)

    def probe_endpoint(self, session, endpoint, timeout_s, sample_count):
        return self._call("probe", {"endpoint": endpoint.to_dict(), "timeout_s": timeout_s,
                                    "sample_count": sample_count}, float(timeout_s) + 6)["probe"]


class AiRemoteService:
    """Async backend used by the page and worker through JetsonConnectionService."""
    marker = "CAMERA_AI_JSON="

    async def execute_with_ssh(self, ssh, action, payload, timeout=15):
        request = {**payload, "action": action}
        command = "python3 -c " + shlex.quote(JETSON_AI_MANAGER) + " " + shlex.quote(
            json.dumps(request, separators=(",", ":"))
        )
        result = await ssh.run(command, timeout=timeout)
        for line in reversed(str(result.stdout).splitlines()):
            if line.startswith(self.marker):
                response = json.loads(line[len(self.marker):])
                if response.get("ok"):
                    return response
                raise AiRemoteError(str(response.get("error_type") or "AI_PROBE_ERROR"), str(response.get("error") or "AI operation failed"), response)
        raise AiRemoteError("AI_PROBE_ERROR", "AI manager returned no structured result")
