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


def _sub_result(
    device, measurements, rules, configuration=None, failures=None,
    status_override=None,
):
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
        "status": status_override or ("PASS" if not reasons else "FAIL"),
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
        statuses = [item["status"] for item in results]
        aggregate_status = (
            "PASS" if statuses and all(status == "PASS" for status in statuses)
            else "FAIL" if "FAIL" in statuses
            else "ERROR" if "ERROR" in statuses
            else "BLOCKED" if "BLOCKED" in statuses
            else "FAIL"
        )
        return {
            "target_scope": str(context.base_configuration.get("target_scope")),
            "selected_camera_count": len(results),
            "all_devices_pass": bool(results) and all(item["status"] == "PASS" for item in results),
            "aggregate_status": aggregate_status,
            "devices": {item["device_uid"]: item for item in results},
            "device_results": {item["device_uid"]: item for item in results},
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


def _record_for_capability(collection, capability):
    return next(
        (
            item for key, item in (collection.get("topics") or {}).items()
            if key.split(":", 1)[0] == capability
            or item.get("capability") == capability
        ),
        {},
    )


def _topic_ready(record):
    return (
        bool(record.get("exists"))
        and bool(record.get("type_matches"))
        and int(record.get("publisher_count") or 0) >= 1
        and bool(record.get("message_received"))
    )


def _strict_timestamp_rollbacks(timestamps):
    values = list(timestamps or ())
    return sum(
        current < previous
        for previous, current in zip(values, values[1:])
    )


class RosCameraInfoHandler(RosHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        manager = context.services["ros_process_manager"]
        for device in self.devices(context):
            context.checkpoint(time.monotonic())
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
                context.log("INFO", f"[{definition.test_id}] Collecting Image and CameraInfo.")
                requirements = (
                    adapter.primary_image_requirement(device),
                    adapter.camera_info_requirement(device),
                )
                collection = manager.collect_capabilities(
                    session, spec, requirements,
                    float(definition.parameters.get("warmup_s") or 0.5),
                    float(definition.parameters.get("collection_timeout_s") or 8),
                    int(definition.parameters.get("sample_count") or 5),
                    equal_timestamps_valid=True,
                    include_message_timestamps=True,
                )
                context.checkpoint(time.monotonic())
                image = _record_for_capability(collection, "color")
                camera_info = _record_for_capability(collection, "camera_info")
                image_received = _topic_ready(image)
                info_received = _topic_ready(camera_info)
                resolution_match = (
                    image_received and info_received
                    and image.get("width") == camera_info.get("width")
                    and image.get("height") == camera_info.get("height")
                )
                non_finite = int(image.get("non_finite_value_count") or 0) + int(
                    camera_info.get("non_finite_value_count") or 0
                )
                finite_values = info_received and non_finite == 0 and all(
                    camera_info.get(name) is not None for name in ("K", "D", "R", "P")
                )
                frame_valid = adapter.frame_relationship_valid(
                    image.get("frame_id"), camera_info.get("frame_id")
                )
                image_rollback_count = (
                    _strict_timestamp_rollbacks(image["message_timestamps"])
                    if "message_timestamps" in image
                    else int(image.get("timestamp_rollback_count") or 0)
                )
                camera_info_rollback_count = (
                    _strict_timestamp_rollbacks(camera_info["message_timestamps"])
                    if "message_timestamps" in camera_info
                    else int(camera_info.get("timestamp_rollback_count") or 0)
                )
                rollback_count = image_rollback_count + camera_info_rollback_count
                measurements = {
                    "image": {
                        "topic": image.get("topic_name"),
                        "width": image.get("width"),
                        "height": image.get("height"),
                        "encoding": image.get("encoding"),
                        "frame_id": image.get("frame_id"),
                        "first_timestamp": image.get("first_timestamp"),
                        "last_timestamp": image.get("last_timestamp"),
                        "sample_count": int(image.get("sample_count") or 0),
                        "timestamp_rollback_count": image_rollback_count,
                    },
                    "camera_info": {
                        "topic": camera_info.get("topic_name"),
                        "width": camera_info.get("width"),
                        "height": camera_info.get("height"),
                        "distortion_model": camera_info.get("distortion_model"),
                        "K": camera_info.get("K"),
                        "D": camera_info.get("D"),
                        "R": camera_info.get("R"),
                        "P": camera_info.get("P"),
                        "frame_id": camera_info.get("frame_id"),
                        "first_timestamp": camera_info.get("first_timestamp"),
                        "last_timestamp": camera_info.get("last_timestamp"),
                        "sample_count": int(camera_info.get("sample_count") or 0),
                        "timestamp_rollback_count": camera_info_rollback_count,
                    },
                    "consistency": {
                        "resolution_match": resolution_match,
                        "frame_relationship_valid": frame_valid,
                        "finite_values": finite_values,
                        "timestamp_monotonic": rollback_count == 0,
                    },
                    "image_received": image_received,
                    "camera_info_received": info_received,
                    "image_width": image.get("width"),
                    "image_height": image.get("height"),
                    "camera_info_width": camera_info.get("width"),
                    "camera_info_height": camera_info.get("height"),
                    "image_encoding": image.get("encoding"),
                    "image_frame_id": image.get("frame_id"),
                    "camera_info_frame_id": camera_info.get("frame_id"),
                    "image_timestamp": image.get("last_timestamp"),
                    "camera_info_timestamp": camera_info.get("last_timestamp"),
                    "fx": camera_info.get("fx"),
                    "fy": camera_info.get("fy"),
                    "cx": camera_info.get("cx"),
                    "cy": camera_info.get("cy"),
                    "resolution_match": resolution_match,
                    "frame_relationship_valid": frame_valid,
                    "finite_values": finite_values,
                    "timestamp_rollback_count": rollback_count,
                    "image_timestamp_rollback_count": image_rollback_count,
                    "camera_info_timestamp_rollback_count": camera_info_rollback_count,
                    "non_finite_value_count": non_finite,
                    "nan_count": int(camera_info.get("nan_count") or 0),
                    "inf_count": int(camera_info.get("inf_count") or 0),
                    "sample_count": min(
                        int(image.get("sample_count") or 0),
                        int(camera_info.get("sample_count") or 0),
                    ),
                    "owned_by_test": session.owned_by_test,
                }
                if not image_received or not info_received:
                    failures.append(_failure_reason(
                        "ROS_CAMERA_INFO_MISSING",
                        "Image and CameraInfo must both be present, correctly typed, and publishing.",
                    ))
                if not resolution_match or not finite_values or not frame_valid:
                    failures.append(_failure_reason(
                        "ROS_CALIBRATION_INVALID",
                        "CameraInfo resolution, numeric calibration, or frame relationship is invalid.",
                    ))
                context.log(
                    "INFO", f"[{definition.test_id}] Image: {image.get('width')}x{image.get('height')}."
                )
                context.log(
                    "INFO", f"[{definition.test_id}] CameraInfo: {camera_info.get('width')}x{camera_info.get('height')}."
                )
                if finite_values:
                    context.log("INFO", f"[{definition.test_id}] Calibration values finite.")
                rules = [
                    {"metric": "image_received", "operator": "==", "expected": True},
                    {"metric": "camera_info_received", "operator": "==", "expected": True},
                    {"metric": "resolution_match", "operator": "==", "expected": True},
                    {"metric": "fx", "operator": ">", "expected": 0},
                    {"metric": "fy", "operator": ">", "expected": 0},
                    {"metric": "finite_values", "operator": "==", "expected": True},
                    {"metric": "non_finite_value_count", "operator": "==", "expected": 0},
                    {"metric": "timestamp_rollback_count", "operator": "==", "expected": 0},
                    {"metric": "frame_relationship_valid", "operator": "==", "expected": True},
                ]
                result = _sub_result(device, measurements, rules, spec.to_dict(), failures)
            except RosRemoteError as exc:
                result = _sub_result(
                    device,
                    {"image_received": False, "camera_info_received": False},
                    [{"metric": "camera_info_received", "operator": "==", "expected": True}],
                    {}, [_failure_reason(exc.code, str(exc))],
                )
            finally:
                if session is not None:
                    cleaned, cleanup_error = self.cleanup_session(context, session)
                    if result is not None:
                        result["measurements"]["cleanup_success"] = cleaned
                        if not cleaned:
                            result["status"] = "FAIL"
                            result["failure_reasons"].append(_failure_reason(
                                "ROS_NODE_EXITED", cleanup_error or "ROS session cleanup failed."
                            ))
            results.append(result)
            context.log(
                "PASS" if result["status"] == "PASS" else "FAIL",
                f"[{definition.test_id}][SN{device.serial}] {result['status']}.",
            )
        return RosNodeLaunchHandler._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "collection_timeout_s": definition.parameters.get("collection_timeout_s"),
            "sample_count": definition.parameters.get("sample_count"),
        }, results


class RosSensorTopicsHandler(RosHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        manager = context.services["ros_process_manager"]
        for device in self.devices(context):
            context.checkpoint(time.monotonic())
            session = None
            result = None
            failures = []
            try:
                adapter, spec, session, status = self.ensure_session(
                    context, definition, device, environment
                )
                if not status.get("node_alive"):
                    raise RosRemoteError("ROS_NODE_TIMEOUT", "Required ROS camera node is not alive.")
                requirements = adapter.sensor_topics(device)
                collection = manager.collect_capabilities(
                    session, spec, requirements,
                    float(definition.parameters.get("warmup_s") or 0.5),
                    float(definition.parameters.get("collection_timeout_s") or 8),
                    int(definition.parameters.get("sample_count") or 20),
                )
                context.checkpoint(time.monotonic())
                topic_records = []
                mandatory_valid = True
                mandatory_rollbacks = 0
                mandatory_non_finite = 0
                mandatory_timeouts = 0
                for index, requirement in enumerate(requirements):
                    record = dict((collection.get("topics") or {}).get(
                        f"{requirement.capability}:{index}", {}
                    ))
                    record.update({
                        "capability": requirement.capability,
                        "classification": requirement.availability,
                        "message_type": record.get("type") or requirement.message_type,
                        "availability_status": (
                            "AVAILABLE" if _topic_ready(record)
                            else "OPTIONAL_NOT_AVAILABLE" if requirement.availability == "OPTIONAL"
                            else "MANDATORY_NOT_AVAILABLE"
                        ),
                    })
                    topic_records.append(record)
                    if requirement.availability == "MANDATORY":
                        ready = _topic_ready(record)
                        rollbacks = int(record.get("timestamp_rollback_count") or 0)
                        non_finite = int(record.get("non_finite_value_count") or 0)
                        timeouts = int(record.get("message_timeout_count") or (0 if record.get("message_received") else 1))
                        mandatory_valid = mandatory_valid and ready and rollbacks == 0 and non_finite == 0 and timeouts == 0
                        mandatory_rollbacks += rollbacks
                        mandatory_non_finite += non_finite
                        mandatory_timeouts += timeouts
                        if not record.get("exists"):
                            failures.append(_failure_reason("ROS_SENSOR_TOPIC_MISSING", record.get("topic_name") or requirement.suffix))
                        elif int(record.get("publisher_count") or 0) < 1:
                            failures.append(_failure_reason("ROS_SENSOR_TOPIC_MISSING", "No publisher: " + str(record.get("topic_name") or requirement.suffix)))
                        elif not record.get("message_received"):
                            failures.append(_failure_reason("ROS_SENSOR_TIMEOUT", record.get("topic_name") or requirement.suffix))
                        if non_finite:
                            failures.append(_failure_reason("ROS_SENSOR_INVALID_VALUE", record.get("topic_name") or requirement.suffix))
                measurements = {
                    "sensor_capabilities": [item.to_dict(spec.namespace) for item in requirements],
                    "sensor_topics": topic_records,
                    "mandatory_sensor_topics_valid": mandatory_valid,
                    "mandatory_timestamp_rollback_count": mandatory_rollbacks,
                    "mandatory_non_finite_value_count": mandatory_non_finite,
                    "mandatory_message_timeout_count": mandatory_timeouts,
                    "temperature_availability": next(
                        (item["availability_status"] for item in topic_records if item["capability"] == "temperature"),
                        "UNSUPPORTED",
                    ),
                    "owned_by_test": session.owned_by_test,
                }
                rules = [
                    {"metric": "mandatory_sensor_topics_valid", "operator": "==", "expected": True},
                    {"metric": "mandatory_timestamp_rollback_count", "operator": "==", "expected": 0},
                    {"metric": "mandatory_non_finite_value_count", "operator": "==", "expected": 0},
                    {"metric": "mandatory_message_timeout_count", "operator": "==", "expected": 0},
                ]
                result = _sub_result(device, measurements, rules, spec.to_dict(), failures)
                imu = next((item for item in topic_records if item["capability"] == "imu"), None)
                if imu:
                    context.log("INFO", f"[{definition.test_id}] IMU topic detected: {imu.get('topic_name')}.")
                    context.log("INFO", f"[{definition.test_id}] Received {imu.get('sample_count', 0)} IMU messages.")
                    context.log("INFO", f"[{definition.test_id}] Timestamp rollback: {imu.get('timestamp_rollback_count', 0)}.")
            except RosRemoteError as exc:
                result = _sub_result(
                    device, {"mandatory_sensor_topics_valid": False},
                    [{"metric": "mandatory_sensor_topics_valid", "operator": "==", "expected": True}],
                    {}, [_failure_reason(exc.code, str(exc))],
                )
            finally:
                if session is not None:
                    cleaned, cleanup_error = self.cleanup_session(context, session)
                    if result is not None:
                        result["measurements"]["cleanup_success"] = cleaned
                        if not cleaned:
                            result["status"] = "FAIL"
                            result["failure_reasons"].append(_failure_reason("ROS_NODE_EXITED", cleanup_error or "ROS session cleanup failed."))
            results.append(result)
            context.log("PASS" if result["status"] == "PASS" else "FAIL", f"[{definition.test_id}][SN{device.serial}] {result['status']}.")
        return RosNodeLaunchHandler._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "collection_timeout_s": definition.parameters.get("collection_timeout_s"),
            "sample_count": definition.parameters.get("sample_count"),
        }, results


class RosQosMatrixHandler(RosHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        manager = context.services["ros_process_manager"]
        for device in self.devices(context):
            context.checkpoint(time.monotonic())
            session = None
            result = None
            failures = []
            try:
                adapter, spec, session, status = self.ensure_session(context, definition, device, environment)
                if not status.get("node_alive"):
                    raise RosRemoteError("ROS_NODE_TIMEOUT", "Required ROS camera node is not alive.")
                matrix_result = manager.qos_matrix(
                    session, spec, adapter.qos_topics(device),
                    float(definition.parameters.get("graph_discovery_timeout_s") or 5),
                    float(definition.parameters.get("subscriber_timeout_s") or 8),
                    int(definition.parameters.get("sample_count") or 3),
                )
                context.checkpoint(time.monotonic())
                matrix = list((matrix_result.get("topics") or {}).values())
                discovered = bool(matrix) and all(item.get("qos_metadata_discovered") for item in matrix)
                compatible = bool(matrix) and all(
                    int(item.get("compatible_message_count") or 0) > 0
                    and not item.get("compatible_timeout") for item in matrix
                )
                negative_valid = all(
                    item.get("negative_actual_result") == item.get("negative_expected_result")
                    if item.get("negative_test_attempted") else
                    item.get("negative_actual_result") in {"NOT_APPLICABLE", "SKIPPED"}
                    for item in matrix
                )
                missing_publishers = [
                    item.get("topic") for item in matrix
                    if not item.get("qos_metadata_discovered")
                ]
                if not discovered:
                    failures.append(_failure_reason(
                        "ROS_QOS_DISCOVERY_FAILED",
                        "Publisher endpoint QoS metadata was not discovered for: "
                        + ", ".join(str(topic) for topic in missing_publishers),
                    ))
                if discovered and not compatible:
                    failures.append(_failure_reason("ROS_QOS_MESSAGE_TIMEOUT", "A compatible subscriber did not receive messages."))
                if not negative_valid:
                    failures.append(_failure_reason("ROS_QOS_VALIDATION_FAILED", "Negative QoS behavior did not match request/offered expectations."))
                measurements = {
                    "qos_matrix": matrix,
                    "publisher_qos_discovered": discovered,
                    "qos_discovery_failed_topics": missing_publishers,
                    "compatible_subscribers_received": compatible,
                    "negative_qos_valid": negative_valid,
                    "negative_not_applicable_count": sum(not item.get("negative_test_attempted") for item in matrix),
                    "probe_environment": matrix_result.get("probe_environment") or {},
                    "owned_by_test": session.owned_by_test,
                }
                rules = [
                    {"metric": "publisher_qos_discovered", "operator": "==", "expected": True},
                    {"metric": "compatible_subscribers_received", "operator": "==", "expected": True},
                    {"metric": "negative_qos_valid", "operator": "==", "expected": True},
                ]
                result = _sub_result(device, measurements, rules, spec.to_dict(), failures)
                context.log(
                    "INFO" if discovered else "FAIL",
                    f"[{definition.test_id}] Publisher QoS "
                    + ("discovered." if discovered else "discovery failed."),
                )
                received = sum(int(item.get("compatible_message_count") or 0) for item in matrix)
                context.log("INFO", f"[{definition.test_id}] Compatible subscriber received {received} messages.")
                if negative_valid:
                    context.log("INFO", f"[{definition.test_id}] Negative QoS behavior matched expectation or was not applicable.")
            except RosRemoteError as exc:
                result = _sub_result(
                    device, {"publisher_qos_discovered": False},
                    [{"metric": "publisher_qos_discovered", "operator": "==", "expected": True}],
                    {}, [_failure_reason(exc.code, str(exc))],
                )
            finally:
                if session is not None:
                    cleaned, cleanup_error = self.cleanup_session(context, session)
                    if result is not None:
                        result["measurements"]["cleanup_success"] = cleaned
                        if not cleaned:
                            result["status"] = "FAIL"
                            result["failure_reasons"].append(_failure_reason("ROS_NODE_EXITED", cleanup_error or "ROS session cleanup failed."))
            results.append(result)
            context.log("PASS" if result["status"] == "PASS" else "FAIL", f"[{definition.test_id}][SN{device.serial}] {result['status']}.")
        return RosNodeLaunchHandler._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "subscriber_timeout_s": definition.parameters.get("subscriber_timeout_s"),
            "graph_discovery_timeout_s": definition.parameters.get("graph_discovery_timeout_s"),
            "sample_count": definition.parameters.get("sample_count"),
        }, results


class RosBagIntegrityHandler(RosHandlerBase):
    def setup(self, context, definition):
        super().setup(context, definition)
        self._bag_sessions = []
        self._cleanup_timeout_s = float(
            definition.parameters.get("cleanup_timeout_s") or 8
        )
        self.partial_measurements = {}
        self.partial_configuration = {
            "target_scope": self.target_scope(context),
            "record_duration_s": definition.parameters.get("record_duration_s"),
        }

    def cleanup(self, context, definition):
        errors = []
        manager = context.services["ros_process_manager"]
        for session in reversed(self._bag_sessions):
            try:
                stopped = manager.stop_bag_process(
                    session, self._cleanup_timeout_s
                )
                if not (stopped.get("stopped") or stopped.get("external")):
                    errors.append(
                        f"Owned {session.kind} process {session.session_id} did not stop."
                    )
            except Exception as exc:
                errors.append(str(exc))
        self._bag_sessions = []
        try:
            super().cleanup(context, definition)
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def _stop_bag(self, manager, session, timeout_s=None):
        response = manager.stop_bag_process(
            session, timeout_s or self._cleanup_timeout_s
        )
        if response.get("stopped") and session in self._bag_sessions:
            self._bag_sessions.remove(session)
        return response

    @staticmethod
    def _resolved_topics(collection, requirements):
        result = []
        records = collection.get("topics") or {}
        for index, requirement in enumerate(requirements):
            record = records.get(f"{requirement.capability}:{index}", {})
            result.append((requirement, record))
        return result

    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        results = []
        manager = context.services["ros_process_manager"]
        record_duration = float(definition.parameters.get("record_duration_s") or 15)
        replay_timeout = float(definition.parameters.get("replay_timeout_s") or 12)
        for device in self.devices(context):
            context.checkpoint(time.monotonic())
            camera_session = None
            spec = None
            record_session = None
            replay_session = None
            result = None
            failures = []
            cleanup_success = True
            measurements = {
                "host_evidence_result_path": str(
                    context.result_root + "/" + definition.test_id + "/result.json"
                ),
                "configured_record_duration_s": record_duration,
                "record_duration_s": 0.0,
                "recorded_topics": [],
                "message_count_by_topic": {},
                "replay_topics": [],
                "replay_message_count_by_topic": {},
                "deserialize_error_count": 0,
                "cleanup_success": False,
            }
            try:
                adapter, spec, camera_session, status = self.ensure_session(
                    context, definition, device, environment
                )
                if not status.get("node_alive"):
                    raise RosRemoteError("ROS_NODE_TIMEOUT", "Required ROS camera node is not alive.")
                requirements = adapter.bag_topics(device)
                resolution = manager.collect_capabilities(
                    camera_session, spec, requirements,
                    float(definition.parameters.get("warmup_s") or 0.5),
                    float(definition.parameters.get("topic_resolution_timeout_s") or 8),
                    1,
                )
                resolved = self._resolved_topics(resolution, requirements)
                selected = [
                    record.get("topic_name") for requirement, record in resolved
                    if record.get("topic_name") and (
                        requirement.availability != "MANDATORY" or _topic_ready(record)
                    ) and record.get("exists")
                ]
                mandatory = [
                    record.get("topic_name") for requirement, record in resolved
                    if requirement.availability == "MANDATORY" and record.get("topic_name")
                ]
                missing_live = [
                    record.get("topic_name") or requirement.suffix
                    for requirement, record in resolved
                    if requirement.availability == "MANDATORY" and not _topic_ready(record)
                ]
                if missing_live:
                    raise RosRemoteError(
                        "ROSBAG_TOPIC_MISSING",
                        "Mandatory live topics were unavailable: " + ", ".join(missing_live),
                    )
                preflight = manager.bag_preflight(
                    camera_session.setup_files,
                    float(definition.parameters.get("metadata_timeout_s") or 8),
                )
                if not preflight.get("available"):
                    raise RosRemoteError(
                        "ROSBAG_UNAVAILABLE",
                        "; ".join(preflight.get("errors") or ["rosbag2 record/play is unavailable"]),
                    )
                measurements.update({
                    "selected_topics": selected,
                    "mandatory_topics": mandatory,
                    "bag_storage_format": preflight.get("default_storage"),
                })
                context.log("INFO", f"[{definition.test_id}] Recording selected topics for {record_duration:g} seconds.")
                record_session = manager.start_bag_record(
                    camera_session.setup_files, selected,
                    float(definition.parameters.get("record_startup_timeout_s") or 8),
                )
                self._bag_sessions.append(record_session)
                measurements.update({
                    "bag_path": record_session.bag_path,
                    "record_started": True,
                    "record_session": record_session.to_dict(),
                })
                self.partial_measurements = dict(measurements)
                started = time.monotonic()
                next_status = started
                while time.monotonic() - started < record_duration:
                    context.checkpoint(time.monotonic())
                    now = time.monotonic()
                    if now >= next_status:
                        process_status = manager.bag_status(
                            record_session,
                            float(definition.parameters.get("record_startup_timeout_s") or 8),
                        )
                        if not process_status.get("process_alive"):
                            raise RosRemoteError(
                                "ROSBAG_RECORD_FAILED",
                                process_status.get("process_error")
                                or process_status.get("stderr_summary")
                                or "Recorder exited before the configured duration.",
                            )
                        next_status = now + 1.0
                    time.sleep(min(0.1, max(0.0, record_duration - (time.monotonic() - started))))
                measurements["record_duration_s"] = round(time.monotonic() - started, 3)
                record_stop = self._stop_bag(
                    manager, record_session,
                    float(definition.parameters.get("record_shutdown_timeout_s") or 8),
                )
                record_session = None
                measurements.update({
                    "record_process_exit_code": record_stop.get("process_exit_code"),
                    "record_errors": [record_stop.get("process_error")] if record_stop.get("process_error") else [],
                    "record_process_ok": bool(record_stop.get("stopped")) and not bool(record_stop.get("process_error")),
                })
                if not record_stop.get("stopped"):
                    raise RosRemoteError("ROSBAG_RECORD_FAILED", "Owned recorder did not stop cleanly.")
                if record_stop.get("process_error"):
                    raise RosRemoteError("ROSBAG_RECORD_FAILED", str(record_stop["process_error"]))
                inspection = manager.inspect_bag(
                    type("RecordedBag", (), {"bag_path": measurements["bag_path"]})(),
                    float(definition.parameters.get("metadata_timeout_s") or 8),
                )
                measurements.update(inspection)
                counts = inspection.get("message_count_by_topic") or {}
                metadata_valid = (
                    inspection.get("bag_directory_exists")
                    and inspection.get("metadata_exists")
                    and inspection.get("metadata_readable")
                    and bool(inspection.get("storage_files"))
                    and float(inspection.get("record_duration_s") or 0) > 0
                )
                mandatory_recorded = all(int(counts.get(topic) or 0) > 0 for topic in mandatory)
                measurements["metadata_valid"] = metadata_valid
                measurements["mandatory_topics_recorded"] = mandatory_recorded
                if not metadata_valid:
                    failures.append(_failure_reason("ROSBAG_METADATA_INVALID", inspection.get("metadata_error") or "Bag metadata/storage is invalid."))
                if not mandatory_recorded:
                    failures.append(_failure_reason("ROSBAG_TOPIC_MISSING", "A mandatory selected topic is missing or has zero messages."))
                context.log("INFO", f"[{definition.test_id}] Bag metadata {'valid' if metadata_valid else 'invalid'}.")

                if camera_session.owned_by_test:
                    isolation = "STOP_TEST_OWNED_CAMERA"
                    cleaned, cleanup_error = self.cleanup_session(context, camera_session)
                    camera_session = None
                    if not cleaned:
                        raise RosRemoteError("ROSBAG_REPLAY_FAILED", cleanup_error or "Unable to stop test-owned camera before replay.")
                    remappings = {}
                elif preflight.get("supports_remap"):
                    isolation = "TEST_NAMESPACE_REMAP"
                    prefix = "/test_replay/" + record_stop.get("session", {}).get("session_id", "phase83b")[:12]
                    remappings = {topic: prefix + topic for topic in selected}
                else:
                    isolation = "BLOCKED_EXTERNAL_NODE_NO_REMAP"
                    measurements.update({
                        "replay_isolation_strategy": isolation,
                        "replay_started": False,
                        "replay_blocked": True,
                    })
                    result = _sub_result(
                        device, measurements,
                        [{"metric": "replay_started", "operator": "==", "expected": True}],
                        spec.to_dict(),
                        [_failure_reason("ROSBAG_REPLAY_ISOLATION_UNAVAILABLE", "External camera node is active and installed rosbag play cannot safely remap topics.")],
                        status_override="BLOCKED",
                    )
                    raise RosRemoteError("ROSBAG_REPLAY_ISOLATION_BLOCKED_HANDLED", "")

                replay_topics = {remappings.get(topic, topic): (inspection.get("message_type_by_topic") or {}).get(topic) for topic in mandatory}
                if any(not message_type for message_type in replay_topics.values()):
                    raise RosRemoteError("ROSBAG_METADATA_INVALID", "Mandatory topic type metadata is missing.")
                measurements.update({
                    "replay_isolation_strategy": isolation,
                    "replay_topics": list(replay_topics),
                })
                context.log("INFO", f"[{definition.test_id}] Starting replay.")
                record_reference = type("RecordedBag", (), {
                    "setup_files": tuple(environment.get("setup_files") or ()),
                    "bag_path": measurements["bag_path"],
                })()
                replay_session = manager.start_bag_replay(
                    record_reference, selected, remappings,
                    float(definition.parameters.get("replay_startup_timeout_s") or 8),
                )
                self._bag_sessions.append(replay_session)
                measurements["replay_started"] = True
                replay_start_status = manager.bag_status(
                    replay_session,
                    float(definition.parameters.get("replay_startup_timeout_s") or 8),
                )
                if not replay_start_status.get("process_alive"):
                    raise RosRemoteError(
                        "ROSBAG_REPLAY_FAILED",
                        replay_start_status.get("process_error")
                        or replay_start_status.get("stderr_summary")
                        or "Replay process exited during startup.",
                    )
                replay_collection = manager.collect_topic_names(
                    replay_session.setup_files, replay_topics, replay_timeout,
                    int(definition.parameters.get("replay_sample_count") or 1),
                )
                context.checkpoint(time.monotonic())
                replay_records = replay_collection.get("topics") or {}
                replay_counts = {
                    topic: int(replay_records.get(topic, {}).get("sample_count") or 0)
                    for topic in replay_topics
                }
                deserialize_errors = sum(
                    int(replay_records.get(topic, {}).get("invalid_sample_count") or 0)
                    for topic in replay_topics
                )
                replay_valid = all(count > 0 for count in replay_counts.values())
                measurements.update({
                    "replay_message_count_by_topic": replay_counts,
                    "deserialize_error_count": deserialize_errors,
                    "mandatory_replay_messages_received": replay_valid,
                })
                if not replay_valid:
                    failures.append(_failure_reason("ROSBAG_REPLAY_TIMEOUT", "Mandatory replay messages were not received."))
                if deserialize_errors:
                    failures.append(_failure_reason("ROSBAG_DESERIALIZE_ERROR", f"Replay produced {deserialize_errors} invalid/deserialization samples."))
                context.log("INFO", f"[{definition.test_id}] Replay messages {'received' if replay_valid else 'timed out'}.")
            except RosRemoteError as exc:
                if exc.code != "ROSBAG_REPLAY_ISOLATION_BLOCKED_HANDLED":
                    failures.append(_failure_reason(exc.code, str(exc)))
                measurements.setdefault("record_started", False)
                measurements.setdefault("replay_started", False)
            finally:
                if replay_session is not None:
                    try:
                        replay_stop = self._stop_bag(manager, replay_session)
                        measurements["replay_process_exit_code"] = replay_stop.get("process_exit_code")
                        cleanup_success = cleanup_success and bool(replay_stop.get("stopped"))
                        if replay_stop.get("process_error"):
                            failures.append(_failure_reason("ROSBAG_REPLAY_FAILED", str(replay_stop["process_error"])))
                    except Exception as exc:
                        cleanup_success = False
                        failures.append(_failure_reason("ROSBAG_REPLAY_FAILED", str(exc)))
                if record_session is not None:
                    try:
                        cleanup_success = cleanup_success and bool(self._stop_bag(manager, record_session).get("stopped"))
                    except Exception as exc:
                        cleanup_success = False
                        failures.append(_failure_reason("ROSBAG_RECORD_FAILED", str(exc)))
                if camera_session is not None:
                    cleaned, cleanup_error = self.cleanup_session(context, camera_session)
                    cleanup_success = cleanup_success and cleaned
                    if not cleaned:
                        failures.append(_failure_reason("ROS_NODE_EXITED", cleanup_error or "ROS session cleanup failed."))
                measurements["cleanup_success"] = cleanup_success
                self.partial_measurements = dict(measurements)

            if result is None:
                rules = [
                    {"metric": "record_started", "operator": "==", "expected": True},
                    {"metric": "record_process_ok", "operator": "==", "expected": True},
                    {"metric": "metadata_valid", "operator": "==", "expected": True},
                    {"metric": "mandatory_topics_recorded", "operator": "==", "expected": True},
                    {"metric": "replay_started", "operator": "==", "expected": True},
                    {"metric": "mandatory_replay_messages_received", "operator": "==", "expected": True},
                    {"metric": "deserialize_error_count", "operator": "==", "expected": 0},
                    {"metric": "cleanup_success", "operator": "==", "expected": True},
                ]
                result = _sub_result(device, measurements, rules, spec.to_dict() if spec else {}, failures)
            results.append(result)
            context.log("PASS" if result["status"] == "PASS" else "FAIL", f"[{definition.test_id}][SN{device.serial}] {result['status']}.")
        return RosNodeLaunchHandler._aggregate(context, environment, results), {
            "target_scope": self.target_scope(context),
            "record_duration_s": record_duration,
            "replay_timeout_s": replay_timeout,
        }, results


def register_ros_camera_handlers(registry):
    registry.register("ros.environment", RosEnvironmentHandler())
    registry.register("ros.node_launch", RosNodeLaunchHandler())
    registry.register("ros.topic_health", RosTopicHealthHandler())
    registry.register("ros.image_profile", RosImageProfileHandler())
    registry.register("ros.camera_info", RosCameraInfoHandler())
    registry.register("ros.sensor_topics", RosSensorTopicsHandler())
    registry.register("ros.qos_matrix", RosQosMatrixHandler())
    registry.register("ros.bag_integrity", RosBagIntegrityHandler())
