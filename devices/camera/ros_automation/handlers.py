import re
import time

from core.testing.errors import TestBlockedError
from core.testing.evaluator import TestEvaluator
from devices.camera.ros_automation.registry import (
    UnsupportedRosCameraAdapterError,
)
from devices.camera.ros_automation.remote import RosRemoteError


class RosBlockedError(TestBlockedError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _failure_reason(code, message):
    return {"code": code, "message": message}


def _sub_result(device, measurements, rules, configuration=None, failures=None):
    rule_results = TestEvaluator().evaluate(measurements, rules)
    reasons = list(failures or ())
    reasons.extend(
        _failure_reason(
            "ACCEPTANCE_RULE_FAILED",
            f"{item['metric']}={item['actual']} {item['operator']} {item['expected']}",
        )
        for item in rule_results
        if not item["passed"]
    )
    return {
        "device_uid": device.device_uid,
        "model": device.model,
        "serial": device.serial,
        "status": "PASS" if not reasons else "FAIL",
        "configuration": configuration or {},
        "measurements": measurements,
        "rules": rules,
        "rule_results": rule_results,
        "failure_reasons": reasons,
    }


class RosHandlerBase:
    def __init__(self):
        self._sessions = []

    def validate(self, context, _definition):
        client = context.services["remote_client"]
        if not client.connected:
            raise RosBlockedError(
                "JETSON_NOT_CONNECTED",
                "Jetson is not connected. Connect from Dashboard first.",
            )
        devices = self.devices(context)
        if not devices:
            raise RosBlockedError(
                "CAMERA_NOT_FOUND",
                "No physical camera exists in the captured target snapshot.",
            )
        registry = context.services["ros_adapter_registry"]
        try:
            for device in devices:
                registry.resolve(device)
        except UnsupportedRosCameraAdapterError as exc:
            raise RosBlockedError(exc.code, str(exc)) from exc
        busy_serials = set(context.base_configuration.get("busy_serials") or ())
        conflict = next(
            (device for device in devices if device.serial in busy_serials), None
        )
        if conflict is not None:
            raise RosBlockedError(
                "CAMERA_BUSY",
                f"Camera Monitor owns {conflict.model} SN{conflict.serial}; stop its stream first.",
            )

    def setup(self, _context, _definition):
        self._sessions = []

    def cleanup(self, context, _definition):
        manager = context.services["ros_process_manager"]
        errors = []
        for session in reversed(self._sessions):
            if not session.owned_by_test:
                continue
            try:
                response = manager.stop_node(session)
                if not response.get("stopped"):
                    remaining = response.get("remaining_pids") or []
                    errors.append(
                        "ROS session " + session.session_id
                        + " did not stop; remaining PIDs: "
                        + (", ".join(str(pid) for pid in remaining) or "unknown")
                    )
            except Exception as exc:
                errors.append(str(exc))
        self._sessions = []
        if errors:
            raise RuntimeError("; ".join(errors))

    @staticmethod
    def devices(context):
        return tuple(context.services.get("selected_devices") or ())

    @staticmethod
    def target_scope(context):
        return str(context.base_configuration.get("target_scope") or "INDIVIDUAL")

    def environment(self, context, expected_distro="humble"):
        devices = self.devices(context)
        registry = context.services["ros_adapter_registry"]
        packages = registry.required_packages(devices)
        manager = context.services["ros_process_manager"]
        environment = manager.probe_environment(packages, expected_distro)
        return environment, packages

    def require_runnable_environment(self, context, definition):
        expected_distro = str(definition.parameters.get("expected_distro") or "humble")
        environment, packages = self.environment(context, expected_distro)
        if not environment.get("ros2_available") or not environment.get("ros_environment_loaded"):
            raise RosBlockedError(
                "ROS_ENVIRONMENT_UNAVAILABLE",
                "; ".join(environment.get("errors") or ["ROS 2 environment is unavailable"]),
            )
        if environment.get("ros_distro") != expected_distro:
            raise RosBlockedError(
                "ROS_ENVIRONMENT_UNAVAILABLE",
                f"Expected ROS_DISTRO={expected_distro}, found {environment.get('ros_distro') or '-'}.",
            )
        return environment, packages

    def ensure_session(self, context, definition, device, environment):
        registry = context.services["ros_adapter_registry"]
        manager = context.services["ros_process_manager"]
        adapter = registry.resolve(device)
        launch_spec = adapter.build_launch_spec(device)
        package = (environment.get("packages") or {}).get(launch_spec.package, {})
        if package.get("installed") is not True:
            raise RosRemoteError(
                "ROS_DRIVER_MISSING",
                f"Required package {launch_spec.package} is not installed.",
            )
        context.log(
            "INFO",
            f"[{definition.test_id}][SN{device.serial}] Starting {launch_spec.package}.",
        )
        started = time.monotonic()
        session = manager.start_node(
            device, launch_spec, environment.get("setup_files") or ()
        )
        self._sessions.append(session)
        if not session.owned_by_test:
            return adapter, launch_spec, session, {
                "process_alive": True,
                "node_alive": True,
                "node_names": [launch_spec.expected_node],
                "startup_time_s": 0.0,
                "external_verified": True,
            }
        startup_timeout = float(definition.parameters.get("startup_timeout_s") or 25)
        context.log(
            "INFO",
            f"[{definition.test_id}][SN{device.serial}] Waiting for node {launch_spec.expected_node}.",
        )
        last = {}
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            context.checkpoint(time.monotonic())
            last = manager.status(session)
            if last.get("node_alive"):
                last["startup_time_s"] = round(time.monotonic() - started, 3)
                return adapter, launch_spec, session, last
            if not last.get("process_alive"):
                last["startup_time_s"] = round(time.monotonic() - started, 3)
                return adapter, launch_spec, session, last
            time.sleep(0.35)
        last["startup_time_s"] = round(time.monotonic() - started, 3)
        last["startup_timeout"] = True
        return adapter, launch_spec, session, last

    def stop_session(self, context, session):
        if not session.owned_by_test:
            if session in self._sessions:
                self._sessions.remove(session)
            return {"stopped": False, "external": True}
        response = context.services["ros_process_manager"].stop_node(session)
        if response.get("stopped") and session in self._sessions:
            self._sessions.remove(session)
        return response

    def cleanup_session(self, context, session):
        try:
            response = self.stop_session(context, session)
            return bool(response.get("external") or response.get("stopped")), None
        except Exception as exc:
            # Keep the session in _sessions so TestRunner's final cleanup gets
            # a second bounded attempt even when per-device cleanup fails.
            return False, str(exc)


class RosEnvironmentHandler(RosHandlerBase):
    def execute(self, context, definition):
        expected = str(definition.parameters.get("expected_distro") or "humble")
        context.log("INFO", f"[{definition.test_id}] Checking ROS environment.")
        environment, packages = self.environment(context, expected)
        driver_results = environment.get("packages") or {}
        installed = all(
            driver_results.get(package, {}).get("installed") is True
            for package in packages
        )
        measurements = {
            "jetson_connected": True,
            "ros2_available": bool(environment.get("ros2_available")),
            "ros_distro": environment.get("ros_distro"),
            "ros_environment_loaded": bool(environment.get("ros_environment_loaded")),
            "required_driver_count": len(packages),
            "driver_results": driver_results,
            "all_required_drivers_installed": installed,
            "selected_camera_count": len(self.devices(context)),
            "environment_warnings": environment.get("warnings") or [],
            "fatal_environment_errors": len(environment.get("errors") or []),
            "environment_ready": bool(environment.get("environment_ready")),
            "workspace_setup": environment.get("workspace_setup"),
            "setup_files": environment.get("setup_files") or [],
        }
        context.log("INFO", f"[{definition.test_id}] ROS_DISTRO={environment.get('ros_distro') or '-'}.")
        for package in packages:
            found = driver_results.get(package, {}).get("installed") is True
            context.log(
                "INFO" if found else "FAIL",
                f"[{definition.test_id}] {package} {'found' if found else 'missing'}.",
            )
        configuration = {
            "target_scope": self.target_scope(context),
            "required_packages": list(packages),
            "expected_distro": expected,
            "ros_environment": environment,
        }
        return measurements, configuration, []


class RosNodeLaunchHandler(RosHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        for device in self.devices(context):
            context.checkpoint(time.monotonic())
            failures = []
            session = None
            launch_spec = None
            status = {}
            cleanup = {"stopped": False}
            try:
                _adapter, launch_spec, session, status = self.ensure_session(
                    context, definition, device, environment
                )
                log_text = str(status.get("stderr_summary") or "")
                detected_match = re.search(r"Serial Number\s*(?:->|:)\s*(?:S/N\s*)?(\d+)", log_text)
                detected_serial = status.get("detected_serial")
                if detected_serial is None and detected_match:
                    detected_serial = detected_match.group(1)
                serial_configured = any(
                    item == f"serial_number:={device.serial}"
                    or item == f"serial_no:=_{device.serial}"
                    for item in launch_spec.arguments
                )
                measurements = {
                    "device_uid": device.device_uid,
                    "model": device.model,
                    "serial": device.serial,
                    "driver": launch_spec.driver,
                    "ros_camera_model": launch_spec.ros_camera_model,
                    "namespace": launch_spec.namespace,
                    "launch_success": bool(status.get("node_alive")),
                    "startup_time_s": status.get("startup_time_s"),
                    "node_names": status.get("node_names") or [],
                    "node_alive": bool(status.get("node_alive")),
                    "process_alive": bool(status.get("process_alive")),
                    "selected_serial": launch_spec.selected_serial,
                    "detected_serial": detected_serial,
                    "serial_selection_configured": serial_configured,
                    "serial_verified": bool(
                        status.get("external_verified")
                        or status.get("serial_verified")
                    ),
                    "owned_by_test": session.owned_by_test,
                    "process_exit_code": status.get("process_exit_code"),
                    "stderr_summary": log_text[-2000:],
                    "log_path": session.log_path,
                }
                if status.get("startup_timeout"):
                    failures.append(_failure_reason("ROS_NODE_TIMEOUT", "Expected node did not become ready before the startup timeout."))
                elif not status.get("process_alive"):
                    failures.append(_failure_reason("ROS_NODE_EXITED", "ROS launch process exited during startup."))
            except RosRemoteError as exc:
                failures.append(_failure_reason(exc.code, str(exc)))
                measurements = {
                    "device_uid": device.device_uid,
                    "model": device.model,
                    "serial": device.serial,
                    "driver": device.ros_driver,
                    "ros_camera_model": device.ros_camera_model,
                    "namespace": device.ros_namespace_hint,
                    "launch_success": False,
                    "node_alive": False,
                    "process_alive": False,
                    "selected_serial": device.serial,
                    "serial_verified": False,
                    "owned_by_test": False,
                    "stderr_summary": str(exc)[-2000:],
                }
            finally:
                if session is not None:
                    cleanup_success, cleanup_error = self.cleanup_session(
                        context, session
                    )
                    cleanup = {"stopped": cleanup_success}
                    if cleanup_error:
                        failures.append(
                            _failure_reason("ROS_NODE_EXITED", cleanup_error)
                        )
            measurements["cleanup_success"] = bool(
                cleanup.get("external") or cleanup.get("stopped")
            )
            rules = [
                {"metric": "launch_success", "operator": "==", "expected": True},
                {"metric": "node_alive", "operator": "==", "expected": True},
                {"metric": "process_alive", "operator": "==", "expected": True},
                {"metric": "serial_verified", "operator": "==", "expected": True},
                {"metric": "cleanup_success", "operator": "==", "expected": True},
            ]
            result = _sub_result(
                device,
                measurements,
                rules,
                launch_spec.to_dict() if launch_spec else {},
                failures,
            )
            results.append(result)
            context.log(
                "PASS" if result["status"] == "PASS" else "FAIL",
                f"[{definition.test_id}][SN{device.serial}] {result['status']}.",
            )
        return self._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "startup_timeout_s": definition.parameters.get("startup_timeout_s"),
        }, results

    @staticmethod
    def _aggregate(context, environment, results):
        return {
            "target_scope": str(context.base_configuration.get("target_scope")),
            "selected_camera_count": len(results),
            "all_devices_pass": bool(results) and all(item["status"] == "PASS" for item in results),
            "devices": {item["device_uid"]: item for item in results},
            "ros_environment": environment,
        }


class RosTopicHealthHandler(RosHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        manager = context.services["ros_process_manager"]
        for device in self.devices(context):
            session = None
            result = None
            failures = []
            try:
                adapter, spec, session, status = self.ensure_session(
                    context, definition, device, environment
                )
                if not status.get("node_alive"):
                    code = "ROS_NODE_EXITED" if not status.get("process_alive") else "ROS_NODE_TIMEOUT"
                    raise RosRemoteError(code, "Required ROS camera node is not alive.")
                collection = manager.collect(
                    session,
                    spec,
                    adapter.mandatory_topics(device),
                    float(definition.parameters.get("warmup_s") or 1),
                    float(definition.parameters.get("topic_timeout_s") or 8),
                    1,
                )
                topics = collection.get("topics") or {}
                missing = [name for name, item in topics.items() if not item.get("exists")]
                invalid_types = [name for name, item in topics.items() if not item.get("type_matches")]
                no_publishers = [name for name, item in topics.items() if int(item.get("publisher_count") or 0) < 1]
                timeouts = [name for name, item in topics.items() if not item.get("message_received")]
                measurements = {
                    "node_alive": True,
                    "namespace": spec.namespace,
                    "mandatory_topic_count": len(spec.mandatory_topics),
                    "topics": topics,
                    "missing_topics": missing,
                    "invalid_types": invalid_types,
                    "topics_without_publishers": no_publishers,
                    "message_timeouts": timeouts,
                    "mandatory_topics_present": not missing,
                    "mandatory_types_match": not invalid_types,
                    "mandatory_publishers_present": not no_publishers,
                    "mandatory_messages_received": not timeouts,
                    "mandatory_topic_timeout_count": len(timeouts),
                    "collection_duration_s": collection.get("collection_duration_s"),
                    "owned_by_test": session.owned_by_test,
                }
                for topic in missing:
                    failures.append(_failure_reason("ROS_TOPIC_MISSING", topic))
                for topic in invalid_types:
                    failures.append(_failure_reason("ROS_TOPIC_TYPE_MISMATCH", topic))
                for topic in timeouts:
                    failures.append(_failure_reason("ROS_TOPIC_TIMEOUT", topic))
                rules = [
                    {"metric": "node_alive", "operator": "==", "expected": True},
                    {"metric": "mandatory_topics_present", "operator": "==", "expected": True},
                    {"metric": "mandatory_types_match", "operator": "==", "expected": True},
                    {"metric": "mandatory_publishers_present", "operator": "==", "expected": True},
                    {"metric": "mandatory_messages_received", "operator": "==", "expected": True},
                    {"metric": "mandatory_topic_timeout_count", "operator": "==", "expected": 0},
                ]
                result = _sub_result(device, measurements, rules, spec.to_dict(), failures)
            except RosRemoteError as exc:
                result = _sub_result(
                    device,
                    {"node_alive": False, "mandatory_topics_present": False, "mandatory_messages_received": False},
                    [{"metric": "node_alive", "operator": "==", "expected": True}],
                    {},
                    [_failure_reason(exc.code, str(exc))],
                )
            finally:
                if session is not None:
                    cleanup_success, cleanup_error = self.cleanup_session(
                        context, session
                    )
                    if result is not None:
                        result["measurements"]["cleanup_success"] = cleanup_success
                    if result is not None and not cleanup_success:
                        result["status"] = "FAIL"
                        result["failure_reasons"].append(_failure_reason(
                            "ROS_NODE_EXITED",
                            cleanup_error or "Test-owned ROS process did not stop cleanly.",
                        ))
            results.append(result)
        return RosNodeLaunchHandler._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "topic_timeout_s": definition.parameters.get("topic_timeout_s"),
        }, results


class RosImageProfileHandler(RosHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        manager = context.services["ros_process_manager"]
        fps_ratio_min = float(definition.parameters.get("fps_ratio_min") or 0.95)
        for device in self.devices(context):
            session = None
            result = None
            failures = []
            try:
                adapter, spec, session, status = self.ensure_session(
                    context, definition, device, environment
                )
                if not status.get("node_alive"):
                    code = "ROS_NODE_EXITED" if not status.get("process_alive") else "ROS_NODE_TIMEOUT"
                    raise RosRemoteError(code, "Required ROS camera node is not alive.")
                primary = next(
                    item for item in spec.mandatory_topics
                    if item.topic(spec.namespace) == spec.primary_image_topic
                )
                collection = manager.collect(
                    session,
                    spec,
                    (primary,),
                    float(definition.parameters.get("warmup_s") or 2),
                    float(definition.parameters.get("measurement_timeout_s") or 10),
                    int(definition.parameters.get("sample_count") or 100),
                )
                topic = (collection.get("topics") or {}).get(spec.primary_image_topic, {})
                profile = spec.requested_profile
                calculated_fps = float(topic.get("calculated_fps") or 0)
                fps_ratio = calculated_fps / profile.fps if profile.fps else 0.0
                measurements = {
                    "node_alive": True,
                    "topic": spec.primary_image_topic,
                    "message_type": topic.get("type"),
                    "topic_exists": bool(topic.get("exists")),
                    "message_received": bool(topic.get("message_received")),
                    "width": topic.get("width"),
                    "height": topic.get("height"),
                    "encoding": topic.get("encoding"),
                    "encoding_supported": topic.get("encoding") in profile.encodings,
                    "step": topic.get("step"),
                    "frame_id": topic.get("frame_id"),
                    "sample_count": topic.get("sample_count", 0),
                    "first_timestamp": topic.get("first_timestamp"),
                    "last_timestamp": topic.get("last_timestamp"),
                    "timestamp_rollback_count": topic.get("timestamp_rollback_count", 0),
                    "duration_s": topic.get("duration_s", 0),
                    "calculated_fps": calculated_fps,
                    "host_receive_fps": topic.get("host_receive_fps"),
                    "configured_fps": profile.fps,
                    "fps_ratio": round(fps_ratio, 6),
                    "owned_by_test": session.owned_by_test,
                }
                if not measurements["topic_exists"]:
                    failures.append(_failure_reason("ROS_TOPIC_MISSING", spec.primary_image_topic))
                elif not measurements["message_received"]:
                    failures.append(_failure_reason("ROS_TOPIC_TIMEOUT", spec.primary_image_topic))
                if measurements["width"] != profile.width or measurements["height"] != profile.height:
                    failures.append(_failure_reason("ROS_PROFILE_MISMATCH", "Actual image resolution differs from the requested ROS profile."))
                if not measurements["encoding_supported"]:
                    failures.append(_failure_reason("ROS_PROFILE_MISMATCH", "Actual image encoding is not supported by the adapter profile."))
                if fps_ratio < fps_ratio_min:
                    failures.append(_failure_reason("ROS_FPS_BELOW_THRESHOLD", f"Measured FPS ratio {fps_ratio:.3f} is below {fps_ratio_min:.3f}."))
                rules = [
                    {"metric": "topic_exists", "operator": "==", "expected": True},
                    {"metric": "message_received", "operator": "==", "expected": True},
                    {"metric": "width", "operator": "==", "expected": profile.width},
                    {"metric": "height", "operator": "==", "expected": profile.height},
                    {"metric": "encoding_supported", "operator": "==", "expected": True},
                    {"metric": "fps_ratio", "operator": ">=", "expected": fps_ratio_min},
                    {"metric": "timestamp_rollback_count", "operator": "==", "expected": 0},
                ]
                result = _sub_result(
                    device,
                    measurements,
                    rules,
                    {
                        **spec.to_dict(),
                        "requested": profile.to_dict(),
                    },
                    failures,
                )
            except RosRemoteError as exc:
                result = _sub_result(
                    device,
                    {"node_alive": False, "message_received": False},
                    [{"metric": "node_alive", "operator": "==", "expected": True}],
                    {},
                    [_failure_reason(exc.code, str(exc))],
                )
            finally:
                if session is not None:
                    cleanup_success, cleanup_error = self.cleanup_session(
                        context, session
                    )
                    if result is not None:
                        result["measurements"]["cleanup_success"] = cleanup_success
                    if result is not None and not cleanup_success:
                        result["status"] = "FAIL"
                        result["failure_reasons"].append(_failure_reason(
                            "ROS_NODE_EXITED",
                            cleanup_error or "Test-owned ROS process did not stop cleanly.",
                        ))
            results.append(result)
        return RosNodeLaunchHandler._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "fps_ratio_min": fps_ratio_min,
            "sample_count": definition.parameters.get("sample_count"),
            "warmup_s": definition.parameters.get("warmup_s"),
            "measurement_timeout_s": definition.parameters.get("measurement_timeout_s"),
        }, results


def register_ros_camera_handlers(registry):
    registry.register("ros.environment", RosEnvironmentHandler())
    registry.register("ros.node_launch", RosNodeLaunchHandler())
    registry.register("ros.topic_health", RosTopicHealthHandler())
    registry.register("ros.image_profile", RosImageProfileHandler())
