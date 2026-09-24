"""Compact, responsive Audio automation dashboard."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from desktop_app.audio.audio_automation import AudioActionResult, AudioLogEvent
from desktop_app.audio.audio_models import AudioRecordingConfig
from desktop_app.audio.speaker_channel import SpeakerPhysicalVerification
from desktop_app.audio.audio_runtime import (
    AudioIterationCampaignResult,
    AudioIterationResult,
    AudioRuntimeEvent,
    AudioRuntimeMonitor,
    AudioRuntimeSample,
)
from desktop_app.ui.widgets import SectionFrame, StatusChip


class _ExecutionLogHighlighter(QSyntaxHighlighter):
    """Keep the terminal-style log readable without rebuilding its document."""

    def __init__(self, document):
        super().__init__(document)
        self._formats = {
            "default": self._format("#E6EDF3"),
            "pass": self._format("#56D364"),
            "warn": self._format("#E3B341"),
            "fail": self._format("#FF7B72"),
            "command": self._format("#79C0FF"),
        }

    @staticmethod
    def _format(color: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        return fmt

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        fmt = self._formats["default"]
        upper = text.upper()
        if "[FAIL]" in upper or "[ERROR]" in upper:
            fmt = self._formats["fail"]
        elif "[WARN]" in upper or "[WARNING]" in upper:
            fmt = self._formats["warn"]
        elif "[PASS]" in upper:
            fmt = self._formats["pass"]
        elif "COMMAND" in upper:
            fmt = self._formats["command"]
        self.setFormat(0, len(text), fmt)


class AudioAutomatedPage(QWidget):
    """Run reusable Audio actions, runtime monitoring, and capture campaigns."""

    def __init__(
        self,
        audio_manager,
        jetson_service,
        source_provider: Callable[[], str | None],
        evidence_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self.audio_manager = audio_manager
        self.jetson_service = jetson_service
        self.source_provider = source_provider
        self.evidence_manager = evidence_manager or getattr(audio_manager, "evidence_manager", None)
        self._connected = False
        self._last_capture_path: str | None = None
        self._build_ui()

        self.runtime_monitor = AudioRuntimeMonitor(
            jetson_service,
            self.evidence_manager,
            process_state_provider=self._process_state,
            parent=self,
        )
        self.runtime_monitor.sample_received.connect(self._on_runtime_sample)
        self.runtime_monitor.event_detected.connect(self._on_runtime_event)
        self.runtime_monitor.state_changed.connect(self._on_monitor_state)
        self.runtime_monitor.failed.connect(self._on_monitor_failed)
        self.runtime_monitor.finished.connect(self._on_monitor_finished)
        audio_manager.recording_output.connect(
            lambda text: self.runtime_monitor.record_process_output(text, "capture")
        )
        audio_manager.playback_output.connect(
            lambda text: self.runtime_monitor.record_process_output(text, "playback")
        )
        audio_manager.speaker_test_output.connect(
            lambda text: self.runtime_monitor.record_process_output(text, "speaker-test")
        )

        from desktop_app.audio.audio_runtime import AudioIterationRunner

        self.iteration_runner = AudioIterationRunner(
            evidence_manager=self.evidence_manager,
            log_callback=self.audio_manager._automation_log,
            parent=self,
        )
        self.iteration_runner.progress_changed.connect(self._on_iteration_progress)
        self.iteration_runner.iteration_started.connect(self._on_iteration_started)
        self.iteration_runner.iteration_finished.connect(self._on_iteration_finished)
        self.iteration_runner.finished.connect(self._on_iteration_finished_campaign)

        audio_manager.automation_action_started.connect(self._on_action_started)
        audio_manager.automation_action_finished.connect(self._on_action_finished)
        audio_manager.automation_log_event.connect(self._on_log_event)
        audio_manager.speaker_channel_updated.connect(self._on_speaker_channel_updated)
        self.set_connected(jetson_service.is_connected)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        title = QLabel("Audio Automated Test")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Audio measurement, evidence, and reusable reliability actions")
        subtitle.setObjectName("Muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self._key_label("Status"))
        self.test_status_chip = StatusChip("IDLE", "idle")
        self.test_status_chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        status_row.addWidget(self.test_status_chip)
        status_row.addWidget(self._key_label("Phase"))
        self.phase_label = self._value_label("Ready")
        status_row.addWidget(self.phase_label, 1)
        status_row.addWidget(self._key_label("Elapsed"))
        self.test_elapsed_label = self._value_label("00:00:00")
        self.test_elapsed_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        status_row.addWidget(self.test_elapsed_label)
        root.addLayout(status_row)

        state_card = SectionFrame("Device / Audio State", "info")
        state_grid = QGridLayout()
        state_grid.setHorizontalSpacing(12)
        state_grid.setVerticalSpacing(6)
        self._device_labels: dict[str, QLabel] = {}
        state_fields = (
            ("usb", "USB Device"),
            ("alsa", "ALSA Card"),
            ("pulse_source", "Pulse Source"),
            ("pulse_sink", "Pulse Sink"),
            ("default_source", "Default Source"),
            ("default_sink", "Default Sink"),
            ("mixer", "PCM,1"),
            ("session", "Session"),
        )
        for index, (key, label) in enumerate(state_fields):
            row, column = divmod(index, 2)
            base_column = column * 2
            state_grid.addWidget(self._key_label(label), row, base_column)
            value = self._value_label("UNKNOWN")
            self._device_labels[key] = value
            state_grid.addWidget(value, row, base_column + 1)
        self.session_label = self._device_labels["session"]
        self.session_label.setText("NO SESSION")

        state_grid.addWidget(self._key_label("Evidence"), 4, 0)
        self.evidence_label = self._value_label("-")
        state_grid.addWidget(self.evidence_label, 4, 1, 1, 3)
        state_grid.addWidget(self._key_label("Last Capture"), 5, 0)
        self.last_capture_label = self._value_label("-")
        state_grid.addWidget(self.last_capture_label, 5, 1)
        state_grid.addWidget(self._key_label("Last Analysis"), 5, 2)
        self.last_analysis_label = self._value_label("-")
        state_grid.addWidget(self.last_analysis_label, 5, 3)
        for column in (1, 3):
            state_grid.setColumnStretch(column, 1)
        state_card.body_layout.addLayout(state_grid)
        session_actions = QHBoxLayout()
        session_actions.addStretch(1)
        self.new_session_button = self._button("New Session", self.new_session)
        session_actions.addWidget(self.new_session_button)
        state_card.body_layout.addLayout(session_actions)

        runtime_card = SectionFrame("Audio Runtime", "purple")
        runtime_grid = QGridLayout()
        runtime_grid.setHorizontalSpacing(10)
        runtime_grid.setVerticalSpacing(4)
        self.monitor_status_chip = StatusChip("IDLE", "idle")
        self.monitor_status_chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        runtime_grid.addWidget(self._key_label("Monitor"), 0, 0)
        runtime_grid.addWidget(self.monitor_status_chip, 0, 1)
        self._monitor_labels: dict[str, QLabel] = {}
        for row, (key, label, default) in enumerate(
            (
                ("elapsed", "Elapsed", "00:00:00"),
                ("xrun", "XRUN", "0"),
                ("usb_reset", "USB Reset", "0"),
                ("disconnect", "Disconnect", "0"),
                ("audio_error", "Audio Error", "0"),
            ),
            start=1,
        ):
            runtime_grid.addWidget(self._key_label(label), row, 0)
            value = self._value_label(default)
            self._monitor_labels[key] = value
            runtime_grid.addWidget(value, row, 1)
        runtime_grid.setColumnStretch(1, 1)
        runtime_card.body_layout.addLayout(runtime_grid)

        self._runtime_audio_labels: dict[str, QLabel] = {}

        self._runtime_chips: dict[str, StatusChip] = {}
        runtime_actions = QGridLayout()
        runtime_actions.setHorizontalSpacing(8)
        for row, (key, label) in enumerate(
            (("capture", "Capture"), ("playback", "Playback"), ("full_duplex", "Full Duplex"))
        ):
            runtime_actions.addWidget(self._key_label(label), row, 0)
            chip = StatusChip("IDLE", "idle")
            chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            self._runtime_chips[key] = chip
            runtime_actions.addWidget(chip, row, 1)
        runtime_card.body_layout.addLayout(runtime_actions)

        top_row = QGridLayout()
        top_row.setHorizontalSpacing(10)
        top_row.addWidget(state_card, 0, 0)
        top_row.addWidget(runtime_card, 0, 1)
        top_row.setColumnStretch(0, 3)
        top_row.setColumnStretch(1, 2)
        root.addLayout(top_row)

        actions_card = SectionFrame("Actions", "info")
        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(7)
        self.precheck_button = self._button("Pre-check", self.run_precheck, primary=True)
        self.baseline_button = self._button("Collect Baseline", self.run_precheck)
        action_grid.addWidget(self.precheck_button, 0, 0)
        action_grid.addWidget(self.baseline_button, 0, 1)
        action_grid.addWidget(self._key_label("Duration"), 1, 0)
        self.duration_spin = self._spin(1, 3600, 10, " sec")
        action_grid.addWidget(self.duration_spin, 1, 1)
        action_grid.addWidget(self._key_label("Sample Rate"), 2, 0)
        self.sample_rate_combo = self._combo((8000, 16000, 44100, 48000), current=16000, suffix=" Hz")
        action_grid.addWidget(self.sample_rate_combo, 2, 1)
        action_grid.addWidget(self._key_label("Channels"), 3, 0)
        self.channels_spin = self._spin(1, 8, 2)
        action_grid.addWidget(self.channels_spin, 3, 1)
        action_grid.setColumnStretch(1, 1)
        actions_card.body_layout.addLayout(action_grid)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(7)
        self.capture_button = self._button("Capture", self.run_capture, primary=True)
        self.playback_button = self._button("Play Last Capture", self.run_playback, primary=True)
        self.analyze_button = self._button("Analyze Last WAV", self.run_analysis)
        self.full_duplex_button = self._button("Full Duplex", self.run_full_duplex, primary=True)
        self.stop_button = self._button("STOP", self.stop_actions, danger=True)
        buttons.addWidget(self.capture_button, 0, 0)
        buttons.addWidget(self.playback_button, 0, 1)
        buttons.addWidget(self.analyze_button, 1, 0)
        buttons.addWidget(self.full_duplex_button, 1, 1)
        buttons.addWidget(self.stop_button, 2, 0, 1, 2)
        actions_card.body_layout.addLayout(buttons)
        self.auto_analyze_check = QCheckBox("Auto Analyze after Capture")
        self.auto_analyze_check.setChecked(True)
        actions_card.body_layout.addWidget(self.auto_analyze_check)

        analysis_card = SectionFrame("Audio Analyse", "primary")
        self.analysis_card = analysis_card
        self.analysis_empty_label = self._value_label("No successful captured WAV is available.")
        analysis_card.body_layout.addWidget(self.analysis_empty_label)
        self.analysis_summary = QWidget()
        self.analysis_summary.setVisible(False)
        summary_grid = QGridLayout(self.analysis_summary)
        summary_grid.setContentsMargins(0, 0, 0, 0)
        summary_grid.setHorizontalSpacing(10)
        summary_grid.setVerticalSpacing(2)
        self._analysis_summary_labels: dict[str, QLabel] = {}
        for column, (key, title) in enumerate(
            (("file", "File"), ("duration", "Duration"), ("rate", "Rate"), ("channels", "Channels"), ("bit_depth", "Bit Depth"))
        ):
            base_column = column * 2
            summary_grid.addWidget(self._key_label(title), 0, base_column)
            value = self._value_label("-")
            self._analysis_summary_labels[key] = value
            summary_grid.addWidget(value, 0, base_column + 1)
        summary_grid.setColumnStretch(1, 3)
        analysis_card.body_layout.addWidget(self.analysis_summary)
        self.analysis_groups_grid = QGridLayout()
        self.analysis_groups_grid.setHorizontalSpacing(8)
        self.analysis_groups_grid.setVerticalSpacing(8)
        self._analysis_groups: dict[str, tuple[SectionFrame, QGridLayout]] = {}
        for index, (key, label, semantic) in enumerate(
            (
                ("signal", "Signal Level", "info"),
                ("quality", "Audio Quality", "pass"),
                ("channels", "Channel Analysis", "purple"),
                ("format", "Capture Format", "teal"),
            )
        ):
            frame = SectionFrame(label, semantic)
            grid = QGridLayout()
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(2)
            frame.body_layout.addLayout(grid)
            frame.body_layout.setAlignment(grid, Qt.AlignmentFlag.AlignTop)
            frame.setVisible(False)
            self._analysis_groups[key] = (frame, grid)
            self.analysis_groups_grid.addWidget(frame, index // 2, index % 2)
        self.analysis_groups_grid.setColumnStretch(0, 1)
        self.analysis_groups_grid.setColumnStretch(1, 1)
        self.analysis_groups_grid.setRowStretch(0, 1)
        self.analysis_groups_grid.setRowStretch(1, 1)
        analysis_card.body_layout.addLayout(self.analysis_groups_grid)
        root.addWidget(analysis_card)

        critical_card = SectionFrame("Critical Criteria / Result", "warning")
        self.critical_card = critical_card
        self.criteria_grid = QGridLayout()
        self.criteria_grid.setHorizontalSpacing(10)
        self.criteria_grid.setVerticalSpacing(3)
        critical_card.body_layout.addLayout(self.criteria_grid)
        self._set_criteria_empty()

        lower_row = QGridLayout()
        lower_row.setHorizontalSpacing(10)
        lower_row.addWidget(actions_card, 0, 0, Qt.AlignmentFlag.AlignTop)
        lower_row.addWidget(critical_card, 0, 1, Qt.AlignmentFlag.AlignTop)
        lower_row.setColumnStretch(0, 1)
        lower_row.setColumnStretch(1, 1)
        root.addLayout(lower_row)

        speaker_card = SectionFrame("Speaker Channel Test", "info")
        self.speaker_channel_card = speaker_card
        speaker_grid = QGridLayout()
        speaker_grid.setHorizontalSpacing(8)
        speaker_grid.setVerticalSpacing(4)
        speaker_grid.addWidget(self._key_label("Output"), 0, 0)
        self.speaker_output_label = self._value_label("reSpeaker XVF3800")
        speaker_grid.addWidget(self.speaker_output_label, 0, 1)
        speaker_grid.addWidget(self._key_label("Volume"), 0, 2)
        self.speaker_volume_label = self._value_label("L -- / R --")
        speaker_grid.addWidget(self.speaker_volume_label, 0, 3)
        speaker_grid.addWidget(self._key_label("PCM,1"), 1, 0)
        self.speaker_pcm1_label = self._value_label("--")
        speaker_grid.addWidget(self.speaker_pcm1_label, 1, 1)
        speaker_grid.addWidget(self._key_label("Overall"), 1, 2)
        self.speaker_overall_label = self._value_label("NOT RUN")
        speaker_grid.addWidget(self.speaker_overall_label, 1, 3)
        speaker_grid.setColumnStretch(1, 1)
        speaker_grid.setColumnStretch(3, 1)
        speaker_card.body_layout.addLayout(speaker_grid)

        test_buttons = QHBoxLayout()
        self.speaker_left_button = self._button("Test LEFT", lambda: self.run_speaker_channel("LEFT"), primary=True)
        self.speaker_right_button = self._button("Test RIGHT", lambda: self.run_speaker_channel("RIGHT"), primary=True)
        test_buttons.addWidget(self.speaker_left_button)
        test_buttons.addWidget(self.speaker_right_button)
        speaker_card.body_layout.addLayout(test_buttons)

        self._speaker_execution_chips: dict[str, StatusChip] = {}
        self._speaker_verification_labels: dict[str, QLabel] = {}
        self._speaker_verification_buttons: dict[str, list[QPushButton]] = {"LEFT": [], "RIGHT": []}
        verification_grid = QGridLayout()
        verification_grid.setHorizontalSpacing(6)
        verification_grid.setVerticalSpacing(3)
        for row, channel in enumerate(("LEFT", "RIGHT"), start=0):
            verification_grid.addWidget(self._key_label(channel), row, 0)
            execution_chip = StatusChip("NOT RUN", "idle")
            self._speaker_execution_chips[channel] = execution_chip
            verification_grid.addWidget(execution_chip, row, 1)
            physical = self._value_label("NOT VERIFIED")
            self._speaker_verification_labels[channel] = physical
            verification_grid.addWidget(physical, row, 2)
            labels = (("Heard " + channel, SpeakerPhysicalVerification.HEARD_CORRECT_SIDE.value), ("Wrong Side", SpeakerPhysicalVerification.HEARD_WRONG_SIDE.value), ("Both Speakers", SpeakerPhysicalVerification.BOTH_SPEAKERS.value), ("No Sound", SpeakerPhysicalVerification.NO_SOUND.value))
            for column, (label, value) in enumerate(labels, start=3):
                button = self._button(label, lambda checked=False, c=channel, v=value: self.verify_speaker_channel(c, v))
                button.setMinimumWidth(92)
                self._speaker_verification_buttons[channel].append(button)
                verification_grid.addWidget(button, row, column)
        speaker_card.body_layout.addLayout(verification_grid)
        self._reset_speaker_channel_ui()
        root.addWidget(speaker_card)

        reliability_card = SectionFrame("Reliability / Iteration", "teal")
        reliability_grid = QGridLayout()
        reliability_grid.setHorizontalSpacing(8)
        reliability_grid.setVerticalSpacing(7)
        reliability_grid.addWidget(self._key_label("Action"), 0, 0)
        self.iteration_action_combo = QComboBox()
        self.iteration_action_combo.addItem("Capture", "capture")
        self._set_control_height(self.iteration_action_combo)
        reliability_grid.addWidget(self.iteration_action_combo, 0, 1)
        reliability_grid.addWidget(self._key_label("Iterations"), 0, 2)
        self.iteration_count_spin = self._spin(1, 10000, 20)
        reliability_grid.addWidget(self.iteration_count_spin, 0, 3)
        reliability_grid.addWidget(self._key_label("Action Time"), 1, 0)
        self.iteration_duration_spin = self._spin(1, 3600, 5, " sec")
        reliability_grid.addWidget(self.iteration_duration_spin, 1, 1)
        reliability_grid.addWidget(self._key_label("Interval"), 1, 2)
        self.iteration_interval_spin = self._spin(0, 3600, 1, " sec")
        reliability_grid.addWidget(self.iteration_interval_spin, 1, 3)
        self.run_iteration_button = self._button("Run Iteration", self.run_iteration, primary=True)
        self.iteration_stop_button = self._button("Stop", self.stop_actions, danger=True)
        reliability_grid.addWidget(self.run_iteration_button, 2, 0, 1, 2)
        reliability_grid.addWidget(self.iteration_stop_button, 2, 2, 1, 2)
        reliability_grid.setColumnStretch(1, 1)
        reliability_grid.setColumnStretch(3, 1)
        reliability_card.body_layout.addLayout(reliability_grid)
        self.iteration_progress_label = self._value_label("Cycle 0 / 0")
        self.iteration_counts_label = self._value_label("PASS 0   FAIL 0   BLOCKED 0")
        self.iteration_last_label = self._value_label("Last Result: -")
        reliability_card.body_layout.addWidget(self.iteration_progress_label)
        reliability_card.body_layout.addWidget(self.iteration_counts_label)
        reliability_card.body_layout.addWidget(self.iteration_last_label)
        root.addWidget(reliability_card)

        log_card = SectionFrame("Execution Log", "console")
        log_toolbar = QHBoxLayout()
        log_toolbar.addWidget(QLabel("Centralized Audio execution events"))
        log_toolbar.addStretch(1)
        self.log_auto_scroll_check = QCheckBox("Auto-scroll")
        self.log_auto_scroll_check.setChecked(True)
        self.log_auto_scroll_check.toggled.connect(self._set_log_auto_scroll)
        self.log_wrap_check = QCheckBox("Wrap")
        self.log_wrap_check.setChecked(True)
        self.log_wrap_check.toggled.connect(self._set_log_wrap)
        log_toolbar.addWidget(self.log_auto_scroll_check)
        log_toolbar.addWidget(self.log_wrap_check)
        self.clear_log_button = self._button("Clear", self.clear_log)
        self.copy_log_button = self._button("Copy", self.copy_log)
        self.save_log_button = self._button("Save Log", self.save_log)
        log_toolbar.addWidget(self.clear_log_button)
        log_toolbar.addWidget(self.copy_log_button)
        log_toolbar.addWidget(self.save_log_button)
        log_card.body_layout.addLayout(log_toolbar)
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("AudioExecutionLog")
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_text.setMinimumHeight(180)
        self.log_text.setMaximumHeight(260)
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.log_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._log_auto_scroll = True
        self._log_programmatic_scroll = False
        self.log_text.verticalScrollBar().valueChanged.connect(self._on_log_scroll)
        self._log_highlighter = _ExecutionLogHighlighter(self.log_text.document())
        log_card.body_layout.addWidget(self.log_text)
        root.addWidget(log_card)

        self.stop_button.setEnabled(False)
        self.iteration_stop_button.setEnabled(False)
        self._set_analysis_empty()

    @staticmethod
    def _key_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("KeyLabel")
        label.setMinimumHeight(24)
        return label

    @staticmethod
    def _value_label(text: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("ValueLabel")
        label.setMinimumHeight(24)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _set_control_height(widget) -> None:
        widget.setMinimumHeight(30)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @staticmethod
    def _default_sample_rate() -> int:
        return 16000

    @staticmethod
    def _default_channels() -> int:
        return 2

    @classmethod
    def _button(cls, text: str, callback, *, primary: bool = False, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("DangerButton" if danger else "PrimaryButton" if primary else "SmallButton")
        button.setMinimumHeight(30)
        button.setMinimumWidth(108)
        button.clicked.connect(callback)
        return button

    @classmethod
    def _spin(cls, minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSuffix(suffix)
        cls._set_control_height(widget)
        return widget

    @classmethod
    def _combo(cls, values, *, current, suffix: str = "") -> QComboBox:
        widget = QComboBox()
        for value in values:
            widget.addItem(f"{value}{suffix}", value)
        index = widget.findData(current)
        widget.setCurrentIndex(max(0, index))
        cls._set_control_height(widget)
        return widget

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        for button in (
            self.precheck_button,
            self.baseline_button,
            self.capture_button,
            self.playback_button,
            self.analyze_button,
            self.full_duplex_button,
            self.run_iteration_button,
            self.speaker_left_button,
            self.speaker_right_button,
        ):
            button.setEnabled(connected)
        self.new_session_button.setEnabled(connected and not self.iteration_runner.active)
        if not connected:
            for key in ("usb", "alsa", "pulse_source", "pulse_sink", "default_source", "default_sink", "mixer"):
                self._device_labels[key].setText("BLOCKED")
        self._update_session_labels()
        self._update_speaker_channel_controls()

    def run_speaker_channel(self, channel: str) -> None:
        if self._ensure_session():
            self.audio_manager.run_speaker_channel_test(
                channel,
                frequency_hz=500,
                duration_sec=3,
            )

    def verify_speaker_channel(self, channel: str, verification: str) -> None:
        try:
            self.audio_manager.set_speaker_physical_verification(channel, verification)
        except ValueError as exc:
            self._append_local_log("VERIFY", "FAIL", str(exc))

    def _reset_speaker_channel_ui(self) -> None:
        for channel in ("LEFT", "RIGHT"):
            chip = self._speaker_execution_chips[channel]
            chip.set_state("idle", "NOT RUN")
            self._speaker_verification_labels[channel].setText("NOT VERIFIED")
        self.speaker_volume_label.setText("L -- / R --")
        self.speaker_pcm1_label.setText("--")
        self.speaker_overall_label.setText("NOT RUN")
        self.speaker_channel_card.set_semantic("neutral")
        self._update_speaker_channel_controls()

    def _on_speaker_channel_updated(self, summary: dict) -> None:
        for channel, key in (("LEFT", "left"), ("RIGHT", "right")):
            item = summary.get(key, {})
            execution = str(item.get("execution", "NOT RUN"))
            style = {"PASS": "ok", "FAIL": "error", "BLOCKED": "idle", "STOPPED": "idle", "RUNNING": "warning"}.get(execution, "idle")
            self._speaker_execution_chips[channel].set_state(style, execution)
            self._speaker_verification_labels[channel].setText(str(item.get("physical_verification", "NOT VERIFIED")))
        pulse = summary.get("pulse_state") or {}
        left = pulse.get("left_volume_percent")
        right = pulse.get("right_volume_percent")
        self.speaker_volume_label.setText(f"L {left if left is not None else '--'}% / R {right if right is not None else '--'}%")
        pcm = (summary.get("left", {}).get("pcm1_percent") or summary.get("right", {}).get("pcm1_percent"))
        self.speaker_pcm1_label.setText(f"{pcm}% READY" if pcm is not None else "--")
        overall = str(summary.get("overall", "NOT RUN"))
        self.speaker_overall_label.setText(overall)
        self.speaker_channel_card.set_semantic({"PASS": "pass", "FAIL": "error", "BLOCKED": "neutral", "MANUAL VERIFY": "warning"}.get(overall, "neutral"))
        self._update_speaker_channel_controls()

    def _update_speaker_channel_controls(self) -> None:
        active = bool(self.audio_manager.speaker_channel_active)
        connected = self._connected and not active
        self.speaker_left_button.setEnabled(connected)
        self.speaker_right_button.setEnabled(connected)
        results = getattr(self.audio_manager, "speaker_channel_results", {})
        for channel, buttons in self._speaker_verification_buttons.items():
            enabled = not active and results.get(channel, {}).get("execution_status") == "PASS"
            for button in buttons:
                button.setEnabled(enabled)

    def run_precheck(self) -> None:
        if self._ensure_session():
            self.audio_manager.run_audio_precheck()

    def _capture_config(self, prefix: str = "auto_capture", duration: int | None = None) -> AudioRecordingConfig:
        source = self.source_provider() or ""
        filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav"
        if self.evidence_manager is not None and self.evidence_manager.is_active:
            filename = self.evidence_manager.next_capture_name(prefix=prefix)
            output_path = f"~/audio_test_logs/{self.evidence_manager.session_id}/capture/{filename}"
        else:
            output_path = f"~/audio_test_logs/AUTO_AUDIO_{datetime.now().strftime('%Y%m%d_%H%M%S')}/{filename}"
        return AudioRecordingConfig(
            source_name=source,
            output_path=output_path,
            sample_rate=int(self.sample_rate_combo.currentData()),
            channels=self.channels_spin.value(),
            sample_format="S16_LE",
            duration_seconds=duration if duration is not None else self.duration_spin.value(),
        )

    def run_capture(self) -> None:
        if self._ensure_session():
            self.audio_manager.capture_audio(self._capture_config())

    def run_playback(self) -> None:
        if self._ensure_session():
            self.audio_manager.start_automation_playback()

    def run_analysis(self) -> None:
        if not self._ensure_session():
            return
        self.audio_manager.analyze_last_wav()

    def run_full_duplex(self) -> None:
        if self._ensure_session():
            self.audio_manager.start_full_duplex(self._capture_config("full_duplex"))

    def run_iteration(self) -> None:
        if not self._ensure_session() or self.iteration_runner.active:
            return
        if self.iteration_action_combo.currentData() != "capture":
            self._append_local_log("ITERATION", "WARN", "Only Capture is available in this phase.")
            return
        self._start_runtime_monitor()
        started = self.iteration_runner.start(
            action="capture",
            iterations=self.iteration_count_spin.value(),
            interval_sec=self.iteration_interval_spin.value(),
            start_action=lambda _iteration: self.audio_manager.capture_audio(
                self._capture_config("capture_iteration", self.iteration_duration_spin.value())
            ),
            stop_action=self.audio_manager.stop_automation,
            stop_on_failure=False,
        )
        if not started:
            self._stop_runtime_monitor()

    def new_session(self) -> None:
        if self._audio_operation_active() or self.iteration_runner.active:
            self._append_local_log("SESSION", "WARN", "Stop the active Audio operation before starting a new session.")
            return
        if self.evidence_manager is not None and self.evidence_manager.is_active:
            self.evidence_manager.end_session()
        self.audio_manager.reset_speaker_channel_results()
        self._reset_speaker_channel_ui()
        self._ensure_session()

    def _ensure_session(self) -> bool:
        if self.evidence_manager is None:
            return True
        if not self.evidence_manager.is_active:
            try:
                state = getattr(self.jetson_service, "state", None)
                session_id = self.evidence_manager.start_automation_session(
                    host=getattr(state, "host", ""),
                    jetson_info=getattr(state, "jetson_info", None) or {},
                    jetson_connected=self.jetson_service.is_connected,
                )
                self.audio_manager._automation_log("SESSION", "START", session_id)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._append_local_log("SESSION", "FAIL", f"Could not create evidence session: {exc}")
                return False
        self._update_session_labels()
        return True

    def _update_session_labels(self) -> None:
        if self.evidence_manager is None or not self.evidence_manager.is_active:
            self.session_label.setText("NO SESSION")
            self.session_label.setToolTip("No active audio evidence session")
            self.evidence_label.setText("-")
            self.evidence_label.setToolTip("")
            return
        evidence_path = str(self.evidence_manager.evidence_path or "")
        self._set_display_label(self.session_label, self.evidence_manager.session_id or "-", maximum=30)
        self._set_display_label(self.evidence_label, self.evidence_manager.local_display_path, maximum=72)
        self.session_label.setToolTip(evidence_path)
        self.evidence_label.setToolTip(evidence_path)

    def stop_actions(self) -> None:
        if self.iteration_runner.active:
            self.iteration_runner.stop()
        self.audio_manager.stop_automation()
        self._stop_runtime_monitor()

    def clear_log(self) -> None:
        self.audio_manager.automation_logger.clear()
        self.log_text.clear()
        self.log_auto_scroll_check.setChecked(True)

    def copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_text.toPlainText())

    def save_log(self) -> None:
        if self.evidence_manager is not None and self.evidence_manager.evidence_path is not None:
            path = self.evidence_manager.evidence_path
        else:
            stamp = self.audio_manager.automation_logger.session_started_at.strftime("%Y%m%d_%H%M%S")
            path = self.audio_manager.automation_logger.save_session(Path.home() / "audio_test_logs" / f"AUTO_AUDIO_{stamp}")
        self._append_local_log("EVIDENCE", "INFO", f"Execution log saved: {path}")

    def _on_log_event(self, event: AudioLogEvent) -> None:
        self._append_log_line(event.format_line())

    def _append_local_log(self, module: str, level: str, message: str) -> None:
        self._append_log_line(f"{datetime.now().strftime('%H:%M:%S')} [{module}] [{level}] {message}")

    def _append_log_line(self, line: str) -> None:
        self.log_text.appendPlainText(line)
        if self._log_auto_scroll:
            scrollbar = self.log_text.verticalScrollBar()
            self._log_programmatic_scroll = True
            scrollbar.setValue(scrollbar.maximum())
            self._log_programmatic_scroll = False

    def _on_log_scroll(self, value: int) -> None:
        if self._log_programmatic_scroll:
            return
        scrollbar = self.log_text.verticalScrollBar()
        self._log_auto_scroll = value >= scrollbar.maximum() - 2
        self.log_auto_scroll_check.blockSignals(True)
        self.log_auto_scroll_check.setChecked(self._log_auto_scroll)
        self.log_auto_scroll_check.blockSignals(False)

    def _set_log_auto_scroll(self, enabled: bool) -> None:
        self._log_auto_scroll = enabled
        if enabled:
            scrollbar = self.log_text.verticalScrollBar()
            self._log_programmatic_scroll = True
            scrollbar.setValue(scrollbar.maximum())
            self._log_programmatic_scroll = False

    def _set_log_wrap(self, enabled: bool) -> None:
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if enabled else QPlainTextEdit.LineWrapMode.NoWrap
        self.log_text.setLineWrapMode(mode)

    def _on_action_started(self, action: str) -> None:
        self.test_status_chip.set_state("warning", "RUNNING")
        self.phase_label.setText(action.replace("_", " ").title())
        key = self._runtime_key(action)
        if key:
            self._set_runtime(key, "RUNNING")
            self._set_runtime_audio_value(key, "RUNNING")
            if not self.iteration_runner.active:
                self._start_runtime_monitor()
        self.stop_button.setEnabled(True)
        self.iteration_stop_button.setEnabled(self.iteration_runner.active)
        if action == "speaker_channel_test":
            self._update_speaker_channel_controls()

    def _on_action_finished(self, result: AudioActionResult) -> None:
        key = self._runtime_key(result.action)
        if key:
            self._set_runtime(key, result.state)
            self._set_runtime_audio_value(key, result.state)

        if self.iteration_runner.active and result.action == "capture":
            self._last_capture_path = result.data.get("local_path") or result.data.get("output_file") or self._last_capture_path
            self._update_last_capture_label()
            self.iteration_runner.handle_action_result(result)
            self._update_session_labels()
            return

        if result.action == "audio_precheck":
            self._update_precheck_state(result)
            self._set_test_result(result, "Pre-check complete")
        elif result.action == "capture":
            self._last_capture_path = result.data.get("local_path") or result.data.get("output_file") or self._last_capture_path
            self._update_last_capture_label()
            if result.success and self.auto_analyze_check.isChecked():
                self.run_analysis()
            else:
                self._stop_runtime_monitor()
                self._set_test_result(result, "Capture complete")
        elif result.action == "analyze_wav":
            self._update_analysis(result)
            self._stop_runtime_monitor()
            self._set_test_result(result, "Analysis complete")
        elif result.action in {"playback", "full_duplex"}:
            self._stop_runtime_monitor()
            self._set_test_result(result, f"{result.action.replace('_', ' ').title()} complete")
        elif result.action == "speaker_channel_test":
            self._stop_runtime_monitor()
            self._set_test_result(result, "Speaker channel execution complete")
        self._update_session_labels()
        self.stop_button.setEnabled(False)

    def _set_test_result(self, result: AudioActionResult, phase: str) -> None:
        display_state = "PASS" if result.success else "FAIL"
        self.test_status_chip.set_state("ok" if result.success else "error", display_state)
        self.phase_label.setText(phase if result.success else f"{phase} — {result.message}")

    @staticmethod
    def _runtime_key(action: str) -> str | None:
        return {"capture": "capture", "playback": "playback", "full_duplex": "full_duplex"}.get(action)

    def _set_runtime(self, key: str, state: str) -> None:
        chip = self._runtime_chips[key]
        normalized = str(state).upper()
        style = {"PASS": "ok", "RUNNING": "warning", "FAIL": "error", "BLOCKED": "idle", "STOPPED": "idle"}.get(normalized, "idle")
        chip.set_state(style, normalized)

    def _set_runtime_audio_value(self, key: str, value: str) -> None:
        label = self._runtime_audio_labels.get(key)
        if label is not None:
            label.setText(str(value))

    def _update_precheck_state(self, result: AudioActionResult) -> None:
        steps = result.data.get("steps", {})
        for key in ("usb", "alsa", "pulse_source", "pulse_sink"):
            step = steps.get(key, {})
            self._set_device_value(key, str(step.get("state", "UNKNOWN")))
        alsa_data = result.data.get("alsa", {})
        if alsa_data.get("card_index") is not None:
            self._set_device_value("alsa", f"{alsa_data.get('state', 'PASS')} — card {alsa_data['card_index']}")
        pulse = result.data.get("pulse", {})
        self._set_device_value("default_source", pulse.get("default_source") or "NOT FOUND")
        self._set_device_value("default_sink", pulse.get("default_sink") or "NOT FOUND")
        mixer = result.data.get("mixer", {})
        level = mixer.get("final_level_percent")
        mixer_text = f"{level}% READY" if mixer.get("ready") and level is not None else "NOT AVAILABLE"
        self._set_device_value("mixer", mixer_text)
        self._set_runtime_audio_value("pcm1", mixer_text)
        self._set_runtime_audio_value("device", alsa_data.get("card_name") or "reSpeaker XVF3800")

    def _set_device_value(self, key: str, value: str) -> None:
        self._set_display_label(self._device_labels[key], value)

    @staticmethod
    def _set_display_label(label: QLabel, value: object, maximum: int = 42) -> None:
        """Keep dense technical values scannable while preserving the full value."""
        full = str(value)
        display = full if len(full) <= maximum else full[: maximum - 3] + "..."
        label.setText(display)
        label.setToolTip(full)

    def _update_last_capture_label(self) -> None:
        if self._last_capture_path:
            self._set_display_label(self.last_capture_label, Path(self._last_capture_path).name)

    def _set_analysis_empty(self) -> None:
        self.analysis_empty_label.setText("No successful captured WAV is available.")
        self.analysis_empty_label.setVisible(True)
        self.analysis_summary.setVisible(False)
        for frame, grid in self._analysis_groups.values():
            self._clear_grid(grid)
            frame.setVisible(False)
        self._set_criteria_empty()

    @staticmethod
    def _clear_grid(grid: QGridLayout) -> None:
        while grid.count():
            item = grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _populate_analysis_group(self, key: str, rows: list[tuple[str, str]]) -> None:
        frame, grid = self._analysis_groups[key]
        self._clear_grid(grid)
        for row, (label, value) in enumerate(rows):
            grid.addWidget(self._key_label(label), row, 0)
            grid.addWidget(self._value_label(value), row, 1)
        frame.setVisible(bool(rows))

    def _set_criteria_empty(self) -> None:
        self._clear_grid(self.criteria_grid)
        self.criteria_grid.addWidget(self._value_label("No criteria evaluated."), 0, 0, 1, 4)

    def _update_criteria(self, result: AudioActionResult, data: dict) -> None:
        self._clear_grid(self.criteria_grid)
        headers = ("Criterion", "Expected", "Actual", "Result")
        for column, header in enumerate(headers):
            self.criteria_grid.addWidget(self._key_label(header), 0, column)

        actual_format = (
            f"{data.get('sample_rate_hz', data.get('sample_rate'))} Hz / "
            f"{data.get('channels')} ch / {data.get('bit_depth')}-bit"
        )
        rows = [
            ("WAV readability", "metadata readable", "readable" if result.success else result.message, "PASS" if result.success else "FAIL"),
            ("Capture format", "reported", actual_format, "PASS" if result.success else "FAIL"),
        ]
        if result.success:
            rows.extend(
                (
                    ("Clipping", "observation only", "YES" if data.get("clipping_detected") else "NO", "OBSERVED"),
                    ("Channel balance", "observation only", self._format_db(data.get("channel_delta_db")), "OBSERVED"),
                )
            )
        for row, values in enumerate(rows, start=1):
            for column, value in enumerate(values):
                self.criteria_grid.addWidget(self._value_label(str(value)), row, column)
        self.critical_card.set_semantic("pass" if result.success else "error")

    def _update_analysis(self, result: AudioActionResult) -> None:
        if not result.success:
            self._set_analysis_empty()
            self.analysis_empty_label.setText(f"No successful captured WAV is available. {result.message}")
            self.critical_card.set_semantic("error")
            return
        data = result.data
        self.analysis_empty_label.setVisible(False)
        self.analysis_summary.setVisible(True)
        analysis_path = str(data.get("path", "-"))
        summary_values = {
            "file": Path(analysis_path).name,
            "duration": f"{data.get('duration_sec', 0):.3f} s",
            "rate": f"{data.get('sample_rate_hz', data.get('sample_rate'))} Hz",
            "channels": str(data.get("channels", "-")),
            "bit_depth": f"{data.get('bit_depth', '-')}-bit",
        }
        for key, value in summary_values.items():
            self._set_display_label(self._analysis_summary_labels[key], value, maximum=36)
        self._analysis_summary_labels["file"].setToolTip(analysis_path)
        self._set_display_label(self.last_analysis_label, Path(analysis_path).name)
        channels = data.get("channels_data", [])
        peak_values = [channel.get("peak") for channel in channels if channel.get("peak") is not None]
        rms_values = [channel.get("rms") for channel in channels if channel.get("rms") is not None]
        signal_rows: list[tuple[str, str]] = []
        if peak_values:
            signal_rows.append(("Max Peak", self._format_metric(max(peak_values))))
        if rms_values:
            signal_rows.append(("Max RMS", self._format_metric(max(rms_values))))
        if channels:
            signal_rows.append(("Signal", "ZERO" if all(item.get("zero_signal") for item in channels) else "PRESENT"))
        self._populate_analysis_group("signal", signal_rows)

        clipping_counts = [channel.get("clipping_count") or 0 for channel in channels]
        clipping_ratios = [channel.get("clipping_ratio") for channel in channels if channel.get("clipping_ratio") is not None]
        quality_rows: list[tuple[str, str]] = []
        if "clipping_detected" in data:
            quality_rows.append(("Clipping", "YES" if data.get("clipping_detected") else "NO"))
        if clipping_counts:
            quality_rows.append(("Clip Samples", str(sum(clipping_counts))))
        if clipping_ratios:
            quality_rows.append(("Max Clip Ratio", f"{max(clipping_ratios) * 100:.4f}%"))
        self._populate_analysis_group("quality", quality_rows)

        channel_rows: list[tuple[str, str]] = []
        for channel in channels:
            number = channel.get("channel")
            channel_rows.extend(
                (
                    (f"CH{number} Peak", self._format_metric(channel.get("peak"))),
                    (f"CH{number} Peak dBFS", self._format_db(channel.get("peak_dbfs"))),
                    (f"CH{number} RMS", self._format_metric(channel.get("rms"))),
                    (f"CH{number} RMS dBFS", self._format_db(channel.get("rms_dbfs"))),
                    (f"CH{number} Zero Signal", "YES" if channel.get("zero_signal") else "NO"),
                )
            )
        if data.get("channel_delta_db") is not None:
            channel_rows.append(("Channel Delta", self._format_db(data.get("channel_delta_db"))))
        self._populate_analysis_group("channels", channel_rows)

        format_rows: list[tuple[str, str]] = []
        if data.get("sample_rate_hz", data.get("sample_rate")) is not None:
            format_rows.append(("Sample Rate", f"{data.get('sample_rate_hz', data.get('sample_rate'))} Hz"))
        if data.get("channels") is not None:
            format_rows.append(("Channels", str(data.get("channels"))))
        if data.get("bit_depth") is not None:
            format_rows.append(("Bit Depth", f"{data.get('bit_depth')}-bit"))
        if data.get("frame_count") is not None:
            format_rows.append(("Frames", str(data.get("frame_count"))))
        self._populate_analysis_group("format", format_rows)
        self._update_criteria(result, data)

    @staticmethod
    def _format_metric(value) -> str:
        return "n/a" if value is None else f"{value:.6f}"

    @staticmethod
    def _format_db(value) -> str:
        return "-∞ dBFS" if value is None else f"{value:.2f} dBFS"

    def _process_state(self) -> dict[str, bool]:
        return {
            "capture_running": bool(self.audio_manager.recording_active),
            "playback_running": bool(self.audio_manager.playback_active or self.audio_manager.playback_busy),
        }

    def _start_runtime_monitor(self) -> None:
        if self.runtime_monitor.active:
            return
        if self.runtime_monitor.start():
            self.audio_manager._automation_log("MONITOR", "START", "Runtime monitoring started")

    def _stop_runtime_monitor(self) -> None:
        if self.runtime_monitor.active:
            self.runtime_monitor.stop()

    def _on_monitor_state(self, state: str) -> None:
        style = {"RUNNING": "warning", "PASS": "ok", "STOPPED": "idle", "BLOCKED": "idle", "FAIL": "error"}.get(state, "idle")
        self.monitor_status_chip.set_state(style, state)

    def _on_monitor_failed(self, message: str) -> None:
        self.audio_manager._automation_log("MONITOR", "ERROR", message)

    def _on_monitor_finished(self, summary: dict) -> None:
        events = summary.get("events", {})
        self.audio_manager._automation_log(
            "MONITOR",
            "STOP" if summary.get("state") == "STOPPED" else "PASS",
            "Runtime monitor stopped: "
            f"XRUN={events.get('xrun', 0)} USB_RESET={events.get('usb_reset', 0)} "
            f"DISCONNECT={events.get('disconnect', 0)}",
        )

    def _on_runtime_sample(self, sample: AudioRuntimeSample) -> None:
        self._monitor_labels["elapsed"].setText(self._format_elapsed(sample.elapsed_sec))
        self.test_elapsed_label.setText(self._format_elapsed(sample.elapsed_sec))
        self._monitor_labels["xrun"].setText(str(sample.xrun_count))
        self._monitor_labels["usb_reset"].setText(str(sample.usb_reset_count))
        self._monitor_labels["disconnect"].setText(str(sample.disconnect_count))
        self._monitor_labels["audio_error"].setText(str(sample.audio_error_count))

    def _on_runtime_event(self, event: AudioRuntimeEvent) -> None:
        module = {
            "USB_DISCONNECT": "USB",
            "USB_RECONNECT": "USB",
            "USB_RESET": "USB",
            "XRUN": "MONITOR",
            "AUDIO_ERROR": "AUDIO",
            "IO_ERROR": "AUDIO",
        }.get(event.category, "SYSTEM")
        self.audio_manager._automation_log(module, event.severity, event.message)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _on_iteration_started(self, iteration: int, total: int) -> None:
        self.iteration_progress_label.setText(f"Cycle {iteration} / {total}")
        self.iteration_stop_button.setEnabled(True)

    def _on_iteration_progress(self, completed: int, total: int) -> None:
        self.iteration_progress_label.setText(f"Cycle {completed} / {total}")
        self._update_iteration_counts()

    def _on_iteration_finished(self, result: AudioIterationResult) -> None:
        self.iteration_last_label.setText(f"Last Result: #{result.iteration} {result.status} / {result.duration_sec:.2f} sec")
        self._update_iteration_counts()

    def _on_iteration_finished_campaign(self, result: AudioIterationCampaignResult) -> None:
        self._update_iteration_counts(result)
        self.iteration_stop_button.setEnabled(False)
        self._stop_runtime_monitor()
        self.stop_button.setEnabled(False)

    def _update_iteration_counts(self, campaign: AudioIterationCampaignResult | None = None) -> None:
        if campaign is not None:
            self.iteration_progress_label.setText(f"Cycle {campaign.completed} / {campaign.total}")
            self.iteration_counts_label.setText(
                f"PASS {campaign.pass_count}   FAIL {campaign.fail_count}   "
                f"BLOCKED {campaign.blocked_count}   STOPPED {campaign.stopped_count}"
            )
            return
        results = getattr(self.iteration_runner, "_results", [])
        self.iteration_counts_label.setText(
            f"PASS {sum(item.status == 'PASS' for item in results)}   "
            f"FAIL {sum(item.status == 'FAIL' for item in results)}   "
            f"BLOCKED {sum(item.status == 'BLOCKED' for item in results)}"
        )

    def _audio_operation_active(self) -> bool:
        return bool(
            self.audio_manager.busy
            or self.audio_manager.playback_busy
            or self.audio_manager.recording_busy
            or self.audio_manager.speaker_test_active
        )
