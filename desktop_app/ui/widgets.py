from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


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
