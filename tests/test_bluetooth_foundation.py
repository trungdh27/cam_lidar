import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.bluetooth import BluetoothManager, BluetoothTestRunner, BluetoothTestStatus
from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.bluetooth_page import BluetoothPage


class BluetoothFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = JetsonState()
        self.service = JetsonConnectionService(self.state)
        self.manager = BluetoothManager(self.service)
        self.runner = BluetoothTestRunner(self.manager)
        self.page = BluetoothPage(
            self.state, self.service, self.manager, self.runner
        )

    def tearDown(self):
        self.service.shutdown()
        self.page.deleteLater()

    def test_uses_shared_connection_and_starts_unknown(self):
        self.assertIs(self.manager.jetson_service, self.service)
        self.assertEqual(self.page.jetson_chip.text_label.text(), "Disconnected")
        self.assertEqual(self.page.controller_chip.text_label.text(), "Unknown")
        self.assertEqual(self.page.expected_role_label.text(), "Peripheral")

        self.state.set_connected({}, {})
        self.app.processEvents()
        self.assertEqual(self.page.jetson_chip.text_label.text(), "Connected")

    def test_catalog_selection_and_disconnected_execution_are_truthful(self):
        tests = self.page.test_page
        self.assertEqual(
            [tests.test_table.item(row, 1).text() for row in range(3)],
            ["TC-JBT-ENV-001", "TC-JBT-ENV-002", "TC-JBT-ROLE-001"],
        )
        tests.select_all_visible()
        self.assertEqual(len(tests.selected_test_ids()), 3)
        self.assertTrue(
            all(
                tests.test_table.item(row, 0).checkState()
                == Qt.CheckState.Checked
                for row in range(3)
            )
        )
        tests.run_selected()
        self.assertTrue(
            all(
                self.runner.status_for(test_id)
                == BluetoothTestStatus.ERROR
                for test_id in tests.selected_test_ids()
            )
        )
        self.assertNotIn("NOT IMPLEMENTED", tests.live_log.toPlainText())


if __name__ == "__main__":
    unittest.main()
