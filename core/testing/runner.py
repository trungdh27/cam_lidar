import json
import re
import time
import traceback
from pathlib import Path

from core.testing.errors import TestBlockedError, TestCancelledError, TestTimeoutError
from core.testing.models import TestResult, TestStatus, utc_now


class TestRunner:
    def __init__(self, registry, evaluator):
        self.registry, self.evaluator = registry, evaluator

    def run_one(self, definition, context):
        result = TestResult(definition.test_id, definition.name, definition.automation_key,
                            status=TestStatus.RUNNING, started_at=utc_now(), device=dict(context.device),
                            rules=list(definition.rules))
        started = time.monotonic(); context.deadline = started + definition.timeout_s
        handler = self.registry.resolve(definition.automation_key)
        try:
            handler.validate(context, definition)
            handler.setup(context, definition)
            measurements, configuration, sub_results = handler.execute(context, definition)
            context.checkpoint(time.monotonic())
            result.measurements, result.configuration, result.sub_results = measurements, configuration, sub_results
            result.cycles = list(measurements.get("cycles") or ())
            result.rule_results = self.evaluator.evaluate(measurements, definition.rules)
            failed = [item for item in result.rule_results if not item["passed"]]
            result.failure_reasons = [f"{x['metric']}={x['actual']} {x['operator']} {x['expected']}" for x in failed]
            aggregate_status = measurements.get("aggregate_status")
            if failed and aggregate_status in {"BLOCKED", "ERROR"}:
                result.status = TestStatus(aggregate_status)
                blocked = next(
                    (
                        reason for item in sub_results
                        if item.get("status") == aggregate_status
                        for reason in item.get("failure_reasons") or ()
                    ),
                    None,
                )
                if blocked:
                    result.error = {
                        "code": blocked.get("code", "PREREQUISITE_BLOCKED"),
                        "message": blocked.get("message", "Device execution was blocked."),
                    }
            else:
                result.status = TestStatus.FAIL if failed else TestStatus.PASS
        except TestBlockedError as exc:
            result.status = TestStatus.BLOCKED
            result.error = {
                "code": exc.code,
                "message": _safe_diagnostic(str(exc)),
                "diagnostics": _safe_diagnostic_data(getattr(exc, "diagnostics", None)),
            }
        except TestCancelledError as exc:
            result.status = TestStatus.CANCELLED; result.error = {"code": exc.code, "message": str(exc)}
            result.measurements = dict(getattr(handler, "partial_measurements", {}) or {})
            result.configuration = dict(getattr(handler, "partial_configuration", {}) or {})
            result.cycles = list(result.measurements.get("cycles") or ())
        except TestTimeoutError as exc:
            result.status = TestStatus.ERROR
            result.error = _exception_evidence(exc, exc.code)
        except Exception as exc:
            result.status = TestStatus.ERROR
            result.error = _exception_evidence(exc, getattr(exc, "code", "INTERNAL_ERROR"))
        finally:
            try: handler.cleanup(context, definition)
            except Exception as exc:
                result.cleanup_errors.append(str(exc)); result.status = TestStatus.ERROR
            result.finished_at = utc_now(); result.duration_s = round(time.monotonic() - started, 3)
            self._write_result(context, result)
        return result

    @staticmethod
    def _write_result(context, result):
        target = Path(context.result_root) / result.test_case_id
        target.mkdir(parents=True, exist_ok=False)
        (target / "result.json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")


_SECRET_PATTERNS = (
    re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)([^\s,;]+)"),
)


def _safe_diagnostic(value):
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1\2<redacted>", text)
    return text[:4000]


def _exception_evidence(exc, code):
    frames = traceback.extract_tb(exc.__traceback__)
    location = frames[-1] if frames else None
    return {
        "code": str(code),
        "message": _safe_diagnostic(str(exc)),
        "type": type(exc).__name__,
        "exception_type": type(exc).__name__,
        "source_file": location.filename if location else None,
        "source_line": location.lineno if location else None,
        "function": location.name if location else None,
        "traceback": _safe_diagnostic("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
        "diagnostics": _safe_diagnostic_data(getattr(exc, "diagnostics", None)),
        "remote_evidence": _safe_diagnostic_data(getattr(exc, "payload", None)),
    }


def _safe_diagnostic_data(value):
    if isinstance(value, dict):
        return {str(key): _safe_diagnostic_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_diagnostic_data(item) for item in value]
    if isinstance(value, str):
        return _safe_diagnostic(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_diagnostic(value)
