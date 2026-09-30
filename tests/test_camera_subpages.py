import os
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QSizePolicy

from desktop_app.services.camera_inventory_service import CameraInventoryService
from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.camera_page import CameraPage
from devices.camera.discovery import normalize_realsense_device, normalize_zed_device
from devices.camera.discovery import normalize_ros_camera_device
from devices.camera.inventory import CameraInventorySnapshot
from devices.camera.ros_registry import RosCameraDriverRegistry


class CameraSubpageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = JetsonState()
        self.service = JetsonConnectionService(self.state)
        self.inventory_service = CameraInventoryService(self.service)
        self.page = CameraPage(
            self.state,
            self.service,
            camera_inventory_service=self.inventory_service,
        )

    def tearDown(self):
        self.service.shutdown()
        self.page.deleteLater()

    def visible_ids(self):
        return [
            self.page.test_table.item(row, 1).text()
            for row in range(self.page.test_table.rowCount())
            if not self.page.test_table.isRowHidden(row)
        ]

    def test_three_subpages_filters_details_and_persistence(self):
        self.assertEqual(self.page.camera_pages.count(), 3)
        self.assertEqual(self.page.camera_pages.currentIndex(), 0)
        self.page.tests_tab_button.click()
        self.assertEqual(self.page.camera_pages.currentIndex(), 1)
        self.page.ros_automation_tab_button.click()
        self.assertEqual(self.page.camera_pages.currentIndex(), 2)
        self.page.monitor_tab_button.click()
        self.assertEqual(self.page.camera_pages.currentIndex(), 0)

        self.assertEqual(self.page.test_table.rowCount(), 7)
        self.assertEqual(
            {
                self.page.test_table.item(row, 1).text()
                for row in range(self.page.test_table.rowCount())
            },
            {"CS-007", "CS-008", "RF-001", "RF-002", "RF-003", "RF-004", "SR-004"},
        )
        self.page.test_search.setText("RF-")
        self.assertEqual(self.visible_ids(), ["RF-001", "RF-002", "RF-003", "RF-004"])
        self.page.test_search.clear()
        self.page.test_category_filter.setCurrentText("Resolution & FPS")
        self.assertEqual(self.visible_ids(), ["RF-001", "RF-002", "RF-003", "RF-004"])

        rf_row = 2
        self.page.test_table.item(rf_row, 0).setCheckState(Qt.CheckState.Checked)
        self.page.test_search.setText("CS-007")
        self.assertEqual(self.page.test_table.item(rf_row, 0).checkState(), Qt.CheckState.Checked)
        self.page.test_search.clear()
        self.page.test_category_filter.setCurrentText("All")

        self.page._on_test_row_clicked(rf_row, 1)
        details = self.page.test_detail_text.toPlainText()
        self.assertIn("camera.resolution_fps", details)
        self.assertIn("HD4K", details)
        self.assertIn("Average FPS Ratio >= 0.95", details)

        self.page._set_test_status("RF-001", "PASS")
        self.page.test_status_filter.setCurrentText("PASS")
        self.assertEqual(self.visible_ids(), ["RF-001"])
        self.page._set_camera_subpage(0)
        self.page._set_camera_subpage(2)
        self.page._set_camera_subpage(1)
        self.assertEqual(self.page.test_statuses["RF-001"], "PASS")
        self.assertIsNone(self.page.test_runner_worker)

    def test_inventory_exists_only_on_ros_automation_and_service_is_shared(self):
        self.assertIs(self.page.camera_inventory_service, self.inventory_service)
        self.assertTrue(
            self.page.ros_automation_page.isAncestorOf(self.page.ros_inventory_card)
        )
        self.assertFalse(
            self.page.automated_tests_page.isAncestorOf(self.page.ros_inventory_card)
        )
        self.assertFalse(
            self.page.automated_tests_page.isAncestorOf(self.page.target_camera_combo)
        )
        self.assertEqual(self.page.target_camera_combo.currentText(), "All Cameras")
        self.assertIsNone(self.page.target_camera_combo.currentData())
        self.assertGreaterEqual(self.page.test_table.minimumHeight(), 280)

    def test_monitor_omits_duplicate_automated_tests_summary_and_expands_log(self):
        monitor_text = self.page.monitor_page.findChildren(QLabel)
        self.assertNotIn("Automated Tests", [label.text() for label in monitor_text])
        self.assertEqual(self.page.camera_pages.count(), 3)
        self.assertEqual(self.page.tests_tab_button.text(), "AUTOMATED TESTS")
        layout = self.page.monitor_page.layout()
        self.assertEqual(layout.count(), 3)
        self.assertGreater(layout.stretch(2), layout.stretch(0))

    def test_monitor_header_cards_are_compact_and_overview_scrolls(self):
        self.assertEqual(
            self.page.camera_device_card.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Maximum,
        )
        self.assertEqual(
            self.page.device_overview_card.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Maximum,
        )
        self.assertEqual(
            self.page.overview_table.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertEqual(
            self.page.overview_table.verticalHeader().defaultSectionSize(), 20
        )
        self.assertGreater(self.page.overview_table.maximumHeight(), 0)
        self.assertEqual(self.page.monitor_page.layout().stretch(2), 1)

    def test_monitor_compact_top_row_releases_space_to_live_log(self):
        self.page.resize(1710, 864)
        self.page.show()
        self.app.processEvents()

        monitor_layout = self.page.monitor_page.layout()
        self.assertEqual(monitor_layout.stretch(0), 0)
        self.assertEqual(monitor_layout.stretch(1), 0)
        self.assertEqual(monitor_layout.stretch(2), 1)
        self.assertLess(
            self.page.device_overview_card.height(),
            self.page.live_log_card.height(),
        )
        self.assertGreater(self.page.live_log.height(), 110)
        self.assertLessEqual(
            self.page.device_overview_card.height(),
            self.page.camera_device_card.height() + 60,
        )

    def test_ros_automation_lists_phase_8_3a_and_8_3b_tests_and_renders_details(self):
        self.assertEqual(self.page.ros_test_table.rowCount(), 13)
        self.assertEqual(
            [
                self.page.ros_test_table.horizontalHeaderItem(column).text()
                for column in range(self.page.ros_test_table.columnCount())
            ],
            ["Select", "ID", "Test Name", "Target", "Duration", "Status"],
        )
        self.assertEqual(
            [
                self.page.ros_test_table.item(row, 1).text()
                for row in range(self.page.ros_test_table.rowCount())
            ],
            [
                "ROS-001", "ROS-002", "ROS-003", "ROS-004", "ROS-005", "ROS-006", "ROS-007", "ROS-008",
                "ROS-REC-001", "ROS-REC-002", "ROS-REC-005", "ROS-REC-006", "ROS-REC-007",
            ],
        )
        self.page._on_ros_test_row_clicked(3, 1)
        details = self.page.ros_test_detail_text.toPlainText()
        self.assertIn("ros.image_profile", details)
        self.assertIn("Sample Count: 100", details)
        self.assertIn("Fps Ratio Min: 0.95", details)
        self.assertIn("All Cameras", details)

        zed = RosCameraDriverRegistry().map_device(
            normalize_zed_device({
                "model": "ZED X Mini",
                "serial_number": "58651554",
                "device_path": "/dev/i2c-9",
                "api": "Camera",
            }),
            {"zed_wrapper": True},
        )
        self._publish_inventory((zed,), "2026-09-08T01:00:00+00:00")
        self.page.target_camera_combo.setCurrentIndex(1)
        details = self.page.ros_test_detail_text.toPlainText()
        self.assertIn("ZED X Mini — SN58651554", details)
        self.assertIn("Prerequisites: ROS Graph: Required", details)
        self.assertIn("Physical Identity: Not Required", details)
        self.assertNotIn("Driver: zed_wrapper", details)

    def test_ros_workspace_prioritizes_table_and_places_log_beside_it(self):
        self.page.resize(1600, 900)
        self.page.show()
        self.page._set_camera_subpage(2)
        self.app.processEvents()

        self.assertEqual(
            self.page.ros_workspace_splitter.orientation(),
            Qt.Orientation.Horizontal,
        )
        self.assertEqual(
            self.page.ros_tests_splitter.orientation(),
            Qt.Orientation.Vertical,
        )
        workspace_sizes = self.page.ros_workspace_splitter.sizes()
        self.assertGreater(workspace_sizes[0], workspace_sizes[1])
        self.assertGreaterEqual(self.page.ros_test_table.viewport().height(), 340)
        self.assertEqual(
            self.page.ros_test_table.verticalHeader().defaultSectionSize(), 34
        )
        self.assertTrue(
            self.page.ros_automation_page.isAncestorOf(
                self.page.ros_execution_log
            )
        )

    def test_ros_inventory_collapses_after_successful_discovery(self):
        self.assertFalse(self.page._inventory_expanded)
        self.page.inventory_toggle_button.click()
        self.assertTrue(self.page._inventory_expanded)

        zed = RosCameraDriverRegistry().map_device(
            normalize_zed_device({
                "model": "ZED X Mini",
                "serial_number": "58651554",
                "device_path": "/dev/i2c-9",
                "api": "Camera",
            }),
            {"zed_wrapper": True},
        )
        self._publish_inventory((zed,), "2026-09-08T02:00:00+00:00")
        self.assertFalse(self.page._inventory_expanded)
        self.assertEqual(self.page.inventory_toggle_button.text(), "SHOW DETAILS")
        self.assertEqual(self.page.inventory_count_label.text(), "1 Detected")
        self.assertTrue(
            self.page.ros_status_card.isAncestorOf(
                self.page.inventory_discover_button
            )
        )

    def test_ros_log_filters_clear_and_auto_scroll_state(self):
        self.page._append_ros_log("INFO", "info event")
        self.page._append_ros_log("PASS", "pass event")
        self.page._append_ros_log("FAIL", "failure event")
        self.page._append_ros_log("WARNING", "warning event")
        self.page._append_ros_log("ERROR", "error event")

        self.page.ros_log_filter_combo.setCurrentText("INFO")
        text = self.page.ros_execution_log.toPlainText()
        self.assertIn("info event", text)
        self.assertNotIn("pass event", text)
        self.page.ros_log_filter_combo.setCurrentText("ERROR")
        text = self.page.ros_execution_log.toPlainText()
        self.assertIn("failure event", text)
        self.assertIn("error event", text)
        self.assertTrue(self.page.ros_log_auto_scroll_check.isChecked())
        self.page.ros_log_clear_button.click()
        self.assertEqual(self.page.ros_execution_log.toPlainText(), "")
        self.assertEqual(self.page._ros_log_entries, [])

    def test_ros_status_and_results_persist_across_subpages(self):
        result = {
            "status": "PASS",
            "measurements": {
                "ros_distro": "humble",
                "selected_camera_count": 1,
            },
            "rule_results": [],
            "sub_results": [],
        }
        self.page.ros_test_results["ROS-001"] = result
        self.page._set_ros_test_status("ROS-001", "PASS")
        self.page._on_ros_test_row_clicked(0, 1)
        self.page.monitor_tab_button.click()
        self.page.tests_tab_button.click()
        self.page.ros_automation_tab_button.click()
        self.assertEqual(self.page.ros_test_statuses["ROS-001"], "PASS")
        self.assertIs(self.page.ros_test_results["ROS-001"], result)
        self.assertEqual(self.page.ros_test_table.item(0, 5).text(), "PASS")
        self.assertIn("Latest Result: PASS", self.page.ros_test_detail_text.toPlainText())
        self.assertEqual(self.page.ros_pass_label.text(), "PASS 1")

    def test_phase83b_result_detail_uses_compact_test_specific_summary(self):
        self.page.ros_test_results["ROS-005"] = {
            "status": "PASS",
            "measurements": {"all_devices_pass": True, "selected_camera_count": 1},
            "rule_results": [],
            "sub_results": [{
                "serial": "58651554",
                "status": "PASS",
                "measurements": {
                    "image": {"width": 1920, "height": 1200},
                    "camera_info": {
                        "width": 1920, "height": 1200,
                        "distortion_model": "plumb_bob",
                        "K": list(range(9)),
                    },
                    "fx": 700.0,
                    "fy": 701.0,
                    "finite_values": True,
                    "frame_relationship_valid": True,
                },
                "rule_results": [],
            }],
        }
        self.page._set_ros_test_status("ROS-005", "PASS")
        self.page._show_ros_test_details("ROS-005")
        detail = self.page.ros_test_detail_text.toPlainText()
        self.assertIn("image=1920x1200", detail)
        self.assertIn("CameraInfo=1920x1200", detail)
        self.assertIn("distortion=plumb_bob", detail)
        self.assertNotIn("[0, 1, 2, 3", detail)

    def test_inventory_refresh_keeps_table_combo_and_details_consistent(self):
        registry = RosCameraDriverRegistry()
        zed = registry.map_device(
            normalize_zed_device({
                "model": "ZED X Mini", "serial_number": "100",
                "device_path": "/dev/zed0", "api": "Camera",
            }),
            {"zed_wrapper": True},
        )
        realsense = registry.map_device(
            normalize_realsense_device({
                "model": "Intel RealSense D435i", "serial": "200",
                "physical_port": "2-1", "usb_type_descriptor": "2.1",
                "backend": "pyrealsense2", "profile_status": "AVAILABLE",
            }),
            {"realsense2_camera": True},
        )
        self._publish_inventory((zed, realsense), "2026-09-07T01:00:00+00:00")

        table_uids = [
            self.page.inventory_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for row in range(self.page.inventory_table.rowCount())
        ]
        combo_uids = [
            self.page.target_camera_combo.itemData(index)
            for index in range(1, self.page.target_camera_combo.count())
        ]
        self.assertEqual(table_uids, [zed.device_uid, realsense.device_uid])
        self.assertEqual(combo_uids, table_uids)
        self.assertIs(
            self.inventory_service.get_device(realsense.device_uid), realsense
        )

        self.page.target_camera_combo.setCurrentIndex(2)
        self.assertEqual(
            self.page.camera_target_selection.selected_device_uid,
            realsense.device_uid,
        )
        self.assertIn(realsense.device_uid, self.page.inventory_detail_text.toPlainText())
        self.assertEqual(self.page.inventory_table.currentRow(), 1)

        refreshed = replace(realsense, physical_port="3-4")
        self._publish_inventory((zed, refreshed), "2026-09-07T01:01:00+00:00")
        self.assertEqual(
            self.page.target_camera_combo.currentData(), realsense.device_uid
        )
        self.assertIn("Physical Port: 3-4", self.page.inventory_detail_text.toPlainText())
        self.assertIn("Camera inventory complete: 2", self.page.ros_execution_log.toPlainText())

        self.page._set_test_status("CS-007", "PASS")
        self.page.monitor_tab_button.click()
        self.page.ros_automation_tab_button.click()
        self.assertEqual(self.page.inventory_table.rowCount(), 2)
        self.assertEqual(self.page.target_camera_combo.currentData(), realsense.device_uid)
        self.assertEqual(self.page.test_statuses["CS-007"], "PASS")

    def test_ros_read_only_monitor_never_uses_direct_sdk_or_stops_production(self):
        device = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "53204228",
            "driver_name": "ZED SDK", "driver_version": "5.4.1", "input_type": "GMSL",
            "resolution": "1920x1080", "target_fps": "60",
            "ros_node": "/sensors/zed_x_mini",
            "device_info_topic": "/sensors/camera/zed_x_mini/device_info",
            "rgb_topic": "/sensors/camera/zed_x_mini/rgb", "stream_active": True,
        })
        self.inventory_service.inventory._devices = (device,)
        self.page.device_combo.clear()
        self.page.device_combo.addItem("ZED X Mini / SN53204228", device.device_uid)
        self.page._connect_ros_read_only(device)
        self.assertEqual(self.page.connection_status_label.text(), "CONNECTED VIA ROS")
        self.assertIn("ROS READ ONLY", self.page.overview_table.item(12, 1).text())

        starts, stops = [], []
        self.page.ros_monitor_controller.start = lambda payload: starts.append(payload)
        self.page.ros_monitor_controller.stop = lambda: stops.append(True)
        self.page._request_action("start_stream")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["rgb_topic"], "/sensors/camera/zed_x_mini/rgb")
        self.page._request_action("stop_stream")
        self.assertEqual(len(stops), 1)
        self.page._request_action("disconnect")
        self.assertGreaterEqual(len(stops), 2)
        self.assertEqual(self.page.connection_state.value, "DISCONNECTED")

    def test_device_selection_replaces_incompatible_zed_profile_and_stream_options(self):
        zed = RosCameraDriverRegistry().map_device(
            normalize_zed_device({
                "model": "ZED X One 4K", "serial_number": "111", "api": "Camera",
            }), {"zed_wrapper": True},
        )
        d435i = RosCameraDriverRegistry().map_device(
            normalize_realsense_device({
                "model": "Intel RealSense D435i", "serial": "242322076751",
                "physical_port": "2-1", "usb_type_descriptor": "5000",
                "backend": "pyrealsense2", "profile_status": "AVAILABLE",
            }), {"realsense2_camera": True},
        )
        self._publish_inventory((zed, d435i), "2026-09-25T01:00:00+00:00")
        self.page.device_combo.setCurrentIndex(0)
        self.assertEqual(self.page.model_combo.currentData(), "zed_x_one_4k")
        self.assertIn("HD4K", self.page.resolution_combo.itemText(0))
        self.page.device_combo.setCurrentIndex(1)
        self.assertEqual(self.page.model_combo.currentData(), "realsense_d435i")
        self.assertNotIn("HD4K", [
            self.page.resolution_combo.itemText(index)
            for index in range(self.page.resolution_combo.count())
        ])
        self.assertEqual(self.page.overview_table.item(0, 1).text(), "D435i")
        self.assertEqual(self.page.overview_table.item(2, 1).text(), "242322076751")

    def test_discovery_logs_zero_adapter_and_final_inventory(self):
        device = normalize_realsense_device({
            "model": "Intel RealSense D435i", "serial": "242322076751",
            "physical_port": "2-1", "usb_type_descriptor": "5000",
            "backend": "pyrealsense2", "profile_status": "AVAILABLE",
        })
        snapshot = CameraInventorySnapshot(
            devices=(device,), adapter_errors={}, adapter_warnings={},
            discovered_at="2026-09-25T01:00:00+00:00", ros2_available=True,
            adapter_candidates={"zed": (), "realsense": (device,)}, raw_candidate_count=1,
        )
        self.inventory_service.inventory._devices = (device,)
        self.inventory_service.inventory._snapshot = snapshot
        self.page._on_inventory_discovery_completed(snapshot)
        log = self.page.live_log.toPlainText()
        self.assertIn("ZED physical discovery returned 0 candidate(s).", log)
        self.assertIn("Final camera: Intel RealSense D435i SN242322076751", log)
        self.assertIn("Raw candidates: 1", log)

    def _publish_inventory(self, devices, discovered_at):
        snapshot = CameraInventorySnapshot(
            devices=tuple(devices),
            adapter_errors={},
            adapter_warnings={},
            discovered_at=discovered_at,
            ros2_available=True,
        )
        self.inventory_service.inventory._devices = tuple(devices)
        self.inventory_service.inventory._snapshot = snapshot
        self.page._on_inventory_discovery_completed(snapshot)


if __name__ == "__main__":
    unittest.main()
