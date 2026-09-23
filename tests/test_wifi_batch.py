"""Drive the real runtime queue through completion signals without DUT mutations."""
import asyncio
from dataclasses import replace
import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from desktop_app.wifi.runtime import WifiRuntime
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.batch_network import BatchNetwork
from desktop_app.wifi.auto_suite import AutoSuiteBatchRunner
from tests.test_wifi_auto_suite import ready_setup, passing_measurements
from tests.test_wifi_module import SharedService, app, result


CASES = {case.test_id: case for case in load_auto_catalog()}
AP1 = CASES["TC-JET-24G-001"]
AP2 = CASES["TC-JET-24G-002"]
AP6 = CASES["TC-JET-24G-006"]
STA3 = CASES["TC-JET-STA-24G-003"]
STA5 = CASES["TC-JET-STA-24G-005"]


class NetworkHooks:
    def __init__(self):
        self.calls = []
        self.network = "original"

    async def snapshot(self, ssh):
        self.calls.append("snapshot")
        return {"network": self.network}

    async def prepare(self, ssh, state):
        self.calls.append("prepare")
        self.network = "target"

    async def restore(self, ssh, original):
        self.calls.append("restore")
        self.network = original.get("network", "original")


class SSH:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.unrecoverable = False

    async def run(self, command, **kwargs):
        self.calls.append(command)
        if self.unrecoverable or self.fail_once:
            self.fail_once = False
            raise ConnectionError("transport lost")
        return result(command, "WIFI_BATCH_CONTROL_OK" if "WIFI_BATCH_CONTROL_OK" in command else "evidence")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def connect(self):
        self.calls.append("connect")
        if self.unrecoverable:
            raise ConnectionError("transport lost")


class Harness:
    def __init__(self, tmp_path, monkeypatch, setup=None):
        app()
        monkeypatch.setattr(WifiRuntime, "refresh_local_wifi_discovery", lambda self: None)
        self.service, self.ssh, self.hooks = SharedService(), SSH(), NetworkHooks()
        self.runtime = WifiRuntime(self.service, tmp_path, auto_collector=self.collect)
        self.runtime.auto_setup = setup or ready_setup()
        self.refreshes = 0
        self.on_refresh = lambda count: None
        self.on_collect = lambda case, values: values
        self.executed, self.cleaned, self.baselines = [], [], []
        self.simultaneous = self.maximum = self.processed = 0
        monkeypatch.setattr(self.runtime, "refresh_auto_discovery", self.refresh)

    def refresh(self):
        self.refreshes += 1
        self.on_refresh(self.refreshes)
        self.runtime._discovery_request_id = "discovery"
        def done():
            self.runtime._discovery_request_id = None
            self.runtime.auto_discovery_completed.emit(True, "")
        QTimer.singleShot(0, done)
        return "discovery"

    async def collect(self, ssh, case, setup):
        self.simultaneous += 1
        self.maximum = max(self.maximum, self.simultaneous)
        self.executed.append(case.test_id)
        self.baselines.append(dict(self.runtime.active.metrics))
        try:
            await ssh.run("iw dev wlP1p1s0 info", timeout=5)
            values = self.on_collect(case, passing_measurements(case, setup))
            return {"measurements": values}
        finally:
            self.cleaned.append(case.test_id)
            self.simultaneous -= 1

    def begin(self, cases):
        return self.runtime.start_batch("VD", cases, self.hooks)

    def step(self):
        QApplication.processEvents()
        if self.processed < len(self.service.operations):
            index = self.processed
            self.processed += 1
            name, operation = self.service.operations[index]
            request = f"{name}:{index + 1}"
            try:
                capture = asyncio.run(operation(self.ssh))
            except Exception:
                self.service.operation_failed.emit(request, "safe transport error")
            else:
                self.service.operation_succeeded.emit(request, capture)
        QApplication.processEvents()

    def finish(self):
        for _ in range(100):
            self.step()
            if not self.runtime.batch.running:
                return self.runtime.batch
        raise AssertionError("Batch did not finish")

    def until_attempt(self):
        for _ in range(20):
            QApplication.processEvents()
            if self.runtime.active and self.runtime.capture_pending:
                return self.runtime.active
            self.step()
        raise AssertionError("Attempt did not start")


def test_optional_global_dependency_does_not_block_unrelated_and_blocked_has_no_attempt(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), local_iperf3_available=False))
    h.begin([AP1, AP6, AP2])
    state = h.finish()
    assert [entry.status for entry in state.entries] == ["PASS", "SKIPPED", "PASS"]
    assert state.entries[1].reason and not state.entries[1].attempt
    assert not list(tmp_path.rglob(AP6.test_id))
    assert h.executed == [AP1.test_id, AP2.test_id]
    assert h.refreshes == 3  # initial discovery and every eligible test
    assert state.counts["SKIPPED"] == 1


def test_refresh_precedes_frozen_membership_and_readiness_is_rechecked(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), local_iperf3_available=False))
    def changed(count):
        if count == 1:
            h.runtime.auto_setup.local_iperf3_available = True
        if count == 3:
            h.runtime.auto_setup.local_iperf3_available = False
    h.on_refresh = changed
    h.begin([AP1, AP6, AP2])
    state = h.finish()
    assert tuple(entry.case.test_id for entry in state.queue) == (AP1.test_id, AP6.test_id, AP2.test_id)
    assert [entry.status for entry in state.entries] == ["PASS", "SKIPPED", "PASS"]
    assert "iperf" in state.entries[1].reason.lower()
    assert state.entries[1].attempt == ""


def test_initially_blocked_never_added_after_environment_improves(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), local_iperf3_available=False))
    h.on_refresh = lambda count: setattr(h.runtime.auto_setup, "local_iperf3_available", True) if count > 1 else None
    h.begin([AP1, AP6, AP2])
    state = h.finish()
    assert len(state.queue) == 2
    assert h.executed == [AP1.test_id, AP2.test_id]


@pytest.mark.parametrize("outcome", ["FAIL", "ERROR", "MEASUREMENT ERROR", "NEEDS REVIEW"])
def test_independent_tests_continue_after_nonpass(tmp_path, monkeypatch, outcome):
    setup = replace(ready_setup(), current_mode="managed", current_ssid="EXT-24", laptop_ssid="EXT-24")
    h = Harness(tmp_path, monkeypatch, setup)
    def values(case, measurements):
        if case == STA3:
            if outcome == "FAIL":
                measurements["Band"] = "5 GHz"
            elif outcome == "ERROR":
                return {}
            elif outcome == "MEASUREMENT ERROR":
                measurements["Disconnect collector error"] = "Unreliable parser evidence"
        return measurements
    if outcome == "NEEDS REVIEW":
        from tests.test_wifi_optional_thresholds import UDP, wifi_setup
        h.runtime.auto_setup = wifi_setup()
        cases = [UDP, CASES["TC-JET-5G-006"]]
    else:
        cases = [STA3, STA5]
    h.on_collect = values
    h.begin(cases)
    state = h.finish()
    assert [entry.status for entry in state.entries] == [outcome, "PASS"]
    assert h.maximum == 1 and h.cleaned == h.executed
    assert h.hooks.calls == ["snapshot", "prepare", "restore"]
    assert state.status == "COMPLETED"  # a summary, not a fake DUT PASS/FAIL


def test_lost_shared_ssh_recovers_and_resumes_same_attempt(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.begin([AP1, AP2])
    original = h.until_attempt()
    phases = []
    h.runtime.collector_update.connect(lambda update: phases.append(update.get("phase")))
    h.ssh.fail_once = True
    state = h.finish()
    assert state.entries[0].attempt == str(original.directory)
    assert len(h.runtime.attempts("VD", AP1.test_id)) == 1
    assert [entry.status for entry in state.entries] == ["PASS", "PASS"]
    assert h.ssh.calls.count("connect") == 1
    assert not h.runtime._batch_control_lost.is_set()
    assert phases == ["RECOVERING", "MEASURING"]


def test_unrecoverable_control_aborts_remaining_without_dut_failure(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.begin([AP1, AP2])
    h.until_attempt()
    h.ssh.unrecoverable = True
    state = h.finish()
    assert state.status == "ABORTED"
    assert [entry.status for entry in state.entries] == ["ERROR", "SKIPPED"]
    assert "control" in state.entries[1].reason.lower()
    assert not h.runtime.attempts("VD", AP2.test_id)
    assert h.cleaned == [AP1.test_id]
    assert h.hooks.calls[-1] == "restore"


def test_stop_cancels_remaining_cleans_and_restores(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    def stop(case, values):
        h.runtime.stop_batch()
        return values
    h.on_collect = stop
    h.begin([AP1, AP2])
    state = h.finish()
    assert state.status == "STOPPED"
    assert [entry.status for entry in state.entries] == ["STOPPED", "SKIPPED"]
    assert state.counts["FAIL"] == 0
    assert h.cleaned == [AP1.test_id]
    assert h.hooks.network == "original"
    assert h.hooks.calls[-1] == "restore"


def test_stop_before_attempt_and_restore_after_preparation(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.begin([AP1, AP2])
    h.step()  # snapshot completed, preparation submitted
    h.runtime.stop_batch()
    state = h.finish()
    assert not h.executed
    assert all(entry.status == "SKIPPED" for entry in state.entries)
    assert h.hooks.network == "original"


def test_serial_exclusivity_and_all_measurements_reset(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    def pollute(case, values):
        h.runtime.active.update_metrics({"Disconnects": 9, "Reconnects": 8, "RTT samples": [99], "Errors": 5})
        h.runtime.active.trends["RSSI"] = [99]
        h.runtime.active.events.append({"message": "old-only"})
        return values
    h.on_collect = pollute
    h.begin([AP1, AP2])
    first = h.until_attempt()
    assert h.runtime.start("VD", AP2) is None
    assert h.runtime.start_batch("VD", [AP2]) is None
    assert h.runtime.active is first
    h.finish()
    assert h.maximum == 1
    assert h.baselines == [{"Disconnects": 0, "Reconnects": 0, "Disconnect events": []}] * 2
    assert h.hooks.network == "original"


def test_skipped_preflight_preserves_previous_completed_result(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    attempt = h.runtime.start("VD", AP6)
    h.runtime.complete_auto(passing_measurements(AP6, h.runtime.auto_setup))
    h.runtime.auto_setup.local_iperf3_available = False
    h.begin([AP6, AP2])
    state = h.finish()
    assert state.entries[0].status == "SKIPPED"
    assert h.runtime.latest_status("VD", AP6.test_id) == "PASS"
    assert len(h.runtime.attempts("VD", AP6.test_id)) == 1
    assert attempt.final_result == "PASS"


def test_run_all_ready_scope_ignores_filters_and_selected_is_explicit(tmp_path, monkeypatch):
    from desktop_app.ui.wifi_page import WifiEnvironmentPage
    h = Harness(tmp_path, monkeypatch)
    group = [case for case in CASES.values() if case.catalog_group == "AP_24G"]
    page = WifiEnvironmentPage("VD", h.runtime, group, "AUTO")
    started = []
    monkeypatch.setattr(h.runtime, "start_batch", lambda environment, cases: started.append(tuple(cases)))
    page.filters["Status"].setCurrentText("BLOCKED")
    page.selected = {AP2.test_id}
    page._run_all_ready()
    page._run_selected()
    assert started == [tuple(group), (AP2,)]
    page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_mixed_subgroup_rejected_and_batch_summary_persisted(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="subgroup"):
        h.begin([AP1, STA3])
    h.begin([AP1, AP2])
    state = h.finish()
    summary = json.loads(next(tmp_path.rglob("batch.json")).read_text())
    assert summary["membership"] == [AP1.test_id, AP2.test_id]
    assert summary["counts"]["PASS"] == 2 and summary["counts"]["REMAINING"] == 0
    assert summary["status"] == state.status == "COMPLETED"


def test_synchronous_compatibility_runner_does_not_poison_group(tmp_path):
    setup = ready_setup()
    def execute(case):
        if case == AP1:
            raise RuntimeError("collector failed")
        return passing_measurements(case, setup)
    runner = AutoSuiteBatchRunner(setup, execute)
    runner.enqueue([AP1, AP2])
    assert [entry.status for entry in runner.run()] == ["ERROR", "PASS"]


def test_production_network_snapshot_configures_once_restores_uuid_and_owned_clone(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), current_band="5 GHz"))
    network = BatchNetwork(h.runtime)
    original_uuid = "11111111-1111-1111-1111-111111111111"
    profile = original_uuid
    commands = []
    class StatefulSSH:
        async def run(self, command, **kwargs):
            nonlocal profile
            commands.append(command)
            if "connection up" in command:
                profile = original_uuid if " uuid " in command else "batch-profile"
            return result(command, profile if "GENERAL.CON-UUID" in command else "")
    async def local(command, timeout):
        return original_uuid, "", 0
    import desktop_app.wifi.auto_collectors as collectors
    monkeypatch.setattr(collectors, "_local", local)
    async def run():
        ssh = StatefulSSH()
        original = await network.snapshot(ssh)
        entry = SimpleNamespace(case=AP1, status="WAITING")
        await network.prepare(ssh, SimpleNamespace(queue=(entry,)))
        assert profile == "batch-profile"
        assert h.runtime.effective_auto_setup().ap_profile == network.clone
        await network.restore(ssh, original)
    asyncio.run(run())
    assert profile == original_uuid
    assert sum("connection clone" in command for command in commands) == 1
    assert sum("connection delete" in command for command in commands) == 1
    assert not any("killall" in command or "pkill" in command for command in commands)


def test_stop_during_real_collector_measurement_waits_for_owned_cleanup(tmp_path, monkeypatch):
    import desktop_app.wifi.auto_collectors as collectors
    h = Harness(tmp_path, monkeypatch)
    h.runtime.auto_collector = collectors.collect_auto_case
    commands = []
    async def local(command, timeout, on_output=None):
        if command.startswith("iw dev"):
            return "Connected to aa:bb:cc:dd:ee:ff\n SSID: JET-24", "", 0
        if command.startswith("ip route"):
            return "192.168.2.22 dev wlan0 src 192.168.2.30", "", 0
        if command.startswith("iperf3"):
            asyncio.get_running_loop().call_soon(h.runtime.stop_batch)
            await asyncio.Event().wait()
        return "", "", 0
    class Remote(SSH):
        async def run(self, command, **kwargs):
            commands.append(command)
            if "SERVER_STATE=" in command:
                return result(command, "SERVER_STATE=STARTED_BY_APP\nSERVER_OWNED=YES\nSERVER_PID=123\nSERVER_START=456\nSERVER_DIR=/tmp/cam_lidar_iperf.aBc123\n")
            if command.startswith("ip route get"):
                return result(command, "192.168.2.30 dev wlP1p1s0")
            if command.endswith(" info"):
                return result(command, "Interface wlP1p1s0\n type AP\n ssid JET-24\n channel 6 (2437 MHz)")
            return result(command)
    h.ssh = Remote()
    monkeypatch.setattr(collectors, "_local", local)
    h.begin([AP6, AP2])
    state = h.finish()
    assert [entry.status for entry in state.entries] == ["STOPPED", "SKIPPED"]
    assert any("then kill 123" in command for command in commands)
    assert h.hooks.network == "original"
    assert not any("killall" in command or "pkill" in command for command in commands)


def test_target_preparation_failure_still_restores_and_creates_no_attempt(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    async def failure(ssh, state):
        h.hooks.network = "partial-target"
        raise RuntimeError("configuration rejected")
    h.hooks.prepare = failure
    h.begin([AP1, AP2])
    state = h.finish()
    assert state.status == "ABORTED"
    assert h.hooks.network == "original"
    assert not list(tmp_path.rglob("attempt_*"))
    assert all(entry.status == "SKIPPED" for entry in state.entries)


def test_restore_failure_is_batch_error_not_dut_fail(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    async def failure(ssh, original):
        raise RuntimeError("restore verification failed")
    h.hooks.restore = failure
    h.begin([AP1, AP2])
    state = h.finish()
    assert state.status == "ERROR" and state.phase == "COMPLETED"
    assert [entry.status for entry in state.entries] == ["PASS", "PASS"]
    assert state.counts["FAIL"] == 0 and "restoration" in state.reason


def test_alternate_control_retained_until_original_network_restoration(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), ssh_transport="Wi-Fi",
                control_interface="wlP1p1s0", backup_ethernet_ip="192.168.9.22"))
    network = BatchNetwork(h.runtime)
    calls = []
    class Remote:
        config = SimpleNamespace(host="192.168.2.22")
        connected = True
        async def disconnect(self):
            calls.append(("disconnect", self.config.host))
            self.connected = False
        async def connect(self):
            calls.append(("connect", self.config.host))
            self.connected = True
        async def run(self, command, **kwargs):
            calls.append((command, self.config.host))
            return result(command, "original-uuid")
    async def local(command, timeout):
        return "original-uuid", "", 0
    import desktop_app.wifi.auto_collectors as collectors
    monkeypatch.setattr(collectors, "_local", local)
    async def run():
        ssh = Remote()
        original = await network.snapshot(ssh)
        await network.prepare(ssh, SimpleNamespace(queue=(SimpleNamespace(case=CASES["TC-JET-24G-012"], status="WAITING"),)))
        assert ssh.config.host == "192.168.9.22"
        await network.restore(ssh, original)
        assert ssh.config.host == "192.168.2.22"
    asyncio.run(run())
    connects = [host for command, host in calls if command == "connect"]
    assert connects == ["192.168.9.22", "192.168.2.22"]
    restore_queries = [(command, host) for command, host in calls if "GENERAL.CON-UUID" in command]
    assert restore_queries[-1][1] == "192.168.9.22"


def test_disruptive_without_alternate_or_registered_recovery_is_skipped(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), ssh_transport="Wi-Fi",
                control_interface="wlP1p1s0", backup_ethernet_ready=False))
    case = CASES["TC-JET-24G-012"]
    h.begin([case, AP2])
    state = h.finish()
    assert state.entries[0].status == "SKIPPED"
    assert state.entries[0].reason and not state.entries[0].attempt
    assert h.executed == [AP2.test_id]


def test_runtime_guard_stops_measurement_if_required_band_changes(tmp_path, monkeypatch):
    import desktop_app.wifi.auto_collectors as collectors
    h = Harness(tmp_path, monkeypatch)
    h.runtime.auto_collector = collectors.collect_auto_case
    commands = []
    class Remote(SSH):
        async def run(self, command, **kwargs):
            commands.append(command)
            return result(command, "Interface wlP1p1s0\n type AP\n channel 36 (5180 MHz)")
    h.ssh = Remote()
    h.begin([AP6])
    state = h.finish()
    assert state.entries[0].status == "ERROR"
    assert "immediately before measurement" in state.entries[0].reason
    assert not any("SERVER_STATE=" in command for command in commands)


def test_batch_ui_counters_timer_and_terminal_summary_do_not_rebuild_criteria(tmp_path, monkeypatch):
    from desktop_app.ui.wifi_page import WifiEnvironmentPage
    h = Harness(tmp_path, monkeypatch)
    page = WifiEnvironmentPage("VD", h.runtime, [AP1, AP2], "AUTO")
    page.show()
    h.begin([AP1, AP2])
    h.until_attempt()
    QApplication.processEvents()
    initial_builds = page._criteria_build_count
    assert "1 / 2" in page.batch_text.text()
    assert "REMAINING: 1" in page.batch_text.text()
    for _ in range(3):
        page._tick_elapsed()
    assert page._criteria_build_count == initial_builds
    h.finish()
    assert "AUTO batch COMPLETED" in page.batch_text.text()
    assert "PASS: 2" in page.batch_text.text() and "REMAINING: 0" in page.batch_text.text()
    assert page.batch_stop.isHidden()
    page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_recovery_never_replays_uncertain_mutating_command(tmp_path, monkeypatch):
    from desktop_app.wifi.batch_network import RecoveringSSH
    h = Harness(tmp_path, monkeypatch)
    h.ssh.fail_once = True
    proxy = RecoveringSSH(h.ssh, h.runtime, "test-attempt")
    command = "ip route add default via 192.168.2.1"
    with pytest.raises(RuntimeError, match="not replayed"):
        asyncio.run(proxy.run(command, timeout=5))
    assert h.ssh.calls.count(command) == 1
    assert not h.runtime._batch_control_lost.is_set()


def test_uncertain_command_response_is_infrastructure_error_and_next_attempt_resets(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    async def collector(ssh, case, setup):
        values = passing_measurements(case, setup)
        if case == AP1:
            h.ssh.fail_once = True
            try:
                await ssh.run("sudo -n nmcli connection up owned-profile", timeout=5)
            except RuntimeError:
                values["Band"] = "5 GHz"  # unreliable outcome must not become DUT FAIL
        return {"measurements": values}
    h.runtime.auto_collector = collector
    h.begin([AP1, AP2])
    state = h.finish()
    assert [entry.status for entry in state.entries] == ["ERROR", "PASS"]
    assert "outcome uncertain" in state.entries[0].reason
    assert not h.runtime._batch_execution_error


def test_per_test_preflight_error_does_not_block_other_cases(tmp_path, monkeypatch):
    import desktop_app.wifi.batch as batch
    h = Harness(tmp_path, monkeypatch)
    normal = batch.readiness_status
    def preflight(case, setup):
        if case == AP1:
            raise ValueError("invalid prerequisite data")
        return normal(case, setup)
    monkeypatch.setattr(batch, "readiness_status", preflight)
    h.begin([AP1, AP2])
    state = h.finish()
    assert [entry.status for entry in state.entries] == ["SKIPPED", "PASS"]
    assert state.entries[0].readiness == "ERROR"
    assert not state.entries[0].attempt
    assert "ValueError" in state.entries[0].reason


def test_optional_snapshot_probe_failure_only_blocks_dependent_disruptive_cases(tmp_path, monkeypatch):
    import desktop_app.wifi.auto_collectors as collectors
    h = Harness(tmp_path, monkeypatch)
    network = BatchNetwork(h.runtime)
    commands = []
    class Remote(SSH):
        async def run(self, command, **kwargs):
            commands.append(command)
            if "GENERAL.CON-UUID" in command:
                return result(command, "", 1)
            return result(command, "")
    async def local(command, timeout):
        return "", "", 1
    monkeypatch.setattr(collectors, "_local", local)
    h.ssh = Remote()
    h.hooks = network
    disruptive = CASES["TC-JET-24G-012"]
    h.begin([AP1, disruptive, AP2])
    state = h.finish()
    assert [entry.status for entry in state.entries] == ["PASS", "SKIPPED", "PASS"]
    assert "capture original" in state.entries[1].reason
    assert not state.entries[1].attempt
    assert state.status == "COMPLETED"
    assert not any("connection up" in command or "connection delete" in command for command in commands)


@pytest.mark.parametrize("label", ["rf_samples", "iperf_cleanup"])
def test_known_read_only_loop_and_owned_cleanup_can_resume_after_recovery(tmp_path, monkeypatch, label):
    import desktop_app.wifi.auto_collectors as collectors
    from desktop_app.wifi.batch_network import RecoveringSSH
    h = Harness(tmp_path, monkeypatch, replace(ready_setup(), current_mode="managed",
                       current_ssid="EXT-24", laptop_ssid="EXT-24"))
    if label == "rf_samples":
        command = next(spec.command for spec in collectors.command_plan(STA3, h.runtime.auto_setup)
                       if spec.label == label)
    else:
        command = collectors.iperf_cleanup_command("SERVER_STATE=STARTED_BY_APP\nSERVER_OWNED=YES\nSERVER_PID=123\nSERVER_START=456\nSERVER_DIR=/tmp/cam_lidar_iperf.aBc123\n")
    assert command
    h.ssh.fail_once = True
    proxy = RecoveringSSH(h.ssh, h.runtime, "same-attempt")
    result = asyncio.run(proxy.run(command, timeout=5, _batch_retry_safe=True))
    assert result.exit_status == 0 and h.ssh.calls.count(command) == 2
    assert not h.runtime._batch_control_lost.is_set() and not h.runtime._batch_execution_error
