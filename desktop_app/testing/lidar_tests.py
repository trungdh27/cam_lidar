from __future__ import annotations

import ipaddress
import statistics
from typing import Iterable

from PySide6.QtCore import QTimer

from desktop_app.state.lidar_runtime_state import LidarStreamStatus
from desktop_app.testing.test_case import (
    AutomationLevel,
    TestCaseDefinition,
)
from desktop_app.testing.test_executor import BaseTestExecutor
from desktop_app.testing.test_registry import TestRegistry
from desktop_app.testing.test_result import TestOutcome, TestStatus


class ImmediateExecutor(BaseTestExecutor):
    def start(self, definition, context) -> None:
        try:
            outcome = self.evaluate(context)
        except Exception as exc:
            outcome = TestOutcome(
                TestStatus.ERROR,
                f"Executor error: {type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
            )
        QTimer.singleShot(0, lambda: self.finish(outcome))

    def evaluate(self, context) -> TestOutcome:
        raise NotImplementedError


class EthernetInterfaceExecutor(ImmediateExecutor):
    def evaluate(self, context) -> TestOutcome:
        expected = context.network_profile.jetson.interface
        snapshot = context.jetson_state.network_snapshot or {}
        interface = next(
            (
                item
                for item in snapshot.get("interfaces", [])
                if item.get("name") == expected
            ),
            None,
        )
        if interface is None:
            return TestOutcome(
                TestStatus.FAIL,
                f"Expected interface {expected} was not detected.",
                {"interface": expected, "detected": False},
            )
        flags = {str(flag).upper() for flag in interface.get("flags", [])}
        state = str(interface.get("operstate") or "UNKNOWN").upper()
        carrier = interface.get("carrier") is True
        physical = interface.get("physical") is True
        link_up = state == "UP" or {"UP", "LOWER_UP"} <= flags
        measurements = {
            "interface": expected,
            "detected": True,
            "physical": physical,
            "state": state,
            "carrier": "UP" if carrier else "DOWN",
            "mac": interface.get("mac_address"),
            "flags": sorted(flags),
        }
        actual = (
            f"Interface {expected} detected. State={state}, "
            f"Carrier={'UP' if carrier else 'DOWN'}, "
            f"MAC={interface.get('mac_address') or '-'}."
        )
        return TestOutcome(
            TestStatus.PASS if physical and carrier and link_up else TestStatus.FAIL,
            actual,
            measurements,
        )


class IpProfileVerificationExecutor(ImmediateExecutor):
    def evaluate(self, context) -> TestOutcome:
        verification = context.network_verification or {}
        expected_host = context.network_profile.jetson.cidr
        expected_lidar = str(context.network_profile.lidar.ip)
        expected_network = context.network_profile.jetson.network
        detected_lidar = (context.discovery_result or {}).get("lidar_ip")
        lidar_in_subnet = ipaddress.ip_address(expected_lidar) in ipaddress.ip_network(
            expected_network
        )
        passed = bool(
            verification.get("status") == "NETWORK_READY"
            and expected_host in verification.get("ipv4_addresses", [])
            and detected_lidar == expected_lidar
            and lidar_in_subnet
        )
        measurements = {
            "expected_host_cidr": expected_host,
            "observed_host_addresses": verification.get("ipv4_addresses", []),
            "expected_lidar_ip": expected_lidar,
            "detected_lidar_ip": detected_lidar,
            "expected_network": expected_network,
            "verification_status": verification.get("status"),
        }
        actual = (
            f"Host={expected_host}; LiDAR expected={expected_lidar}, "
            f"detected={detected_lidar or '-'}; "
            f"Network={expected_network}; "
            f"Verification={verification.get('status') or 'NOT_VERIFIED'}."
        )
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            actual,
            measurements,
        )


class DeviceDiscoveryExecutor(ImmediateExecutor):
    def evaluate(self, context) -> TestOutcome:
        result = context.discovery_result or {}
        expected_model = context.selected_model
        expected_ip = str(context.network_profile.lidar.ip)
        found = result.get("found") is True
        passed = bool(
            found
            and result.get("status") == "FOUND"
            and result.get("model") == expected_model
            and result.get("lidar_ip") == expected_ip
            and result.get("serial")
        )
        measurements = {
            "found": found,
            "model": result.get("model"),
            "serial": result.get("serial"),
            "lidar_ip": result.get("lidar_ip"),
            "sdk_version": result.get("sdk_version"),
            "status": result.get("status"),
        }
        actual = (
            f"Model={result.get('model') or '-'}, "
            f"Serial={result.get('serial') or '-'}, "
            f"LiDAR IP={result.get('lidar_ip') or '-'}, "
            f"SDK={result.get('sdk_version') or '-'}, "
            f"Status={result.get('status') or 'NOT_FOUND'}."
        )
        infrastructure_statuses = {
            "NETWORK_ERROR",
            "SDK_HELPER_MISSING",
            "SDK_VERSION_ERROR",
            "SDK_INIT_ERROR",
            "SDK_DISCOVERY_ERROR",
        }
        if result.get("status") in infrastructure_statuses:
            return TestOutcome(
                TestStatus.ERROR,
                actual,
                measurements,
                error=result.get("reason") or result.get("status"),
            )
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            actual,
            measurements,
        )


class MetricWindowExecutor(BaseTestExecutor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.context = None
        self.samples = []
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._sample)
        self.finish_timer = QTimer(self)
        self.finish_timer.setSingleShot(True)
        self.finish_timer.timeout.connect(self._complete)

    def start(self, definition, context) -> None:
        self.context = context
        self.samples = []
        duration_sec = self.duration_sec(context)
        self._sample()
        self.timer.start()
        self.finish_timer.start(max(1, int(duration_sec * 1000)))

    def duration_sec(self, context) -> float:
        return context.network_profile.testing.sample_window_sec

    def cancel(self) -> None:
        super().cancel()
        self.timer.stop()
        self.finish_timer.stop()

    def _sample(self) -> None:
        if not self.cancelled and self.context is not None:
            self.samples.append(self.context.runtime_snapshot())

    def _complete(self) -> None:
        self.timer.stop()
        if self.cancelled:
            return
        try:
            self.finish(self.evaluate(self.context, self.samples))
        except Exception as exc:
            self.finish(
                TestOutcome(
                    TestStatus.ERROR,
                    f"Metric evaluation error: {type(exc).__name__}: {exc}",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        raise NotImplementedError


class PointDataPathExecutor(MetricWindowExecutor):
    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        packet_rates = _numbers(samples, "point_packet_rate_hz")
        point_rates = _numbers(samples, "point_count")
        stale_count = sum(
            item.get("stream_status") != LidarStreamStatus.STREAMING.value
            for item in samples
        )
        passed = bool(
            packet_rates
            and point_rates
            and min(packet_rates) > 0
            and min(point_rates) > 0
            and stale_count == 0
        )
        measurements = {
            "sample_count": len(samples),
            "sample_window_sec": self.duration_sec(context),
            "point_packet_rate": _stats(packet_rates),
            "point_rate": _stats(point_rates),
            "stale_count": stale_count,
        }
        actual = (
            f"Point stream sampled for {self.duration_sec(context):.1f} s. "
            f"Average packet rate={_average(packet_rates):.2f} pkt/s; "
            f"Average point rate={_average(point_rates):.0f} pts/s; "
            f"stale_count={stale_count}."
        )
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            actual,
            measurements,
        )


class ImuDataPathExecutor(MetricWindowExecutor):
    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        rates = _numbers(samples, "imu_rate_hz")
        inactive_count = sum(
            item.get("imu_status") != "ACTIVE" for item in samples
        )
        passed = bool(rates and min(rates) > 0 and inactive_count == 0)
        measurements = {
            "sample_count": len(samples),
            "sample_window_sec": self.duration_sec(context),
            "imu_rate": _stats(rates),
            "inactive_count": inactive_count,
        }
        actual = (
            f"IMU sampled for {self.duration_sec(context):.1f} s. "
            f"Average rate={_average(rates):.2f} Hz; "
            f"inactive_count={inactive_count}."
        )
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            actual,
            measurements,
        )


class ImuRateExecutor(MetricWindowExecutor):
    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        rates = _numbers(samples, "imu_rate_hz")
        average = _average(rates)
        threshold = context.network_profile.testing.model_thresholds(
            context.selected_model
        ).imu_rate
        measurements = {
            "measurement_only": not threshold.configured,
            "sample_count": len(rates),
            "imu_rate": _stats(rates),
            "target_hz": threshold.target,
            "tolerance_percent": threshold.tolerance_percent,
        }
        if not rates:
            return TestOutcome(
                TestStatus.FAIL,
                "No IMU rate measurement was available.",
                measurements,
            )
        bounds = threshold.bounds()
        if bounds is None:
            return TestOutcome(
                TestStatus.SKIPPED,
                f"Measured average IMU rate={average:.2f} Hz. "
                "Acceptance tolerance is not configured; measurement retained.",
                measurements,
            )
        passed = bounds[0] <= average <= bounds[1]
        measurements["acceptance_min_hz"] = bounds[0]
        measurements["acceptance_max_hz"] = bounds[1]
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            f"Average IMU rate={average:.2f} Hz; "
            f"configured range={bounds[0]:.2f}–{bounds[1]:.2f} Hz.",
            measurements,
        )


class PointRateExecutor(MetricWindowExecutor):
    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        rates = _numbers(samples, "point_count")
        average = _average(rates)
        threshold = context.network_profile.testing.model_thresholds(
            context.selected_model
        ).point_rate
        measurements = {
            "measurement_only": not threshold.configured,
            "sample_count": len(rates),
            "point_rate": _stats(rates),
            "target_pts_s": threshold.target,
            "tolerance_percent": threshold.tolerance_percent,
        }
        if not rates:
            return TestOutcome(
                TestStatus.FAIL,
                "No point-rate measurement was available.",
                measurements,
            )
        bounds = threshold.bounds()
        if bounds is None:
            return TestOutcome(
                TestStatus.SKIPPED,
                f"Measured average Point Rate={average:.0f} pts/s. "
                "Acceptance tolerance is not configured; measurement retained.",
                measurements,
            )
        passed = bounds[0] <= average <= bounds[1]
        measurements["acceptance_min_pts_s"] = bounds[0]
        measurements["acceptance_max_pts_s"] = bounds[1]
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            f"Average Point Rate={average:.0f} pts/s; "
            f"configured range={bounds[0]:.0f}–{bounds[1]:.0f} pts/s.",
            measurements,
        )


class PacketLossExecutor(MetricWindowExecutor):
    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        loss_values = _numbers(samples, "packet_loss_percent")
        supported = all(item.get("packet_loss_supported") for item in samples)
        threshold = context.network_profile.testing.model_thresholds(
            context.selected_model
        ).packet_loss_max_percent
        packet_counts = _numbers(samples, "point_packet_counter")
        lost_counts = _numbers(samples, "lost_point_packet_counter")
        measurements = {
            "measurement_only": threshold is None,
            "sample_count": len(loss_values),
            "loss_percent": _stats(loss_values),
            "received": int(packet_counts[-1] - packet_counts[0])
            if len(packet_counts) >= 2
            else None,
            "lost": int(lost_counts[-1] - lost_counts[0])
            if len(lost_counts) >= 2
            else None,
            "max_loss_percent": threshold,
        }
        if not supported or not loss_values:
            return TestOutcome(
                TestStatus.SKIPPED,
                "Packet-loss sequence measurement is unsupported or unavailable.",
                measurements,
            )
        maximum = max(loss_values)
        if threshold is None:
            return TestOutcome(
                TestStatus.SKIPPED,
                f"Measured packet loss={maximum:.3f}%. "
                "Maximum acceptance threshold is not configured.",
                measurements,
            )
        passed = maximum <= threshold
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            f"Maximum packet loss={maximum:.3f}%; "
            f"configured maximum={threshold:.3f}%.",
            measurements,
        )


class TimestampMonotonicityExecutor(MetricWindowExecutor):
    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        timestamps = [
            int(item["lidar_timestamp"])
            for item in samples
            if item.get("lidar_timestamp") is not None
        ]
        rollback_count = sum(
            current < previous
            for previous, current in zip(timestamps, timestamps[1:])
        )
        equal_count = sum(
            current == previous
            for previous, current in zip(timestamps, timestamps[1:])
        )
        measurements = {
            "sample_count": len(timestamps),
            "rollback_count": rollback_count,
            "equal_count": equal_count,
            "first_timestamp": timestamps[0] if timestamps else None,
            "last_timestamp": timestamps[-1] if timestamps else None,
        }
        passed = len(timestamps) >= 2 and rollback_count == 0
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            f"Timestamp samples={len(timestamps)}, "
            f"rollback_count={rollback_count}, equal_count={equal_count}.",
            measurements,
        )


class ContinuousOperationExecutor(MetricWindowExecutor):
    def duration_sec(self, context) -> float:
        return context.network_profile.testing.continuous_operation.duration_sec

    def evaluate(self, context, samples: list[dict]) -> TestOutcome:
        config = context.network_profile.testing.continuous_operation
        packet_rates = _numbers(samples, "point_packet_rate_hz")
        point_rates = _numbers(samples, "point_count")
        imu_rates = _numbers(samples, "imu_rate_hz")
        loss_values = _numbers(samples, "packet_loss_percent")
        stale_count = sum(
            item.get("stream_status") == LidarStreamStatus.STALE.value
            for item in samples
        )
        error_count = sum(
            item.get("stream_status") == LidarStreamStatus.ERROR.value
            for item in samples
        )
        passed = bool(
            packet_rates
            and point_rates
            and imu_rates
            and min(packet_rates) >= config.min_point_packet_rate
            and min(point_rates) >= config.min_point_rate
            and min(imu_rates) >= config.min_imu_rate
            and stale_count == 0
            and error_count == 0
        )
        measurements = {
            "mode": config.mode,
            "duration_sec": self.duration_sec(context),
            "sample_count": len(samples),
            "point_packet_rate": _stats(packet_rates),
            "point_rate": _stats(point_rates),
            "imu_rate": _stats(imu_rates),
            "packet_loss_percent": _stats(loss_values),
            "stale_count": stale_count,
            "error_count": error_count,
            "configured_minimums": {
                "point_packet_rate": config.min_point_packet_rate,
                "point_rate": config.min_point_rate,
                "imu_rate": config.min_imu_rate,
            },
        }
        actual = (
            f"Continuous operation completed for {self.duration_sec(context):.1f} s. "
            f"Average point rate={_average(point_rates):.0f} pts/s; "
            f"average IMU rate={_average(imu_rates):.2f} Hz; "
            f"stale_count={stale_count}; error_count={error_count}."
        )
        return TestOutcome(
            TestStatus.PASS if passed else TestStatus.FAIL,
            actual,
            measurements,
        )


def build_lidar_test_registry(profile) -> TestRegistry:
    sample_timeout = profile.testing.sample_window_sec + 5.0
    continuous_timeout = (
        profile.testing.continuous_operation.duration_sec + 10.0
    )
    registry = TestRegistry()
    registry.register_many(
        [
            TestCaseDefinition(
                id="LID-CON-001",
                device="lidar",
                name="Ethernet Interface Detection",
                group="Connectivity",
                description="Detect the profile-selected physical interface and verify link/carrier.",
                automation_level=AutomationLevel.AUTO,
                priority="P0",
                timeout_sec=5,
                requires_jetson=True,
                executor=EthernetInterfaceExecutor,
                order=10,
            ),
            TestCaseDefinition(
                id="LID-CON-002",
                device="lidar",
                name="Host / LiDAR IP Profile Verification",
                group="Connectivity",
                description="Verify host CIDR, detected LiDAR IP, and subnet against the selected profile.",
                automation_level=AutomationLevel.AUTO,
                priority="P0",
                timeout_sec=5,
                requires_jetson=True,
                requires_device=True,
                executor=IpProfileVerificationExecutor,
                order=20,
            ),
            TestCaseDefinition(
                id="LID-CON-003",
                device="lidar",
                name="Livox Device Discovery",
                group="Connectivity",
                description="Reuse shared Livox discovery and validate model, serial, IP, and SDK result.",
                automation_level=AutomationLevel.AUTO,
                priority="P0",
                timeout_sec=20,
                requires_jetson=True,
                requires_device=True,
                executor=DeviceDiscoveryExecutor,
                order=30,
            ),
            TestCaseDefinition(
                id="LID-STR-001",
                device="lidar",
                name="Point Data Path Verification",
                group="Streaming",
                description="Verify live point packets and points without a performance threshold.",
                automation_level=AutomationLevel.AUTO,
                priority="P0",
                timeout_sec=sample_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=PointDataPathExecutor,
                order=40,
            ),
            TestCaseDefinition(
                id="LID-STR-002",
                device="lidar",
                name="IMU Data Path Verification",
                group="Streaming",
                description="Verify ACTIVE IMU callbacks and a positive measured rate.",
                automation_level=AutomationLevel.AUTO,
                priority="P0",
                timeout_sec=sample_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=ImuDataPathExecutor,
                order=50,
            ),
            TestCaseDefinition(
                id="LID-STR-003",
                device="lidar",
                name="IMU Rate Verification",
                group="Streaming",
                description="Measure IMU rate; acceptance requires configured profile tolerance.",
                automation_level=AutomationLevel.AUTO,
                priority="P1",
                timeout_sec=sample_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=ImuRateExecutor,
                order=60,
            ),
            TestCaseDefinition(
                id="LID-STR-004",
                device="lidar",
                name="Point Rate Verification",
                group="Streaming",
                description="Measure point rate; acceptance requires configured profile tolerance.",
                automation_level=AutomationLevel.AUTO,
                priority="P1",
                timeout_sec=sample_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=PointRateExecutor,
                order=70,
            ),
            TestCaseDefinition(
                id="LID-STR-005",
                device="lidar",
                name="Packet Loss Verification",
                group="Streaming",
                description="Reuse runtime udp_cnt loss calculation and configured maximum.",
                automation_level=AutomationLevel.AUTO,
                priority="P1",
                timeout_sec=sample_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=PacketLossExecutor,
                order=80,
            ),
            TestCaseDefinition(
                id="LID-TIM-001",
                device="lidar",
                name="Timestamp Monotonicity",
                group="Timing",
                description="Verify that sampled LiDAR timestamps never roll backward.",
                automation_level=AutomationLevel.AUTO,
                priority="P1",
                timeout_sec=sample_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=TimestampMonotonicityExecutor,
                order=90,
            ),
            TestCaseDefinition(
                id="LID-ROB-001",
                device="lidar",
                name="Continuous Operation",
                group="Robustness",
                description="Aggregate runtime health for the configured development or qualification duration.",
                automation_level=AutomationLevel.AUTO,
                priority="P1",
                timeout_sec=continuous_timeout,
                requires_jetson=True,
                requires_network=True,
                requires_device=True,
                requires_stream=True,
                executor=ContinuousOperationExecutor,
                order=100,
            ),
        ]
    )
    return registry


def _numbers(samples: Iterable[dict], key: str) -> list[float]:
    values = []
    for sample in samples:
        value = sample.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    return values


def _average(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _stats(values: list[float]) -> dict:
    if not values:
        return {"min": None, "max": None, "average": None, "sample_count": 0}
    return {
        "min": min(values),
        "max": max(values),
        "average": _average(values),
        "sample_count": len(values),
    }
