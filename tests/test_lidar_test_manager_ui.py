import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from desktop_app.ui.main_window import MainWindow
from desktop_app.ui.lidar_test_dialogs import LidarTestDetailsDialog


class LidarTestManagerUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()
        self.page = self.window.lidar_page

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_population_initial_state_and_filters(self):
        self.assertEqual(self.page.test_table.rowCount(), 36)
        ids = [self.page.test_table.item(row, 1).text() for row in range(36)]
        self.assertEqual(sum(value.startswith("LID-") for value in ids), 10)
        self.assertEqual(sum(value.startswith("TC-") for value in ids), 26)
        self.assertTrue(all(self.page.test_table.item(row, 5).text() == "NOT RUN" for row in range(36)))

        self.page.test_search.setText("TC-")
        self.page.test_automation_filter.setCurrentText("Auto")
        visible = [row for row in range(36) if not self.page.test_table.isRowHidden(row)]
        self.assertEqual(len(visible), 13)
        self.page._select_all_visible()
        checked = [row for row in range(36) if self.page.test_table.item(row, 0).checkState() == Qt.CheckState.Checked]
        self.assertEqual(checked, visible)

        self.page.test_group_filter.setCurrentText("ROS Driver & Topics")
        self.assertEqual(sum(not self.page.test_table.isRowHidden(row) for row in range(36)), 7)
        self.page._clear_test_selection()
        self.assertFalse(any(self.page.test_table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(36)))

    def test_source_details_show_required_fields(self):
        definition = self.page.test_registry.get("TC-PCD-003")
        dialog = LidarTestDetailsDialog(definition, self.page.test_results[definition.id])
        try:
            text = dialog.findChild(type(self.page.live_log)).toPlainText()
            for label in ("Purpose", "Precondition", "Requirement", "Operation Procedure", "Expected Result", "Parameters", "Current Result", "Evidence"):
                self.assertIn(label, text)
            self.assertIn("Historical Source Status", text)
            self.assertIn("FAIL", text)
            self.assertIn("NOT_RUN", text)
        finally:
            dialog.close()

    def test_internal_point_cloud_tab_does_not_restart_stream(self):
        request_id = self.page.stream_service.request_id
        self.assertEqual(self.page.content_stack.currentIndex(), 0)
        self.assertTrue(self.page.monitor_tab_button.isChecked())

        self.page.pointcloud_tab_button.click()
        self.assertEqual(self.page.content_stack.currentIndex(), 1)
        self.assertIs(self.page.content_stack.currentWidget(), self.page.pointcloud_page)
        self.assertEqual(self.page.stream_service.request_id, request_id)

    def test_ros2_tab_is_internal_and_does_not_restart_native_stream(self):
        request_id = self.page.stream_service.request_id
        self.assertEqual(self.page.content_stack.currentIndex(), 0)
        self.page.ros2_tab_button.click()
        self.assertEqual(self.page.content_stack.currentIndex(), 2)
        self.assertIs(self.page.content_stack.currentWidget(), self.page.ros2_page)
        self.assertEqual(self.page.stream_service.request_id, request_id)
        self.page.pointcloud_tab_button.click()
        self.assertEqual(self.page.content_stack.currentIndex(), 1)
        self.page.monitor_tab_button.click()
        self.assertEqual(self.page.content_stack.currentIndex(), 0)
        self.assertEqual(self.page.stream_service.request_id, request_id)

        self.page.monitor_tab_button.click()
        self.assertEqual(self.page.content_stack.currentIndex(), 0)
        self.assertIs(self.page.content_stack.currentWidget(), self.page.page_scroll)
        self.assertEqual(self.page.stream_service.request_id, request_id)


if __name__ == "__main__":
    unittest.main()
