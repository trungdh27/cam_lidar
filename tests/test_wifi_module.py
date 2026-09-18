"""VD workbook, isolated environments, navigation and safe Wi-Fi run shell."""
import asyncio
import json
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from desktop_app.ui.wifi_page import WifiPage, WifiMonitor, redact_secrets
from desktop_app.wifi.catalog import (EXCLUDED_IDS, SOURCE, load_catalog, load_config,
                                      load_execution_order, source_rows)
from desktop_app.wifi.runtime import WifiRuntime, metric_kind


class SharedService(QObject):
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, str)

    def __init__(self, connected=True):
        super().__init__()
        self.is_connected = connected
        self.operations = []

    def submit_operation(self, name, operation):
        self.operations.append((name, operation))
        return f"{name}:{len(self.operations)}"


def app():
    return QApplication.instance() or QApplication([])


def result(command, stdout="", exit_status=0):
    return SimpleNamespace(command=command, stdout=stdout, stderr="", exit_status=exit_status)


def test_authoritative_workbook_catalog_config_and_order():
    assert SOURCE.is_file()
    rows = source_rows()
    assert len(rows) == 27
    ids = [row["ID TC"] for row in rows]
    assert ids == [f"TC-WIFI-C{index:02d}" for index in range(1, 28)]
    assert EXCLUDED_IDS == {"TC-WIFI-C08"}
    vd = load_catalog("VD")
    assert len(vd) == 26
    assert [case.test_id for case in vd] == [test_id for test_id in ids if test_id not in EXCLUDED_IDS]
    assert all(case.category == case.source_fields["Group"] for case in vd)
    assert all(case.procedure == case.source_fields["Operation / Procedure"] for case in vd)
    assert load_catalog("VMO") == [] and load_catalog("VR") == []
    config = load_config()
    assert config["Robot hostname"] == "RD03-1425025055966"
    assert config["Wi-Fi interface"] == "wlP1p1s0"
    assert config["Hotspot profile"] == "Hotspot"
    assert config["Throughput @10m"] == ">= 5 Mbps"
    assert config["Robot SSID"] == "NOT CONFIGURED"
    assert config["Throughput Threshold"] == "NOT CONFIGURED"
    order = load_execution_order()
    assert len(order) == 7
    assert vd[0].phase == order[0]["Phase"]
    assert vd[-1].phase == order[-1]["Phase"]


def test_ui_navigation_empty_states_and_monitor_templates(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    page.resize(1050, 500)
    page.show()
    QApplication.processEvents()
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == ["PRE-TEST", "VD", "VMO", "VR"]
    vd = page.environments["VD"]
    assert vd.table.rowCount() == 26
    assert "TC-WIFI-C08" not in vd.by_id
    assert page.environments["VMO"].empty.text() == "No VMO Wi-Fi test catalog loaded."
    assert page.environments["VR"].empty.text() == "No VR Wi-Fi test catalog loaded."
    assert vd.table.horizontalHeaderItem(2).text() == "Group"
    vd.filters["Status"].setCurrentText("NOT RUN")
    vd.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    selected = vd.table.item(0, 1).text()
    vd.table.verticalScrollBar().setValue(vd.table.verticalScrollBar().maximum())
    scroll = vd.table.verticalScrollBar().value()
    vd._cell_clicked(0, 1)
    assert vd.stack.currentWidget() is vd.detail_view
    assert vd.detail_id.text() == selected
    vd.back_to_list()
    QApplication.processEvents()
    assert vd.filters["Status"].currentText() == "NOT RUN"
    assert selected in vd.selected
    assert vd.table.verticalScrollBar().value() == scroll
    page.tabs.setCurrentIndex(2)
    page.tabs.setCurrentIndex(1)
    QApplication.processEvents()
    assert vd.table.verticalScrollBar().value() == scroll
    assert not page.environments["VMO"].selected
    monitor = WifiMonitor()
    expected = {10: "latency", 11: "rf", 12: "throughput", 13: "recovery",
                15: "boot", 16: "endurance", 23: "band_throughput", 24: "wifi6",
                26: "latency", 27: "security_matrix"}
    for number, kind in expected.items():
        case = vd.by_id[f"TC-WIFI-C{number:02d}"]
        assert metric_kind(case) == kind
        attempt = SimpleNamespace(case=case, metrics={"RSSI": -64.0}, trends={}, elapsed_seconds=0)
        monitor.render(attempt)
        assert monitor.current_kind == kind
        assert all(len(chart.points) <= 120 for chart in monitor.charts.values())
    monitor.close()
    page.close()


def test_running_navigation_evidence_and_environment_history(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    vd = page.environments["VD"]
    case = vd.by_id["TC-WIFI-C23"]
    vd._start_case(case)
    attempt = page.runtime.active
    assert attempt.status == "RUNNING"
    assert vd.stack.currentWidget() is vd.execution_view
    assert [vd.layers.tabText(i) for i in range(3)] == ["SUMMARY", "RAW LOG", "EVIDENCE"]
    page.runtime.add_metrics({"RSSI": -64.0, "2.4 GHz Average": 8.7})
    assert vd.monitor.cards["2.4 GHz Average"].value.text() == "8.7 Mbps"
    assert vd.monitor.rssi_bars["RSSI"][0].isVisibleTo(vd.monitor)
    assert vd.monitor.matrix.rowCount() == 2
    assert page.runtime.start("VD", vd.by_id["TC-WIFI-C24"]) is None
    vd.back_to_list()
    assert page.runtime.active is attempt
    assert attempt.status == "RUNNING"
    assert vd.mini.isVisibleTo(vd.list_view) or not page.isVisible()
    vd.show_execution()
    assert page.runtime.active is attempt
    assert vd.stack.currentWidget() is vd.execution_view
    vd._review("NEEDS REVIEW")
    assert json.loads((attempt.directory / "result.json").read_text())["status"] == "NEEDS REVIEW"
    assert (attempt.directory / "raw").is_dir()
    assert (attempt.directory / "artifacts").is_dir()
    vd.show_history()
    assert vd.history_table.rowCount() == 1
    assert page.environments["VMO"].history_table.rowCount() == 0
    vd.back_to_list()
    page.close()


def test_raw_secret_display_is_redacted_without_changing_evidence(tmp_path):
    app()
    page = WifiPage(SharedService(), evidence_root=tmp_path)
    vd = page.environments["VD"]
    vd._start_case(vd.by_id["TC-WIFI-C27"])
    raw = "psk: private-value\nsecurity: WPA3\n"
    page.runtime.append_raw(raw)
    vd.layers.setCurrentIndex(1)
    vd._render_execution()
    assert "private-value" not in vd.raw_view.toPlainText()
    assert "[REDACTED]" in vd.raw_view.toPlainText()
    assert raw in (page.runtime.active.directory / "raw" / "commands.log").read_text()
    assert "[REDACTED]" in redact_secrets(raw)
    page.close()


def test_safe_auto_uses_shared_connection_and_raw_log(tmp_path):
    app()
    service = SharedService()
    runtime = WifiRuntime(service, evidence_root=tmp_path)
    case = load_catalog("VD")[0]
    attempt = runtime.start("VD", case)
    assert len(service.operations) == 1
    assert runtime.capture_pending
    class FakeSSH:
        async def run(self, command, timeout):
            assert timeout <= 20
            return result(command, "wlP1p1s0:wifi:connected\n")
    results = asyncio.run(service.operations[0][1](FakeSSH()))
    assert len(results) == 7
    assert all(result.command.startswith(("nmcli", "ip", "iw", "readlink", "ethtool", "journalctl")) for result in results)
    service.operation_succeeded.emit("wifi-inspect:1", results)
    assert not runtime.capture_pending
    assert "nmcli" in (attempt.directory / "raw" / "commands.log").read_text()
    assert attempt.status == "COMPLETED"
    assert attempt.auto_result == "NEEDS REVIEW"
    assert runtime.latest_status("VD", case.test_id) == "NEEDS REVIEW"
    assert runtime.latest_status("VMO", case.test_id) == "NOT RUN"


def test_pretest_configured_read_only_checks(tmp_path):
    app()
    service = SharedService()
    page = WifiPage(service, evidence_root=tmp_path)
    page.pretest.run_all()
    assert len(service.operations) == 1
    class FakeSSH:
        async def run(self, command, timeout):
            if command.startswith("nmcli -t"):
                return result(command, "wlP1p1s0:wifi:connected")
            if command.startswith("systemctl"):
                return result(command, "active")
            if command.startswith("nmcli -g"):
                return result(command, "Hotspot")
            if command.startswith("iw list"):
                return result(command, "Supported interface modes:\n * AP")
            if command.startswith("ip -brief"):
                return result(command, "eno1 UP")
            return result(command, "/usr/bin/tool")
    results = asyncio.run(service.operations[0][1](FakeSSH()))
    service.operation_succeeded.emit("wifi-pretest:1", results)
    assert page.pretest.table.item(page.pretest.rows["Wi-Fi interface"], 2).text() == "PASS"
    assert page.pretest.table.item(page.pretest.rows["Wi-Fi capability"], 2).text() == "PASS"
    assert page.pretest.table.item(page.pretest.rows["Backup Ethernet"], 2).text() == "PASS"
    page.close()
