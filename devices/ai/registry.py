from devices.ai.models import AiModule


CONFIRMED = "CONFIRMED"
STRONG_CANDIDATE = "STRONG_CANDIDATE"
WEAK_CANDIDATE = "WEAK_CANDIDATE"


class AiModuleRegistry:
    """Deterministic in-memory registration of evidence-backed static modules."""
    def registered_modules(self, environment):
        modules = []
        seen = set()
        for raw in environment.get("modules") or ():
            module = AiModule.from_dict(raw)
            confidence = str(module.metadata.get("confidence") or "")
            if not module.module_uid or module.module_uid in seen or confidence != CONFIRMED:
                continue
            # A registry entry must be static. Runtime PIDs/topics are evidence,
            # not the module identity or persistent configuration.
            if module.metadata.get("pid") is not None:
                continue
            modules.append(module); seen.add(module.module_uid)
        return tuple(sorted(modules, key=lambda item: item.module_uid))

    @staticmethod
    def candidates(environment):
        return tuple(environment.get("module_candidates") or ())
