"""Exercise native Qt wheel routing, not just direct wheelEvent calls."""
import ast
import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QPlainTextEdit,
    QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from desktop_app.ui.widgets import (
    ClickWheelComboBox, ClickWheelDoubleSpinBox, ClickWheelSlider, ClickWheelSpinBox,
)


def wheel_over(widget, delta=-120):
    """Enter through QWindow so Qt performs hit testing and parent propagation."""
    window = widget.window()
    point = widget.mapTo(window, widget.rect().center())
    QTest.wheelEvent(window.windowHandle(), point, QPoint(0, delta))
    QApplication.processEvents()


class WheelControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def host(self, control):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        blank = QLabel("Blank page area")
        layout.addWidget(blank)
        layout.addWidget(control)
        other = QLineEdit()
        layout.addWidget(other)
        filler = QWidget()
        filler.setMinimumHeight(1200)
        layout.addWidget(filler)
        scroll.setWidget(content)
        scroll.resize(420, 300)
        scroll.show()
        scroll.activateWindow()
        self.app.processEvents()
        self.addCleanup(scroll.close)
        self.addCleanup(scroll.deleteLater)
        return scroll, blank, other

    @staticmethod
    def control(kind):
        control = kind()
        if isinstance(control, QComboBox):
            control.addItems(["first", "second", "third"])
            control.setCurrentIndex(1)
        else:
            control.setRange(0, 100)
            control.setValue(20)
        return control

    @staticmethod
    def value(control):
        return control.currentIndex() if isinstance(control, QComboBox) else control.value()

    def click(self, control):
        if isinstance(control, (QSpinBox, QDoubleSpinBox)):
            QTest.mouseClick(control.lineEdit(), Qt.MouseButton.LeftButton)
        else:
            QTest.mouseClick(control, Qt.MouseButton.LeftButton)
        if isinstance(control, QComboBox):
            self.app.processEvents()
            control.hidePopup()
        self.app.processEvents()

    def test_hover_routes_to_scroll_area_and_click_then_blank_disarms(self):
        for kind in (ClickWheelSpinBox, ClickWheelDoubleSpinBox, ClickWheelComboBox, ClickWheelSlider):
            with self.subTest(kind=kind.__name__):
                control = self.control(kind)
                scroll, blank, other = self.host(control)
                other.setFocus()
                before = self.value(control)
                wheel_over(control)
                self.assertEqual(self.value(control), before)
                self.assertGreater(scroll.verticalScrollBar().value(), 0)
                scroll.verticalScrollBar().setValue(0)
                self.click(control)
                before = self.value(control)
                wheel_over(control, 120)
                self.assertNotEqual(self.value(control), before)
                self.assertEqual(scroll.verticalScrollBar().value(), 0)
                QTest.mouseClick(blank, Qt.MouseButton.LeftButton)
                self.assertFalse(control.hasFocus())
                before = self.value(control)
                wheel_over(control)
                self.assertEqual(self.value(control), before)
                self.assertGreater(scroll.verticalScrollBar().value(), 0)
                scroll.close()

    def test_initial_and_programmatic_focus_do_not_arm_wheel(self):
        for kind in (ClickWheelSpinBox, ClickWheelDoubleSpinBox, ClickWheelComboBox):
            with self.subTest(kind=kind.__name__):
                control = self.control(kind)
                scroll, _, other = self.host(control)
                self.assertTrue(control.hasFocus())
                other.setFocus()
                control.setFocus()
                before = self.value(control)
                wheel_over(control)
                self.assertEqual(self.value(control), before)
                self.assertGreater(scroll.verticalScrollBar().value(), 0)
                self.assertEqual(control.focusPolicy(), Qt.FocusPolicy.StrongFocus)
                scroll.close()

    def test_unarmed_direct_event_is_ignored(self):
        for kind in (ClickWheelSpinBox, ClickWheelDoubleSpinBox, ClickWheelComboBox):
            with self.subTest(kind=kind.__name__):
                control = self.control(kind)
                scroll, _, other = self.host(control)
                other.setFocus()
                point = control.rect().center()
                event = QWheelEvent(QPointF(point), QPointF(control.mapToGlobal(point)),
                                    QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                before = self.value(control)
                QApplication.sendEvent(control, event)
                self.assertFalse(event.isAccepted())
                self.assertEqual(self.value(control), before)
                scroll.close()

    def test_focus_transfer_keyboard_and_tab_remain_usable(self):
        for kind in (ClickWheelSpinBox, ClickWheelDoubleSpinBox, ClickWheelComboBox):
            with self.subTest(kind=kind.__name__):
                control = self.control(kind)
                scroll, _, other = self.host(control)
                self.click(control)
                before = self.value(control)
                QTest.keyClick(control, Qt.Key.Key_Down)
                self.assertNotEqual(self.value(control), before)
                QTest.keyClick(control, Qt.Key.Key_Up)
                self.assertEqual(self.value(control), before)
                if isinstance(control, (QSpinBox, QDoubleSpinBox)):
                    control.selectAll()
                    QTest.keyClicks(control, "42")
                    QTest.keyClick(control, Qt.Key.Key_Return)
                    self.assertEqual(control.value(), 42)
                QTest.keyClick(control, Qt.Key.Key_Tab)
                self.assertTrue(other.hasFocus())
                before = self.value(control)
                wheel_over(control)
                self.assertEqual(self.value(control), before)
                self.assertGreater(scroll.verticalScrollBar().value(), 0)
                scroll.verticalScrollBar().setValue(0)
                QTest.keyClick(other, Qt.Key.Key_Backtab)
                self.assertTrue(control.hasFocus())
                wheel_over(control)
                self.assertEqual(self.value(control), before)
                scroll.close()

    def test_combo_popup_scrolls_normally(self):
        combo = ClickWheelComboBox()
        combo.addItems([f"Item {i}" for i in range(150)])
        scroll, _, _ = self.host(combo)
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        bar = combo.view().verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        before = bar.value()
        wheel_over(combo.view().viewport())
        self.assertGreater(bar.value(), before)
        self.assertEqual(scroll.verticalScrollBar().value(), 0)
        combo.hidePopup()

    def test_nested_scroll_area_receives_wheel(self):
        spin = self.control(ClickWheelSpinBox)
        inner, _, other = self.host(spin)
        outer, _, _ = self.host(inner)
        inner.setFixedHeight(200)
        other.setFocus()
        self.app.processEvents()
        wheel_over(spin)
        self.assertEqual(spin.value(), 20)
        self.assertGreater(inner.verticalScrollBar().value(), 0)
        self.assertEqual(outer.verticalScrollBar().value(), 0)

    def test_text_and_table_viewports_keep_scrolling(self):
        for viewer in (QPlainTextEdit(), QTableWidget(100, 1)):
            with self.subTest(viewer=type(viewer).__name__):
                if isinstance(viewer, QPlainTextEdit):
                    viewer.setPlainText("\n".join(f"Log line {i}" for i in range(100)))
                else:
                    for row in range(100):
                        viewer.setItem(row, 0, QTableWidgetItem(str(row)))
                viewer.setFixedHeight(150)
                scroll, _, _ = self.host(viewer)
                wheel_over(viewer.viewport())
                self.assertGreater(viewer.verticalScrollBar().value(), 0)
                self.assertEqual(scroll.verticalScrollBar().value(), 0)
                scroll.close()

    def test_ui_pages_do_not_construct_unguarded_value_controls(self):
        root = Path(__file__).resolve().parents[1] / "desktop_app" / "ui"
        unsafe = {"QSpinBox", "QDoubleSpinBox", "QComboBox", "QSlider"}
        for path in root.glob("*.py"):
            if path.name == "widgets.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, unsafe, f"{path.name}:{node.lineno}")


if __name__ == "__main__":
    unittest.main()
