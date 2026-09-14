from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from devices.livox.testing.guided import GuidedDecision, GuidedSubmission


class LidarTestDetailsDialog(QDialog):
    def __init__(self, definition, result=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"LiDAR Test Details — {definition.id}")
        self.resize(820, 720)
        result_data = result.to_dict() if result is not None else {}
        text = QTextEdit()
        text.setReadOnly(True)
        fields = (
            ("ID", definition.id),
            ("Group", definition.group),
            ("Automation Level", definition.automation_level.value),
            ("Purpose", definition.purpose or definition.description),
            ("Precondition", definition.precondition or "-"),
            ("Requirement", definition.requirements or "-"),
            ("Operation Procedure", definition.procedure or "-"),
            ("Expected Result", definition.expected_result or "-"),
            ("Parameters", "\n".join(definition.parameters) or "-"),
            ("Prerequisites", "\n".join(definition.prerequisites) or "None"),
            ("Current Result", result_data.get("status", "NOT_RUN")),
            ("Actual Result", result_data.get("actual_result", "-")),
            ("Evidence", "\n".join(result_data.get("evidence", [])) or "-"),
            ("Source", definition.source or "-"),
            ("Historical Source Status (reference only)", definition.source_status or "-"),
            ("Historical Source Actual Result (reference only)", definition.source_actual_result or "-"),
        )
        text.setPlainText("\n\n".join(f"{label}\n{value}" for label, value in fields))
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)


class LidarGuidedTestDialog(QDialog):
    def __init__(self, request, callback, parent=None):
        super().__init__(parent)
        self.request = request
        self.callback = callback
        self.references = []
        self._submitted = False
        self.setWindowTitle(f"Guided LiDAR Test — {request.test_id}")
        self.resize(780, 680)
        layout = QVBoxLayout(self)
        detail = QTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText(
            f"ID\n{request.test_id}\n\n"
            f"Test\n{request.name}\n\n"
            f"Purpose\n{request.purpose}\n\n"
            f"Precondition\n{request.precondition}\n\n"
            f"Operation Procedure\n{request.procedure}\n\n"
            f"Expected Result\n{request.expected_result}\n\n"
            f"Parameters\n" + ("\n".join(request.parameters) or "-") +
            f"\n\nTimeout\n{request.timeout_sec:.1f} seconds. Timeout is an execution ERROR."
        )
        layout.addWidget(detail, 3)
        layout.addWidget(QLabel("Operator Notes"))
        self.notes = QPlainTextEdit()
        layout.addWidget(self.notes, 1)
        evidence_row = QHBoxLayout()
        self.evidence_label = QLabel("No evidence references attached")
        attach = QPushButton("Attach Evidence Reference")
        attach.clicked.connect(self._attach)
        evidence_row.addWidget(self.evidence_label, 1)
        evidence_row.addWidget(attach)
        layout.addLayout(evidence_row)
        actions = QHBoxLayout()
        for label, decision in (
            ("Confirm PASS", GuidedDecision.CONFIRM),
            ("Reject / FAIL", GuidedDecision.REJECT),
            ("Skip", GuidedDecision.SKIP),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=decision: self._submit(value))
            actions.addWidget(button)
        layout.addLayout(actions)

    def _attach(self):
        path, _ = QFileDialog.getOpenFileName(self, "Attach LiDAR Evidence Reference")
        if path and path not in self.references:
            self.references.append(str(Path(path)))
            self.evidence_label.setText("\n".join(self.references))

    def _submit(self, decision):
        if self._submitted:
            return
        self._submitted = True
        self.callback(
            GuidedSubmission(
                decision=decision,
                notes=self.notes.toPlainText().strip(),
                evidence_references=tuple(self.references),
            )
        )
        self.accept()

    def closeEvent(self, event):
        if not self._submitted:
            self._submit(GuidedDecision.SKIP)
        event.accept()


class LidarGuidedDialogProvider:
    def __init__(self, parent):
        self.parent = parent
        self._dialogs = {}

    def request(self, request, callback):
        dialog = LidarGuidedTestDialog(request, callback, self.parent)
        self._dialogs[request.test_id] = dialog
        dialog.finished.connect(lambda _code, test_id=request.test_id: self._dialogs.pop(test_id, None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def cancel(self, test_id: str):
        dialog = self._dialogs.pop(test_id, None)
        if dialog is not None:
            dialog._submitted = True
            dialog.reject()
