from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class AiEndpoint:
    name: str
    message_type: str = ""
    role: str = ""
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        return cls(
            name=str(value.get("name") or ""),
            message_type=str(value.get("message_type") or ""),
            role=str(value.get("role") or ""),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AiLaunchSpec:
    method: str
    command: tuple[str, ...] = ()
    expected_node: str = ""
    approved: bool = False
    package: str = ""
    launch_file: str = ""
    executable: str = ""
    arguments: tuple[str, ...] = ()
    working_directory: str = ""
    launch_ready: bool = False

    @classmethod
    def from_dict(cls, value):
        return cls(
            method=str(value.get("method") or ""),
            command=tuple(str(item) for item in value.get("command") or ()),
            expected_node=str(value.get("expected_node") or ""),
            approved=bool(value.get("approved")),
            package=str(value.get("package") or ""),
            launch_file=str(value.get("launch_file") or ""),
            executable=str(value.get("executable") or ""),
            arguments=tuple(str(item) for item in value.get("arguments") or ()),
            working_directory=str(value.get("working_directory") or ""),
            launch_ready=bool(value.get("launch_ready")),
        )

    def to_dict(self):
        payload = asdict(self)
        payload["command"] = list(self.command)
        payload["arguments"] = list(self.arguments)
        return payload


@dataclass(frozen=True)
class AiModule:
    module_uid: str
    display_name: str
    function_type: str = "generic_inference"
    runtime: str = "UNKNOWN"
    package: str = ""
    executable: str = ""
    process_name: str = ""
    ros_node: str = ""
    input_topics: tuple[AiEndpoint, ...] = ()
    output_topics: tuple[AiEndpoint, ...] = ()
    model_name: str = ""
    model_path: str = ""
    launch_spec: AiLaunchSpec | None = None
    status: str = "UNKNOWN"
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        launch = value.get("launch_spec") or {}
        return cls(
            module_uid=str(value.get("module_uid") or ""),
            display_name=str(value.get("display_name") or value.get("module_uid") or "AI Module"),
            function_type=str(value.get("function_type") or "generic_inference"),
            runtime=str(value.get("runtime") or "UNKNOWN"),
            package=str(value.get("package") or ""),
            executable=str(value.get("executable") or ""),
            process_name=str(value.get("process_name") or ""),
            ros_node=str(value.get("ros_node") or ""),
            input_topics=tuple(AiEndpoint.from_dict(item) for item in value.get("input_topics") or ()),
            output_topics=tuple(AiEndpoint.from_dict(item) for item in value.get("output_topics") or ()),
            model_name=str(value.get("model_name") or ""),
            model_path=str(value.get("model_path") or ""),
            launch_spec=AiLaunchSpec.from_dict(launch) if launch else None,
            status=str(value.get("status") or "UNKNOWN"),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self):
        return {
            "module_uid": self.module_uid,
            "display_name": self.display_name,
            "function_type": self.function_type,
            "runtime": self.runtime,
            "package": self.package,
            "executable": self.executable,
            "process_name": self.process_name,
            "ros_node": self.ros_node,
            "input_topics": [item.to_dict() for item in self.input_topics],
            "output_topics": [item.to_dict() for item in self.output_topics],
            "model_name": self.model_name,
            "model_path": self.model_path,
            "launch_spec": self.launch_spec.to_dict() if self.launch_spec else None,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AiSession:
    session_id: str
    module_uid: str
    pid: int | None = None
    process_group: int | None = None
    started_at: str | None = None
    log_path: str | None = None
    owned_by_test: bool = False
    launch_command_summary: str = ""
    expected_node: str = ""
    setup_files: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value):
        return cls(
            session_id=str(value.get("session_id") or "external"),
            module_uid=str(value.get("module_uid") or ""),
            pid=int(value["pid"]) if value.get("pid") is not None else None,
            process_group=int(value["process_group"]) if value.get("process_group") is not None else None,
            started_at=value.get("started_at"),
            log_path=value.get("log_path"),
            owned_by_test=bool(value.get("owned_by_test")),
            launch_command_summary=str(value.get("launch_command_summary") or ""),
            expected_node=str(value.get("expected_node") or ""),
            setup_files=tuple(value.get("setup_files") or ()),
        )

    def to_dict(self):
        payload = asdict(self)
        payload["setup_files"] = list(self.setup_files)
        return payload
