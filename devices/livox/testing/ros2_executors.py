from __future__ import annotations

import shlex
import re
import statistics
from typing import Callable

from devices.livox.testing.ros2_parsers import (
    configured_bounds,
    parse_ros2_hz,
    parse_sectioned_output,
)
from devices.livox.testing.test_executor import BaseTestExecutor
from devices.livox.testing.test_result import TestOutcome, TestStatus


def _q(value) -> str:
    return shlex.quote(str(value))


def _section(name: str, command: str) -> str:
    return f"printf '%s\\n' '__LIDAR_SECTION__={name}'; ({command})"


def _shell(profile, commands: list[str], *, domain_id: int | None = None) -> str:
    setup = list(profile.setup_commands)
    if domain_id is not None:
        setup.append(f"export ROS_DOMAIN_ID={int(domain_id)}")
    return "LC_ALL=C; " + "; ".join(setup + commands)


class Ros2CommandExecutor(BaseTestExecutor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._service = None
        self._request_id = None
        self._definition = None
        self._context = None

    def start(self, definition, context) -> None:
        self._definition = definition
        self._context = context
        profile = getattr(context, "ros2_target_profile", None)
        service = getattr(context, "lidar_ros2_service", None)
        if profile is None or service is None or not profile.enabled:
            self.finish(
                TestOutcome(
                    TestStatus.SKIPPED,
                    "Production ROS2 target is not configured.",
                    {"missing_prerequisite": "Production ROS2 target"},
                )
            )
            return
        early = self.preflight(profile)
        if early is not None:
            self.finish(early)
            return
        self._service = service
        service.request_succeeded.connect(self._on_success)
        service.request_failed.connect(self._on_failure)
        service.request_cancelled.connect(self._on_cancelled)
        self._request_id = service.execute(
            definition.id,
            self.build_command(profile),
            timeout=definition.timeout_sec,
        )
        if self._request_id is None:
            self._disconnect()
            self.finish(
                TestOutcome(TestStatus.SKIPPED, "Production ROS2 target is disabled.")
            )

    def preflight(self, profile) -> TestOutcome | None:
        return None

    def build_command(self, profile) -> str:
        raise NotImplementedError

    def evaluate(self, profile, result: dict) -> TestOutcome:
        raise NotImplementedError

    def cancel(self) -> None:
        if self._service is not None and self._request_id is not None:
            self._service.cancel(self._request_id)
        self._disconnect()
        super().cancel()

    def _on_success(self, request_id: str, result: dict) -> None:
        if request_id != self._request_id or self.cancelled:
            return
        profile = self._context.ros2_target_profile
        self._disconnect()
        if result.get("exit_status") != 0:
            self.finish(
                TestOutcome(
                    TestStatus.FAIL,
                    f"Remote check exited with status {result.get('exit_status')}.",
                    _bounded_remote_summary(result),
                )
            )
            return
        try:
            outcome = self.evaluate(profile, result)
        except Exception as exc:
            outcome = TestOutcome(
                TestStatus.ERROR,
                f"ROS2 output parsing failed: {type(exc).__name__}: {exc}",
                _bounded_remote_summary(result),
                error=f"{type(exc).__name__}: {exc}",
            )
        self.finish(outcome)

    def _on_failure(self, request_id: str, error: str) -> None:
        if request_id != self._request_id or self.cancelled:
            return
        self._disconnect()
        self.finish(
            TestOutcome(
                TestStatus.ERROR,
                f"Production ROS2 command failed: {error}",
                error=error,
            )
        )

    def _on_cancelled(self, request_id: str) -> None:
        if request_id == self._request_id:
            self._disconnect()

    def _disconnect(self) -> None:
        service = self._service
        if service is not None:
            for signal, slot in (
                (service.request_succeeded, self._on_success),
                (service.request_failed, self._on_failure),
                (service.request_cancelled, self._on_cancelled),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._service = None


class Ros2EnvironmentExecutor(Ros2CommandExecutor):
    def build_command(self, p) -> str:
        topics = " ".join(_q(item.name) for item in p.topics.values())
        return _shell(p, [
            _section("ARCH", "uname -m"),
            _section("KERNEL", "uname -r"),
            _section("ADDR", f"ip -br addr show dev {_q(p.lidar_interface)}"),
            _section("CONTAINER", f"docker inspect -f '{{{{.State.Running}}}}' {_q(p.container_name)}"),
            _section("NODES", "ros2 node list"),
            _section("TOPICS", "ros2 topic list"),
            _section("EXPECTED", f"printf '%s\\n' {topics}"),
        ], domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result) -> TestOutcome:
        sections = parse_sectioned_output(result["stdout"])
        missing = []
        if p.host_cidr.split("/")[0] not in sections.get("ADDR", ""):
            missing.append("host IP")
        if sections.get("CONTAINER") != "true":
            missing.append("running container")
        if p.lidar_publisher not in sections.get("NODES", "").splitlines():
            missing.append("LiDAR publisher")
        listed = set(sections.get("TOPICS", "").splitlines())
        missing.extend(item.name for item in p.topics.values() if item.name not in listed)
        measurements = {"sections": sections, "missing": missing}
        return TestOutcome(
            TestStatus.PASS if not missing else TestStatus.FAIL,
            "Production ROS2 environment checks passed." if not missing else "Missing: " + ", ".join(missing),
            measurements,
        )


class Ros2NetworkExecutor(Ros2CommandExecutor):
    def build_command(self, p) -> str:
        ports = "|".join(str(value) for value in p.udp_ports)
        return _shell(p, [
            _section("ADDR", f"ip -br addr show dev {_q(p.lidar_interface)}"),
            _section("NEIGH", f"ip neigh show dev {_q(p.lidar_interface)}"),
            _section("NETWORK_MODE", f"docker inspect -f '{{{{.HostConfig.NetworkMode}}}}' {_q(p.container_name)}"),
            _section("UDP", f"ss -lunp | grep -E {_q(':' + ports)} || true"),
            _section("POINT_HZ", f"timeout 10 ros2 topic hz {_q(p.topics['pointcloud2'].name)} || true"),
            _section("IMU_HZ", f"timeout 10 ros2 topic hz {_q(p.topics['imu'].name)} || true"),
        ])

    def evaluate(self, p, result) -> TestOutcome:
        sections = parse_sectioned_output(result["stdout"])
        checks = {
            "host_cidr": p.host_cidr.split("/")[0] in sections.get("ADDR", ""),
            "lidar_neighbor": p.lidar_ip in sections.get("NEIGH", ""),
            "container_host_network": sections.get("NETWORK_MODE") == p.container_network_mode,
            "udp_ports": all(f":{port}" in sections.get("UDP", "") for port in p.udp_ports),
        }
        if not all(checks.values()):
            return TestOutcome(TestStatus.FAIL, "Network checks failed.", {"checks": checks, "sections": sections})
        measurements = {"checks": checks, "pointcloud_rate": parse_ros2_hz(sections.get("POINT_HZ", "")), "imu_rate": parse_ros2_hz(sections.get("IMU_HZ", ""))}
        return _needs_criteria("Network structure and data-flow indicators were collected; official rate tolerances are not configured.", measurements)


class Ros2PublisherTopicsExecutor(Ros2CommandExecutor):
    def build_command(self, p) -> str:
        commands = [_section("NODES", "ros2 node list")]
        for key, topic in p.topics.items():
            commands.append(_section(key.upper(), f"ros2 topic info -v {_q(topic.name)}"))
        commands.extend((
            _section("POINT_HZ", f"timeout 10 ros2 topic hz {_q(p.topics['pointcloud2'].name)} || true"),
            _section("IMU_HZ", f"timeout 10 ros2 topic hz {_q(p.topics['imu'].name)} || true"),
        ))
        return _shell(p, commands, domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result) -> TestOutcome:
        sections = parse_sectioned_output(result["stdout"])
        checks = {"node": p.lidar_publisher in sections.get("NODES", "").splitlines()}
        for key, topic in p.topics.items():
            text = sections.get(key.upper(), "")
            checks[key] = topic.message_type in text and p.lidar_publisher in text
        if not all(checks.values()):
            return TestOutcome(TestStatus.FAIL, "Required ROS2 publisher/topic/type checks failed.", {"checks": checks})
        point_metrics = parse_ros2_hz(sections.get("POINT_HZ", ""))
        imu_metrics = parse_ros2_hz(sections.get("IMU_HZ", ""))
        measurements = {"checks": checks, "pointcloud_rate": point_metrics, "imu_rate": imu_metrics}
        point_bounds = configured_bounds(p.acceptance.get("pointcloud_rate_hz"), p.acceptance.get("pointcloud_rate_tolerance_percent"))
        imu_bounds = configured_bounds(p.acceptance.get("imu_rate_hz"), p.acceptance.get("imu_rate_tolerance_percent"))
        if point_bounds is None or imu_bounds is None:
            return _needs_criteria("ROS2 topology passed; official rate tolerances are not configured.", measurements)
        rates_pass = point_metrics["mean_hz"] is not None and imu_metrics["mean_hz"] is not None and point_bounds[0] <= point_metrics["mean_hz"] <= point_bounds[1] and imu_bounds[0] <= imu_metrics["mean_hz"] <= imu_bounds[1]
        return TestOutcome(TestStatus.PASS if rates_pass else TestStatus.FAIL, "ROS2 topology and rates met configured criteria." if rates_pass else "ROS2 runtime rates failed configured criteria.", measurements)


class _RateExecutor(Ros2CommandExecutor):
    topic_key = ""
    target_key = ""
    tolerance_key = ""

    def build_command(self, p) -> str:
        duration = int(p.sampling.get("topic_rate_duration_sec", 10))
        topic = p.topics[self.topic_key].name
        return _shell(p, [_section("INFO", f"ros2 topic info -v {_q(topic)}"), _section("HZ", f"timeout {duration} ros2 topic hz {_q(topic)} || true")], domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result) -> TestOutcome:
        sections = parse_sectioned_output(result["stdout"])
        metrics = parse_ros2_hz(sections.get("HZ", ""))
        info = sections.get("INFO", "")
        topic = p.topics[self.topic_key]
        structural = topic.message_type in info and p.lidar_publisher in info
        target = self.configured_target(p, sections)
        tolerance = p.acceptance.get(self.tolerance_key)
        bounds = configured_bounds(target, tolerance)
        measurements = {"rate": metrics, "target_hz": target, "tolerance_percent": tolerance, "publisher_type_valid": structural}
        if not structural:
            return TestOutcome(TestStatus.FAIL, "Topic type or publisher did not match the production profile.", measurements)
        if metrics["mean_hz"] is None:
            return TestOutcome(TestStatus.FAIL, "No runtime rate measurement was produced.", measurements)
        if bounds is None:
            return _needs_criteria("Rate measured; official tolerance is not configured.", measurements)
        passed = bounds[0] <= metrics["mean_hz"] <= bounds[1]
        measurements["acceptance_bounds_hz"] = list(bounds)
        return TestOutcome(TestStatus.PASS if passed else TestStatus.FAIL, f"Measured mean rate {metrics['mean_hz']:.3f} Hz; configured range {bounds[0]:.3f}-{bounds[1]:.3f} Hz.", measurements)

    def configured_target(self, p, _sections):
        return p.acceptance.get(self.target_key)


class Ros2PointCloudRateExecutor(_RateExecutor):
    topic_key = "pointcloud2"
    target_key = "pointcloud_rate_hz"
    tolerance_key = "pointcloud_rate_tolerance_percent"

    def build_command(self, p) -> str:
        base = super().build_command(p)
        config = p.sampling.get("production_config_path")
        return base + "; " + _section("CONFIG", f"docker exec {_q(p.container_name)} grep -E 'publish_freq' {_q(config)}")

    def configured_target(self, p, sections):
        values = re.findall(r"publish_freq[^0-9]*([0-9]+(?:\.[0-9]+)?)", sections.get("CONFIG", ""))
        return float(values[-1]) if values else None


class Ros2ImuTopicExecutor(_RateExecutor):
    topic_key = "imu"
    target_key = "imu_rate_hz"
    tolerance_key = "imu_rate_tolerance_percent"


class Ros2CustomMessageExecutor(_RateExecutor):
    topic_key = "custom"
    target_key = "custom_rate_hz"
    tolerance_key = "custom_rate_tolerance_percent"

    def build_command(self, p) -> str:
        topic = _q(p.topics["custom"].name)
        duration = int(p.sampling.get("topic_rate_duration_sec", 10))
        return _shell(p, [
            _section("INFO", f"ros2 topic info -v {topic}"),
            _section("INTERFACE", f"ros2 interface show {_q(p.topics['custom'].message_type)}"),
            _section("MESSAGE", f"ros2 topic echo {topic} --once"),
            _section("HZ", f"timeout {duration} ros2 topic hz {topic} || true"),
        ], domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        info = sections.get("INFO", "")
        message = sections.get("MESSAGE", "")
        fields = list(p.sampling.get("custom_fields", []))
        checks = {"publisher_type": p.topics["custom"].message_type in info and p.lidar_publisher in info, "interface": bool(sections.get("INTERFACE")), "decoded_message": bool(message), "required_fields": bool(fields) and all(re.search(rf"(^|\\s){re.escape(field)}:", message, re.MULTILINE) for field in fields)}
        metrics = parse_ros2_hz(sections.get("HZ", ""))
        measurements = {"checks": checks, "rate": metrics, "required_fields": fields}
        if not all(checks.values()) or metrics["mean_hz"] is None:
            return TestOutcome(TestStatus.FAIL, "CustomMsg type, interface, data, fields, or rate was invalid.", measurements)
        bounds = configured_bounds(p.acceptance.get("custom_rate_hz"), p.acceptance.get("custom_rate_tolerance_percent"))
        if bounds is None:
            return _needs_criteria("CustomMsg structure/data passed; official rate tolerance is not configured.", measurements)
        passed = bounds[0] <= metrics["mean_hz"] <= bounds[1]
        return TestOutcome(TestStatus.PASS if passed else TestStatus.FAIL, "CustomMsg met configured criteria." if passed else "CustomMsg rate failed configured criteria.", measurements)


class Ros2PointCloudSchemaExecutor(Ros2CommandExecutor):
    def build_command(self, p) -> str:
        topic = _q(p.topics["pointcloud2"].name)
        fields = ("header", "height", "width", "fields", "point_step", "row_step", "is_dense")
        commands = [_section("TYPE", f"ros2 topic type {topic}")]
        commands.extend(_section(field.upper(), f"ros2 topic echo {topic} --once --field {field}") for field in fields)
        commands.append(_section("DATA_BYTES", f"ros2 topic echo {topic} --once --field data | wc -c"))
        return _shell(p, commands, domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result) -> TestOutcome:
        sections = parse_sectioned_output(result["stdout"])
        checks = {
            "type": sections.get("TYPE") == p.topics["pointcloud2"].message_type,
            "header_stamp": "stamp" in sections.get("HEADER", ""),
            "frame": p.pointcloud_frame in sections.get("HEADER", ""),
            "width": _positive_integer(sections.get("WIDTH", "")),
            "height": _positive_integer(sections.get("HEIGHT", "")),
            "fields": bool(sections.get("FIELDS")),
            "point_step": bool(sections.get("POINT_STEP")),
            "row_step": bool(sections.get("ROW_STEP")),
            "is_dense": bool(sections.get("IS_DENSE")),
            "non_empty_data": _positive_integer(sections.get("DATA_BYTES", "")),
        }
        return TestOutcome(TestStatus.PASS if all(checks.values()) else TestStatus.FAIL, "PointCloud2 schema checks passed." if all(checks.values()) else "PointCloud2 schema checks failed.", {"checks": checks, "summary": sections})


class Ros2FrameTfExecutor(Ros2CommandExecutor):
    def preflight(self, p):
        if not p.imu_frame or not p.robot_base_frame:
            return _needs_criteria("IMU frame and robot base frame are not configured.", {"missing_parameters": ["imu_frame", "robot_base_frame"]})
        return None

    def build_command(self, p) -> str:
        pc = _q(p.topics["pointcloud2"].name)
        imu = _q(p.topics["imu"].name)
        config = p.sampling.get("production_config_path")
        return _shell(p, [_section("PC_FRAME", f"ros2 topic echo {pc} --once --field header.frame_id"), _section("IMU_FRAME", f"ros2 topic echo {imu} --once --field header.frame_id"), _section("CONFIG_FRAME", f"docker exec {_q(p.container_name)} grep -E 'frame_id' {_q(config)}"), _section("TF", f"timeout 10 ros2 run tf2_ros tf2_echo {_q(p.robot_base_frame)} {_q(p.pointcloud_frame)}")], domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        checks = {"pointcloud_frame": p.pointcloud_frame in sections.get("PC_FRAME", ""), "imu_frame": p.imu_frame in sections.get("IMU_FRAME", ""), "config_frame": p.pointcloud_frame in sections.get("CONFIG_FRAME", ""), "tf": bool(sections.get("TF"))}
        return TestOutcome(TestStatus.PASS if all(checks.values()) else TestStatus.FAIL, "Frame and TF checks passed." if all(checks.values()) else "Frame or TF consistency check failed.", {"checks": checks, "sections": sections})


class Ros2BagExecutor(Ros2CommandExecutor):
    def preflight(self, p):
        if not p.playback_isolation_configured:
            return _needs_criteria("Playback was not attempted: isolated_ros_domain_id is missing or unsafe.", {"playback_attempted": False})
        return None

    def build_command(self, p) -> str:
        if not p.playback_isolation_configured:
            raise ValueError("Safe rosbag playback requires an isolated ROS domain")
        duration = int(p.sampling.get("bag_duration_sec", 30))
        root = str(p.sampling.get("bag_root", "/tmp/cam_lidar_rosbags"))
        bag = f"{root}/tc_ros_007_$$"
        topics = [p.topics["pointcloud2"].name, p.topics["imu"].name]
        production = int(p.production_ros_domain_id)
        isolated = int(p.isolated_ros_domain_id)
        record = f"mkdir -p {_q(root)}; export ROS_DOMAIN_ID={production}; timeout -s INT {duration} ros2 bag record -o {bag} " + " ".join(_q(t) for t in topics) + " || test $? -eq 124"
        info = _section("BAG_INFO", f"ros2 bag info {bag}")
        probe = f"{bag}.isolated_probe.txt"
        playback = (
            f"export ROS_DOMAIN_ID={isolated}; "
            f"timeout {duration + 15} ros2 topic echo {_q(topics[0])} --once --field header > {probe} & probe_pid=$!; "
            f"timeout {duration + 15} ros2 bag play {bag} --rate 10.0; wait $probe_pid; "
            + _section("PLAYBACK_OBS", f"wc -c < {probe}")
        )
        reference = _section("BAG_PATH", f"printf '%s\\n' {bag}")
        return _shell(p, [record, info, playback, reference])

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        info = sections.get("BAG_INFO", "")
        checks = {key: topic.name in info and topic.message_type in info for key, topic in (("pointcloud2", p.topics["pointcloud2"]), ("imu", p.topics["imu"]))}
        duration_values = re.findall(r"Duration:\s*([0-9]+(?:\.[0-9]+)?)s", info)
        duration_sec = float(duration_values[-1]) if duration_values else None
        topic_counts = {
            topic.name: int(value)
            for topic in p.topics.values()
            for value in re.findall(rf"Topic:\s*{re.escape(topic.name)}\s*\|[^\n]*Count:\s*(\d+)", info)
        }
        checks["duration"] = duration_sec is not None and duration_sec >= float(p.sampling.get("bag_duration_sec", 30))
        checks["message_counts"] = all(topic_counts.get(topic.name, 0) > 0 for topic in (p.topics["pointcloud2"], p.topics["imu"]))
        try:
            observed_bytes = int(sections.get("PLAYBACK_OBS", "0"))
        except ValueError:
            observed_bytes = 0
        checks["isolated_playback"] = observed_bytes > 0
        measurements = {"checks": checks, "bag_path": sections.get("BAG_PATH"), "duration_sec": duration_sec, "topic_counts": topic_counts, "isolated_ros_domain_id": p.isolated_ros_domain_id, "isolated_observed_bytes": observed_bytes, "bag_info": info[:16000]}
        return TestOutcome(TestStatus.PASS if all(checks.values()) else TestStatus.FAIL, "Bounded bag record/info and isolated playback completed." if all(checks.values()) else "Bag validation failed.", measurements)


class Ros2PointCloudMetricsExecutor(Ros2CommandExecutor):
    def build_command(self, p):
        topic = _q(p.topics["pointcloud2"].name)
        count = int(p.sampling.get("pointcloud_frame_count", 20))
        return _shell(p, [_section("SAMPLES", f"for i in $(seq 1 {count}); do w=$(ros2 topic echo {topic} --once --field width); h=$(ros2 topic echo {topic} --once --field height); printf '%s %s\\n' \"$w\" \"$h\"; done"), _section("HZ", f"timeout 10 ros2 topic hz {topic} || true")], domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        counts = []
        for line in sections.get("SAMPLES", "").splitlines():
            values = line.split()
            if len(values) == 2 and all(value.isdigit() for value in values):
                counts.append(int(values[0]) * int(values[1]))
        mean = statistics.fmean(counts) if counts else None
        std = statistics.pstdev(counts) if len(counts) > 1 else 0.0 if counts else None
        cv = std / mean if mean else None
        rate = parse_ros2_hz(sections.get("HZ", ""))
        measurements = {"sample_count": len(counts), "point_count_min": min(counts) if counts else None, "point_count_max": max(counts) if counts else None, "point_count_mean": mean, "point_count_std": std, "point_count_cv": cv, "empty_frame_count": sum(value == 0 for value in counts), "frame_rate": rate, "point_rate_mean": mean * rate["mean_hz"] if mean is not None and rate["mean_hz"] is not None else None}
        if not counts or measurements["empty_frame_count"]:
            return TestOutcome(TestStatus.FAIL, "PointCloud2 frame samples were absent or empty.", measurements)
        if p.acceptance.get("point_count_cv_max") is None or p.acceptance.get("large_drop_threshold") is None:
            return _needs_criteria("Point counts measured; official stability thresholds are not configured.", measurements)
        cv_limit = float(p.acceptance["point_count_cv_max"])
        drop_limit = float(p.acceptance["large_drop_threshold"])
        large_drops = sum(current < previous * (1.0 - drop_limit) for previous, current in zip(counts, counts[1:]))
        measurements["large_drop_count"] = large_drops
        passed = cv is not None and cv <= cv_limit and large_drops == 0
        return TestOutcome(TestStatus.PASS if passed else TestStatus.FAIL, "Point-count samples met configured criteria." if passed else "Point-count stability failed configured criteria.", measurements)


class Ros2TimestampExecutor(Ros2CommandExecutor):
    def build_command(self, p):
        duration = int(p.sampling.get("timestamp_duration_sec", 30))
        commands = []
        for key in ("pointcloud2", "imu"):
            topic = _q(p.topics[key].name)
            commands.append(_section(key.upper(), f"timeout {duration} ros2 topic echo {topic} --field header.stamp || true"))
        return _shell(p, commands, domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        measurements = {"topics": {key: _timestamp_summary(sections.get(key.upper(), ""), p.acceptance.get("large_gap_threshold_sec")) for key in ("pointcloud2", "imu")}}
        summaries = measurements["topics"].values()
        if any(item["sample_count"] < 2 or item["rollback_count"] for item in summaries):
            return TestOutcome(TestStatus.FAIL, "ROS2 timestamp rollback or insufficient samples detected.", measurements)
        if p.acceptance.get("duplicate_timestamp_limit") is None or p.acceptance.get("large_gap_threshold_sec") is None:
            return _needs_criteria("ROS2 timestamp measurements retained; duplicate/gap criteria are not configured.", measurements)
        return TestOutcome(TestStatus.PASS, "Both ROS2 header timestamp streams met configured criteria.", measurements)


class Ros2TimeSyncExecutor(Ros2CommandExecutor):
    def build_command(self, p):
        return _shell(p, [_section("TIMEDATECTL", "timedatectl"), _section("CHRONY", "command -v chronyc >/dev/null && chronyc tracking || true"), _section("PTP", "systemctl --no-pager --plain status ptp4l 2>/dev/null || true"), _section("SYSTEM_CLOCK", "date +%s.%N"), _section("POINT_STAMP", f"ros2 topic echo {_q(p.topics['pointcloud2'].name)} --once --field header.stamp"), _section("IMU_STAMP", f"ros2 topic echo {_q(p.topics['imu'].name)} --once --field header.stamp")], domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        try:
            system_clock = float(sections.get("SYSTEM_CLOCK", ""))
        except ValueError:
            system_clock = None
        point_stamp = _extract_stamp(sections.get("POINT_STAMP", ""))
        imu_stamp = _extract_stamp(sections.get("IMU_STAMP", ""))
        offsets = [abs(value - system_clock) for value in (point_stamp, imu_stamp) if value is not None and system_clock is not None]
        measurements = {"timedatectl": sections.get("TIMEDATECTL"), "chrony": sections.get("CHRONY"), "ptp": sections.get("PTP"), "declared_sync_mechanism": p.acceptance.get("declared_sync_mechanism"), "maximum_clock_offset_sec": p.acceptance.get("maximum_clock_offset_sec"), "system_clock": system_clock, "pointcloud_stamp": point_stamp, "imu_stamp": imu_stamp, "absolute_offsets_sec": offsets}
        if not sections.get("TIMEDATECTL"):
            return TestOutcome(TestStatus.FAIL, "No timedatectl status was available.", measurements)
        if p.acceptance.get("declared_sync_mechanism") is None or p.acceptance.get("maximum_clock_offset_sec") is None:
            return _needs_criteria("Time synchronization mechanisms recorded; declared mechanism/absolute offset requirement is not configured.", measurements)
        if len(offsets) != 2:
            return TestOutcome(TestStatus.FAIL, "Unable to compare both ROS2 header clocks to system time.", measurements)
        maximum = float(p.acceptance["maximum_clock_offset_sec"])
        passed = all(value <= maximum for value in offsets)
        return TestOutcome(TestStatus.PASS if passed else TestStatus.FAIL, "Both ROS2 header clocks met the configured offset requirement." if passed else "A ROS2 header clock exceeded the configured offset requirement.", measurements)


class Ros2SlamQosExecutor(Ros2CommandExecutor):
    def preflight(self, p):
        if not p.slam_node:
            return TestOutcome(TestStatus.SKIPPED, "SLAM node is not configured.", {"missing_prerequisite": "slam_node"})
        if not p.acceptance.get("expected_slam_qos"):
            return _needs_criteria("SLAM QoS acceptance profile is not configured.", {"missing_parameter": "expected_slam_qos"})
        return None

    def build_command(self, p):
        commands = [_section("NODE", f"ros2 node info {_q(p.slam_node)}")]
        for key, topic in p.topics.items():
            commands.append(_section(key.upper(), f"ros2 topic info -v {_q(topic.name)}"))
        return _shell(p, commands, domain_id=p.production_ros_domain_id)

    def evaluate(self, p, result):
        sections = parse_sectioned_output(result["stdout"])
        node = sections.get("NODE", "")
        subscribed = [topic.name for topic in p.topics.values() if topic.name in node]
        expected_qos = str(p.acceptance.get("expected_slam_qos"))
        endpoint_sections = [sections.get(key.upper(), "") for key, topic in p.topics.items() if topic.name in subscribed]
        passed = bool(subscribed) and all("Subscription count: 0" not in text and expected_qos in text for text in endpoint_sections)
        return TestOutcome(TestStatus.PASS if passed else TestStatus.FAIL, "SLAM input endpoints and QoS were compatible." if passed else "SLAM input subscription/QoS checks failed.", {"subscribed_inputs": subscribed, "sections": sections})


def _timestamp_summary(output: str, gap_threshold) -> dict:
    import re
    import statistics
    seconds = [int(value) for value in re.findall(r"sec:\s*(-?\d+)", output)]
    nanoseconds = [int(value) for value in re.findall(r"nanosec:\s*(\d+)", output)]
    stamps = [sec + nano / 1_000_000_000 for sec, nano in zip(seconds, nanoseconds)]
    deltas = [current - previous for previous, current in zip(stamps, stamps[1:])]
    return {"sample_count": len(stamps), "rollback_count": sum(value < 0 for value in deltas), "duplicate_count": sum(value == 0 for value in deltas), "min_delta": min(deltas) if deltas else None, "max_delta": max(deltas) if deltas else None, "mean_delta": statistics.fmean(deltas) if deltas else None, "std_delta": statistics.pstdev(deltas) if len(deltas) > 1 else None, "large_gap_count": sum(value > float(gap_threshold) for value in deltas) if gap_threshold is not None else None}


def _needs_criteria(message: str, measurements: dict) -> TestOutcome:
    values = dict(measurements)
    values.update({"measurement_only": True, "needs_criteria": True})
    return TestOutcome(TestStatus.SKIPPED, message, values)


def _bounded_remote_summary(result: dict) -> dict:
    return {"exit_status": result.get("exit_status"), "stderr": str(result.get("stderr") or "")[:16000], "output_truncated": bool(result.get("output_truncated"))}


def _positive_integer(text: str) -> bool:
    values = re.findall(r"\d+", str(text))
    return bool(values) and int(values[-1]) > 0


def _extract_stamp(text: str) -> float | None:
    seconds = re.findall(r"sec:\s*(-?\d+)", str(text))
    nanoseconds = re.findall(r"nanosec:\s*(\d+)", str(text))
    if not seconds or not nanoseconds:
        return None
    return int(seconds[0]) + int(nanoseconds[0]) / 1_000_000_000


ROS2_EXECUTORS: dict[str, type[BaseTestExecutor]] = {
    "ros2_environment": Ros2EnvironmentExecutor,
    "ros2_network": Ros2NetworkExecutor,
    "ros2_topics": Ros2PublisherTopicsExecutor,
    "ros2_pointcloud_rate": Ros2PointCloudRateExecutor,
    "ros2_imu_topic": Ros2ImuTopicExecutor,
    "ros2_custom_message": Ros2CustomMessageExecutor,
    "ros2_pointcloud_schema": Ros2PointCloudSchemaExecutor,
    "ros2_frame_tf": Ros2FrameTfExecutor,
    "ros2_bag": Ros2BagExecutor,
    "ros2_pointcloud_metrics": Ros2PointCloudMetricsExecutor,
    "ros2_timestamps": Ros2TimestampExecutor,
    "ros2_time_sync": Ros2TimeSyncExecutor,
    "ros2_slam_qos": Ros2SlamQosExecutor,
}
