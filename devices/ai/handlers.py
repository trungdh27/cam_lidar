import time

from core.testing.errors import TestBlockedError
from core.testing.evaluator import TestEvaluator
from devices.ai.models import AiModule, AiSession
from devices.ai.remote import AiRemoteError


def _reason(code, message):
    return {"code": code, "message": message}


class AiBlockedError(TestBlockedError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _sub_result(module, measurements, rules, configuration, failures=(), status=None):
    rule_results = TestEvaluator().evaluate(measurements, rules)
    reasons = list(failures)
    reasons.extend(
        _reason("ACCEPTANCE_RULE_FAILED", f"{item['metric']}={item['actual']} {item['operator']} {item['expected']}")
        for item in rule_results if not item["passed"]
    )
    return {
        "module_uid": module.module_uid, "display_name": module.display_name,
        "status": status or ("PASS" if not reasons else "FAIL"),
        "configuration": configuration, "measurements": measurements,
        "rules": rules, "rule_results": rule_results, "failure_reasons": reasons,
    }


class AiHandlerBase:
    def __init__(self):
        self._sessions = []
        self.partial_measurements = {}
        self.partial_configuration = {}

    def validate(self, context, _definition):
        if not context.services["remote_client"].connected:
            raise AiBlockedError("JETSON_NOT_CONNECTED", "Jetson is not connected. Connect from Dashboard first.")

    def setup(self, context, definition):
        self._sessions = []
        self.partial_measurements = {"modules": []}
        self.partial_configuration = {"target_scope": context.base_configuration.get("target_scope", "INDIVIDUAL"), **definition.parameters}

    def cleanup(self, context, _definition):
        manager = context.services["ai_process_manager"]
        errors = []
        for session in reversed(self._sessions):
            if not session.owned_by_test:
                continue
            try:
                response = manager.stop(session)
                if not response.get("stopped"):
                    errors.append("Owned AI session %s was not stopped" % session.session_id)
            except Exception as exc:
                errors.append(str(exc))
        self._sessions = []
        if errors:
            raise RuntimeError("; ".join(errors))

    @staticmethod
    def _environment(context):
        adapter = context.services["ai_runtime_adapter"]
        return adapter.discover_environment(context.services["ai_process_manager"])

    @staticmethod
    def _modules(context, environment):
        adapter = context.services["ai_runtime_adapter"]
        discovered = adapter.discover_modules(environment)
        snapshot = tuple(context.services.get("selected_ai_modules") or ())
        if not snapshot:
            return discovered
        discovered_by_uid = {item.module_uid: item for item in discovered}
        return tuple(discovered_by_uid.get(item.module_uid, item) for item in snapshot)

    def _require_modules(self, context, environment):
        modules = self._modules(context, environment)
        if not modules:
            raise AiBlockedError("AI_MODULE_NOT_DISCOVERED", "No AI module could be discovered or selected on the connected execution host.")
        return modules

    @staticmethod
    def _configuration(module, environment, session=None):
        return {
            "module": module.to_dict(), "module_uid": module.module_uid,
            "runtime": module.runtime, "function_type": module.function_type,
            "execution_host": environment.get("execution_host", "UNKNOWN"),
            "owned_by_test": session.owned_by_test if session else False,
        }

    def ensure_ai_session(self, context, definition, module, environment):
        manager = context.services["ai_process_manager"]
        adapter = context.services["ai_runtime_adapter"]
        external = module.status in {"EXTERNAL", "DISCOVERED", "RUNNING"} or bool(module.process_name or module.ros_node)
        if external:
            alive = bool(module.ros_node in (environment.get("candidate_nodes") or ()) or module.metadata.get("pid"))
            session = AiSession("external-" + module.module_uid, module.module_uid, owned_by_test=False,
                                expected_node=module.ros_node, setup_files=tuple(environment.get("setup_files") or ()))
            return session, {"process_alive": alive, "expected_node_alive": bool(module.ros_node and module.ros_node in (environment.get("candidate_nodes") or ())), "external_verified": alive, "startup_time_s": 0.0}
        if not adapter.can_launch(module):
            raise AiBlockedError("AI_LAUNCH_FAILED", "No approved launch specification is available for %s." % module.display_name)
        started = time.monotonic()
        session = adapter.start(manager, module, environment.get("setup_files") or ())
        self._sessions.append(session)
        deadline = time.monotonic() + float(definition.parameters.get("startup_timeout_s") or 25)
        status = {}
        while time.monotonic() < deadline:
            context.checkpoint(time.monotonic())
            status = adapter.status(manager, session)
            node_ok = not session.expected_node or bool(status.get("expected_node_alive"))
            if status.get("process_alive") and node_ok:
                status["startup_time_s"] = round(time.monotonic() - started, 3)
                return session, status
            if not status.get("process_alive"):
                status["startup_time_s"] = round(time.monotonic() - started, 3)
                return session, status
            time.sleep(.2)
        status["startup_timeout"] = True
        status["startup_time_s"] = round(time.monotonic() - started, 3)
        return session, status

    def _stop_session(self, context, session):
        if not session or not session.owned_by_test:
            return {"external": True, "stopped": False}
        response = context.services["ai_process_manager"].stop(session)
        if response.get("stopped") and session in self._sessions:
            self._sessions.remove(session)
        return response

    @staticmethod
    def _aggregate(environment, results):
        statuses = [item["status"] for item in results]
        return {
            "all_modules_pass": bool(statuses) and all(item == "PASS" for item in statuses),
            "module_count": len(results), "environment": environment,
            "aggregate_status": "BLOCKED" if any(item == "BLOCKED" for item in statuses) else "FAIL",
        }

    def _probe(self, context, session, endpoint, timeout_s, sample_count):
        return context.services["ai_runtime_adapter"].probe_endpoint(
            context.services["ai_process_manager"], session, endpoint, timeout_s, sample_count,
        )


class AiEnvironmentHandler(AiHandlerBase):
    def execute(self, context, definition):
        context.log("INFO", "[AI-001] Discovering AI environment.")
        environment = self._environment(context)
        modules = self._modules(context, environment)
        # AI-001 is allowed to block for a missing module, but discovery data
        # must survive in result.json so runtime availability is never hidden
        # behind the module prerequisite.
        if not modules:
            measurements = {
                "runtime_available": bool(environment.get("runtime_available")),
                "runtime_name": environment.get("runtime_name"),
                "runtime_version": environment.get("runtime_version"),
                "module_count": int(environment.get("module_count") or 0),
                "module_discovered": bool(environment.get("module_discovered")),
                "module_running": bool(environment.get("module_running")),
                "runtime_probe_results": environment.get("runtime_probe_results") or environment.get("runtimes") or [],
                "environment": environment,
            }
            rules = [{"metric": "module_discovered", "operator": "==", "expected": True}]
            reason = _reason("AI_MODULE_NOT_DISCOVERED", "No AI module could be discovered or selected on the connected execution host.")
            blocked = {
                "module_uid": "", "display_name": "AI Environment", "status": "BLOCKED",
                "configuration": {"execution_host": environment.get("execution_host", "UNKNOWN")},
                "measurements": measurements, "rules": rules,
                "rule_results": TestEvaluator().evaluate(measurements, rules),
                "failure_reasons": [reason],
            }
            aggregate = self._aggregate(environment, [blocked])
            aggregate.update({key: measurements[key] for key in (
                "runtime_available", "runtime_name", "runtime_version", "module_count",
                "module_discovered", "module_running", "runtime_probe_results",
            )})
            return aggregate, {"environment": environment}, [blocked]
        results = []
        installed_packages = set(environment.get("ai_packages") or ())
        runtimes = {item.get("name") for item in environment.get("runtimes") or () if item.get("available")}
        for module in modules:
            package_ok = not module.package or module.package in installed_packages
            runtime_ok = module.runtime in {"", "UNKNOWN"} or module.runtime in runtimes
            model_ok = bool(module.model_name or module.model_path or environment.get("model_candidates"))
            measurements = {
                "runtime_available": runtime_ok, "runtime_name": module.runtime or environment.get("runtime_name"),
                "runtime_version": environment.get("runtime_version"), "required_package_count": int(bool(module.package)),
                "package_results": {module.package: package_ok} if module.package else {},
                "ros_environment_ready": bool(environment.get("ros_available")), "model_configuration_found": model_ok,
                "fatal_environment_error_count": len(environment.get("errors") or ()), "candidate_module_count": len(modules),
                "module_count": int(environment.get("module_count") or len(modules)),
                "module_discovered": bool(environment.get("module_discovered", True)),
                "module_running": bool(environment.get("module_running")),
                "runtime_probe_results": environment.get("runtime_probe_results") or environment.get("runtimes") or [],
                "gpu_runtime_available": bool(environment.get("gpu_available")), "warnings": environment.get("warnings") or [],
            }
            failures = []
            if not runtime_ok: failures.append(_reason("AI_RUNTIME_UNAVAILABLE", "Required runtime is unavailable."))
            if not package_ok: failures.append(_reason("AI_PACKAGE_MISSING", "Required AI package is unavailable."))
            if not model_ok: failures.append(_reason("AI_MODEL_CONFIG_MISSING", "Model/configuration was not discovered."))
            rules = [{"metric": "runtime_available", "operator": "==", "expected": True}, {"metric": "model_configuration_found", "operator": "==", "expected": True}, {"metric": "fatal_environment_error_count", "operator": "==", "expected": 0}]
            results.append(_sub_result(module, measurements, rules, self._configuration(module, environment), failures))
        aggregate = self._aggregate(environment, results)
        return aggregate, {"environment": environment}, results


class AiLaunchHandler(AiHandlerBase):
    def execute(self, context, definition):
        environment = self._environment(context); modules = self._require_modules(context, environment); results = []
        for module in modules:
            failures = []; session = None
            try:
                context.log("INFO", f"[AI-002] Checking {module.display_name}.")
                session, status = self.ensure_ai_session(context, definition, module, environment)
                healthy = bool(status.get("process_alive")) and (not session.expected_node or bool(status.get("expected_node_alive")))
                measurements = {"module_uid": module.module_uid, "runtime": module.runtime, "launch_method": module.launch_spec.method if module.launch_spec else "external_reuse", "process_node_identity": session.expected_node or module.process_name, "owned_by_test": session.owned_by_test, "startup_time_s": status.get("startup_time_s"), "process_alive": bool(status.get("process_alive")), "expected_node_alive": bool(status.get("expected_node_alive")), "model_initialized": healthy, "exit_code": status.get("exit_code"), "stderr_summary": str(status.get("stderr_summary") or "")[-8192:], "cleanup_success": False}
                if not healthy: failures.append(_reason("AI_PROCESS_EXITED" if not status.get("process_alive") else "AI_NODE_TIMEOUT", "AI module did not remain healthy during startup."))
                cleanup = self._stop_session(context, session); measurements["cleanup_success"] = bool(cleanup.get("external") or cleanup.get("stopped")); session = None
                if not measurements["cleanup_success"]: failures.append(_reason("AI_CLEANUP_FAILED", "Test-owned AI session was not cleaned up."))
            except AiBlockedError as exc:
                measurements = {"process_alive": False, "expected_node_alive": False, "cleanup_success": True}; failures.append(_reason(exc.code, str(exc))); status = "BLOCKED"
            except AiRemoteError as exc:
                measurements = {"process_alive": False, "expected_node_alive": False, "cleanup_success": False}; failures.append(_reason(exc.code, str(exc))); status = "FAIL"
            else: status = None
            rules = [{"metric": "process_alive", "operator": "==", "expected": True}, {"metric": "cleanup_success", "operator": "==", "expected": True}]
            results.append(_sub_result(module, measurements, rules, self._configuration(module, environment, session), failures, status))
        return self._aggregate(environment, results), {"environment": environment}, results


class _AiEndpointHandler(AiHandlerBase):
    endpoint_kind = "input"
    code_prefix = "AI_INPUT"

    def execute(self, context, definition):
        environment = self._environment(context); modules = self._require_modules(context, environment); results = []
        for module in modules:
            session = None; failures = []
            endpoint = getattr(context.services["ai_runtime_adapter"], "resolve_" + self.endpoint_kind)(module)
            measurements = {"endpoint": endpoint.name if endpoint else "", "message_type": endpoint.message_type if endpoint else "", "publisher_count": 0, "sample_count": 0, "observed_rate_hz": 0.0, "timestamp_rollback_count": 0, "deserialize_error_count": 0, "structural_error_count": 0}
            try:
                if endpoint is None: raise AiRemoteError(self.code_prefix + "_NOT_FOUND", "Module exposes no discovered %s endpoint." % self.endpoint_kind)
                session, status = self.ensure_ai_session(context, definition, module, environment)
                if not status.get("process_alive"): raise AiRemoteError("AI_PROCESS_EXITED", "AI module is not healthy.")
                context.log("INFO", f"[{definition.test_id}] Monitoring {self.endpoint_kind} endpoint {endpoint.name}.")
                probe = self._probe(context, session, endpoint, definition.parameters.get("sample_timeout_s") or 8, definition.parameters.get("sample_count") or 1)
                measurements.update(probe)
                if not probe.get("endpoint_exists"): failures.append(_reason(self.code_prefix + "_NOT_FOUND", "Endpoint was not found."))
                if not probe.get("type_matches"): failures.append(_reason(self.code_prefix + "_TYPE_MISMATCH", "Endpoint message type differs from discovery."))
                if not probe.get("sample_count"): failures.append(_reason(self.code_prefix + "_TIMEOUT", "Endpoint produced no real message."))
                if probe.get("deserialize_error_count") or probe.get("structural_error_count"): failures.append(_reason("AI_OUTPUT_INVALID" if self.endpoint_kind == "output" else "AI_INPUT_INVALID", "Endpoint payload failed structural validation."))
                if probe.get("timestamp_rollback_count"): failures.append(_reason(self.code_prefix + "_INVALID", "Endpoint timestamps rolled back."))
            except AiRemoteError as exc:
                failures.append(_reason(exc.code, str(exc)))
            finally:
                cleanup = self._stop_session(context, session); measurements["cleanup_success"] = bool(cleanup.get("external") or cleanup.get("stopped"))
            rules = [{"metric": "endpoint_exists", "operator": "==", "expected": True}, {"metric": "publisher_count", "operator": ">", "expected": 0}, {"metric": "sample_count", "operator": ">", "expected": 0}, {"metric": "deserialize_error_count", "operator": "==", "expected": 0}, {"metric": "structural_error_count", "operator": "==", "expected": 0}, {"metric": "timestamp_rollback_count", "operator": "==", "expected": 0}, {"metric": "cleanup_success", "operator": "==", "expected": True}]
            results.append(_sub_result(module, measurements, rules, self._configuration(module, environment, session), failures))
        return self._aggregate(environment, results), {"environment": environment}, results


class AiInputHandler(_AiEndpointHandler):
    endpoint_kind, code_prefix = "input", "AI_INPUT"


class AiOutputHandler(_AiEndpointHandler):
    endpoint_kind, code_prefix = "output", "AI_OUTPUT"


class AiInferenceSmokeHandler(AiHandlerBase):
    def execute(self, context, definition):
        environment = self._environment(context); modules = self._require_modules(context, environment); results = []
        for module in modules:
            session = None; failures = []
            inp = context.services["ai_runtime_adapter"].resolve_input(module); out = context.services["ai_runtime_adapter"].resolve_output(module)
            measurements = {"input_received": False, "runtime_healthy": False, "output_received": False, "output_structurally_valid": False, "input_count": 0, "output_count": 0, "input_rate_hz": 0.0, "output_rate_hz": 0.0, "correlation_supported": False, "correlation_result": "NOT_APPLICABLE", "correlated_output_count": 0, "fatal_error_count": 0, "cleanup_success": False}
            try:
                if inp is None: raise AiRemoteError("AI_INPUT_NOT_FOUND", "No input endpoint is configured.")
                if out is None: raise AiRemoteError("AI_OUTPUT_NOT_FOUND", "No output endpoint is configured.")
                session, status = self.ensure_ai_session(context, definition, module, environment); measurements["runtime_healthy"] = bool(status.get("process_alive"))
                if not measurements["runtime_healthy"]: raise AiRemoteError("AI_INFERENCE_NOT_RUNNING", "AI runtime is not healthy.")
                input_probe = self._probe(context, session, inp, definition.parameters.get("sample_timeout_s") or 10, definition.parameters.get("input_sample_count") or 1)
                output_probe = self._probe(context, session, out, definition.parameters.get("sample_timeout_s") or 10, definition.parameters.get("output_sample_count") or 1)
                correlation_supported = input_probe.get("first_timestamp") is not None and output_probe.get("first_timestamp") is not None
                correlated = int(correlation_supported and output_probe.get("last_timestamp") >= input_probe.get("first_timestamp"))
                measurements.update({"input_count": input_probe.get("sample_count", 0), "output_count": output_probe.get("sample_count", 0), "input_rate_hz": input_probe.get("observed_rate_hz", 0.0), "output_rate_hz": output_probe.get("observed_rate_hz", 0.0), "input_received": bool(input_probe.get("sample_count")), "output_received": bool(output_probe.get("sample_count")), "output_structurally_valid": bool(output_probe.get("structurally_valid")), "correlation_supported": correlation_supported, "correlation_result": "SUPPORTED" if correlation_supported else "NOT_APPLICABLE", "correlated_output_count": correlated})
                if not measurements["input_received"]: failures.append(_reason("AI_INPUT_TIMEOUT", "No input data was received."))
                if not measurements["output_received"]: failures.append(_reason("AI_PIPELINE_NO_OUTPUT", "No output data was received while input was active."))
                if not measurements["output_structurally_valid"]: failures.append(_reason("AI_OUTPUT_INVALID", "Output failed generic structural validation."))
                if correlation_supported and not correlated: failures.append(_reason("AI_CORRELATION_FAILED", "No trustworthy timestamp association was observed."))
            except AiRemoteError as exc:
                failures.append(_reason(exc.code, str(exc))); measurements["fatal_error_count"] += 1
            finally:
                cleanup = self._stop_session(context, session); measurements["cleanup_success"] = bool(cleanup.get("external") or cleanup.get("stopped"))
            rules = [{"metric": "input_received", "operator": "==", "expected": True}, {"metric": "runtime_healthy", "operator": "==", "expected": True}, {"metric": "output_received", "operator": "==", "expected": True}, {"metric": "output_structurally_valid", "operator": "==", "expected": True}, {"metric": "fatal_error_count", "operator": "==", "expected": 0}, {"metric": "cleanup_success", "operator": "==", "expected": True}]
            results.append(_sub_result(module, measurements, rules, self._configuration(module, environment, session), failures))
        return self._aggregate(environment, results), {"environment": environment}, results


def register_ai_handlers(registry):
    registry.register("ai.environment", AiEnvironmentHandler())
    registry.register("ai.launch", AiLaunchHandler())
    registry.register("ai.input", AiInputHandler())
    registry.register("ai.output", AiOutputHandler())
    registry.register("ai.inference_smoke", AiInferenceSmokeHandler())
