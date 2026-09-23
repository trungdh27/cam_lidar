"""Actionable setup explanations derived from the existing per-TC dependencies."""


def blocker_guidance(dependency, case):
    key = dependency.key
    why = f"{case.name} needs this prerequisite to collect and validate its required evidence."
    fix, actions = dependency.action, [("recheck", "RECHECK")]
    if key == "shared_ssh_ready":
        why = "This test needs remote Jetson commands through the shared Dashboard connection."
        fix = "Connect to Jetson from Dashboard, then click RECHECK."
        actions = [("dashboard", "GO TO DASHBOARD"), ("recheck", "RECHECK")]
    elif key in {"distance_1m_confirmed", "distance_10m_confirmed"}:
        distance = "10 m" if "10m" in key else ">=1 m"
        why = f"This test validates Wi-Fi operation at {distance}."
        fix = f"Place the laptop at {distance}, then confirm its position."
        actions = [("confirm", f"CONFIRM {distance} POSITION")]
    elif key == "allow_disruptive":
        why = "This test intentionally changes or interrupts the active Wi-Fi state."
        fix = "Confirm authorization only after the control and recovery paths shown below are acceptable."
        actions = [("confirm", "AUTHORIZE DISRUPTIVE EXECUTION"), ("recheck", "CANCEL / RECHECK")]
    elif key in {"saved_wifi_profile_available", "valid_access_capability"}:
        why = "The runner must reconnect the laptop using a reusable valid NetworkManager profile."
        fix = "Connect to the DUT SSID using NetworkManager and save its profile, then detect it again."
        actions = [("recheck", "DETECT SAVED PROFILE"), ("setup", "OPEN SETUP")]
    elif key.startswith("external_ssid") or key in {"external_credential", "wifi_credential", "security_requirement", "recovery_configuration"}:
        fix = "Refresh runtime discovery. This value is not entered manually in the tester workflow."
        actions = [("recheck", "RECHECK")]
    elif key in {"current_band", "ap_runtime", "current_mode"}:
        why = "The test must measure the source-required operating band and mode."
        fix = dependency.action + "; then click RECHECK. Automatic changes require a verified safe control path."
        actions = [("setup", "OPEN SETUP"), ("recheck", "RECHECK")]
    elif key in {"full_wifi_qual_path", "client_association_ready", "network_path_ready"}:
        why = "Measurements must use the DUT Wi-Fi link in both directions."
        fix = "Connect the laptop to the DUT SSID. Recheck to verify association and both Wi-Fi routes."
        actions = [("recheck", "RECHECK"), ("setup", "OPEN SETUP")]
    elif key in {"auto_reconnect", "control_path_safety", "client_recovery_control", "ap_restart_control"}:
        why = "The runner must retain control and restore Wi-Fi after deliberately interrupting it."
        fix = "Provide a reachable backup Ethernet connection or a tested reconnect strategy, then recheck."
        actions = [("dashboard", "GO TO DASHBOARD"), ("recheck", "RECHECK")]
    return {"title": dependency.label, "current": dependency.current_state,
            "why": why, "fix": fix, "actions": actions}
