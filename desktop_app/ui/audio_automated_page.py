"""Compact reusable-action dashboard for Audio automated testing."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QComboBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop_app.audio.audio_models import AudioRecordingConfig
from desktop_app.audio.audio_automation import AudioActionResult, AudioLogEvent
from desktop_app.ui.widgets import Card, StatusChip


class AudioAutomatedPage(QWidget):
    """Run reusable Audio capabilities without exposing shell commands in UI code."""

    def __init__(
        self,
        audio_manager,
        jetson_service,
        source_provider: Callable[[], str | None],
        parent=None,
    ):
        super().__init__(parent)
        self.audio_manager = audio_manager
        self.jetson_service = jetson_service
        self.source_provider = source_provider
        self._connected = False
        self._last_capture_path: str | None = None
        self._build_ui()
        audio_manager.automation_action_started.connect(self._on_action_started)
        audio_manager.automation_action_finished.connect(self._on_action_finished)
        audio_manager.automation_log_event.connect(self._on_log_event)
        self.set_connected(jetson_service.is_connected)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        title = QLabel("Audio Automated Test")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Reusable Audio capability actions")
        subtitle.setObjectName("Muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        state_card = Card("Device / Audio State")
        state_grid = QGridLayout()
        state_grid.setHorizontalSpacing(12)
        state_grid.setVerticalSpacing(5)
        self._device_labels: dict[str, QLabel] = {}
        state_fields = (
            ("usb", "USB Device"),
            ("alsa", "ALSA Card"),
            ("pulse_source", "Pulse Source"),
            ("pulse_sink", "Pulse Sink"),
            ("default_source", "Default Source"),
            ("default_sink", "Default Sink"),
            ("mixer", "PCM,1"),
        )
        for row, (key, label) in enumerate(state_fields):
            state_grid.addWidget(self._key_label(label), row, 0)
            value = QLabel("UNKNOWN")
            value.setWordWrap(True)
            value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self._device_labels[key] = value
            state_grid.addWidget(value, row, 1)
        state_card.body_layout.addLayout(state_grid)

        runtime_card = Card("Audio Runtime")
        runtime_grid = QGridLayout()
        runtime_grid.setHorizontalSpacing(12)
        runtime_grid.setVerticalSpacing(5)
        self._runtime_chips: dict[str, StatusChip] = {}
        for row, (key, label) in enumerate(
            (("capture", "Capture"), ("playback", "Playback"), ("full_duplex", "Full Duplex"))
        ):
            runtime_grid.addWidget(self._key_label(label), row, 0)
            chip = StatusChip("IDLE", "idle")
            self._runtime_chips[key] = chip
            runtime_grid.addWidget(chip, row, 1)
        runtime_card.body_layout.addLayout(runtime_grid)

        actions_card = Card("Actions")
        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(6)
        self.precheck_button = self._button("Pre-check", self.run_precheck, primary=True)
        self.capture_button = self._button("Capture", self.run_capture, primary=True)
        self.playback_button = self._button("Play Last Capture", self.run_playback, primary=True)
        self.analyze_button = self._button("Analyze Last WAV", self.run_analysis)
        self.full_duplex_button = self._button("Full Duplex", self.run_full_duplex, primary=True)
        self.stop_button = self._button("Stop", self.stop_actions, danger=True)
        action_grid.addWidget(self.precheck_button, 0, 0)
        action_grid.addWidget(self.capture_button, 0, 1)
        action_grid.addWidget(self.playback_button, 0, 2)
        action_grid.addWidget(self.analyze_button, 1, 0)
        action_grid.addWidget(self.full_duplex_button, 1, 1)
        action_grid.addWidget(self.stop_button, 1, 2)
        action_grid.addWidget(self._key_label("Duration"), 2, 0)
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 3600)
        self.duration_spin.setValue(10)
        self.duration_spin.setSuffix(" sec")
        action_grid.addWidget(self.duration_spin, 2, 1)
        action_grid.addWidget(self._key_label("Sample rate"), 3, 0)
        self.sample_rate_combo = QComboBox()
        for rate in (8000, 16000, 44100, 48000):
            self.sample_rate_combo.addItem(f"{rate} Hz", rate)
        self.sample_rate_combo.setCurrentIndex(1)
        action_grid.addWidget(self.sample_rate_combo, 3, 1)
        action_grid.addWidget(self._key_label("Channels"), 4, 0)
        self.channels_spin = QSpinBox()
        self.channels_spin.setRange(1, 8)
        self.channels_spin.setValue(2)
        action_grid.addWidget(self.channels_spin, 4, 1)
        actions_card.body_layout.addLayout(action_grid)

        analysis_card = Card("Last WAV")
        self.analysis_label = QLabel("No capture analyzed yet.")
        self.analysis_label.setWordWrap(True)
        analysis_card.body_layout.addWidget(self.analysis_label)

        log_card = Card("Execution Log")
        log_toolbar = QHBoxLayout()
        self.clear_log_button = self._button("Clear Log", self.clear_log)
        self.save_log_button = self._button("Save Log", self.save_log)
        log_toolbar.addStretch(1)
        log_toolbar.addWidget(self.clear_log_button)
        log_toolbar.addWidget(self.save_log_button)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(150)
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_card.body_layout.addLayout(log_toolbar)
        log_card.body_layout.addWidget(self.log_text)

        top_row = QHBoxLayout()
        top_row.addWidget(state_card, 1)
        top_row.addWidget(runtime_card, 1)
        root.addLayout(top_row)
        middle_row = QHBoxLayout()
        middle_row.addWidget(actions_card, 1)
        middle_row.addWidget(analysis_card, 1)
        root.addLayout(middle_row)
        root.addWidget(log_card, 1)

    @staticmethod
    def _key_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("KeyLabel")
        return label

    @staticmethod
    def _button(text: str, callback, *, primary: bool = False, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("PrimaryButton")
        elif danger:
            button.setObjectName("DangerButton")
        else:
            button.setObjectName("SmallButton")
        button.clicked.connect(callback)
        return button

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        for button in (
            self.precheck_button,
            self.capture_button,
            self.playback_button,
            self.analyze_button,
            self.full_duplex_button,
        ):
            button.setEnabled(connected)
        self.stop_button.setEnabled(False)
        if not connected:
            self._device_labels["usb"].setText("BLOCKED")
            self._device_labels["alsa"].setText("BLOCKED")
            self._device_labels["pulse_source"].setText("BLOCKED")
            self._device_labels["pulse_sink"].setText("BLOCKED")
            self._device_labels["mixer"].setText("BLOCKED")

    def run_precheck(self) -> None:
        self.audio_manager.run_audio_precheck()

    def _capture_config(self, prefix: str = "auto_capture") -> AudioRecordingConfig:
        source = self.source_provider() or ""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return AudioRecordingConfig(
            source_name=source,
            output_path=f"~/audio_test_logs/AUTO_AUDIO_{stamp}/{prefix}.wav",
            sample_rate=int(self.sample_rate_combo.currentData()),
            channels=self.channels_spin.value(),
            sample_format="S16_LE",
            duration_seconds=self.duration_spin.value(),
        )

    def run_capture(self) -> None:
        self.audio_manager.capture_audio(self._capture_config())

    def run_playback(self) -> None:
        self.audio_manager.start_automation_playback()

    def run_analysis(self) -> None:
        self.audio_manager.analyze_last_wav()

    def run_full_duplex(self) -> None:
        self.audio_manager.start_full_duplex(self._capture_config("full_duplex"))

    def stop_actions(self) -> None:
        self.audio_manager.stop_automation()

    def clear_log(self) -> None:
        self.audio_manager.automation_logger.clear()
        self.log_text.clear()

    def save_log(self) -> None:
        stamp = self.audio_manager.automation_logger.session_started_at.strftime("%Y%m%d_%H%M%S")
        path = self.audio_manager.automation_logger.save_session(
            Path.home() / "audio_test_logs" / f"AUTO_AUDIO_{stamp}"
        )
        self._append_local_log("SYSTEM", "INFO", f"Execution log saved: {path}")

    def _on_log_event(self, event: AudioLogEvent) -> None:
        self.log_text.appendPlainText(event.format_line())
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _append_local_log(self, module: str, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"{stamp} [{module}] [{level}] {message}")

    def _on_action_started(self, action: str) -> None:
        key = self._runtime_key(action)
        if key:
            self._set_runtime(key, "RUNNING")
        self.stop_button.setEnabled(True)

    def _on_action_finished(self, result: AudioActionResult) -> None:
        key = self._runtime_key(result.action)
        if key:
            self._set_runtime(key, result.state)
        if result.action == "audio_precheck":
            self._update_precheck_state(result)
        elif result.action == "capture":
            self._last_capture_path = result.data.get("output_file") or self._last_capture_path
        elif result.action == "analyze_wav":
            self._update_analysis(result)
        self.stop_button.setEnabled(False)

    @staticmethod
    def _runtime_key(action: str) -> str | None:
        return {
            "capture": "capture",
            "playback": "playback",
            "full_duplex": "full_duplex",
        }.get(action)

    def _set_runtime(self, key: str, state: str) -> None:
        chip = self._runtime_chips[key]
        style = {
            "PASS": "ok",
            "RUNNING": "warning",
            "FAIL": "error",
            "BLOCKED": "idle",
            "STOPPED": "idle",
        }.get(state, "idle")
        chip.set_state(style, state)

    def _update_precheck_state(self, result: AudioActionResult) -> None:
        steps = result.data.get("steps", {})
        for key in ("usb", "alsa", "pulse_source", "pulse_sink"):
            step = steps.get(key, {})
            self._device_labels[key].setText(str(step.get("state", "UNKNOWN")))
        alsa_data = result.data.get("alsa", {})
        if alsa_data.get("card_index") is not None:
            self._device_labels["alsa"].setText(
                f"{alsa_data.get('state', 'PASS')} — card {alsa_data['card_index']}"
            )
        pulse = result.data.get("pulse", {})
        self._device_labels["default_source"].setText(pulse.get("default_source") or "NOT FOUND")
        self._device_labels["default_sink"].setText(pulse.get("default_sink") or "NOT FOUND")
        mixer = result.data.get("mixer", {})
        level = mixer.get("final_level_percent")
        self._device_labels["mixer"].setText(
            f"{level}% READY" if mixer.get("ready") and level is not None else "NOT AVAILABLE"
        )

    def _update_analysis(self, result: AudioActionResult) -> None:
        if not result.success:
            self.analysis_label.setText(result.message)
            return
        data = result.data
        self.analysis_label.setText(
            f"File: {data.get('path', '-')}\n"
            f"Duration: {data.get('duration_sec', 0):.2f} sec\n"
            f"Format: {data.get('sample_rate')} Hz / {data.get('channels')} ch / {data.get('bit_depth')}-bit\n"
            f"Peak/RMS: {self._channel_summary(data, 'channel_1')}"
            f"{self._channel_summary(data, 'channel_2')}\n"
            f"Delta: {data.get('channel_delta_db', 'n/a')} dB | Clipping: {data.get('clipping')}"
        )

    @staticmethod
    def _channel_summary(data: dict, key: str) -> str:
        channel = data.get(key)
        if not channel:
            return ""
        number = key.rsplit("_", 1)[-1]
        return f"CH{number} peak={channel['peak']:.4f} rms={channel['rms']:.4f} "
