"""Dynamic, vendor-neutral ROS camera graph discovery.

This module deliberately knows message types, not production namespaces or
driver packages.  It is shared by ROS-001 through ROS-008 so a robot may
rename nodes, move a camera under another namespace, or use a custom wrapper
without changing the automation suite.
"""

from dataclasses import dataclass, field
from typing import Iterable


IMAGE = "sensor_msgs/msg/Image"
COMPRESSED_IMAGE = "sensor_msgs/msg/CompressedImage"
CAMERA_INFO = "sensor_msgs/msg/CameraInfo"
IMU = "sensor_msgs/msg/Imu"
POINTCLOUD = "sensor_msgs/msg/PointCloud2"
TEMPERATURE = "sensor_msgs/msg/Temperature"

CAMERA_MESSAGE_TYPES = {
    IMAGE: "image",
    COMPRESSED_IMAGE: "compressed_image",
    CAMERA_INFO: "camera_info",
    IMU: "imu",
    POINTCLOUD: "pointcloud",
    TEMPERATURE: "temperature",
}


def _normalise_namespace(value: str) -> str:
    value = "/" + str(value or "").strip("/")
    return "/" if value == "//" else value


def _camera_namespace(topic: str, message_type: str) -> str:
    """Find a stable camera root from a topic without relying on its name.

    A final role branch (``color``, ``depth``, ``imu``…) is removed only when
    a typed endpoint makes that relationship plausible.  This groups common
    RealSense layouts as well as flat/custom production topics.
    """
    parts = [part for part in str(topic).strip("/").split("/") if part]
    if len(parts) < 2:
        return "/"
    parent = parts[:-1]
    role_branches = {
        "color", "rgb", "depth", "imu", "infra1", "infra2", "infrared",
        "aligned_depth_to_color", "pointcloud", "point_cloud", "points", "stereo",
        "rect", "registered", "temperature",
    }
    if message_type in {IMAGE, COMPRESSED_IMAGE, CAMERA_INFO, IMU, POINTCLOUD}:
        while parent and parent[-1].lower() in role_branches:
            parent = parent[:-1]
    return _normalise_namespace("/".join(parent))


def _is_depth_topic(topic: str) -> bool:
    return "depth" in str(topic).lower().split("/") or "depth" in str(topic).lower()


@dataclass(frozen=True)
class CameraRosEndpoint:
    namespace: str
    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    image_topics: tuple[str, ...] = ()
    depth_topics: tuple[str, ...] = ()
    camera_info_topics: tuple[str, ...] = ()
    imu_topics: tuple[str, ...] = ()
    pointcloud_topics: tuple[str, ...] = ()
    temperature_topics: tuple[str, ...] = ()
    compressed_image_topics: tuple[str, ...] = ()
    device_info_topics: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    publisher_metadata: dict[str, tuple[dict, ...]] = field(default_factory=dict)

    @property
    def primary_image_topic(self) -> str | None:
        return (self.image_topics or self.compressed_image_topics or (None,))[0]

    @property
    def usable(self) -> bool:
        return bool(self.primary_image_topic)

    def capabilities(self) -> dict[str, bool]:
        return {
            "image": bool(self.image_topics),
            "depth": bool(self.depth_topics),
            "camera_info": bool(self.camera_info_topics),
            "imu": bool(self.imu_topics),
            "pointcloud": bool(self.pointcloud_topics),
            "temperature": bool(self.temperature_topics),
            "compressed_image": bool(self.compressed_image_topics),
        }

    def to_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "vendor": self.vendor,
            "model": self.model,
            "serial": self.serial,
            "image_topics": list(self.image_topics),
            "depth_topics": list(self.depth_topics),
            "camera_info_topics": list(self.camera_info_topics),
            "imu_topics": list(self.imu_topics),
            "pointcloud_topics": list(self.pointcloud_topics),
            "temperature_topics": list(self.temperature_topics),
            "compressed_image_topics": list(self.compressed_image_topics),
            "device_info_topics": list(self.device_info_topics),
            "nodes": list(self.nodes),
            "publisher_metadata": {
                topic: list(records)
                for topic, records in self.publisher_metadata.items()
            },
            "capabilities": self.capabilities(),
        }


@dataclass(frozen=True)
class CameraRosAssociation:
    device_uid: str
    endpoint: CameraRosEndpoint | None
    confidence: str
    score: int
    evidence: tuple[str, ...] = ()

    @property
    def reliable(self) -> bool:
        return self.endpoint is not None and self.score >= 70

    def to_dict(self) -> dict:
        return {
            "device_uid": self.device_uid,
            "namespace": self.endpoint.namespace if self.endpoint else None,
            "confidence": self.confidence,
            "score": self.score,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class RosCameraGraphDiscovery:
    nodes: tuple[str, ...] = ()
    topic_types: dict[str, tuple[str, ...]] = field(default_factory=dict)
    publisher_counts: dict[str, int] = field(default_factory=dict)
    publisher_nodes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    publisher_metadata: dict[str, tuple[dict, ...]] = field(default_factory=dict)
    candidates: tuple[CameraRosEndpoint, ...] = ()
    associations: tuple[CameraRosAssociation, ...] = ()
    stage_timings_s: dict[str, float] = field(default_factory=dict)

    def association_for(self, device_uid: str) -> CameraRosAssociation | None:
        return next((item for item in self.associations if item.device_uid == device_uid), None)

    def to_dict(self) -> dict:
        return {
            "node_count": len(self.nodes),
            "topic_count": len(self.topic_types),
            "camera_candidates": [item.to_dict() for item in self.candidates],
            "associations": [item.to_dict() for item in self.associations],
            "stage_timings_s": dict(self.stage_timings_s),
            "publisher_metadata": {
                topic: list(records)
                for topic, records in self.publisher_metadata.items()
            },
        }


def discover_camera_graph(snapshot: dict, physical_devices: Iterable = ()) -> RosCameraGraphDiscovery:
    """Create camera candidates from a serialisable rclpy graph snapshot."""
    raw_topics = snapshot.get("topics") or {}
    publisher_counts = {
        str(topic): int(value or 0)
        for topic, value in (snapshot.get("publisher_counts") or {}).items()
    }
    publisher_nodes = {
        str(topic): tuple(sorted(set(str(node) for node in nodes if node)))
        for topic, nodes in (snapshot.get("publisher_nodes") or {}).items()
    }
    publisher_metadata = {
        str(topic): tuple(dict(record or {}) for record in records or ())
        for topic, records in (snapshot.get("publisher_metadata") or {}).items()
    }
    node_parameters = {
        str(node): dict(values or {})
        for node, values in (snapshot.get("node_parameters") or {}).items()
    }
    topic_types = {
        str(topic): tuple(str(kind) for kind in kinds)
        for topic, kinds in raw_topics.items()
    }
    grouped: dict[str, dict[str, set[str]]] = {}
    for topic, kinds in topic_types.items():
        for message_type in kinds:
            capability = CAMERA_MESSAGE_TYPES.get(message_type)
            if capability is None:
                continue
            namespace = _camera_namespace(topic, message_type)
            bucket = grouped.setdefault(namespace, {
                "image_topics": set(), "depth_topics": set(), "camera_info_topics": set(),
                "imu_topics": set(), "pointcloud_topics": set(),
                "temperature_topics": set(),
                "compressed_image_topics": set(), "device_info_topics": set(), "nodes": set(),
            })
            key = capability + "_topics"
            if capability == "image" and _is_depth_topic(topic):
                key = "depth_topics"
            bucket[key].add(topic)
            bucket["nodes"].update(publisher_nodes.get(topic, ()))

    # Device-info is metadata, not a universally standard ROS type.  Attach it
    # only to an already image-backed candidate; it never creates a camera by
    # itself and is therefore not a name-only classification shortcut.
    for topic in topic_types:
        if not topic.endswith("/device_info"):
            continue
        parent = _normalise_namespace(topic.rsplit("/", 1)[0])
        candidate = grouped.get(parent)
        if candidate is not None:
            candidate["device_info_topics"].add(topic)

    metadata_by_namespace = snapshot.get("camera_metadata") or {}
    candidates = tuple(
        CameraRosEndpoint(
            namespace=namespace,
            vendor=(metadata_by_namespace.get(namespace) or {}).get("vendor"),
            model=(metadata_by_namespace.get(namespace) or {}).get("model"),
            serial=str((metadata_by_namespace.get(namespace) or {}).get("serial_number") or "") or None,
            publisher_metadata={
                topic: publisher_metadata.get(topic, ())
                for topic in (
                    *values["image_topics"], *values["depth_topics"],
                    *values["camera_info_topics"], *values["imu_topics"],
                    *values["pointcloud_topics"], *values["temperature_topics"],
                    *values["compressed_image_topics"],
                )
            },
            **{key: tuple(sorted(value)) for key, value in values.items()},
        )
        for namespace, values in sorted(grouped.items())
        if values["image_topics"] or values["compressed_image_topics"]
    )
    devices = tuple(physical_devices or ())
    associations = tuple(_associate(device, candidates, node_parameters) for device in devices)
    return RosCameraGraphDiscovery(
        nodes=tuple(sorted(set(str(node) for node in snapshot.get("nodes") or ()))),
        topic_types=topic_types,
        publisher_counts=publisher_counts,
        publisher_nodes=publisher_nodes,
        publisher_metadata=publisher_metadata,
        candidates=candidates,
        associations=associations,
        stage_timings_s=dict(snapshot.get("stage_timings_s") or {}),
    )


def _associate(device, candidates: tuple[CameraRosEndpoint, ...], node_parameters: dict[str, dict]) -> CameraRosAssociation:
    scores = []
    for candidate in candidates:
        score, evidence = 0, []
        serial = str(getattr(device, "serial", "") or "")
        if serial and candidate.serial and serial == candidate.serial:
            score += 100
            evidence.append("runtime serial match")
        parameter_values = {
            str(name).lower(): str(value)
            for node in candidate.nodes
            for name, value in node_parameters.get(node, {}).items()
        }
        if serial and any(serial in value for name, value in parameter_values.items() if "serial" in name or "device" in name):
            score += 100
            evidence.append("publisher node serial/device parameter match")
        # Inventory device-info association is authoritative runtime evidence.
        rgb_topic = getattr(device, "rgb_topic", None)
        if rgb_topic and rgb_topic in (*candidate.image_topics, *candidate.compressed_image_topics):
            score += 90
            evidence.append("inventory RGB topic match")
        ros_node = getattr(device, "ros_node", None)
        if ros_node and ros_node in candidate.nodes:
            score += 80
            evidence.append("inventory ROS node match")
        info_topic = getattr(device, "device_info_topic", None)
        if info_topic and info_topic in candidate.device_info_topics:
            score += 90
            evidence.append("inventory device-info topic match")
        namespace_hint = getattr(device, "ros_namespace_hint", None)
        if namespace_hint and _normalise_namespace(namespace_hint) == candidate.namespace:
            score += 40
            evidence.append("inventory namespace match")
        # A name hint is deliberately weak: it supports diagnosis but cannot
        # independently turn a physical device into a mapped PASS.
        model = str(getattr(device, "model", "")).lower()
        model_parameter_matches = [
            endpoint for endpoint in candidates
            if model and any(
                model in str(value).lower()
                for node in endpoint.nodes
                for name, value in node_parameters.get(node, {}).items()
                if "model" in str(name).lower()
            )
        ]
        if candidate in model_parameter_matches:
            if len(model_parameter_matches) == 1:
                score += 70
                evidence.append("unique publisher node model parameter match")
            else:
                score += 25
                evidence.append("non-unique publisher node model parameter match")
        if model and any(token and token in candidate.namespace.lower() for token in model.split()):
            score += 5
            evidence.append("model namespace hint")
        runtime_model = str(candidate.model or "").lower()
        normalized_device_model = "".join(character for character in model if character.isalnum())
        normalized_runtime_model = "".join(character for character in runtime_model if character.isalnum())
        model_matches = [
            endpoint for endpoint in candidates
            if normalized_runtime_model and normalized_runtime_model == "".join(
                character for character in str(endpoint.model or "").lower() if character.isalnum()
            )
        ]
        if normalized_device_model and normalized_device_model == normalized_runtime_model:
            if len(model_matches) == 1:
                score += 55
                evidence.append("unique runtime model match")
            elif len(model_matches) > 1:
                score += 25
                evidence.append("non-unique runtime model match")
        scores.append((score, tuple(evidence), candidate))
    if not scores:
        return CameraRosAssociation(device.device_uid, None, "UNKNOWN", 0)
    scores.sort(key=lambda item: item[0], reverse=True)
    score, evidence, candidate = scores[0]
    equally_scored = len(scores) > 1 and scores[1][0] == score and score > 0
    if score >= 70 and not equally_scored:
        confidence = "HIGH" if score >= 90 else "MEDIUM"
        return CameraRosAssociation(device.device_uid, candidate, confidence, score, evidence)
    if score >= 50 and not equally_scored:
        return CameraRosAssociation(device.device_uid, candidate, "MEDIUM", score, evidence)
    if score:
        return CameraRosAssociation(device.device_uid, None, "PARTIAL", score, evidence)
    return CameraRosAssociation(device.device_uid, None, "UNKNOWN", 0)


def requirements_for_endpoint(endpoint: CameraRosEndpoint):
    """Convert discovered absolute topics to the existing collector contract."""
    from devices.camera.ros_automation.models import RosTopicRequirement

    namespace = endpoint.namespace.rstrip("/")

    def requirement(topic, message_type, capability, availability="OPTIONAL"):
        suffix = topic[len(namespace):].lstrip("/") if namespace and topic.startswith(namespace + "/") else topic.lstrip("/")
        return RosTopicRequirement(suffix, message_type, capability, availability=availability)

    requirements = []
    primary = endpoint.primary_image_topic
    if primary:
        image_type = IMAGE if primary in endpoint.image_topics else COMPRESSED_IMAGE
        requirements.append(requirement(primary, image_type, "color", "MANDATORY"))
    for topic in endpoint.depth_topics:
        requirements.append(requirement(topic, IMAGE, "depth"))
    for topic in endpoint.camera_info_topics:
        requirements.append(requirement(topic, CAMERA_INFO, "camera_info"))
    for topic in endpoint.imu_topics:
        requirements.append(requirement(topic, IMU, "imu"))
    for topic in endpoint.pointcloud_topics:
        requirements.append(requirement(topic, POINTCLOUD, "pointcloud"))
    for topic in endpoint.temperature_topics:
        requirements.append(requirement(topic, TEMPERATURE, "temperature"))
    return tuple(requirements)
