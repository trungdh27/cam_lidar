from devices.ai.models import AiModule
from devices.ai.registry import AiModuleRegistry


class AiRuntimeAdapter:
    """Generic adapter for only discovered, explicitly-described AI modules."""

    def discover_environment(self, manager):
        return manager.discover_environment()

    def __init__(self, registry=None):
        self.registry = registry or AiModuleRegistry()

    def discover_modules(self, environment):
        return self.registry.registered_modules(environment)

    @staticmethod
    def resolve_input(module):
        return module.input_topics[0] if module.input_topics else None

    @staticmethod
    def resolve_output(module):
        return module.output_topics[0] if module.output_topics else None

    @staticmethod
    def can_launch(module):
        return bool(module.launch_spec and module.launch_spec.approved and module.launch_spec.command)

    def start(self, manager, module, setup_files):
        if not self.can_launch(module):
            return None
        return manager.start(module, setup_files)

    @staticmethod
    def status(manager, session):
        return manager.status(session)

    @staticmethod
    def stop(manager, session):
        return manager.stop(session)

    @staticmethod
    def probe_endpoint(manager, session, endpoint, timeout_s, sample_count):
        return manager.probe_endpoint(
            session, endpoint, timeout_s=float(timeout_s), sample_count=int(sample_count),
        )
