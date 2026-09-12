import os
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from desktop_app.services.camera_inventory_service import CameraInventoryService
from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.camera_page import CameraPage
from devices.camera.discovery import normalize_realsense_device, normalize_zed_device
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
        self.page.open_test_manager_button.click()
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
        self.assertIn("zed_wrapper", details)

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
