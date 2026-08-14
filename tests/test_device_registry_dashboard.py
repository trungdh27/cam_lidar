import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from desktop_app.state.device_registry import DeviceRegistry
from desktop_app.state.jetson_state import JetsonState
from desktop_app.state.lidar_runtime_state import LidarRuntimeState
from desktop_app.state.lidar_runtime_state import LidarStreamStatus
from desktop_app.services.lidar_stream_service import LidarStreamService
from desktop_app.ui.dashboard_page import DashboardPage
from desktop_app.ui.dialogs import LidarDeviceInformationDialog
from desktop_app.ui.lidar_page import LidarPage
from devices.livox.profile import load_default_livox_profile


class _FakeJetsonService(QObject):
    connecting = Signal()
    connected = Signal(object)
    disconnected = Signal()
    connection_failed = Signal(str)
    remote_process_started = Signal(str)
    remote_process_output = Signal(str, str, str)
    remote_process_finished = Signal(str, int)
    remote_process_failed = Signal(str, str)

    def __init__(self, state, connected=True):
        super().__init__()
        self.state = state
        self._connected = connected

    @property
    def is_connected(self):
        return self._connected

    def update_network_snapshot(self, snapshot):
        self.state.set_network_snapshot(snapshot)

    def start_remote_process(self, _name, _command):
        return "livox_stream:1" if self._connected else None

    def stop_remote_process(self, _request_id):
        return None


class DeviceRegistryDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = JetsonState()
        self.service = _FakeJetsonService(self.state)
        self.registry = DeviceRegistry()
        self.runtime = LidarRuntimeState()
        self.stream_service = LidarStreamService(
            self.service,
            self.runtime,
            load_default_livox_profile(),
        )

    def test_dashboard_updates_realtime_and_counts_registry_devices(self):
        dashboard = DashboardPage(
            self.state,
            self.service,
            self.registry,
        )

        self.registry.update_device(
            {
                "device": "LiDAR",
                "available": "detected",
                "model": "MID360S",
                "serial": "ARMCP4D0032262",
                "status": "ready",
                "last_update": "2026-08-14T14:00:00+00:00",
            }
        )
        self.app.processEvents()

        self.assertEqual(dashboard._device_data["LiDAR"]["model"], "MID360S")
        self.assertEqual(dashboard._device_data["LiDAR"]["status"], "Ready")
        self.assertEqual(dashboard.metric_cards["devices"].value_label.text(), "1 / 5")
        self.assertEqual(dashboard.metric_cards["online"].value_label.text(), "1")
        self.assertTrue(dashboard.device_action_buttons["LiDAR"].isEnabled())

        self.registry.update_device(
            {
                "device": "Camera",
                "available": "detected",
                "model": "ZED X One",
                "serial": "CAMERA-SN",
                "status": "ready",
            }
        )
        self.app.processEvents()

        self.assertEqual(dashboard.metric_cards["devices"].value_label.text(), "2 / 5")
        self.assertEqual(dashboard.metric_cards["online"].value_label.text(), "2")
        dashboard.close()

    def test_lidar_success_failure_and_disconnect_publish_consistent_state(self):
        page = LidarPage(
            self.state,
            self.service,
            self.registry,
            self.runtime,
            self.stream_service,
        )
        page.model_combo.setCurrentIndex(page.model_combo.findData("MID360S"))
        preflight = {
            "network": {"interfaces": []},
            "network_verification": {
                "ready": True,
                "status": "NETWORK_READY",
                "carrier": True,
                "operstate": "UP",
                "flags": ["UP", "LOWER_UP"],
                "ipv4_addresses": ["192.168.1.5/24"],
            },
            "ping": {
                "reachable": True,
                "average_rtt_ms": 0.43,
            },
        }
        found = {
            **preflight,
            "found": True,
            "status": "FOUND",
            "model": "MID360S",
            "serial": "ARMCP4D0032262",
            "lidar_ip": "192.168.1.162",
            "sdk_version": "1.3.1",
            "dev_type": 35,
        }

        page._on_livox_success(found)

        device = self.registry.device("LiDAR")
        self.assertEqual(device["available"], "detected")
        self.assertEqual(device["status"], "ready")
        self.assertEqual(device["model"], "MID360S")
        self.assertEqual(device["serial"], "ARMCP4D0032262")
        self.assertEqual(page.device_overview_data["Jetson Host IP"], "192.168.1.5")
        self.assertEqual(page.device_overview_data["Status"], "READY")

        page._on_livox_success(
            {
                **preflight,
                "found": False,
                "status": "DEVICE_NOT_FOUND",
                "reason": "timeout",
                "environment": {"sdk_version": "1.3.1"},
            }
        )
        unavailable = self.registry.device("LiDAR")
        self.assertEqual(unavailable["available"], "not detected")
        self.assertEqual(unavailable["status"], "warning")
        self.assertEqual(unavailable["model"], "MID360S")
        self.assertEqual(unavailable["serial"], "ARMCP4D0032262")

        self.registry.update_device(
            {
                "device": "LiDAR",
                "available": "detected",
                "status": "ready",
            }
        )
        page._last_jetson_connected = True
        self.service._connected = False
        page._on_jetson_state_changed(self.state)
        disconnected = self.registry.device("LiDAR")
        self.assertEqual(disconnected["available"], "not detected")
        self.assertEqual(disconnected["status"], "warning")
        self.assertEqual(disconnected["model"], "MID360S")
        page.close()

    def test_live_monitor_labels_units_and_button_states_match_metrics(self):
        page = LidarPage(
            self.state,
            self.service,
            self.registry,
            self.runtime,
            self.stream_service,
        )
        page.model_combo.setCurrentIndex(page.model_combo.findData("MID360S"))
        preflight = {
            "network": {"interfaces": []},
            "network_verification": {
                "ready": True,
                "status": "NETWORK_READY",
                "carrier": True,
                "operstate": "UP",
                "flags": ["UP", "LOWER_UP"],
                "ipv4_addresses": ["192.168.1.5/24"],
            },
            "ping": {"reachable": True, "average_rtt_ms": 0.43},
        }
        page._on_livox_success(
            {
                **preflight,
                "found": True,
                "status": "FOUND",
                "model": "MID360S",
                "serial": "ARMCP4D0032262",
                "lidar_ip": "192.168.1.162",
                "host_ip": "192.168.1.5",
                "sdk_version": "1.3.1",
                "dev_type": 35,
            }
        )
        self.assertEqual(page.current_status_label.text(), "DETECTED")

        self.runtime.update_metrics(
            {
                "state": "STREAMING",
                "cloud_rate_hz": 2081.15,
                "point_packet_rate_hz": 2081.15,
                "point_count": 199790,
                "point_count_unit": "pts/s",
                "imu_status": "ACTIVE",
                "imu_rate_hz": 199.92,
                "packet_loss_percent": 0.0,
                "packet_loss_supported": True,
                "lidar_timestamp": 4842981396010,
                "lidar_time_type": 0,
                "uptime_sec": 20.0,
            }
        )
        self.app.processEvents()

        rows = {
            page.monitor_table.item(row, 0).text(): row
            for row in range(page.monitor_table.rowCount())
        }
        self.assertNotIn("Cloud Rate", rows)
        self.assertNotIn("Point Count", rows)
        self.assertEqual(page.monitor_table.rowCount(), 5)
        packet_row = rows["Point Packet Rate"]
        point_row = rows["Point Rate"]
        imu_row = rows["IMU Rate"]
        self.assertNotIn("LiDAR Timestamp", rows)
        self.assertNotIn("Stream Uptime", rows)
        self.assertEqual(page.monitor_table.item(packet_row, 2).text(), "pkt/s")
        self.assertEqual(page.monitor_table.item(point_row, 2).text(), "pts/s")
        self.assertEqual(page.monitor_table.item(imu_row, 2).text(), "Hz")
        self.assertFalse(page.discover_button.isEnabled())
        self.assertTrue(page.stream_button.isEnabled())
        self.assertEqual(page.stream_button.text(), "■  STOP STREAM")

        self.runtime.set_status(LidarStreamStatus.IDLE)
        self.app.processEvents()
        self.assertTrue(page.discover_button.isEnabled())
        self.assertTrue(page.stream_button.isEnabled())
        self.assertEqual(page.stream_button.text(), "▶  START STREAM")
        self.assertEqual(
            page.monitor_table.item(packet_row, 1).text(),
            "2081.15",
        )
        self.assertEqual(page.monitor_table.item(packet_row, 3).text(), "IDLE")
        page.close()

    def test_lidar_page_uses_simplified_state_driven_controls(self):
        page = LidarPage(
            self.state,
            self.service,
            self.registry,
            self.runtime,
            self.stream_service,
        )
        self.assertFalse(hasattr(page, "verify_network_button"))
        self.assertFalse(hasattr(page, "start_stream_button"))
        self.assertFalse(hasattr(page, "stop_button"))
        self.assertFalse(hasattr(page, "suite_card"))
        self.assertEqual(
            page.device_information_button.text(),
            "DEVICE INFORMATION",
        )
        self.assertEqual(page.test_table.rowCount(), 10)
        self.assertFalse(page.run_test_button.isEnabled())

        page.start_stream = Mock()
        page.stop_stream = Mock()
        page.stream_button.setText("arbitrary presentation text")
        self.runtime.stream_status = LidarStreamStatus.IDLE
        page.toggle_stream()
        page.start_stream.assert_called_once_with()
        page.stop_stream.assert_not_called()

        self.runtime.stream_status = LidarStreamStatus.STREAMING
        page.toggle_stream()
        page.stop_stream.assert_called_once_with()
        page.close()

    def test_device_information_combines_all_three_data_groups(self):
        dialog = LidarDeviceInformationDialog(
            {
                "Vendor": "Livox",
                "Selected Model": "MID-360S",
                "Detected Model": "MID360S",
                "Serial": "ARMCP4D0032262",
                "SDK Version": "1.3.1",
                "Status": "READY",
            },
            {
                "interface": "enxec9a0c19f45f",
                "mac_address": "ec:9a:0c:19:f4:5f",
                "state": "UP",
                "carrier": "UP",
                "jetson_ip": "192.168.1.5/24",
                "lidar_ip": "192.168.1.162",
                "network": "192.168.1.0/24",
                "gateway": "None / Not Required",
                "ping": "0.43 ms",
                "ping_status": "PASS",
                "verification_status": "NETWORK_READY",
            },
            {
                "Discovery Port": "56000",
                "LiDAR Control Port": "56100",
                "Host Control Port": "56101",
            },
        )
        self.assertEqual(dialog.tabs.count(), 3)
        self.assertEqual(
            [dialog.tabs.tabText(index) for index in range(3)],
            ["Device", "Network", "Protocol"],
        )
        device_labels = {
            dialog.device_table.item(row, 0).text()
            for row in range(dialog.device_table.rowCount())
        }
        network_labels = {
            dialog.network_table.item(row, 0).text()
            for row in range(dialog.network_table.rowCount())
        }
        protocol_labels = {
            dialog.protocol_table.item(row, 0).text()
            for row in range(dialog.protocol_table.rowCount())
        }
        self.assertIn("Serial", device_labels)
        self.assertIn("Network Verification", network_labels)
        self.assertIn("Discovery Port", protocol_labels)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
