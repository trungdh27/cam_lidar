import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from desktop_app.ui.camera_controls import (
    ClickWheelComboBox,
    ClickWheelDoubleSpinBox,
    ClickWheelSpinBox,
)


def _wheel_event(widget, delta=-120):
    point = QPoint(8, 8)
    return QWheelEvent(
        QPointF(point), QPointF(widget.mapToGlobal(point)), QPoint(), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )


class _WheelSink(QWidget):
    def __init__(self):
        super().__init__()
        self.wheel_events = 0
        self.layout = QVBoxLayout(self)

    def wheelEvent(self, event):
        self.wheel_events += 1
        event.accept()


class ClickWheelControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _host(self, control):
        host = _WheelSink()
        other = QPushButton("Elsewhere")
        host.layout.addWidget(control)
        host.layout.addWidget(other)
        host.show()
        self.app.processEvents()
        return host, other

    def test_unarmed_combo_ignores_wheel_and_propagates_to_parent(self):
        combo = ClickWheelComboBox()
        combo.addItems(["first", "second", "third"])
        host, _other = self._host(combo)
        event = _wheel_event(combo)
        QApplication.sendEvent(combo, event)
        self.assertEqual(combo.currentIndex(), 0)
        self.assertFalse(event.isAccepted())
        # A non-accepted wheel event is eligible for normal parent/scroll-area
        # propagation; Qt does not synchronously re-dispatch it for a bare
        # QWidget receiver in this headless test.
        self.assertEqual(host.wheel_events, 0)
        host.close()

    def test_click_arms_combo_wheel_and_focus_out_disarms_it(self):
        combo = ClickWheelComboBox()
        combo.addItems(["first", "second", "third"])
        host, other = self._host(combo)
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
        self.assertTrue(combo.view().isVisible())
        combo.hidePopup()
        self.assertTrue(combo._wheel_armed)
        combo.setFocus()
        combo.wheelEvent(_wheel_event(combo))
        self.assertEqual(combo.currentIndex(), 1)
        QTest.mouseClick(other, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(combo._wheel_armed)
        host.close()

    def test_spinboxes_share_click_armed_wheel_behavior(self):
        for control_type in (ClickWheelSpinBox, ClickWheelDoubleSpinBox):
            with self.subTest(control_type=control_type.__name__):
                control = control_type()
                control.setRange(0, 10)
                control.setValue(5)
                host, other = self._host(control)
                QApplication.sendEvent(control, _wheel_event(control))
                self.assertEqual(control.value(), 5)
                QTest.mouseClick(control, Qt.MouseButton.LeftButton)
                self.assertTrue(control._wheel_armed)
                control.setFocus()
                control.wheelEvent(_wheel_event(control, 120))
                self.assertNotEqual(control.value(), 5)
                QTest.mouseClick(other, Qt.MouseButton.LeftButton)
                self.app.processEvents()
                self.assertFalse(control._wheel_armed)
                host.close()


if __name__ == "__main__":
    unittest.main()
