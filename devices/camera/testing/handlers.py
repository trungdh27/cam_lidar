import time

from core.testing.errors import TestBlockedError
from core.testing.evaluator import TestEvaluator


class CameraHandlerBase:
    def __init__(self):
        self.stream_active = False

    def validate(self, context, definition):
        client = context.services["remote_client"]
        if not client.connected:
            raise TestBlockedError("Jetson is not connected. Connect Jetson from Dashboard first.")
        if context.base_configuration.get("manual_stream_active"):
            raise TestBlockedError("Stop the active Camera stream before running automated tests.")
        if not context.base_configuration.get("camera_connected"):
            raise TestBlockedError("Connect and validate the Camera before running automated tests.")
        if not context.device.get("serial"):
            raise TestBlockedError("No discovered Camera serial number is available.")
        if context.device.get("profile_id") != "zed_x_one_4k":
            raise TestBlockedError("Phase 8.1A requires the ZED X One 4K profile.")

    def setup(self, context, definition):
        self.stream_active = False

    def cleanup(self, context, definition):
        if self.stream_active:
            context.services["remote_client"].camera("stop_stream", self._base_payload(context), timeout=10, cleanup=True)
            self.stream_active = False

    @staticmethod
    def _base_payload(context):
        return {
            "profile_id": context.device["profile_id"], "execution_host": "Jetson",
            "device_id": context.device["serial"], "preview_mode": "OFF",
            "host_gstreamer_available": False, "automation_validation": True,
        }

    def _run_mode(self, context, definition, resolution_key, width, height, fps, min_frames):
        payload = {**self._base_payload(context), "resolution_key": resolution_key,
                   "resolution": f"{width} x {height}", "fps": fps, "pixel_format": "BGRA"}
        context.log("INFO", f"[{definition.test_id}] Configuring {resolution_key} @ {fps} FPS.")
        # Pessimistic ownership makes cleanup issue STOP even if START times out
        # after the remote worker was launched but before confirmation arrived.
        self.stream_active = True
        started = context.services["remote_client"].camera("start_stream", payload, timeout=15)
        samples, last = [], started.get("status", {})
        while int(last.get("valid_frame_count", 0)) < min_frames:
            context.checkpoint(time.monotonic())
            time.sleep(0.25)
            response = context.services["remote_client"].camera("stream_status", payload, timeout=5)
            last = response.get("status", {})
            if isinstance(last.get("actual_fps"), (int, float)) and last.get("frame_count", 0) >= max(10, fps):
                samples.append(float(last["actual_fps"]))
        context.log("INFO", f"[{definition.test_id}] {last.get('valid_frame_count', 0)} / {min_frames} valid frames.")
        stopped = context.services["remote_client"].camera("stop_stream", payload, timeout=10)
        self.stream_active = False
        final = stopped.get("status") or last
        average_fps = sum(samples) / len(samples) if samples else float(final.get("actual_fps") or 0)
        dropped = int(final.get("dropped_frames", 0)); frames = int(final.get("frame_count", 0))
        return {
            "open_success": True, "close_success": True,
            "width": final.get("actual_width"), "height": final.get("actual_height"),
            "successful_frame_count": int(final.get("valid_frame_count", 0)),
            "invalid_frame_count": int(final.get("invalid_frame_count", 0)),
            "corrupted_frame_count": int(final.get("corrupted_frame_count", 0)),
            "average_fps": round(average_fps, 3),
            "avg_fps_ratio": round(average_fps / fps, 6) if fps else 0,
            "dropped_frames": dropped, "drop_ratio": dropped / max(1, frames + dropped),
            "duplicate_timestamp_count": int(final.get("duplicate_timestamp_count", 0)),
            "timestamp_rollback_count": int(final.get("timestamp_rollback_count", 0)),
            "dimension_mismatch_count": 0 if final.get("actual_width") == width and final.get("actual_height") == height else 1,
            "continuous_grab_failure": bool(final.get("last_error")),
            "camera_disconnect_count": 0 if final.get("state") in ("stopped", "stopping") else 1,
        }


class BasicGrabHandler(CameraHandlerBase):
    def execute(self, context, definition):
        p = definition.parameters
        metrics = self._run_mode(context, definition, p["resolution_key"], p["width"], p["height"], p["fps_values"][0], p["min_frames"])
        daemon_active = context.services["remote_client"].daemon_active()
        metrics.update(daemon_hang_count=0 if daemon_active else 1, daemon_active=daemon_active)
        return metrics, {"resolution": p["resolution_key"], "fps": p["fps_values"][0]}, []


class Smoke4KHandler(BasicGrabHandler):
    pass


class ResolutionFpsHandler(CameraHandlerBase):
    def execute(self, context, definition):
        p, sub_results = definition.parameters, []
        for fps in p["fps_values"]:
            metrics = self._run_mode(context, definition, p["resolution_key"], p["width"], p["height"], fps, p["min_frames"])
            rules = [
                {"metric": "open_success", "operator": "==", "expected": True},
                {"metric": "width", "operator": "==", "expected": p["width"]},
                {"metric": "height", "operator": "==", "expected": p["height"]},
                {"metric": "dimension_mismatch_count", "operator": "==", "expected": 0},
                {"metric": "avg_fps_ratio", "operator": ">=", "expected": 0.95},
            ]
            if "max_drop_ratio" in p:
                rules.append({"metric": "drop_ratio", "operator": "<=", "expected": p["max_drop_ratio"]})
            if p.get("require_timestamp_integrity"):
                rules.extend((
                    {"metric": "duplicate_timestamp_count", "operator": "==", "expected": 0},
                    {"metric": "timestamp_rollback_count", "operator": "==", "expected": 0},
                ))
            if p.get("require_no_disconnect"):
                rules.append({"metric": "camera_disconnect_count", "operator": "==", "expected": 0})
            rule_results = TestEvaluator().evaluate(metrics, rules)
            sub_results.append({"configuration": {"resolution": p["resolution_key"], "fps": fps},
                                "status": "PASS" if all(x["passed"] for x in rule_results) else "FAIL",
                                "measurements": metrics, "rules": rules, "rule_results": rule_results,
                                "failure_reasons": [f"{x['metric']}={x['actual']} {x['operator']} {x['expected']}"
                                                    for x in rule_results if not x["passed"]]})
            context.log("INFO", f"[{definition.test_id}] Average FPS: {metrics['average_fps']} / {fps}; ratio={metrics['avg_fps_ratio']:.4f}")
        measurements = {"all_subcases_pass": all(x["status"] == "PASS" for x in sub_results),
                        "subcase_count": len(sub_results)}
        if len(sub_results) == 1:
            measurements.update(sub_results[0]["measurements"])
        return measurements, {"resolution": p["resolution_key"], "fps_values": p["fps_values"]}, sub_results


class InvalidConfigHandler(CameraHandlerBase):
    def execute(self, context, definition):
        base = self._base_payload(context)
        serial = int(context.device["serial"])
        good = {**base, "resolution_key": "HD1080", "resolution": "1920 x 1080", "fps": 30, "pixel_format": "BGRA"}
        cases = [
            ("INVALID_SERIAL", {**good, "device_id": str(serial + 999999999)}),
            ("INVALID_RESOLUTION", {**good, "resolution_key": "UNSUPPORTED"}),
            ("INVALID_FPS", {**good, "resolution_key": "HD4K", "resolution": "3840 x 2160", "fps": 60}),
            ("INVALID_FPS", {**good, "fps": 0}), ("INVALID_FPS", {**good, "fps": -1}),
            ("INVALID_FPS", {**good, "fps": "not_a_number"}),
            ("MISSING_PARAMETER", {key: value for key, value in good.items() if key != "resolution_key"}),
        ]
        rejected, recovery, residual_busy, details = 0, 0, 0, []
        for expected, payload in cases:
            context.checkpoint(time.monotonic())
            category, raw = None, None
            self.stream_active = True
            try:
                context.services["remote_client"].camera("start_stream", payload, timeout=15)
            except Exception as exc:
                raw = str(exc); category = normalize_error(raw); rejected += 1
            if self.stream_active:
                context.services["remote_client"].camera("stop_stream", payload, timeout=10); self.stream_active = False
            try:
                self._run_mode(context, definition, "HD1080", 1920, 1080, 30, definition.parameters["recovery_frames"])
                recovery += 1
            except Exception as exc:
                if normalize_error(str(exc)) == "CAMERA_BUSY": residual_busy += 1
                details.append({"expected_category": expected, "actual_category": category, "raw_error": raw, "recovery_error": str(exc)})
                continue
            details.append({"expected_category": expected, "actual_category": category, "raw_error": raw, "recovery": True})
        return {
            "all_invalid_inputs_rejected": rejected == len(cases), "app_crash_count": 0,
            "wrong_camera_open_count": len(cases) - rejected, "residual_busy_count": residual_busy,
            "valid_recovery_after_every_negative_case": recovery == len(cases),
            "invalid_case_results": details,
        }, {"negative_case_count": len(cases)}, []


def normalize_error(message):
    upper = message.upper()
    if "MISSING" in upper or "KEYERROR" in upper: return "MISSING_PARAMETER"
    if "SERIAL" in upper or "NOT DETECTED" in upper: return "INVALID_SERIAL"
    if "RESOLUTION" in upper: return "INVALID_RESOLUTION"
    if "FPS" in upper: return "INVALID_FPS"
    if "BUSY" in upper or "ALREADY" in upper: return "CAMERA_BUSY"
    return "INVALID_PARAMETER"


def register_camera_handlers(registry):
    registry.register("camera.basic_grab", BasicGrabHandler())
    registry.register("camera.smoke_4k", Smoke4KHandler())
    registry.register("camera.resolution_fps", ResolutionFpsHandler())
    registry.register("camera.invalid_config", InvalidConfigHandler())
