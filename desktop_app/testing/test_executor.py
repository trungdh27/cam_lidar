from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from desktop_app.testing.test_result import TestOutcome


class BaseTestExecutor(QObject):
    finished = Signal(object)
    log = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancelled = False

    def start(self, definition, context) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        self.cancelled = True

    def finish(self, outcome: TestOutcome) -> None:
        if not self.cancelled:
            self.finished.emit(outcome)
