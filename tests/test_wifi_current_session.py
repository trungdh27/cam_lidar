import json

from desktop_app.wifi.runtime import WifiRuntime


def _legacy_result(
    root,
    environment="VD",
    test_id="TC-JET-5G-001",
    status="PASS",
):
    directory = (
        root
        / environment
        / "auto"
        / "AP_5G"
        / "20260922T000000Z"
        / test_id
        / "attempt_001"
    )
    directory.mkdir(parents=True)

    result = directory / "result.json"
    result.write_text(
        json.dumps(
            {
                "status": status,
                "final_result": status,
                "started": "2026-09-22T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return result


def test_historical_pass_is_not_current_session_result(tmp_path):
    runtime = WifiRuntime(None, evidence_root=tmp_path)

    result = _legacy_result(tmp_path)

    # Historical lookup remains intact for History/Evidence.
    assert runtime.attempts("VD", "TC-JET-5G-001") == [result]
    assert runtime.latest_status("VD", "TC-JET-5G-001") == "PASS"

    # Normal AUTO campaign must start clean.
    assert (
        runtime.current_session_status(
            "VD",
            "TC-JET-5G-001",
        )
        == "NOT RUN"
    )


def test_current_session_result_has_priority_over_history(tmp_path):
    runtime = WifiRuntime(None, evidence_root=tmp_path)

    _legacy_result(
        tmp_path,
        status="FAIL",
    )

    runtime._current_session_results[
        ("VD", "TC-JET-5G-001")
    ] = "PASS"

    assert (
        runtime.current_session_status(
            "VD",
            "TC-JET-5G-001",
        )
        == "PASS"
    )


def test_new_runtime_starts_new_current_session(tmp_path):
    first = WifiRuntime(None, evidence_root=tmp_path)

    first._current_session_results[
        ("VD", "TC-JET-5G-001")
    ] = "PASS"

    second = WifiRuntime(None, evidence_root=tmp_path)

    assert (
        second.current_session_status(
            "VD",
            "TC-JET-5G-001",
        )
        == "NOT RUN"
    )


def test_reset_current_results_preserves_history(tmp_path):
    runtime = WifiRuntime(None, evidence_root=tmp_path)

    historical = _legacy_result(
        tmp_path,
        status="PASS",
    )

    runtime._current_session_results[
        ("VD", "TC-JET-5G-001")
    ] = "FAIL"

    assert runtime.current_session_status(
        "VD",
        "TC-JET-5G-001",
    ) == "FAIL"

    runtime.reset_current_results(
        "VD",
        {"TC-JET-5G-001"},
    )

    assert runtime.current_session_status(
        "VD",
        "TC-JET-5G-001",
    ) == "NOT RUN"

    # Historical evidence/result remains untouched.
    assert historical.is_file()
    assert runtime.attempts(
        "VD",
        "TC-JET-5G-001",
    ) == [historical]
    assert runtime.latest_status(
        "VD",
        "TC-JET-5G-001",
    ) == "PASS"


def test_reset_clears_finished_active_attempt_projection(tmp_path):
    from pathlib import Path
    from desktop_app.wifi.catalog import load_auto_catalog
    from desktop_app.wifi.runtime import WifiAttempt

    runtime = WifiRuntime(None, evidence_root=tmp_path)

    case = next(
        case
        for case in load_auto_catalog()
        if case.test_id == "TC-JET-5G-001"
    )

    directory = (
        tmp_path
        / "VD"
        / "auto"
        / "AP_5G"
        / runtime.session
        / case.test_id
        / "attempt_001"
    )
    directory.mkdir(parents=True)

    attempt = WifiAttempt(
        environment="VD",
        case=case,
        number=1,
        directory=directory,
    )
    attempt.status = "COMPLETED"
    attempt.auto_result = "PASS"
    attempt.final_result = "PASS"

    runtime.active = attempt
    runtime._current_session_results[
        ("VD", case.test_id)
    ] = "PASS"

    # Reproduces the GUI situation after one completed test.
    assert runtime.current_session_status(
        "VD",
        case.test_id,
    ) == "PASS"

    runtime.reset_current_results(
        "VD",
        {case.test_id},
    )

    assert runtime.active is None
    assert runtime.current_session_status(
        "VD",
        case.test_id,
    ) == "NOT RUN"
