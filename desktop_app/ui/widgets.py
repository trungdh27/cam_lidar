from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
    QSlider, QSpinBox, QVBoxLayout, QWidget,
)


class _ClickWheelFocusFilter(QObject):
    """Handle editor-child clicks and release input focus on blank-area clicks.

    Only mouse presses are observed. Scroll areas, logs, tables and combo popup
    views retain their own wheel handling.
    """

    @staticmethod
    def _control(widget):
        while isinstance(widget, QWidget):
            if isinstance(widget, _ClickWheelMixin):
                return widget
            widget = widget.parentWidget()
        return None

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
            # A popup temporarily owns focus and must keep normal selection and
            # scrolling, including the mouse press that dismisses it.
            if QApplication.activePopupWidget() is None:
                previous = self._control(QApplication.focusWidget())
                # Mouse presses can bubble to a non-focusable parent. Use the
                # original hit position so that bubbling does not disarm the
                # input that was just clicked.
                target = QApplication.widgetAt(event.globalPosition().toPoint())
                clicked = self._control(target or watched)
                if previous is not None and previous is not clicked:
                    previous.clearFocus()
                if clicked is not None and event.button() == Qt.MouseButton.LeftButton:
                    clicked.setFocus(Qt.FocusReason.MouseFocusReason)
                    clicked._wheel_armed = True
        return False


class _ClickWheelMixin:
    """Allow wheel edits after a click, until focus leaves the control.

    Explicit arming prevents initial/programmatic focus from enabling wheel
    edits. StrongFocus preserves Tab/keyboard use without wheel-acquired focus.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._wheel_armed = False
        app = QApplication.instance()
        if not hasattr(app, "_click_wheel_focus_filter"):
            app._click_wheel_focus_filter = _ClickWheelFocusFilter(app)
            app.installEventFilter(app._click_wheel_focus_filter)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # Opening a combo popup can temporarily move focus away. Arm after
        # normal processing as well as on presses in the embedded text editor.
        if event.button() == Qt.MouseButton.LeftButton:
            self._wheel_armed = True

    def focusOutEvent(self, event):
        self._wheel_armed = False
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if not self._wheel_armed or not self.hasFocus():
            # Ignoring (rather than accepting) lets Qt route the original wheel
            # event to the containing scroll area, including nested areas.
            event.ignore()
            return
        super().wheelEvent(event)


class ClickWheelComboBox(_ClickWheelMixin, QComboBox):
    """Combo box with click-enabled wheel selection; popup views are unchanged."""


class ClickWheelSpinBox(_ClickWheelMixin, QSpinBox):
    """Integer input with click-enabled wheel stepping."""


class ClickWheelDoubleSpinBox(_ClickWheelMixin, QDoubleSpinBox):
    """Decimal input with click-enabled wheel stepping."""


class ClickWheelSlider(_ClickWheelMixin, QSlider):
    """Slider with click-enabled wheel adjustment."""


class Card(QFrame):
    def __init__(self, title: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")

        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(14, 12, 14, 12)
        self.root_layout.setSpacing(9)

        if title:
            title_label = QLabel(title)
            title_label.setObjectName("CardTitle")
            self.root_layout.addWidget(title_label)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        self.root_layout.addWidget(self.body)


class SectionFrame(Card):
    """Card with a shared semantic border state for dense test dashboards.

    Semantic values are themed centrally so feature pages can use subtle visual
    grouping without each page owning a collection of inline styles.
    """

    def __init__(self, title: str | None = None, semantic: str = "neutral", parent=None):
        super().__init__(title, parent)
        self.setObjectName("SectionFrame")
        self.set_semantic(semantic)

    def set_semantic(self, semantic: str) -> None:
        self.setProperty("semantic", semantic)
        self.style().unpolish(self)
        self.style().polish(self)


class StatusChip(QFrame):
    def __init__(self, text: str, state: str = "idle", parent=None):
        super().__init__(parent)
        self.setObjectName("StatusChip")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        self.dot = QLabel("●")
        self.text_label = QLabel(text)
        self.text_label.setObjectName("ChipText")

        layout.addWidget(self.dot)
        layout.addWidget(self.text_label)

        self.set_state(state, text)

    def set_state(self, state: str, text: str | None = None):
        if text is not None:
            self.text_label.setText(text)

        self.setProperty("state", state)

        colors = {
            "ok": "#16883F",
            "idle": "#667085",
            "warning": "#B54708",
            "error": "#D92D20",
        }
        color = colors.get(state, "#667085")

        self.dot.setStyleSheet(f"color:{color};")
        self.text_label.setStyleSheet(f"color:{color};")

        self.style().unpolish(self)
        self.style().polish(self)
