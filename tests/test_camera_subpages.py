import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.camera_page import CameraPage


class CameraSubpageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = JetsonState()
        self.service = JetsonConnectionService(self.state)
        self.page = CameraPage(self.state, self.service)

    def tearDown(self):
        self.service.shutdown()
        self.page.deleteLater()

    def visible_ids(self):
        return [
            self.page.test_table.item(row, 1).text()
            for row in range(self.page.test_table.rowCount())
            if not self.page.test_table.isRowHidden(row)
        ]

    def test_subpages_filters_details_and_persistence(self):
        self.assertEqual(self.page.camera_pages.count(), 2)
        self.assertEqual(self.page.camera_pages.currentIndex(), 0)
        self.page.open_test_manager_button.click()
        self.assertEqual(self.page.camera_pages.currentIndex(), 1)
        self.page.monitor_tab_button.click()
        self.assertEqual(self.page.camera_pages.currentIndex(), 0)

        self.assertEqual(self.page.test_table.rowCount(), 7)
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
        self.page._set_camera_subpage(1)
        self.assertEqual(self.page.test_statuses["RF-001"], "PASS")
        self.assertIsNone(self.page.test_runner_worker)


if __name__ == "__main__":
    unittest.main()
