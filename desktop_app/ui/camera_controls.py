"""Camera-page input controls that avoid accidental wheel edits."""

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox


class _ClickWheelMixin:
    """Enable wheel edits only after an explicit mouse press on this control."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_armed = False

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # QComboBox may briefly move focus to its popup while opening; arm
        # after normal processing so that an explicit click still enables it.
        self._wheel_armed = True

    def focusOutEvent(self, event):
        self._wheel_armed = False
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if not self._wheel_armed:
            # Do not accept this event: Qt can deliver it to the containing
            # scroll area instead of changing a hovered input value.
            event.ignore()
            return
        super().wheelEvent(event)


class ClickWheelComboBox(_ClickWheelMixin, QComboBox):
    pass


class ClickWheelSpinBox(_ClickWheelMixin, QSpinBox):
    pass


class ClickWheelDoubleSpinBox(_ClickWheelMixin, QDoubleSpinBox):
    pass
