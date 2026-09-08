from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class RosTopicRequirement:
    suffix: str
    message_type: str
    capability: str
    streaming: bool = True

    def topic(self, namespace: str) -> str:
        return namespace.rstrip("/") + "/" + self.suffix.lstrip("/")


@dataclass(frozen=True)
class RosImageProfile:
    width: int
    height: int
    fps: int
    encodings: tuple[str, ...]
    source: str
    configuration: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["encodings"] = list(self.encodings)
        return payload


@dataclass(frozen=True)
class RosLaunchSpec:
    driver: str
    package: str
    launch_file: str
    arguments: tuple[str, ...]
    namespace: str
    expected_node: str
    selected_serial: str
    serial_parameter: str
    ros_camera_model: str
    requested_profile: RosImageProfile
    mandatory_topics: tuple[RosTopicRequirement, ...]
    primary_image_topic: str

    def command(self) -> list[str]:
        return ["ros2", "launch", self.package, self.launch_file, *self.arguments]

    def to_dict(self) -> dict:
        return {
            "driver": self.driver,
            "package": self.package,
            "launch_file": self.launch_file,
            "arguments": list(self.arguments),
            "namespace": self.namespace,
            "expected_node": self.expected_node,
            "selected_serial": self.selected_serial,
            "serial_parameter": self.serial_parameter,
            "ros_camera_model": self.ros_camera_model,
            "requested_profile": self.requested_profile.to_dict(),
            "mandatory_topics": [asdict(item) for item in self.mandatory_topics],
            "primary_image_topic": self.primary_image_topic,
            "command": self.command(),
        }


@dataclass(frozen=True)
class RosNodeSession:
    session_id: str
    device_uid: str
    driver: str
    namespace: str
    expected_node: str
    selected_serial: str
    owned_by_test: bool
    pid: int | None = None
    process_group: int | None = None
    log_path: str | None = None
    started_at: str | None = None
    launch_command_summary: str | None = None
    setup_files: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict):
        return cls(
            session_id=str(payload.get("session_id") or "external"),
            device_uid=str(payload.get("device_uid") or ""),
            driver=str(payload.get("driver") or ""),
            namespace=str(payload.get("namespace") or ""),
            expected_node=str(payload.get("expected_node") or ""),
            selected_serial=str(payload.get("selected_serial") or ""),
            owned_by_test=bool(payload.get("owned_by_test")),
            pid=int(payload["pid"]) if payload.get("pid") is not None else None,
            process_group=(
                int(payload["process_group"])
                if payload.get("process_group") is not None else None
            ),
            log_path=payload.get("log_path"),
            started_at=payload.get("started_at"),
            launch_command_summary=payload.get("launch_command_summary"),
            setup_files=tuple(payload.get("setup_files") or ()),
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["setup_files"] = list(self.setup_files)
        return payload
