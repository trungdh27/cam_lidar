import time

from core.testing.models import utc_now
from devices.camera.ros_automation.handlers import (
    RosHandlerBase,
    RosNodeLaunchHandler,
    _failure_reason,
    _record_for_capability,
    _sub_result,
    _topic_ready,
)
from devices.camera.ros_automation.remote import RosRemoteError


EXTERNAL_SESSION_CODE = "RECOVERY_REQUIRES_TEST_OWNED_SESSION"


class RosRecoveryHandlerBase(RosHandlerBase):
    """Shared bounded health, loss, ownership, and cleanup operations."""

    def setup(self, context, definition):
        super().setup(context, definition)
        self.partial_measurements = {"cycles": []}
        self.partial_configuration = {
            "target_scope": self.target_scope(context),
            **dict(definition.parameters),
        }

    @staticmethod
    def _primary_record(manager, session, spec, requirement, timeout_s, sample_count=1):
        collection = manager.collect_capabilities(
            session,
            spec,
            (requirement,),
            0,
            float(timeout_s),
            int(sample_count),
        )
        return _record_for_capability(collection, requirement.capability), collection

    @staticmethod
    def _session_metadata(session):
        return {
            "session_id": session.session_id,
            "device_uid": session.device_uid,
            "driver": session.driver,
            "namespace": session.namespace,
            "pid": session.pid,
            "process_group": session.process_group,
            "started_at": session.started_at,
            "log_path": session.log_path,
            "owned_by_test": session.owned_by_test,
            "launch_command_summary": session.launch_command_summary,
        }

    @staticmethod
    def _require_owned(session):
        if not session.owned_by_test:
            raise RosRemoteError(
                EXTERNAL_SESSION_CODE,
                "Recovery fault injection requires a newly created test-owned ROS session; the matching node is externally owned.",
            )

    @staticmethod
    def _audit(
        manager, setup_files, sessions=(), expected_nodes=(), timeout_s=8,
        expected_topics=(),
    ):
        if hasattr(manager, "audit_owned"):
            return manager.audit_owned(
                setup_files,
                sessions=sessions,
                expected_nodes=expected_nodes,
                timeout_s=float(timeout_s),
                expected_topics=expected_topics,
            )
        # Compatibility fallback for deterministic unit-test managers that
        # expose only the pre-recovery start/status/stop facade.
        states = {}
        active = 0
        present = []
        for session in sessions:
            try:
                status = manager.status(session)
            except Exception:
                status = {}
            alive = bool(status.get("process_alive"))
            states[session.session_id] = {
                "metadata_present": alive,
                "identity_matches": True,
                "process_alive": alive,
                "remaining_pids": [session.pid] if alive and session.pid else [],
            }
            active += int(alive)
            if status.get("node_alive") and session.expected_node in expected_nodes:
                present.append(session.expected_node)
        return {
            "session_states": states,
            "active_owned_session_ids": [item.session_id for item in sessions if states[item.session_id]["process_alive"]],
            "active_owned_process_count": active,
            "owned_runtime_directory_count": 0,
            "node_names": list(present),
            "expected_nodes_present": present,
            "publisher_counts": {},
        }

    def _wait_for_node_absent(
        self, context, manager, session, spec, requirement, timeout_s,
    ):
        """Wait on direct graph conditions before reusing a dead session namespace."""
        started = time.monotonic()
        deadline = started + float(timeout_s)
        topic = requirement.topic(spec.namespace)
        last = {}
        external_active = False
        while time.monotonic() < deadline:
            context.checkpoint(time.monotonic())
            remaining = max(0.1, deadline - time.monotonic())
            last = self._audit(
                manager, session.setup_files, (), (session.expected_node,),
                min(remaining, 8), expected_topics=(topic,),
            )
            node_present = session.expected_node in (
                last.get("expected_nodes_present") or ()
            )
            publishers = int((last.get("publisher_counts") or {}).get(topic) or 0)
            graph_ok = last.get("graph_probe_ok", True)
            if graph_ok and not node_present and publishers == 0:
                return {
                    "node_disappearance_detected": True,
                    "node_disappearance_latency_s": round(time.monotonic() - started, 3),
                    "stale_graph_wait_s": round(time.monotonic() - started, 3),
                    "old_publisher_absent": True,
                    "external_active": False,
                    "audit": last,
                }
            # A same-name node plus a live publisher and live messages after
            # the owned group is dead is an external conflict, never stale
            # graph state.  This probe is read-only and touches no process.
            if graph_ok and node_present and publishers > 0:
                record, _ = self._primary_record(
                    manager, session, spec, requirement,
                    min(1.0, remaining), 1,
                )
                if bool(record.get("message_received")):
                    external_active = True
                    break
            time.sleep(0.15)
        return {
            "node_disappearance_detected": False,
            "node_disappearance_latency_s": None,
            "stale_graph_wait_s": round(time.monotonic() - started, 3),
            "old_publisher_absent": bool(
                not int((last.get("publisher_counts") or {}).get(topic) or 0)
            ),
            "external_active": external_active,
            "audit": last,
        }

    def _poll_absent(
        self, context, manager, session, setup_files, process_timeout_s,
        node_timeout_s,
    ):
        started = time.monotonic()
        process_deadline = started + float(process_timeout_s)
        node_deadline = started + float(node_timeout_s)
        process_exit_at = None
        node_loss_at = None
        last = {}
        while time.monotonic() < max(process_deadline, node_deadline):
            context.checkpoint(time.monotonic())
            last = self._audit(
                manager,
                setup_files,
                (session,),
                (session.expected_node,),
                min(float(process_timeout_s), float(node_timeout_s), 8),
            )
            state = (last.get("session_states") or {}).get(session.session_id, {})
            now = time.monotonic()
            if process_exit_at is None and not state.get("process_alive"):
                process_exit_at = now
            if (
                node_loss_at is None
                and last.get("graph_probe_ok", True)
                and session.expected_node not in (last.get("expected_nodes_present") or ())
            ):
                node_loss_at = now
            process_done = process_exit_at is not None or now >= process_deadline
            node_done = node_loss_at is not None or now >= node_deadline
            if process_done and node_done:
                break
            time.sleep(0.1)
        return {
            "process_exit_detected": process_exit_at is not None,
            "node_loss_detected": node_loss_at is not None,
            "process_exit_detection_latency_s": (
                round(process_exit_at - started, 3) if process_exit_at else None
            ),
            "node_loss_detection_latency_s": (
                round(node_loss_at - started, 3) if node_loss_at else None
            ),
            "detection_latency_s": (
                round(max(process_exit_at, node_loss_at) - started, 3)
                if process_exit_at and node_loss_at else None
            ),
            "graph_probe_method": last.get("graph_probe_method"),
            "graph_probe_ok": last.get("graph_probe_ok"),
            "graph_probe_error": last.get("graph_probe_error"),
            "audit": last,
        }

    def _poll_process_exit(self, context, manager, session, setup_files, timeout_s):
        """Boundedly confirm an exact owned process group has exited.

        Interruption recovery deliberately releases dead ownership before it
        waits for graph convergence.  ROS discovery is independent of process
        ownership and can lag a terminated launch process.
        """
        started = time.monotonic()
        deadline = started + float(timeout_s)
        last = {}
        while time.monotonic() < deadline:
            context.checkpoint(time.monotonic())
            remaining = max(0.1, deadline - time.monotonic())
            last = self._audit(
                manager, setup_files, (session,), (), min(remaining, 8),
            )
            state = (last.get("session_states") or {}).get(session.session_id, {})
            if not state.get("process_alive"):
                return {
                    "process_exit_detected": True,
                    "process_exit_detection_latency_s": round(
                        time.monotonic() - started, 3
                    ),
                    "audit": last,
                }
            time.sleep(0.1)
        return {
            "process_exit_detected": False,
            "process_exit_detection_latency_s": None,
            "audit": last,
        }

    def _release(self, manager, session):
        if hasattr(manager, "release_node"):
            response = manager.release_node(session)
            released = bool(response.get("released"))
        else:
            response = {"released": True}
            released = True
        if released and session in self._sessions:
            self._sessions.remove(session)
        return released, response

    def _reconcile_dead_session(self, manager, session, loss):
        """Release a known-owned dead session without waiting on ROS graph cache.

        Node disappearance remains an acceptance criterion where required, but
        it is not ownership: a dead owned process group with no children can
        and must be reconciled even while discovery converges.
        """
        if not session.owned_by_test:
            return False, {"external": True, "reason": EXTERNAL_SESSION_CODE}
        state = (loss.get("audit") or {}).get("session_states", {}).get(
            session.session_id, {}
        )
        children = state.get("remaining_pids") or ()
        process_dead = bool(loss.get("process_exit_detected")) and not bool(
            state.get("process_alive")
        )
        if not process_dead or children:
            return False, {
                "process_dead": process_dead,
                "remaining_pids": list(children),
            }
        released, response = self._release(manager, session)
        response = dict(response)
        response.update({
            "process_dead": process_dead,
            "remaining_pids": list(children),
        })
        return released, response

    def _stop_verify_release(self, context, definition, session):
        manager = context.services["ros_process_manager"]
        started = time.monotonic()
        stop = manager.stop_node(session)
        loss = self._poll_absent(
            context,
            manager,
            session,
            session.setup_files,
            definition.parameters.get("process_exit_timeout_s") or 8,
            definition.parameters.get("node_disappearance_timeout_s") or 8,
        )
        released = False
        release_response = {}
        if stop.get("stopped") and loss["process_exit_detected"] and loss["node_loss_detected"]:
            released, release_response = self._release(manager, session)
        return {
            "stop_success": bool(stop.get("stopped")),
            "shutdown_time_s": round(time.monotonic() - started, 3),
            "node_removed": loss["node_loss_detected"],
            "owned_process_remaining": not loss["process_exit_detected"],
            "cleanup_success": bool(
                stop.get("stopped")
                and loss["process_exit_detected"]
                and loss["node_loss_detected"]
                and released
            ),
            "exit_code": (
                (stop.get("process_status") or {}).get("exit_code")
                if isinstance(stop.get("process_status"), dict)
                else stop.get("process_exit_code")
            ),
            "stderr_summary": str(stop.get("stderr_summary") or "")[-8192:],
            "loss": loss,
            "released": released,
            "release_diagnostics": {
                "process_status": release_response.get("process_status"),
                "stderr_summary": str(release_response.get("stderr_summary") or "")[-8192:],
            },
        }

    @staticmethod
    def _aggregate(context, environment, results):
        aggregate = RosNodeLaunchHandler._aggregate(context, environment, results)
        flattened_cycles = []
        for result in results:
            for cycle in result.get("measurements", {}).get("cycles") or ():
                flattened_cycles.append({"device_uid": result["device_uid"], **cycle})
        aggregate["cycles"] = flattened_cycles
        count_metrics = (
            "cycle_count", "passed_cycle_count", "failed_cycle_count",
            "cleanup_failure_count", "orphan_process_count", "orphan_node_count",
            "stale_session_count", "ownership_leak_count",
            "invalid_session_remaining_count", "successful_cycle_count",
        )
        for key in count_metrics:
            values = [item.get("measurements", {}).get(key) for item in results]
            if any(value is not None for value in values):
                aggregate[key] = sum(int(value or 0) for value in values)
        startup_values = [
            float(cycle["startup_time_s"])
            for cycle in flattened_cycles
            if cycle.get("startup_time_s") is not None
        ]
        if startup_values:
            aggregate.update({
                "startup_time_min": round(min(startup_values), 3),
                "startup_time_avg": round(sum(startup_values) / len(startup_values), 3),
                "startup_time_max": round(max(startup_values), 3),
            })
        aggregate.setdefault("orphan_process_count", 0)
        return aggregate

    @staticmethod
    def _status_for_failures(failures):
        if failures and all(item.get("code") == EXTERNAL_SESSION_CODE for item in failures):
            return "BLOCKED"
        return None


class RosRepeatedLaunchStopHandler(RosRecoveryHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        manager = context.services["ros_process_manager"]
        configured_cycles = int(definition.parameters.get("cycle_count") or 5)
        results = []
        for device in self.devices(context):
            cycles = []
            failures = []
            spec = None
            for index in range(1, configured_cycles + 1):
                context.checkpoint(time.monotonic())
                context.log("INFO", f"[{definition.test_id}] Cycle {index}/{configured_cycles} starting.")
                cycle = {
                    "cycle_index": index,
                    "launch_success": False,
                    "startup_time_s": None,
                    "expected_node_found": False,
                    "representative_topic": None,
                    "message_received": False,
                    "stop_success": False,
                    "shutdown_time_s": None,
                    "node_removed": False,
                    "owned_process_remaining": False,
                    "exit_code": None,
                    "cleanup_success": False,
                    "warnings": [],
                }
                session = None
                try:
                    preflight = self._audit(
                        manager, (), (), (),
                        definition.parameters.get("cleanup_timeout_s") or 8,
                    )
                    if int(preflight.get("active_owned_process_count") or 0):
                        raise RosRemoteError(
                            "ROS_INVALID_SESSION_REMAINING",
                            "A previous test-owned ROS process remained before this cycle.",
                        )
                    adapter, spec, session, status = self.ensure_session(
                        context, definition, device, environment
                    )
                    self._require_owned(session)
                    cycle.update({
                        "launch_success": bool(status.get("process_alive") and status.get("node_alive")),
                        "startup_time_s": status.get("startup_time_s"),
                        "expected_node_found": bool(status.get("node_alive")),
                        "session": self._session_metadata(session),
                    })
                    if not cycle["launch_success"]:
                        code = "ROS_NODE_TIMEOUT" if status.get("process_alive") else "ROS_RECOVERY_INITIAL_HEALTH_FAILED"
                        raise RosRemoteError(code, "The cycle launch did not become healthy.")
                    requirement = adapter.primary_image_requirement(device)
                    record, _collection = self._primary_record(
                        manager, session, spec, requirement,
                        definition.parameters.get("message_timeout_s") or 8,
                    )
                    cycle["representative_topic"] = record.get("topic_name") or requirement.topic(spec.namespace)
                    cycle["message_received"] = _topic_ready(record)
                    if not cycle["message_received"]:
                        failures.append(_failure_reason("ROS_RECOVERY_MESSAGE_TIMEOUT", f"Cycle {index} received no representative message."))
                    else:
                        context.log("INFO", f"[{definition.test_id}] Stream verified.")
                    cleanup = self._stop_verify_release(context, definition, session)
                    cycle.update({key: cleanup[key] for key in (
                        "stop_success", "shutdown_time_s", "node_removed",
                        "owned_process_remaining", "exit_code", "cleanup_success",
                    )})
                    cycle["warnings"] = [cleanup["stderr_summary"][-1000:]] if cleanup["stderr_summary"] else []
                    session = None if cleanup["released"] else session
                    context.log("INFO", f"[{definition.test_id}] Session stopped.")
                    context.log("PASS" if cleanup["cleanup_success"] else "FAIL", f"[{definition.test_id}] Cleanup {'PASS' if cleanup['cleanup_success'] else 'FAIL'}.")
                    if not cleanup["node_removed"]:
                        failures.append(_failure_reason("ROS_NODE_LOSS_NOT_DETECTED", f"Cycle {index} node remained after stop."))
                    if cleanup["owned_process_remaining"]:
                        failures.append(_failure_reason("ROS_ORPHAN_PROCESS_DETECTED", f"Cycle {index} retained an owned process."))
                    if not cleanup["cleanup_success"]:
                        failures.append(_failure_reason("ROS_CLEANUP_FAILED", f"Cycle {index} cleanup failed."))
                except RosRemoteError as exc:
                    failures.append(_failure_reason(exc.code, str(exc)))
                    if session is not None and session.owned_by_test:
                        try:
                            cleanup = self._stop_verify_release(context, definition, session)
                            cycle.update({key: cleanup[key] for key in (
                                "stop_success", "shutdown_time_s", "node_removed",
                                "owned_process_remaining", "exit_code", "cleanup_success",
                            )})
                            session = None if cleanup["released"] else session
                        except Exception as cleanup_exc:
                            failures.append(_failure_reason("ROS_CLEANUP_FAILED", str(cleanup_exc)))
                finally:
                    cycles.append(cycle)
                    self.partial_measurements = {"cycles": list(cycles)}
                    if session is not None and session.owned_by_test:
                        # Retain for TestRunner's cancellation/error cleanup.
                        pass
                if any(item.get("code") == EXTERNAL_SESSION_CODE for item in failures):
                    break
            passed = sum(
                bool(cycle["launch_success"] and cycle["expected_node_found"]
                     and cycle["message_received"] and cycle["stop_success"]
                     and cycle["node_removed"] and not cycle["owned_process_remaining"]
                     and cycle["cleanup_success"])
                for cycle in cycles
            )
            startup = [float(item["startup_time_s"]) for item in cycles if item.get("startup_time_s") is not None]
            measurements = {
                "cycle_count": len(cycles),
                "configured_cycle_count": configured_cycles,
                "all_configured_cycles_complete": len(cycles) == configured_cycles,
                "passed_cycle_count": passed,
                "failed_cycle_count": configured_cycles - passed,
                "startup_time_min": round(min(startup), 3) if startup else None,
                "startup_time_avg": round(sum(startup) / len(startup), 3) if startup else None,
                "startup_time_max": round(max(startup), 3) if startup else None,
                "cleanup_failure_count": sum(not item["cleanup_success"] for item in cycles),
                "orphan_process_count": sum(bool(item["owned_process_remaining"]) for item in cycles),
                "cycles": cycles,
            }
            rules = [
                {"metric": "all_configured_cycles_complete", "operator": "==", "expected": True},
                {"metric": "failed_cycle_count", "operator": "==", "expected": 0},
                {"metric": "cleanup_failure_count", "operator": "==", "expected": 0},
                {"metric": "orphan_process_count", "operator": "==", "expected": 0},
            ]
            results.append(_sub_result(
                device, measurements, rules, spec.to_dict() if spec else {}, failures,
                self._status_for_failures(failures),
            ))
        aggregate = self._aggregate(context, environment, results)
        return aggregate, self.partial_configuration, results


class RosNodeExitRecoveryHandler(RosRecoveryHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        manager = context.services["ros_process_manager"]
        results = []
        for device in self.devices(context):
            failures = []
            initial = None
            recovered = None
            spec = None
            measurements = {
                "initial_session_healthy": False,
                "controlled_exit_injected": False,
                "owned_children_dead_after_fault": False,
                "process_exit_detected": False,
                "node_loss_detected": False,
                "topic_loss_detected": False,
                "stale_ownership_cleared": False,
                "recovery_started": False,
                "recovery_successful": False,
                "correct_camera_selected_after_recovery": False,
                "recovered_message_received": False,
                "final_cleanup_success": False,
                "orphan_process_count": 0,
            }
            try:
                adapter, spec, initial, status = self.ensure_session(context, definition, device, environment)
                self._require_owned(initial)
                requirement = adapter.primary_image_requirement(device)
                baseline, _ = self._primary_record(manager, initial, spec, requirement, definition.parameters.get("message_timeout_s") or 8)
                measurements["initial_session_healthy"] = bool(status.get("process_alive") and status.get("node_alive") and _topic_ready(baseline))
                measurements["initial_session"] = self._session_metadata(initial)
                if not measurements["initial_session_healthy"]:
                    raise RosRemoteError("ROS_RECOVERY_INITIAL_HEALTH_FAILED", "Initial owned ROS session was not healthy.")
                context.log("INFO", f"[{definition.test_id}] Initial session healthy.")
                context.log("INFO", f"[{definition.test_id}] Injecting controlled exit into test-owned process.")
                fault_started = time.monotonic()
                fault = manager.terminate_owned_node(initial, definition.parameters.get("process_exit_timeout_s") or 8)
                measurements["fault_signal"] = fault.get("signal")
                measurements["controlled_exit_injected"] = bool(fault.get("signal_sent"))
                measurements["fault_remaining_pids"] = list(fault.get("remaining_pids") or ())
                loss = self._poll_absent(
                    context, manager, initial, initial.setup_files,
                    definition.parameters.get("process_exit_timeout_s") or 8,
                    definition.parameters.get("node_disappearance_timeout_s") or 8,
                )
                measurements.update({key: loss[key] for key in (
                    "process_exit_detected", "node_loss_detected", "detection_latency_s",
                    "process_exit_detection_latency_s", "node_loss_detection_latency_s",
                    "graph_probe_method", "graph_probe_ok", "graph_probe_error",
                )})
                measurements["unexpected_exit_code"] = fault.get("process_exit_code")
                measurements["owned_children_dead_after_fault"] = not bool(
                    (loss.get("audit") or {}).get("session_states", {}).get(
                        initial.session_id, {}
                    ).get("remaining_pids") or ()
                )
                interrupted, _ = self._primary_record(
                    manager, initial, spec, requirement,
                    definition.parameters.get("message_timeout_s") or 8,
                )
                measurements["topic_loss_detected"] = not bool(interrupted.get("message_received"))
                if not measurements["process_exit_detected"]:
                    failures.append(_failure_reason("ROS_PROCESS_EXIT_NOT_DETECTED", "Owned process exit was not detected."))
                if not measurements["node_loss_detected"]:
                    failures.append(_failure_reason("ROS_NODE_LOSS_NOT_DETECTED", "Expected ROS node loss was not detected."))
                if not measurements["owned_children_dead_after_fault"]:
                    failures.append(_failure_reason("ROS_ORPHAN_PROCESS_DETECTED", "An owned child remained after controlled process-group termination."))
                if measurements["process_exit_detected"]:
                    released, release = self._reconcile_dead_session(
                        manager, initial, loss
                    )
                    measurements["stale_ownership_cleared"] = released
                    measurements["initial_exit_diagnostics"] = str(fault.get("stderr_summary") or release.get("stderr_summary") or "")[-8192:]
                    measurements["dead_session_reconciliation"] = release
                    initial = None if released else initial
                if not measurements["stale_ownership_cleared"]:
                    failures.append(_failure_reason("ROS_STALE_SESSION_DETECTED", "Terminated session ownership could not be released."))
                context.checkpoint(time.monotonic())
                measurements["recovery_started"] = True
                context.log("INFO", f"[{definition.test_id}] Starting recovery session.")
                _adapter, recovery_spec, recovered, recovered_status = self.ensure_session(
                    context, definition, device, environment,
                    definition.parameters.get("recovery_startup_timeout_s") or 25,
                )
                self._require_owned(recovered)
                recovered_record, _ = self._primary_record(
                    manager, recovered, recovery_spec, requirement,
                    definition.parameters.get("message_timeout_s") or 8,
                )
                measurements["correct_camera_selected_after_recovery"] = bool(
                    recovered_status.get("serial_verified")
                    and recovery_spec.selected_serial == device.serial
                )
                measurements["recovered_message_received"] = _topic_ready(recovered_record)
                measurements["recovery_successful"] = bool(
                    recovered_status.get("process_alive") and recovered_status.get("node_alive")
                    and measurements["correct_camera_selected_after_recovery"]
                    and measurements["recovered_message_received"]
                )
                if not measurements["recovery_successful"]:
                    code = "ROS_RECOVERY_MESSAGE_TIMEOUT" if not measurements["recovered_message_received"] else "ROS_RECOVERY_RESTART_FAILED"
                    failures.append(_failure_reason(code, "Recovered session did not become fully healthy."))
                else:
                    context.log("INFO", f"[{definition.test_id}] Recovered stream verified.")
                cleanup = self._stop_verify_release(context, definition, recovered)
                measurements["final_cleanup_success"] = cleanup["cleanup_success"]
                measurements["orphan_process_count"] = int(cleanup["owned_process_remaining"])
                recovered = None if cleanup["released"] else recovered
                if not cleanup["cleanup_success"]:
                    failures.append(_failure_reason("ROS_CLEANUP_FAILED", "Recovered session cleanup failed."))
                measurements["fault_to_recovery_start_s"] = round(time.monotonic() - fault_started, 3)
            except RosRemoteError as exc:
                failures.append(_failure_reason(exc.code, str(exc)))
                for stale in (recovered, initial):
                    if stale is not None and stale.owned_by_test:
                        try:
                            self._stop_verify_release(context, definition, stale)
                        except Exception:
                            pass
            rules = [
                {"metric": "initial_session_healthy", "operator": "==", "expected": True},
                {"metric": "process_exit_detected", "operator": "==", "expected": True},
                {"metric": "controlled_exit_injected", "operator": "==", "expected": True},
                {"metric": "owned_children_dead_after_fault", "operator": "==", "expected": True},
                {"metric": "node_loss_detected", "operator": "==", "expected": True},
                {"metric": "stale_ownership_cleared", "operator": "==", "expected": True},
                {"metric": "recovery_successful", "operator": "==", "expected": True},
                {"metric": "recovered_message_received", "operator": "==", "expected": True},
                {"metric": "final_cleanup_success", "operator": "==", "expected": True},
                {"metric": "orphan_process_count", "operator": "==", "expected": 0},
            ]
            results.append(_sub_result(device, measurements, rules, spec.to_dict() if spec else {}, failures, self._status_for_failures(failures)))
        aggregate = self._aggregate(context, environment, results)
        return aggregate, self.partial_configuration, results


class RosTopicInterruptionHandler(RosRecoveryHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        manager = context.services["ros_process_manager"]
        results = []
        for device in self.devices(context):
            failures = []
            session = None
            recovered = None
            spec = None
            measurements = {
                "baseline_stream_confirmed": False,
                "message_timeout_detected": False,
                "interruption_detected": False,
                "message_count_during_interruption": 0,
                "old_process_dead": False,
                "dead_session_released": False,
                "old_node_disappearance_detected": False,
                "old_node_disappearance_latency_s": None,
                "stale_graph_wait_s": 0.0,
                "old_publisher_absent": False,
                "recovery_started": False,
                "recovery_launch_started": False,
                "recovery_node_alive": False,
                "stream_recovered": False,
                "recovery_message_count": 0,
                "final_cleanup_success": False,
                "orphan_process_count": 0,
            }
            try:
                adapter, spec, session, status = self.ensure_session(context, definition, device, environment)
                self._require_owned(session)
                requirement = adapter.primary_image_requirement(device)
                baseline, _ = self._primary_record(
                    manager, session, spec, requirement,
                    definition.parameters.get("message_timeout_s") or 8,
                    definition.parameters.get("baseline_sample_count") or 3,
                )
                measurements.update({
                    "representative_topic": baseline.get("topic_name") or requirement.topic(spec.namespace),
                    "baseline_message_count": int(baseline.get("sample_count") or 0),
                    "baseline_rate_hz": baseline.get("measured_rate_hz"),
                    "last_message_timestamp": baseline.get("last_timestamp"),
                    "baseline_stream_confirmed": bool(status.get("node_alive") and _topic_ready(baseline)),
                    "initial_session": self._session_metadata(session),
                })
                if not measurements["baseline_stream_confirmed"]:
                    raise RosRemoteError("ROS_RECOVERY_INITIAL_HEALTH_FAILED", "A healthy baseline topic stream was not confirmed.")
                context.log("INFO", f"[{definition.test_id}] Baseline stream confirmed.")
                measurements["interruption_start_time"] = utc_now()
                interruption_started = time.monotonic()
                manager.terminate_owned_node(session, definition.parameters.get("process_exit_timeout_s") or 8)
                interrupted, _ = self._primary_record(
                    manager, session, spec, requirement,
                    definition.parameters.get("interruption_timeout_s") or 5,
                )
                count = int(interrupted.get("sample_count") or 0)
                measurements["message_count_during_interruption"] = count
                measurements["message_timeout_detected"] = not bool(interrupted.get("message_received"))
                measurements["interruption_detected"] = measurements["message_timeout_detected"] and count == 0
                measurements["interruption_detection_latency_s"] = round(time.monotonic() - interruption_started, 3)
                if not measurements["interruption_detected"]:
                    failures.append(_failure_reason("ROS_TOPIC_INTERRUPTION_NOT_DETECTED", "Messages continued or the bounded timeout did not classify interruption."))
                # Ownership can be reconciled as soon as the exact test-owned
                # group is dead.  Graph convergence is checked separately
                # below, after release, so a stale node is never mistaken for
                # an external process or allowed to block recovery immediately.
                loss = self._poll_process_exit(
                    context, manager, session, session.setup_files,
                    definition.parameters.get("process_exit_timeout_s") or 8,
                )
                measurements["old_process_dead"] = bool(loss["process_exit_detected"])
                dead_session = session
                if loss["process_exit_detected"]:
                    released, reconciliation = self._reconcile_dead_session(
                        manager, session, loss
                    )
                    session = None if released else session
                else:
                    released = False
                    reconciliation = {}
                measurements["dead_session_reconciliation"] = reconciliation
                measurements["dead_session_released"] = released
                if not released:
                    failures.append(_failure_reason("ROS_STALE_SESSION_DETECTED", "Interrupted session state could not be released."))
                if released:
                    graph_wait = self._wait_for_node_absent(
                        context, manager, dead_session, spec, requirement,
                        definition.parameters.get("node_disappearance_timeout_s") or 8,
                    )
                    measurements.update({
                        "old_node_disappearance_detected": graph_wait["node_disappearance_detected"],
                        "old_node_disappearance_latency_s": graph_wait["node_disappearance_latency_s"],
                        "stale_graph_wait_s": graph_wait["stale_graph_wait_s"],
                        "old_publisher_absent": graph_wait["old_publisher_absent"],
                    })
                    if graph_wait["external_active"]:
                        failures.append(_failure_reason(
                            EXTERNAL_SESSION_CODE,
                            "A matching node has an active publisher and messages after the owned session exited.",
                        ))
                    elif not graph_wait["node_disappearance_detected"]:
                        failures.append(_failure_reason(
                            "ROS_STALE_GRAPH_TIMEOUT",
                            "The old test node did not disappear from the direct ROS graph before recovery startup.",
                        ))
                    else:
                        measurements["recovery_start_time"] = utc_now()
                        recovery_started = time.monotonic()
                        measurements["recovery_started"] = True
                        measurements["recovery_launch_started"] = True
                        _adapter, recovery_spec, recovered, recovery_status = self.ensure_session(
                            context, definition, device, environment,
                            definition.parameters.get("recovery_startup_timeout_s") or 25,
                        )
                        self._require_owned(recovered)
                        recovered_record, _ = self._primary_record(
                            manager, recovered, recovery_spec, requirement,
                            definition.parameters.get("message_timeout_s") or 8,
                        )
                        measurements["recovery_node_alive"] = bool(
                            recovery_status.get("process_alive") and recovery_status.get("node_alive")
                            and recovery_status.get("serial_verified")
                        )
                        measurements["recovery_message_count"] = int(recovered_record.get("sample_count") or 0)
                        measurements["stream_recovered"] = bool(
                            measurements["recovery_node_alive"] and _topic_ready(recovered_record)
                        )
                        measurements["first_recovered_message_time"] = utc_now() if measurements["stream_recovered"] else None
                        measurements["recovery_latency_s"] = round(time.monotonic() - recovery_started, 3)
                        if not measurements["stream_recovered"]:
                            failures.append(_failure_reason("ROS_RECOVERY_MESSAGE_TIMEOUT", "Representative messages did not resume after recovery."))
                        cleanup = self._stop_verify_release(context, definition, recovered)
                        measurements["final_cleanup_success"] = cleanup["cleanup_success"]
                        measurements["orphan_process_count"] = int(cleanup["owned_process_remaining"])
                        recovered = None if cleanup["released"] else recovered
                        if not cleanup["cleanup_success"]:
                            failures.append(_failure_reason("ROS_CLEANUP_FAILED", "Recovered stream session cleanup failed."))
                # If recovery never started after a successful dead-session
                # reconciliation, cleanup is still complete for test-owned
                # resources.  The structured graph/external failure remains.
                if recovered is None and released and not measurements["recovery_started"]:
                    measurements["final_cleanup_success"] = True
            except RosRemoteError as exc:
                failures.append(_failure_reason(exc.code, str(exc)))
                for stale in (recovered, session):
                    if stale is not None and stale.owned_by_test:
                        try:
                            self._stop_verify_release(context, definition, stale)
                        except Exception:
                            pass
            rules = [
                {"metric": "baseline_stream_confirmed", "operator": "==", "expected": True},
                {"metric": "message_timeout_detected", "operator": "==", "expected": True},
                {"metric": "interruption_detected", "operator": "==", "expected": True},
                {"metric": "message_count_during_interruption", "operator": "==", "expected": 0},
                {"metric": "old_process_dead", "operator": "==", "expected": True},
                {"metric": "dead_session_released", "operator": "==", "expected": True},
                {"metric": "old_node_disappearance_detected", "operator": "==", "expected": True},
                {"metric": "old_publisher_absent", "operator": "==", "expected": True},
                {"metric": "recovery_node_alive", "operator": "==", "expected": True},
                {"metric": "stream_recovered", "operator": "==", "expected": True},
                {"metric": "recovery_message_count", "operator": ">", "expected": 0},
                {"metric": "final_cleanup_success", "operator": "==", "expected": True},
                {"metric": "orphan_process_count", "operator": "==", "expected": 0},
            ]
            results.append(_sub_result(device, measurements, rules, spec.to_dict() if spec else {}, failures, self._status_for_failures(failures)))
        aggregate = self._aggregate(context, environment, results)
        return aggregate, self.partial_configuration, results


class RosLaunchFailureHandler(RosRecoveryHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        manager = context.services["ros_process_manager"]
        registry = context.services["ros_adapter_registry"]
        results = []
        for device in self.devices(context):
            failures = []
            invalid_session = None
            valid_session = None
            valid_spec = None
            measurements = {
                "invalid_launch_attempted": False,
                "invalid_launch_rejected": False,
                "failure_detected": False,
                "error_category": None,
                "process_exit_code": None,
                "startup_timeout_triggered": False,
                "invalid_node_remaining": False,
                "invalid_process_remaining": False,
                "cleanup_success_after_failure": False,
                "valid_retry_started": False,
                "valid_retry_node_alive": False,
                "valid_retry_message_received": False,
                "valid_retry_cleanup_success": False,
                "orphan_process_count": 0,
                "invalid_session_remaining_count": 0,
            }
            adapter = registry.resolve(device)
            invalid_spec, invalid_summary = adapter.build_invalid_launch_spec(device)
            measurements["invalid_configuration_summary"] = invalid_summary
            measurements["invalid_expected_node"] = invalid_spec.expected_node
            measurements["invalid_namespace"] = invalid_spec.namespace
            try:
                measurements["invalid_launch_attempted"] = True
                context.log("INFO", f"[{definition.test_id}] Starting expected invalid launch.")
                if hasattr(manager, "inspect_launch_arguments"):
                    launch_arguments = manager.inspect_launch_arguments(
                        invalid_spec, environment.get("setup_files") or ()
                    )
                    launch_output = str(launch_arguments.get("output") or "")[-8000:]
                    measurements["installed_launch_arguments_checked"] = True
                    measurements["installed_launch_arguments_summary"] = launch_output
                    measurements["invalid_parameter_declared"] = (
                        "camera_model" in launch_output
                        if invalid_summary.get("strategy") == "zed_wrapper_invalid_camera_model"
                        else True
                    )
                    if not measurements["invalid_parameter_declared"]:
                        raise RosRemoteError(
                            "ROS_INVALID_LAUNCH_CONFIGURATION",
                            "Installed launch arguments do not declare the adapter's invalid failure-injection parameter.",
                        )
                else:
                    measurements["installed_launch_arguments_checked"] = False
                try:
                    invalid_session = manager.start_node(device, invalid_spec, environment.get("setup_files") or ())
                    self._sessions.append(invalid_session)
                    self._require_owned(invalid_session)
                    invalid_deadline = time.monotonic() + float(definition.parameters.get("invalid_launch_timeout_s") or 8)
                    invalid_status = {}
                    while time.monotonic() < invalid_deadline:
                        context.checkpoint(time.monotonic())
                        invalid_status = manager.status(invalid_session)
                        if not invalid_status.get("process_alive"):
                            measurements["invalid_launch_rejected"] = True
                            measurements["failure_detected"] = True
                            measurements["error_category"] = "ROS_LAUNCH_FAILED"
                            break
                        if invalid_status.get("node_alive"):
                            measurements["error_category"] = "ROS_EXPECTED_LAUNCH_FAILURE_NOT_DETECTED"
                            break
                        time.sleep(0.1)
                    else:
                        measurements["startup_timeout_triggered"] = True
                        measurements["invalid_launch_rejected"] = True
                        measurements["failure_detected"] = True
                        measurements["error_category"] = "ROS_NODE_TIMEOUT"
                    measurements["process_exit_code"] = invalid_status.get("process_exit_code")
                except RosRemoteError as exc:
                    if exc.code == EXTERNAL_SESSION_CODE:
                        raise
                    measurements["invalid_launch_rejected"] = True
                    measurements["failure_detected"] = True
                    measurements["error_category"] = exc.code
                    measurements["failure_message"] = str(exc)[-2000:]
                if not measurements["invalid_launch_rejected"]:
                    failures.append(_failure_reason("ROS_EXPECTED_LAUNCH_FAILURE_NOT_DETECTED", "The intentionally invalid launch unexpectedly became healthy."))
                else:
                    context.log("INFO", f"[{definition.test_id}] Invalid launch rejected as expected.")
                if invalid_session is not None:
                    cleanup = self._stop_verify_release(context, definition, invalid_session)
                    measurements["cleanup_success_after_failure"] = cleanup["cleanup_success"]
                    measurements["invalid_node_remaining"] = not cleanup["node_removed"]
                    measurements["invalid_process_remaining"] = cleanup["owned_process_remaining"]
                    invalid_session = None if cleanup["released"] else invalid_session
                else:
                    measurements["cleanup_success_after_failure"] = True
                measurements["invalid_session_remaining_count"] = int(
                    measurements["invalid_node_remaining"] or measurements["invalid_process_remaining"]
                )
                if not measurements["cleanup_success_after_failure"]:
                    failures.append(_failure_reason("ROS_CLEANUP_FAILED", "Failed-state cleanup did not complete."))
                if measurements["invalid_session_remaining_count"]:
                    failures.append(_failure_reason("ROS_INVALID_SESSION_REMAINING", "The invalid test session still has a process or node."))
                context.log("INFO", f"[{definition.test_id}] Starting valid retry.")
                measurements["valid_retry_started"] = True
                adapter, valid_spec, valid_session, valid_status = self.ensure_session(context, definition, device, environment)
                self._require_owned(valid_session)
                requirement = adapter.primary_image_requirement(device)
                retry_record, _ = self._primary_record(manager, valid_session, valid_spec, requirement, definition.parameters.get("message_timeout_s") or 8)
                measurements["valid_retry_node_alive"] = bool(valid_status.get("process_alive") and valid_status.get("node_alive") and valid_status.get("serial_verified"))
                measurements["valid_retry_message_received"] = _topic_ready(retry_record)
                if not measurements["valid_retry_node_alive"]:
                    failures.append(_failure_reason("ROS_RECOVERY_RESTART_FAILED", "The valid retry did not become healthy with the selected physical camera."))
                if not measurements["valid_retry_message_received"]:
                    failures.append(_failure_reason("ROS_RECOVERY_MESSAGE_TIMEOUT", "The valid retry produced no representative message."))
                cleanup = self._stop_verify_release(context, definition, valid_session)
                measurements["valid_retry_cleanup_success"] = cleanup["cleanup_success"]
                measurements["orphan_process_count"] = int(cleanup["owned_process_remaining"])
                valid_session = None if cleanup["released"] else valid_session
                if not cleanup["cleanup_success"]:
                    failures.append(_failure_reason("ROS_CLEANUP_FAILED", "Valid retry cleanup failed."))
            except RosRemoteError as exc:
                failures.append(_failure_reason(exc.code, str(exc)))
                for stale in (valid_session, invalid_session):
                    if stale is not None and stale.owned_by_test:
                        try:
                            self._stop_verify_release(context, definition, stale)
                        except Exception:
                            pass
            rules = [
                {"metric": "invalid_launch_rejected", "operator": "==", "expected": True},
                {"metric": "failure_detected", "operator": "==", "expected": True},
                {"metric": "invalid_session_remaining_count", "operator": "==", "expected": 0},
                {"metric": "cleanup_success_after_failure", "operator": "==", "expected": True},
                {"metric": "valid_retry_node_alive", "operator": "==", "expected": True},
                {"metric": "valid_retry_message_received", "operator": "==", "expected": True},
                {"metric": "valid_retry_cleanup_success", "operator": "==", "expected": True},
            ]
            results.append(_sub_result(device, measurements, rules, valid_spec.to_dict() if valid_spec else {}, failures, self._status_for_failures(failures)))
        aggregate = self._aggregate(context, environment, results)
        return aggregate, self.partial_configuration, results


class RosSessionCleanupHandler(RosRecoveryHandlerBase):
    def execute(self, context, definition):
        environment, _packages = self.require_runnable_environment(context, definition)
        manager = context.services["ros_process_manager"]
        configured_cycles = int(definition.parameters.get("cycle_count") or 10)
        results = []
        for device in self.devices(context):
            failures = []
            cycles = []
            created = []
            spec = context.services["ros_adapter_registry"].resolve(device).build_launch_spec(device)
            baseline = self._audit(manager, environment.get("setup_files") or (), (), (spec.expected_node,), definition.parameters.get("cleanup_timeout_s") or 8)
            baseline_nodes = set(baseline.get("node_names") or ())
            baseline_runtime_count = int(baseline.get("owned_runtime_directory_count") or 0)
            baseline_active = int(baseline.get("active_owned_process_count") or 0)
            for index in range(1, configured_cycles + 1):
                context.checkpoint(time.monotonic())
                session = None
                cycle = {
                    "cycle_index": index,
                    "launch_success": False,
                    "health_check_success": False,
                    "cleanup_success": False,
                    "owned_process_count_after": 0,
                    "node_remaining": False,
                    "stale_session_remaining": False,
                    "ownership_released": False,
                }
                context.log("INFO", f"[{definition.test_id}] Cleanup cycle {index}/{configured_cycles} starting.")
                try:
                    adapter, spec, session, status = self.ensure_session(context, definition, device, environment)
                    self._require_owned(session)
                    created.append(session)
                    cycle["launch_success"] = bool(status.get("process_alive") and status.get("node_alive"))
                    cycle["startup_time_s"] = status.get("startup_time_s")
                    requirement = adapter.primary_image_requirement(device)
                    record, _ = self._primary_record(manager, session, spec, requirement, definition.parameters.get("message_timeout_s") or 8)
                    cycle["health_check_success"] = cycle["launch_success"] and _topic_ready(record)
                    cleanup = self._stop_verify_release(context, definition, session)
                    session = None if cleanup["released"] else session
                    audit = self._audit(manager, environment.get("setup_files") or (), tuple(created), (spec.expected_node,), definition.parameters.get("cleanup_timeout_s") or 8)
                    created_states = audit.get("session_states") or {}
                    cycle.update({
                        "cleanup_success": cleanup["cleanup_success"],
                        "owned_process_count_after": sum(bool(item.get("process_alive")) for item in created_states.values()),
                        "node_remaining": spec.expected_node in (audit.get("expected_nodes_present") or ()),
                        "stale_session_remaining": any(item.get("metadata_present") for item in created_states.values()),
                        "ownership_released": cleanup["released"] and session is None,
                    })
                    if not cycle["health_check_success"]:
                        failures.append(_failure_reason("ROS_RECOVERY_INITIAL_HEALTH_FAILED", f"Cleanup cycle {index} health check failed."))
                    if cycle["owned_process_count_after"]:
                        failures.append(_failure_reason("ROS_ORPHAN_PROCESS_DETECTED", f"Cleanup cycle {index} retained an owned process."))
                    if cycle["node_remaining"]:
                        failures.append(_failure_reason("ROS_ORPHAN_NODE_DETECTED", f"Cleanup cycle {index} retained the test node."))
                    if cycle["stale_session_remaining"]:
                        failures.append(_failure_reason("ROS_STALE_SESSION_DETECTED", f"Cleanup cycle {index} retained session metadata."))
                    if not cycle["ownership_released"]:
                        failures.append(_failure_reason("ROS_OWNERSHIP_LEAK", f"Cleanup cycle {index} did not release ownership."))
                    if not cycle["cleanup_success"]:
                        failures.append(_failure_reason("ROS_CLEANUP_FAILED", f"Cleanup cycle {index} failed."))
                except RosRemoteError as exc:
                    failures.append(_failure_reason(exc.code, str(exc)))
                    if session is not None and session.owned_by_test:
                        try:
                            self._stop_verify_release(context, definition, session)
                        except Exception:
                            pass
                finally:
                    cycles.append(cycle)
                    self.partial_measurements = {"cycles": list(cycles)}
                if any(item.get("code") == EXTERNAL_SESSION_CODE for item in failures):
                    break
            final_audit = self._audit(manager, environment.get("setup_files") or (), tuple(created), (spec.expected_node,), definition.parameters.get("cleanup_timeout_s") or 8)
            states = final_audit.get("session_states") or {}
            orphan_process_count = sum(bool(item.get("process_alive")) for item in states.values())
            orphan_node_count = sum(
                node not in baseline_nodes
                for node in final_audit.get("expected_nodes_present") or ()
            )
            stale_session_count = sum(bool(item.get("metadata_present")) for item in states.values())
            ownership_leak_count = sum(not item.get("ownership_released") for item in cycles)
            cleanup_failure_count = sum(not item.get("cleanup_success") for item in cycles)
            successful = sum(
                item.get("launch_success") and item.get("health_check_success")
                and item.get("cleanup_success") and not item.get("owned_process_count_after")
                and not item.get("node_remaining") and not item.get("stale_session_remaining")
                and item.get("ownership_released")
                for item in cycles
            )
            measurements = {
                "baseline": {
                    "relevant_ros_nodes": sorted(baseline_nodes),
                    "test_owned_process_count": baseline_active,
                    "active_session_record_count": baseline_active,
                    "camera_ownership_state": "AVAILABLE",
                    "temporary_session_directory_count": baseline_runtime_count,
                },
                "final": {
                    "test_owned_process_count": int(final_audit.get("active_owned_process_count") or 0),
                    "temporary_session_directory_count": int(final_audit.get("owned_runtime_directory_count") or 0),
                },
                "cycle_count": len(cycles),
                "configured_cycle_count": configured_cycles,
                "all_configured_cycles_complete": len(cycles) == configured_cycles,
                "successful_cycle_count": successful,
                "cleanup_failure_count": cleanup_failure_count,
                "orphan_process_count": orphan_process_count,
                "orphan_node_count": orphan_node_count,
                "stale_session_count": stale_session_count,
                "ownership_leak_count": ownership_leak_count,
                "temporary_directory_leak_count": max(0, int(final_audit.get("owned_runtime_directory_count") or 0) - baseline_runtime_count),
                "next_normal_camera_ros_test_runnable": successful == configured_cycles,
                "cycles": cycles,
            }
            rules = [
                {"metric": "all_configured_cycles_complete", "operator": "==", "expected": True},
                {"metric": "successful_cycle_count", "operator": "==", "expected": configured_cycles},
                {"metric": "cleanup_failure_count", "operator": "==", "expected": 0},
                {"metric": "orphan_process_count", "operator": "==", "expected": 0},
                {"metric": "orphan_node_count", "operator": "==", "expected": 0},
                {"metric": "stale_session_count", "operator": "==", "expected": 0},
                {"metric": "ownership_leak_count", "operator": "==", "expected": 0},
                {"metric": "temporary_directory_leak_count", "operator": "==", "expected": 0},
                {"metric": "next_normal_camera_ros_test_runnable", "operator": "==", "expected": True},
            ]
            results.append(_sub_result(device, measurements, rules, spec.to_dict(), failures, self._status_for_failures(failures)))
        aggregate = self._aggregate(context, environment, results)
        return aggregate, self.partial_configuration, results


def register_ros_recovery_handlers(registry):
    registry.register("ros.recovery.launch_stop", RosRepeatedLaunchStopHandler())
    registry.register("ros.recovery.node_exit", RosNodeExitRecoveryHandler())
    registry.register("ros.recovery.topic_interruption", RosTopicInterruptionHandler())
    registry.register("ros.recovery.launch_failure", RosLaunchFailureHandler())
    registry.register("ros.recovery.cleanup", RosSessionCleanupHandler())
