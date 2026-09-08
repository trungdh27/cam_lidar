import re


ZED_MONO_PROFILES = frozenset({"zed_x_one_4k", "zed_x_one_gs"})


def normalize_zed_model(value: object) -> str:
    text = getattr(value, "name", None) or str(value)
    text = text.upper()
    text = text.replace("CAMERA_MODEL.", "").replace("CAMERA_MODEL_ONE.", "")
    return " ".join(re.findall(r"[A-Z]+|\d+", text))


def zed_model_family(value: object) -> str:
    normalized = normalize_zed_model(value)
    compact = normalized.replace(" ", "")
    if "XONE" in compact:
        if any(token in compact for token in ("GS", "GLOBALSHUTTER")):
            return "zed_x_one_gs"
        if any(token in compact for token in ("UHD", "4K")):
            return "zed_x_one_4k"
        return "zed_x_one"
    if "XMINI" in compact or "ZEDMINI" in compact:
        return "zed_x_mini"
    if "ZEDX" in compact:
        return "zed_x"
    return "unknown_zed"


def canonical_zed_model(value: object) -> str:
    family = zed_model_family(value)
    return {
        "zed_x_one_4k": "ZED X One 4K",
        "zed_x_one_gs": "ZED X One GS",
        "zed_x_one": "ZED X One",
        "zed_x_mini": "ZED X Mini",
        "zed_x": "ZED X",
    }.get(family, str(value).strip() or "Unknown ZED")


def is_camera_one_profile(profile_id: str) -> bool:
    return profile_id in ZED_MONO_PROFILES


def model_matches_profile(value: object, profile_id: str) -> bool:
    family = zed_model_family(value)
    if profile_id == "zed_x_one_4k":
        return family in ("zed_x_one_4k", "zed_x_one")
    if profile_id == "zed_x_one_gs":
        return family in ("zed_x_one_gs", "zed_x_one")
    return family == profile_id
