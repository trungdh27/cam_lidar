"""Focused C01 collection, parsing, evaluation, persistence and Qt execution checks."""
import json
import asyncio

import pytest
from PySide6.QtWidgets import QApplication

from desktop_app.ui.wifi_page import WifiPage
from desktop_app.wifi.catalog import load_catalog
from desktop_app.wifi.evaluation import CriterionSpec, coverage_report, evaluate, specs_for
from desktop_app.wifi.parsers import (parse_nmcli_device_status, parse_nmcli_device_show,
                                      parse_driver, parse_kernel_device_errors)
from desktop_app.wifi.runtime import WifiRuntime
from tests.test_wifi_module import SharedService, app, result


INTERFACE = "wlP1p1s0"
CASE = load_catalog("VD")[0]


@pytest.mark.parametrize("state", ("connected", "disconnected", "connecting", "unavailable"))
def test_nmcli_terse_status_maps_all_visible_fields(state):
    parsed = parse_nmcli_device_status(f"other:ethernet:connected:\n{INTERFACE}:wifi:{state}:My AP\n", INTERFACE)
    assert parsed["Interface"] == INTERFACE
    assert parsed["Device Type"] == "wifi"
    assert parsed["NetworkManager"] == state
    assert parse_nmcli_device_status("other:ethernet:connected:\n", INTERFACE) == {}
    assert parse_nmcli_device_show(f"GENERAL.DEVICE: {INTERFACE}\nGENERAL.TYPE: wifi\nGENERAL.STATE: 100 ({state})", INTERFACE)["NetworkManager"] == state


def c01_results(state="connected", driver="rtl88x2ce", kernel="", status_exit=0,
                ip_exit=0, sysfs_exit=0, ethtool_exit=0, kernel_exit=0):
    return [
        result("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status",
               f"{INTERFACE}:wifi:{state}:AP\n" if status_exit == 0 else "", status_exit),
        result(f"nmcli -f GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION device show {INTERFACE}",
               f"GENERAL.DEVICE: {INTERFACE}\nGENERAL.TYPE: wifi\nGENERAL.STATE: {state}\n" if status_exit == 0 else "", status_exit),
        result(f"ip -br link show {INTERFACE}", f"{INTERFACE} UP\n" if ip_exit == 0 else "", ip_exit),
        result(f"iw dev {INTERFACE} info", f"Interface {INTERFACE}\n"),
        result(f"readlink -f /sys/class/net/{INTERFACE}/device/driver",
               f"/sys/bus/pci/drivers/{driver}\n" if driver and sysfs_exit == 0 else "", sysfs_exit),
        result(f"ethtool -i {INTERFACE}", f"driver: {driver}\n" if driver and ethtool_exit == 0 else "", ethtool_exit),
        result("journalctl -k -b --no-pager", kernel, kernel_exit),
    ]


def run_c01(tmp_path, **kwargs):
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    attempt = runtime.start("VD", CASE)
    service.operation_succeeded.emit("wifi-inspect:1", c01_results(**kwargs))
    return runtime, attempt


def test_c01_all_pass_persists_structured_result(tmp_path):
    runtime, attempt = run_c01(tmp_path)
    assert attempt.status == "COMPLETED"
    assert (attempt.auto_result, attempt.final_result) == ("PASS", "PASS")
    assert [row["status"] for row in attempt.criteria] == ["PASS"] * 5 + ["UNKNOWN"]
    assert attempt.metrics["NetworkManager"] == "connected"
    assert attempt.metrics["Device Type"] == "wifi"
    assert attempt.metrics["Kernel Device Errors"] == 0
    assert len(attempt.evidence) == 7
    saved = json.loads((attempt.directory / "result.json").read_text())
    assert saved["execution_state"] == "COMPLETED"
    assert saved["criteria_summary"]["pass"] == 5
    assert saved["auto_result"] == saved["final_result"] == runtime.latest_status("VD", CASE.test_id) == "PASS"
    assert saved["criteria"][2]["evidence_reference"]


def test_c01_required_fail_and_unknown(tmp_path):
    _, failed = run_c01(tmp_path / "fail", state="unavailable",
                         kernel=f"{INTERFACE}: Device not found\n")
    assert failed.auto_result == "FAIL"
    assert failed.criteria[2]["status"] == failed.criteria[3]["status"] == "FAIL"
    assert "C01-03" in failed.result_reason
    _, missing = run_c01(tmp_path / "missing", status_exit=1, ip_exit=1,
                         driver=None, sysfs_exit=1, ethtool_exit=1, kernel_exit=1)
    assert missing.auto_result == "FAIL"
    assert missing.criteria[0]["status"] == "FAIL"
    _, unknown = run_c01(tmp_path / "unknown", driver=None, sysfs_exit=1,
                         ethtool_exit=1, kernel_exit=1)
    assert unknown.auto_result == "NEEDS REVIEW"
    assert unknown.criteria[3]["status"] == "NOT_COLLECTED"
    assert unknown.criteria[4]["status"] == "NOT_COLLECTED"
    _, fallback = run_c01(tmp_path / "fallback", sysfs_exit=1)
    assert fallback.auto_result == "PASS"
    assert fallback.metrics["Driver/PHY"] == "rtl88x2ce"
    assert fallback.evidence["capture_05.log"]["status"] == "UNAVAILABLE"


def test_capture_error_is_separate_from_auto_result(tmp_path):
    runtime = WifiRuntime(SharedService(connected=False), evidence_root=tmp_path)
    attempt = runtime.start("VD", CASE)
    assert attempt.status == "ERROR"
    assert attempt.auto_result == attempt.final_result == "NEEDS REVIEW"
    saved = json.loads((attempt.directory / "result.json").read_text())
    assert saved["execution_state"] == "ERROR"
    assert saved["auto_result"] == "NEEDS REVIEW"


def test_c01_optional_command_exception_keeps_other_evidence(tmp_path):
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    attempt = runtime.start("VD", CASE)
    outputs = c01_results()
    class FakeSSH:
        async def run(self, command, timeout):
            if command.startswith("ethtool"):
                raise TimeoutError("tool unavailable")
            return next(item for item in outputs if item.command == command)
    captured = asyncio.run(service.operations[0][1](FakeSSH()))
    assert len(captured) == 7 and captured[5].exit_status == -1
    service.operation_succeeded.emit("wifi-inspect:1", captured)
    assert attempt.auto_result == "PASS"
    assert attempt.evidence["capture_06.log"]["status"] == "UNAVAILABLE"


def test_driver_fallback_and_kernel_parser():
    assert parse_driver("/sys/bus/pci/drivers/rtl88x2ce\n", "readlink -f x") == {"Driver/PHY": "rtl88x2ce"}
    assert parse_driver("driver: rtl88x2ce\nfirmware-version: unknown\n", "ethtool -i x") == {"Driver/PHY": "rtl88x2ce"}
    assert parse_driver("", "readlink -f x") == {}
    assert parse_kernel_device_errors(f"{INTERFACE}: Device not found\nother: Device not found\n", INTERFACE) == {"Kernel Device Errors": 1}


def test_evaluator_required_outcomes_and_coverage():
    metrics = {"Interface": INTERFACE, "Device Type": "wifi", "NetworkManager": "connected",
               "Kernel Device Errors": 0, "Driver/PHY": "rtl88x2ce"}
    assert evaluate(CASE, metrics, {}, INTERFACE)[1] == "PASS"
    assert evaluate(CASE, {**metrics, "NetworkManager": "unavailable"}, {}, INTERFACE)[1] == "FAIL"
    assert evaluate(CASE, {key: val for key, val in metrics.items() if key != "Driver/PHY"}, {}, INTERFACE)[1] == "NEEDS REVIEW"
    report = coverage_report(load_catalog("VD"))
    assert len(report) == 26
    assert report[0]["evaluation_coverage"] == "full"
    assert all(item["evaluator_implemented"] and item["unsupported"] == 0 for item in report)
    assert all(spec.collector and spec.parser and spec.check for case in load_catalog("VD")
               for spec in specs_for(case) if spec.check != "manual")


def test_coverage_flags_missing_parser_or_collector(monkeypatch):
    import desktop_app.wifi.evaluation as evaluation
    monkeypatch.setattr(evaluation, "specs_for", lambda _case: [
        CriterionSpec("broken-1", "No parser", "source", "value", check="present", collector="import", parser="does_not_exist"),
        CriterionSpec("broken-2", "No collector", "source", "value", check="present", parser="parse_ping"),
    ])
    report = evaluation.coverage_report([CASE])[0]
    assert report["unsupported"] == 2
    assert not report["evaluator_implemented"]


def test_override_is_separate_from_auto_result(tmp_path):
    runtime, attempt = run_c01(tmp_path)
    with pytest.raises(ValueError):
        runtime.finish("FAIL", "")
    runtime.finish("FAIL", "Tester observed intermittent link", override_by="operator")
    saved = json.loads((attempt.directory / "result.json").read_text())
    assert saved["auto_result"] == "PASS" and saved["final_result"] == "FAIL"
    assert saved["override_by"] == "operator"
    assert saved["override_reason"] and saved["override_timestamp"]


def test_c23_three_runs_per_band_and_manual_distance(tmp_path):
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    case = next(item for item in load_catalog("VD") if item.test_id == "TC-WIFI-C23")
    attempt = runtime.start("VD", case)
    def report(mbps):
        return json.dumps({"end": {"sum_received": {"bits_per_second": mbps * 1_000_000}}})
    for band in ("2.4 GHz", "5 GHz"):
        for rate in (6, 7, 8):
            runtime.ingest_output(report(rate), band=band)
    assert attempt.metrics["2.4 GHz Average"] == 7
    assert attempt.metrics["5 GHz Average"] == 7
    runtime.finish("NEEDS REVIEW", "Distance and connection stability await confirmation")
    assert attempt.auto_result == "NEEDS REVIEW"
    assert attempt.criteria[1]["status"] == attempt.criteria[2]["status"] == "PASS"
    assert attempt.criteria[0]["status"] == "MANUAL_REQUIRED"


def test_c03_safe_capture_is_fully_evaluated(tmp_path):
    case = next(item for item in load_catalog("VD") if item.test_id == "TC-WIFI-C03")
    assert case.mode == "AUTO"
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    attempt = runtime.start("VD", case)
    service.operation_succeeded.emit("wifi-inspect:1", [
        result("systemctl is-active NetworkManager", "active\n"),
        result("nmcli connection show", "NAME UUID TYPE DEVICE\nHotspot abc wifi wlP1p1s0\n"),
        result("iw dev", "phy#0\n\tInterface wlP1p1s0\n\t\ttype managed\n"),
    ])
    assert attempt.status == "COMPLETED" and attempt.auto_result == "PASS"
    assert [row["status"] for row in attempt.criteria] == ["PASS"] * 4


def test_c02_and_c04_check_captured_source_requirements(tmp_path):
    for number, command, output, expected in (
        (2, "iw list", "Supported interface modes:\n * managed\n * AP\nBand 1:\n * 2412 MHz\nBand 2:\n * 5180 MHz\n", 2),
        (4, "nmcli connection show Hotspot", "802-11-wireless.mode: ap\n802-11-wireless.ssid: RD3.02\nipv4.method: shared\n", 2),
    ):
        case = next(item for item in load_catalog("VD") if item.test_id == f"TC-WIFI-C{number:02d}")
        service = SharedService()
        runtime = WifiRuntime(service, evidence_root=tmp_path / str(number))
        attempt = runtime.start("VD", case)
        service.operation_succeeded.emit("wifi-inspect:1", [result(command, output)])
        assert attempt.status == "RUNNING" and attempt.auto_result == "NOT EVALUATED"
        runtime.finish("NEEDS REVIEW", "Remaining conditions need production design confirmation")
        assert attempt.auto_result == "NEEDS REVIEW"
        assert sum(row["status"] == "PASS" for row in attempt.criteria) == expected


def test_c26_uses_average_rtt_without_invented_max_threshold(tmp_path):
    case = next(item for item in load_catalog("VD") if item.test_id == "TC-WIFI-C26")
    runtime = WifiRuntime(SharedService(), evidence_root=tmp_path)
    attempt = runtime.start("VD", case)
    runtime.ingest_output("100 packets transmitted, 100 received, 0% packet loss\n"
                          "rtt min/avg/max/mdev = 10.0/80.0/200.0/5.0 ms\n")
    runtime.finish("NEEDS REVIEW", "Check timeout pattern and project loss threshold")
    assert attempt.auto_result == "NEEDS REVIEW"
    assert attempt.criteria[0]["status"] == attempt.criteria[1]["status"] == "PASS"
    assert attempt.criteria[2]["status"] == "MANUAL_REQUIRED"


def test_c01_qt_log_layout_metrics_search_and_reopen(tmp_path):
    app()
    service = SharedService()
    page = WifiPage(service, evidence_root=tmp_path)
    page.resize(1050, 600)
    page.show()
    page.tabs.setCurrentIndex(1)
    vd = page.environments["VD"]
    vd._start_case(CASE)
    attempt = page.runtime.active
    page.runtime.append_raw("\n".join(f"line {n}" for n in range(500)) + "\n")
    service.operation_succeeded.emit("wifi-inspect:1", c01_results())
    QApplication.processEvents()
    assert vd.monitor.cards["NetworkManager"].value.text() == "connected"
    assert vd.monitor.cards["Driver/PHY"].value.text() == "rtl88x2ce"
    assert vd.monitor.cards["Kernel Device Errors"].value.text() == "0"
    assert vd.criteria_table.rowCount() == 5
    assert "PASS" in vd.auto_result_label.text()
    vd.layers.setCurrentIndex(1)
    vd.log_modes.setCurrentIndex(1)
    QApplication.processEvents()
    assert "line 0" in vd.raw_view.toPlainText() and "line 499" in vd.raw_view.toPlainText()
    assert vd.raw_view.verticalScrollBar().maximum() > 0
    vd.find_log.setText("line 250")
    vd._find_log(False)
    assert vd.raw_view.textCursor().selectedText() == "line 250"
    vd._toggle_log_maximize()
    assert not vd.splitter.widget(0).isVisible()
    vd._toggle_log_maximize()
    assert vd.splitter.widget(0).isVisible()
    assert "line 0" in vd.raw_view.toPlainText()
    assert vd.splitter.widget(0).sizeHint().height() < 1000
    vd.back_to_list()
    assert page.runtime.latest_status("VD", CASE.test_id) == "PASS"
    vd.show_detail(CASE.test_id)
    vd._open_attempt(attempt.directory / "result.json")
    assert vd.criteria_table.rowCount() == 5
    assert vd._view_attempt.criteria == attempt.criteria
    assert vd._view_attempt.evidence == attempt.evidence
    page.close()


def test_long_log_is_file_backed_and_searches_older_content(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    vd = page.environments["VD"]
    vd._start_case(CASE)
    attempt = page.runtime.active
    page.runtime.append_raw("EARLY UNIQUE MARKER\n" + "x" * 280000 + "\nLATE MARKER\n")
    vd.layers.setCurrentIndex(1)
    vd.log_modes.setCurrentIndex(1)
    vd._render_execution()
    assert "LATE MARKER" in vd.raw_view.toPlainText()
    assert "EARLY UNIQUE MARKER" not in vd.raw_view.toPlainText()
    vd.find_log.setText("EARLY UNIQUE MARKER")
    vd._find_log(False)
    assert "EARLY UNIQUE MARKER" in vd.raw_view.toPlainText()
    assert "EARLY UNIQUE MARKER" in (attempt.directory / "raw" / "commands.log").read_text()
    page.close()
