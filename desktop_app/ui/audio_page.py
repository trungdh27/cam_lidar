"""Manual audio discovery, routing, playback, and microphone recording page."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from desktop_app.audio.audio_manager import AudioManager, DISCOVERY_COMMANDS
from desktop_app.audio.audio_models import (
    AudioRecordingConfig,
    AudioPlaybackFile,
    AudioDeviceSnapshot,
    RECORDING_CHANNELS,
    RECORDING_FORMATS,
    RECORDING_SAMPLE_RATES,
    RecordingState,
    readable_device_name,
    resolve_device_description,
)
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.widgets import Card, StatusChip


class AudioPage(QWidget):
    """Displays audio endpoints discovered through the shared Jetson service."""

    def __init__(self, jetson_state: JetsonState, jetson_service, parent=None):
        super().__init__(parent)
        self.jetson_state = jetson_state
        self.jetson_service = jetson_service
        self.audio_manager = AudioManager(jetson_service, self)
        self._last_connected: bool | None = None
        self._has_default_sink = False
        self._volume_busy = False
        self._current_default_sink: str | None = None
        self._current_default_source: str | None = None
        self._routing_busy_kind: str | None = None
        self._file_validated = False
        self._validated_path: str | None = None
        self._recording_elapsed_seconds = 0
        self._recording_elapsed_timer = QTimer(self)
        self._recording_elapsed_timer.setInterval(1000)
        self._recording_elapsed_timer.timeout.connect(self._on_recording_timer_tick)
        self._recording_duration_timer = QTimer(self)
        self._recording_duration_timer.setSingleShot(True)
        self._recording_duration_timer.timeout.connect(self.stop_recording)

        self._build_ui()
        self.jetson_state.state_changed.connect(self._on_jetson_state_changed)
        self.audio_manager.started.connect(self._on_discovery_started)
        self.audio_manager.completed.connect(self._on_discovery_completed)
        self.audio_manager.failed.connect(self._on_discovery_failed)
        self.audio_manager.volume_operation_started.connect(self._on_volume_operation_started)
        self.audio_manager.volume_state_ready.connect(self._on_volume_state_ready)
        self.audio_manager.volume_changed.connect(self._on_volume_changed)
        self.audio_manager.mute_changed.connect(self._on_mute_changed)
        self.audio_manager.volume_failed.connect(self._on_volume_failed)
        self.audio_manager.routing_operation_started.connect(self._on_routing_operation_started)
        self.audio_manager.routing_succeeded.connect(self._on_routing_succeeded)
        self.audio_manager.routing_failed.connect(self._on_routing_failed)
        self.audio_manager.validation_started.connect(self._on_validation_started)
        self.audio_manager.file_validated.connect(self._on_file_validated)
        self.audio_manager.validation_failed.connect(self._on_validation_failed)
        self.audio_manager.playback_state_changed.connect(self._on_playback_state_changed)
        self.audio_manager.playback_started.connect(self._on_playback_started)
        self.audio_manager.playback_finished.connect(self._on_playback_finished)
        self.audio_manager.playback_stopped.connect(self._on_playback_stopped)
        self.audio_manager.playback_failed.connect(self._on_playback_failed)
        self.audio_manager.playback_output.connect(self._on_playback_output)
        self.audio_manager.playback_disconnected.connect(self._on_playback_disconnected)
        self.audio_manager.recording_state_changed.connect(self._on_recording_state_changed)
        self.audio_manager.recording_started.connect(self._on_recording_started)
        self.audio_manager.recording_completed.connect(self._on_recording_completed)
        self.audio_manager.recording_failed.connect(self._on_recording_failed)
        self.audio_manager.recording_output.connect(self._on_recording_output)
        self.audio_manager.recording_disconnected.connect(self._on_recording_disconnected)
        self._on_jetson_state_changed(self.jetson_state)

    def _build_ui(self) -> None:
        self.setObjectName("AudioPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("AudioHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 2, 2, 2)
        header_layout.setSpacing(12)
        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        title = QLabel("Audio Test")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Manual Jetson audio validation")
        subtitle.setObjectName("Muted")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        self.connection_chip = StatusChip("Disconnected", "idle")
        self.connection_status_label = self.connection_chip.text_label
        header_layout.addLayout(title_column, 1)
        header_layout.addWidget(self.connection_chip, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("AudioScrollArea")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        content.setObjectName("AudioContent")
        content_layout = QGridLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setHorizontalSpacing(12)
        content_layout.setVerticalSpacing(12)
        content_layout.setColumnStretch(0, 55)
        content_layout.setColumnStretch(1, 45)
        scroll_area.setWidget(content)
        root.addWidget(scroll_area, 1)

        devices_card = Card("Audio Devices")
        self._set_card_accent(devices_card, "blue")
        devices_grid = QGridLayout()
        devices_grid.setHorizontalSpacing(10)
        devices_grid.setVerticalSpacing(8)
        devices_grid.setColumnStretch(1, 1)
        self.default_output_label = self._value_label()
        self.default_input_label = self._value_label()
        self.output_device_combo = QComboBox()
        self.input_device_combo = QComboBox()
        self.output_device_combo.setEnabled(False)
        self.input_device_combo.setEnabled(False)
        self.set_default_output_button = QPushButton("Set Default")
        self.set_default_output_button.setObjectName("SmallButton")
        self.set_default_output_button.clicked.connect(self.set_default_output)
        self.set_default_input_button = QPushButton("Set Default")
        self.set_default_input_button.setObjectName("SmallButton")
        self.set_default_input_button.clicked.connect(self.set_default_input)
        devices_grid.addWidget(self._key_label("Default Output"), 0, 0)
        devices_grid.addWidget(self.default_output_label, 0, 1, 1, 2)
        devices_grid.addWidget(self._key_label("Available Output"), 1, 0)
        devices_grid.addWidget(self.output_device_combo, 1, 1)
        devices_grid.addWidget(self.set_default_output_button, 1, 2)
        devices_grid.addWidget(self._key_label("Default Input"), 2, 0)
        devices_grid.addWidget(self.default_input_label, 2, 1, 1, 2)
        devices_grid.addWidget(self._key_label("Available Input"), 3, 0)
        devices_grid.addWidget(self.input_device_combo, 3, 1)
        devices_grid.addWidget(self.set_default_input_button, 3, 2)
        devices_card.body_layout.addLayout(devices_grid)

        self.advanced_details_toggle = QToolButton()
        self.advanced_details_toggle.setObjectName("DetailsToggle")
        self.advanced_details_toggle.setText("Advanced Device Details  ▶")
        self.advanced_details_toggle.setCheckable(True)
        self.advanced_details_toggle.setChecked(False)
        self.advanced_details_toggle.toggled.connect(self._toggle_advanced_details)
        devices_card.body_layout.addWidget(self.advanced_details_toggle)

        self.advanced_details_widget = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_details_widget)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        self.advanced_device_tabs = QTabWidget()
        self.advanced_device_tabs.setObjectName("AudioAdvancedTabs")
        self.alsa_cards_text = self._read_only_text()
        self.playback_devices_text = self._read_only_text()
        self.capture_devices_text = self._read_only_text()
        self.advanced_device_tabs.addTab(self.alsa_cards_text, "Cards")
        self.advanced_device_tabs.addTab(self.playback_devices_text, "Playback")
        self.advanced_device_tabs.addTab(self.capture_devices_text, "Capture")
        advanced_layout.addWidget(self.advanced_device_tabs)
        self.advanced_details_widget.setVisible(False)
        devices_card.body_layout.addWidget(self.advanced_details_widget)

        devices_actions = QHBoxLayout()
        devices_actions.addStretch()
        self.refresh_button = QPushButton("Refresh Devices")
        self.refresh_button.setObjectName("PrimaryButton")
        self.refresh_button.clicked.connect(self.refresh_devices)
        devices_actions.addWidget(self.refresh_button)
        devices_card.body_layout.addLayout(devices_actions)
        content_layout.addWidget(devices_card, 0, 0)

        playback_card = Card("Speaker Playback")
        self._set_card_accent(playback_card, "blue")
        playback_grid = QGridLayout()
        playback_grid.setHorizontalSpacing(10)
        playback_grid.setVerticalSpacing(8)
        playback_grid.setColumnStretch(1, 1)
        self.playback_file_edit = QLineEdit()
        self.playback_file_edit.setPlaceholderText("/home/user/audio_test/sample.wav")
        self.playback_file_edit.setToolTip("Remote path on Jetson; WAV files only")
        self.playback_file_edit.textChanged.connect(self._on_playback_path_changed)
        self.validate_file_button = QPushButton("Validate")
        self.validate_file_button.setObjectName("SmallButton")
        self.validate_file_button.clicked.connect(self.validate_playback_file)
        playback_file_row = QHBoxLayout()
        playback_file_row.setSpacing(7)
        playback_file_row.addWidget(self.playback_file_edit, 1)
        playback_file_row.addWidget(self.validate_file_button)
        self.current_file_label = self._value_label()
        self.playback_status_chip = StatusChip("Idle", "idle")
        self.playback_status_label = self.playback_status_chip.text_label
        playback_grid.addWidget(self._key_label("Jetson WAV File"), 0, 0)
        playback_grid.addLayout(playback_file_row, 0, 1)
        playback_grid.addWidget(self._key_label("Current File"), 1, 0)
        playback_grid.addWidget(self.current_file_label, 1, 1)
        playback_grid.addWidget(self._key_label("Status"), 2, 0)
        playback_grid.addWidget(self.playback_status_chip, 2, 1)
        playback_card.body_layout.addLayout(playback_grid)
        playback_buttons = QHBoxLayout()
        playback_buttons.addStretch()
        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("PrimaryButton")
        self.play_button.clicked.connect(self.play_audio)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.clicked.connect(self.stop_audio)
        playback_buttons.addWidget(self.play_button)
        playback_buttons.addWidget(self.stop_button)
        playback_card.body_layout.addLayout(playback_buttons)
        content_layout.addWidget(playback_card, 0, 1)

        volume_card = Card("Speaker Control")
        self._set_card_accent(volume_card, "blue")
        volume_grid = QGridLayout()
        volume_grid.setHorizontalSpacing(10)
        volume_grid.setVerticalSpacing(8)
        volume_grid.setColumnStretch(1, 1)
        self.volume_output_label = self._value_label()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(0)
        self.volume_slider.setEnabled(False)
        self.volume_slider.valueChanged.connect(self._on_slider_value_changed)
        self.volume_slider.sliderReleased.connect(self._apply_slider_volume)
        volume_row = QHBoxLayout()
        volume_row.setSpacing(9)
        volume_row.addWidget(self.volume_slider, 1)
        self.volume_percent_label = QLabel("0 %")
        self.volume_percent_label.setObjectName("AudioValueEmphasis")
        volume_row.addWidget(self.volume_percent_label)
        self.high_volume_warning = QLabel("⚠ High volume – speaker distortion may occur.")
        self.high_volume_warning.setObjectName("WarningLabel")
        self.high_volume_warning.setVisible(False)
        self.mute_status_chip = StatusChip("Unavailable", "idle")
        self.mute_status_label = self.mute_status_chip.text_label
        volume_grid.addWidget(self._key_label("Output"), 0, 0)
        volume_grid.addWidget(self.volume_output_label, 0, 1)
        volume_grid.addWidget(self._key_label("Volume"), 1, 0)
        volume_grid.addLayout(volume_row, 1, 1)
        volume_grid.addWidget(self.high_volume_warning, 2, 1)
        volume_grid.addWidget(self._key_label("State"), 3, 0)
        volume_grid.addWidget(self.mute_status_chip, 3, 1)
        volume_card.body_layout.addLayout(volume_grid)
        volume_buttons = QHBoxLayout()
        self.mute_button = QPushButton("Mute")
        self.mute_button.setObjectName("OutlineButton")
        self.unmute_button = QPushButton("Unmute")
        self.unmute_button.setObjectName("OutlineButton")
        self.refresh_volume_button = QPushButton("Refresh")
        self.refresh_volume_button.setObjectName("SmallButton")
        self.mute_button.clicked.connect(self.mute_speaker)
        self.unmute_button.clicked.connect(self.unmute_speaker)
        self.refresh_volume_button.clicked.connect(self.refresh_volume)
        volume_buttons.addWidget(self.mute_button)
        volume_buttons.addWidget(self.unmute_button)
        volume_buttons.addStretch()
        volume_buttons.addWidget(self.refresh_volume_button)
        volume_card.body_layout.addLayout(volume_buttons)
        content_layout.addWidget(volume_card, 1, 0)

        recording_card = Card("Microphone Recording")
        self._set_card_accent(recording_card, "purple")
        recording_grid = QGridLayout()
        recording_grid.setHorizontalSpacing(9)
        recording_grid.setVerticalSpacing(8)
        recording_grid.setColumnStretch(1, 1)
        recording_grid.setColumnStretch(3, 1)
        self.recording_input_label = self._value_label()
        self.recording_input_label.setText("No input audio device available.")
        self.recording_sample_rate_combo = QComboBox()
        for rate in RECORDING_SAMPLE_RATES:
            self.recording_sample_rate_combo.addItem(f"{rate} Hz", rate)
        self.recording_channels_combo = QComboBox()
        for channels in RECORDING_CHANNELS:
            self.recording_channels_combo.addItem(str(channels), channels)
        self.recording_format_combo = QComboBox()
        for display, backend in RECORDING_FORMATS.items():
            self.recording_format_combo.addItem(display, backend)
        self.recording_duration_spin = QSpinBox()
        self.recording_duration_spin.setRange(1, 3600)
        self.recording_duration_spin.setValue(10)
        self.recording_duration_spin.setSuffix(" sec")
        self.recording_manual_check = QCheckBox("Manual Stop Only")
        self.recording_manual_check.toggled.connect(self.recording_duration_spin.setDisabled)
        self.recording_output_edit = QLineEdit()
        self.recording_output_edit.setPlaceholderText("~/audio_test_logs/manual/record_YYYYMMDD_HHMMSS.wav")
        self.recording_output_edit.setToolTip("Remote Jetson path; Phase 3A records WAV files only")
        self.generate_recording_path_button = QPushButton("Generate Path")
        self.generate_recording_path_button.setObjectName("SmallButton")
        self.generate_recording_path_button.clicked.connect(self.generate_recording_path)
        self.recording_status_chip = StatusChip("Idle", "idle")
        self.recording_status_label = self.recording_status_chip.text_label
        self.recording_elapsed_label = self._value_label()
        self.recording_elapsed_label.setText("00:00")
        self.recorded_file_label = self._value_label()
        self.recorded_file_label.setText("-")
        self.recorded_file_size_label = self._value_label()
        self.recorded_file_size_label.setText("-")

        recording_grid.addWidget(self._key_label("Input Device"), 0, 0)
        recording_grid.addWidget(self.recording_input_label, 0, 1, 1, 3)
        recording_grid.addWidget(self._key_label("Sample Rate"), 1, 0)
        recording_grid.addWidget(self.recording_sample_rate_combo, 1, 1)
        recording_grid.addWidget(self._key_label("Channels"), 1, 2)
        recording_grid.addWidget(self.recording_channels_combo, 1, 3)
        recording_grid.addWidget(self._key_label("Format"), 2, 0)
        recording_grid.addWidget(self.recording_format_combo, 2, 1)
        recording_grid.addWidget(self._key_label("Duration"), 2, 2)
        recording_grid.addWidget(self.recording_duration_spin, 2, 3)
        recording_grid.addWidget(self.recording_manual_check, 3, 1, 1, 3)
        recording_grid.addWidget(self._key_label("Jetson Output WAV"), 4, 0)
        recording_grid.addWidget(self.recording_output_edit, 4, 1, 1, 2)
        recording_grid.addWidget(self.generate_recording_path_button, 4, 3)
        recording_grid.addWidget(self._key_label("Recording"), 5, 0)
        recording_grid.addWidget(self.recording_status_chip, 5, 1)
        recording_grid.addWidget(self._key_label("Elapsed"), 5, 2)
        recording_grid.addWidget(self.recording_elapsed_label, 5, 3)
        recording_grid.addWidget(self._key_label("Recorded File"), 6, 0)
        recording_grid.addWidget(self.recorded_file_label, 6, 1, 1, 3)
        recording_grid.addWidget(self._key_label("Size"), 7, 0)
        recording_grid.addWidget(self.recorded_file_size_label, 7, 1, 1, 3)
        recording_card.body_layout.addLayout(recording_grid)
        recording_buttons = QHBoxLayout()
        recording_buttons.addStretch()
        self.start_recording_button = QPushButton("Start Recording")
        self.start_recording_button.setObjectName("PrimaryButton")
        self.start_recording_button.clicked.connect(self.start_recording)
        self.stop_recording_button = QPushButton("Stop")
        self.stop_recording_button.setObjectName("DangerButton")
        self.stop_recording_button.clicked.connect(self.stop_recording)
        recording_buttons.addWidget(self.start_recording_button)
        recording_buttons.addWidget(self.stop_recording_button)
        recording_card.body_layout.addLayout(recording_buttons)
        content_layout.addWidget(recording_card, 1, 1)

        log_card = Card("Status / Log")
        log_header = QHBoxLayout()
        log_hint = QLabel("Live operations and device messages")
        log_hint.setObjectName("Muted")
        clear_log_button = QPushButton("Clear")
        clear_log_button.setObjectName("SmallButton")
        clear_log_button.clicked.connect(self.clear_log)
        log_header.addWidget(log_hint)
        log_header.addStretch()
        log_header.addWidget(clear_log_button)
        log_card.body_layout.addLayout(log_header)
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("LiveLog")
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(140)
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_card.body_layout.addWidget(self.log_text)
        root.addWidget(log_card)
        self._set_recording_controls_enabled()

    @staticmethod
    def _set_card_accent(card: Card, accent: str) -> None:
        card.setProperty("accent", accent)
        card.style().unpolish(card)
        card.style().polish(card)

    def _toggle_advanced_details(self, expanded: bool) -> None:
        self.advanced_details_widget.setVisible(expanded)
        self.advanced_details_toggle.setText(
            "Advanced Device Details  ▼" if expanded else "Advanced Device Details  ▶"
        )

    def clear_log(self) -> None:
        self.log_text.clear()

    @staticmethod
    def _key_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("KeyLabel")
        return label

    @staticmethod
    def _value_label() -> QLabel:
        label = QLabel()
        label.setObjectName("ValueLabel")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _read_only_text() -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setMinimumHeight(78)
        text.setMaximumHeight(110)
        text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return text

    def refresh_devices(self) -> None:
        self.audio_manager.discover_devices()

    def _on_jetson_state_changed(self, _state: JetsonState) -> None:
        connected = self.jetson_service.is_connected
        self.connection_chip.set_state(
            "ok" if connected else "idle",
            "Connected" if connected else "Disconnected",
        )
        self.refresh_button.setEnabled(
            connected and not self.audio_manager.busy and not self.audio_manager.recording_busy
        )
        self._set_routing_controls_enabled(connected)
        self._set_playback_controls_enabled()
        self._set_recording_controls_enabled()
        if not connected:
            self._clear_devices("Unavailable — Jetson is not connected.")
            if self._last_connected is not False:
                self.append_log("Jetson is not connected.")
        elif self._last_connected is False:
            if not self.audio_manager.playback_active:
                self._set_playback_status("Idle")
            self.append_log("Jetson connected. Audio device discovery is ready.")
        self._last_connected = connected

    def _clear_devices(self, message: str) -> None:
        self._current_default_sink = None
        self._current_default_source = None
        self._routing_busy_kind = None
        self.set_default_output_button.setText("Set Default")
        self.set_default_input_button.setText("Set Default")
        self.default_output_label.setText(message)
        self.default_input_label.setText(message)
        self.recording_input_label.setText(message)
        self.recording_input_label.setToolTip("")
        self._populate_combo(self.output_device_combo, (), message)
        self._populate_combo(self.input_device_combo, (), message)
        self.alsa_cards_text.setPlainText(message)
        self.playback_devices_text.setPlainText(message)
        self.capture_devices_text.setPlainText(message)
        self._clear_volume(message)
        self._set_routing_controls_enabled(False)
        self._reset_playback_for_disconnect()
        self._set_recording_controls_enabled()

    def _on_discovery_started(self) -> None:
        self._clear_volume("Discovering output device...")
        self._current_default_sink = None
        self._current_default_source = None
        self._set_routing_controls_enabled(False)
        self._set_playback_controls_enabled()
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Discovering...")
        self.append_log("Audio device discovery started")
        for label, _command in DISCOVERY_COMMANDS:
            self.append_log(f"Running {label}")

    def _on_discovery_completed(self, snapshot: AudioDeviceSnapshot) -> None:
        self.refresh_button.setText("Refresh Devices")
        self.refresh_button.setEnabled(
            self.jetson_service.is_connected and not self.audio_manager.recording_busy
        )
        if not self.jetson_service.is_connected:
            self._clear_devices("Unavailable — Jetson is not connected.")
            self.append_log("Audio discovery result ignored because Jetson disconnected.")
            return
        self._current_default_sink = snapshot.default_sink
        self._current_default_source = snapshot.default_source
        self.default_output_label.setText(self._device_description(snapshot.sinks, snapshot.default_sink, "Not reported"))
        self.default_output_label.setToolTip(snapshot.default_sink or "")
        self.default_input_label.setText(self._device_description(snapshot.sources, snapshot.default_source, "Not reported"))
        self.default_input_label.setToolTip(snapshot.default_source or "")
        self.recording_input_label.setText(
            self._device_description(snapshot.sources, snapshot.default_source, "No input audio device available.")
        )
        self.recording_input_label.setToolTip(snapshot.default_source or "")
        self._populate_combo(self.output_device_combo, snapshot.sinks, "No output devices detected.")
        self._populate_combo(self.input_device_combo, snapshot.sources, "No input devices detected.")
        self._select_combo_data(self.output_device_combo, snapshot.default_sink)
        self._select_combo_data(self.input_device_combo, snapshot.default_source)
        self._set_routing_controls_enabled(True)
        self._set_recording_controls_enabled()
        self._set_device_text(self.alsa_cards_text, snapshot.alsa_cards, "No ALSA cards detected.")
        self._set_device_text(self.playback_devices_text, snapshot.playback_devices, "No ALSA playback devices detected.")
        self._set_device_text(self.capture_devices_text, snapshot.capture_devices, "No ALSA capture devices detected.")

        self._has_default_sink = bool(snapshot.default_sink)
        if snapshot.default_sink:
            self.volume_output_label.setText(readable_device_name(snapshot.default_sink))
            self.volume_output_label.setToolTip(snapshot.default_sink)
            self._set_volume_controls_enabled(True)
        else:
            self._clear_volume("No output audio device available.")

        for label, error in snapshot.command_errors.items():
            self.append_log(f"ERROR: {label}: {error}")
        self.append_log(f"{len(snapshot.sinks)} output devices detected")
        self.append_log(f"{len(snapshot.sources)} input devices detected")
        self.append_log("ALSA device discovery completed")
        if snapshot.has_devices:
            self.append_log("Audio device discovery PASS")
        else:
            self.append_log("No audio devices detected.")
        if self._has_default_sink:
            self.refresh_volume()

    def _on_discovery_failed(self, error: str) -> None:
        self.refresh_button.setText("Refresh Devices")
        self.refresh_button.setEnabled(
            self.jetson_service.is_connected and not self.audio_manager.recording_busy
        )
        self._set_routing_controls_enabled(False)
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    @staticmethod
    def _device_description(devices, name: str | None, fallback: str) -> str:
        if not name:
            return fallback
        return resolve_device_description(devices, name) or readable_device_name(name)

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: str | None) -> None:
        if value is None:
            return
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def _set_routing_controls_enabled(self, connected: bool) -> None:
        can_route = bool(
            connected
            and not self.audio_manager.busy
            and not self.audio_manager.recording_busy
            and self._routing_busy_kind is None
        )
        self.set_default_output_button.setEnabled(can_route and self.output_device_combo.currentData() is not None)
        self.set_default_input_button.setEnabled(can_route and self.input_device_combo.currentData() is not None)

    def _on_playback_path_changed(self, _path: str) -> None:
        self._file_validated = False
        self._validated_path = None
        if not self.audio_manager.playback_active:
            self.current_file_label.setText("-")
            self._set_playback_status("Idle")
        self._set_playback_controls_enabled()

    def validate_playback_file(self) -> None:
        if self.audio_manager.playback_active:
            return
        self.append_log("Validating audio file...")
        self.audio_manager.validate_file(self.playback_file_edit.text())

    def play_audio(self) -> None:
        path = self.playback_file_edit.text().strip()
        if not self._file_validated or path != self._validated_path:
            self.append_log("ERROR: Validate this WAV file before playback.")
            return
        self.current_file_label.setText(path.rsplit("/", 1)[-1] or path)
        self.append_log("Starting speaker playback...")
        self.append_log(f"File: {path}")
        self.audio_manager.play(path)

    def stop_audio(self) -> None:
        if not self.audio_manager.playback_active:
            return
        self.append_log("Stopping playback...")
        self.audio_manager.stop_playback()

    def _set_playback_status(self, status: str) -> None:
        states = {
            "Playing": "ok",
            "Completed": "ok",
            "Failed": "error",
            "Disconnected": "idle",
            "Starting": "warning",
            "Stopping": "warning",
            "Validating": "warning",
            "Ready": "ok",
        }
        self.playback_status_chip.set_state(states.get(status, "idle"), status)

    def _set_playback_controls_enabled(self) -> None:
        connected = self.jetson_service.is_connected
        active = self.audio_manager.playback_active
        recording_busy = self.audio_manager.recording_busy
        short_busy = self.audio_manager.busy
        self.validate_file_button.setEnabled(
            connected and not active and not recording_busy and not short_busy
        )
        self.play_button.setEnabled(
            connected
            and not active
            and not recording_busy
            and not short_busy
            and self._file_validated
            and self.playback_file_edit.text().strip() == self._validated_path
        )
        self.stop_button.setEnabled(connected and active)

    def _on_validation_started(self) -> None:
        self._set_playback_status("Validating")
        self._set_playback_controls_enabled()

    def _on_file_validated(self, audio_file: AudioPlaybackFile) -> None:
        if not self.jetson_service.is_connected:
            self._reset_playback_for_disconnect()
            self.append_log("Audio file validation result ignored because Jetson disconnected.")
            return
        self._validated_path = audio_file.path
        self._file_validated = self.playback_file_edit.text().strip() == audio_file.path
        if not self._file_validated:
            self._validated_path = None
            self._set_playback_status("Idle")
            self._set_playback_controls_enabled()
            self.append_log("Audio file changed while validation was running; validate again.")
            return
        self.current_file_label.setText("-")
        self._set_playback_status("Ready")
        self._set_playback_controls_enabled()
        self.append_log(f"File found: {audio_file.path}")
        self.append_log("Audio file ready for playback")

    def _on_validation_failed(self, error: str) -> None:
        self._file_validated = False
        self._validated_path = None
        self._set_playback_status("Failed" if self.jetson_service.is_connected else "Disconnected")
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    def _on_playback_state_changed(self, state: str) -> None:
        self._set_playback_status(state)
        self._set_playback_controls_enabled()

    def _on_playback_started(self, path: str) -> None:
        self.current_file_label.setText(path.rsplit("/", 1)[-1] or path)
        self.append_log("Playback started")
        self._set_playback_controls_enabled()

    def _on_playback_finished(self, _path: str, _exit_code: int) -> None:
        self.current_file_label.setText("-")
        self.append_log("Playback completed")
        self._set_playback_controls_enabled()

    def _on_playback_stopped(self) -> None:
        self.current_file_label.setText("-")
        self.append_log("Playback stopped")
        self._set_playback_controls_enabled()

    def _on_playback_failed(self, error: str) -> None:
        self.current_file_label.setText("-")
        self._set_playback_status("Failed" if self.jetson_service.is_connected else "Disconnected")
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    def _on_playback_output(self, message: str) -> None:
        self.append_log(message)

    def _on_playback_disconnected(self) -> None:
        self.current_file_label.setText("-")
        self._file_validated = False
        self._validated_path = None
        self._set_playback_status("Disconnected")
        self._set_playback_controls_enabled()
        self.append_log("Jetson disconnected during audio playback.")

    def _reset_playback_for_disconnect(self) -> None:
        self._file_validated = False
        self._validated_path = None
        self.current_file_label.setText("-")
        self._set_playback_status("Disconnected")
        self._set_playback_controls_enabled()

    def generate_recording_path(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.recording_output_edit.setText(
            f"~/audio_test_logs/manual/record_{timestamp}.wav"
        )
        self.append_log(f"Generated remote recording path: {self.recording_output_edit.text()}")

    def _recording_config_from_ui(self) -> AudioRecordingConfig:
        duration = None if self.recording_manual_check.isChecked() else self.recording_duration_spin.value()
        return AudioRecordingConfig(
            source_name=self._current_default_source or "",
            output_path=self.recording_output_edit.text(),
            sample_rate=int(self.recording_sample_rate_combo.currentData()),
            channels=int(self.recording_channels_combo.currentData()),
            sample_format=self.recording_format_combo.currentText(),
            duration_seconds=duration,
        )

    def start_recording(self) -> None:
        if self.audio_manager.recording_busy:
            return
        try:
            config = self._recording_config_from_ui()
        except (TypeError, ValueError):
            self._on_recording_failed("Select a valid microphone recording configuration.")
            return
        self.recorded_file_label.setText("-")
        self.recorded_file_size_label.setText("-")
        self.recording_elapsed_label.setText("00:00")
        self.append_log("Preparing microphone recording...")
        self.append_log(f"Input: {self.recording_input_label.text()}")
        self.append_log(f"Sample rate: {config.sample_rate} Hz")
        self.append_log(f"Channels: {config.channels}")
        self.append_log(f"Format: {config.sample_format}")
        self.append_log(f"Output: {config.output_path}")
        self.audio_manager.start_recording(config)

    def stop_recording(self) -> None:
        if not self.audio_manager.recording_active:
            return
        self.append_log("Stopping microphone recording...")
        self.audio_manager.stop_recording()

    def _set_recording_status(self, status: str) -> None:
        states = {
            "Completed": "ok",
            "Recording": "ok",
            "Failed": "error",
            "Disconnected": "idle",
            "Starting": "warning",
            "Stopping": "warning",
            "Validating": "warning",
        }
        self.recording_status_chip.set_state(states.get(status, "idle"), status)

    def _set_recording_controls_enabled(self) -> None:
        connected = self.jetson_service.is_connected
        recording_busy = self.audio_manager.recording_busy
        recording_active = self.audio_manager.recording_active
        short_busy = self.audio_manager.busy
        can_configure = connected and not recording_busy
        for widget in (
            self.recording_sample_rate_combo,
            self.recording_channels_combo,
            self.recording_format_combo,
            self.recording_duration_spin,
            self.recording_manual_check,
            self.recording_output_edit,
            self.generate_recording_path_button,
        ):
            widget.setEnabled(can_configure)
        has_source = bool(
            self._current_default_source
            and self.input_device_combo.currentData() == self._current_default_source
        )
        self.start_recording_button.setEnabled(
            connected
            and has_source
            and not recording_busy
            and not short_busy
            and not self.audio_manager.playback_active
        )
        self.stop_recording_button.setEnabled(
            connected
            and recording_active
            and self.audio_manager.recording_state == RecordingState.RECORDING
        )

    def _on_recording_timer_tick(self) -> None:
        self._recording_elapsed_seconds += 1
        minutes, seconds = divmod(self._recording_elapsed_seconds, 60)
        self.recording_elapsed_label.setText(f"{minutes:02d}:{seconds:02d}")

    def _on_recording_state_changed(self, state: str) -> None:
        self._set_recording_status(state)
        self._set_recording_controls_enabled()
        self._set_routing_controls_enabled(self.jetson_service.is_connected)
        self._set_playback_controls_enabled()

    def _on_recording_started(self, path: str) -> None:
        self._recording_elapsed_seconds = 0
        self.recording_elapsed_label.setText("00:00")
        self._recording_elapsed_timer.start()
        if not self.recording_manual_check.isChecked():
            self._recording_duration_timer.start(self.recording_duration_spin.value() * 1000)
        self.append_log("Recording started")

    def _stop_recording_timers(self) -> None:
        self._recording_elapsed_timer.stop()
        self._recording_duration_timer.stop()

    def _on_recording_completed(self, result) -> None:
        self._stop_recording_timers()
        self.recorded_file_label.setText(result.path)
        self.recorded_file_size_label.setText(f"{result.size_bytes} bytes")
        self.append_log("Recording stopped")
        self.append_log("Verifying output WAV...")
        self.append_log("Recorded file created successfully")
        self.append_log(f"File size: {result.size_bytes} bytes")
        self._set_recording_controls_enabled()

    def _on_recording_failed(self, error: str) -> None:
        self._stop_recording_timers()
        self._set_recording_status(
            "Disconnected" if not self.jetson_service.is_connected else "Failed"
        )
        self._set_recording_controls_enabled()
        self._set_routing_controls_enabled(self.jetson_service.is_connected)
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    def _on_recording_output(self, message: str) -> None:
        self.append_log(message)

    def _on_recording_disconnected(self) -> None:
        self._stop_recording_timers()
        self._set_recording_status("Disconnected")
        self._set_recording_controls_enabled()
        self._set_routing_controls_enabled(False)
        self._set_playback_controls_enabled()
        self.append_log("Jetson disconnected during microphone recording.")

    def set_default_output(self) -> None:
        sink_name = self.output_device_combo.currentData()
        if not sink_name:
            return
        if sink_name == self._current_default_sink:
            self.append_log("Selected device is already the default output.")
            return
        description = self.output_device_combo.currentText()
        self.append_log("Setting default output device...")
        self.append_log(f"Requested sink: {description}")
        self.audio_manager.set_default_sink(str(sink_name))

    def set_default_input(self) -> None:
        source_name = self.input_device_combo.currentData()
        if not source_name:
            return
        if source_name == self._current_default_source:
            self.append_log("Selected device is already the default input.")
            return
        description = self.input_device_combo.currentText()
        self.append_log("Setting default input device...")
        self.append_log(f"Requested source: {description}")
        self.audio_manager.set_default_source(str(source_name))

    def _on_routing_operation_started(self, kind: str) -> None:
        self._routing_busy_kind = kind
        if kind == "sink":
            self.set_default_output_button.setText("Setting Default Output...")
        else:
            self.set_default_input_button.setText("Setting Default Input...")
        self._set_routing_controls_enabled(False)
        self._set_volume_controls_enabled(False)
        self._set_playback_controls_enabled()

    def _on_routing_succeeded(self, result) -> None:
        self._routing_busy_kind = None
        self.set_default_output_button.setText("Set Default")
        self.set_default_input_button.setText("Set Default")
        if not self.jetson_service.is_connected:
            self.append_log("ERROR: Routing result ignored because Jetson disconnected.")
            return
        label = "output" if result.kind == "sink" else "input"
        self.append_log(f"Default {label} updated successfully")
        self.append_log("Refreshing device state...")
        self.refresh_devices()

    def _on_routing_failed(self, error: str) -> None:
        self._routing_busy_kind = None
        self.set_default_output_button.setText("Set Default")
        self.set_default_input_button.setText("Set Default")
        self._set_routing_controls_enabled(self.jetson_service.is_connected)
        self._set_volume_controls_enabled(self._has_default_sink)
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    def _clear_volume(self, message: str) -> None:
        self._has_default_sink = False
        self._volume_busy = False
        self.volume_output_label.setText(message)
        self.volume_output_label.setToolTip("")
        self.volume_slider.setEnabled(False)
        self.mute_button.setEnabled(False)
        self.unmute_button.setEnabled(False)
        self.refresh_volume_button.setEnabled(False)
        self.mute_status_chip.set_state("idle", "Unavailable")
        self.high_volume_warning.setVisible(False)

    def _set_volume_controls_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled and self.jetson_service.is_connected and not self._volume_busy)
        self.volume_slider.setEnabled(enabled)
        self.mute_button.setEnabled(enabled)
        self.unmute_button.setEnabled(enabled)
        self.refresh_volume_button.setEnabled(enabled)
        self.refresh_button.setEnabled(
            self.jetson_service.is_connected
            and not self.audio_manager.busy
            and not self.audio_manager.recording_busy
        )

    def _on_slider_value_changed(self, value: int) -> None:
        self.volume_percent_label.setText(f"{value} %")
        self.high_volume_warning.setVisible(value >= 80)

    def _apply_slider_volume(self) -> None:
        if not self._has_default_sink or self._volume_busy:
            return
        percent = self.volume_slider.value()
        self.append_log(f"Setting speaker volume to {percent}%")
        self.audio_manager.set_volume(percent)

    def refresh_volume(self) -> None:
        if not self._has_default_sink or not self.jetson_service.is_connected or self._volume_busy:
            return
        self.append_log("Reading speaker volume...")
        self.audio_manager.get_volume_state()

    def mute_speaker(self) -> None:
        if not self._has_default_sink or self._volume_busy:
            return
        self.append_log("Muting speaker...")
        self.audio_manager.mute()

    def unmute_speaker(self) -> None:
        if not self._has_default_sink or self._volume_busy:
            return
        self.append_log("Unmuting speaker...")
        self.audio_manager.unmute()

    def _on_volume_operation_started(self, _kind: str) -> None:
        self._volume_busy = True
        self._set_volume_controls_enabled(False)
        self._set_playback_controls_enabled()

    def _on_volume_state_ready(self, state) -> None:
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
            return
        self._volume_busy = False
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(state.volume_percent)
        self.volume_slider.blockSignals(False)
        self._on_slider_value_changed(state.volume_percent)
        self._set_mute_status(state.muted)
        self._set_volume_controls_enabled(True)
        self._set_playback_controls_enabled()
        self.append_log(f"Current volume: {state.volume_percent}%")
        self.append_log(f"Speaker state: {'Muted' if state.muted else 'Unmuted'}")

    def _on_volume_changed(self, percent: int) -> None:
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
            return
        self._volume_busy = False
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(percent)
        self.volume_slider.blockSignals(False)
        self._on_slider_value_changed(percent)
        self._set_volume_controls_enabled(True)
        self._set_playback_controls_enabled()
        self.append_log(f"Volume updated successfully: {percent}%")

    def _on_mute_changed(self, muted: bool) -> None:
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
            return
        self._volume_busy = False
        self._set_mute_status(muted)
        self._set_volume_controls_enabled(True)
        self._set_playback_controls_enabled()
        self.append_log(f"Speaker {'muted' if muted else 'unmuted'}")

    def _set_mute_status(self, muted: bool) -> None:
        self.mute_status_chip.set_state("warning" if muted else "ok", "Muted" if muted else "Unmuted")

    def _on_volume_failed(self, error: str) -> None:
        self._volume_busy = False
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
        else:
            self._set_volume_controls_enabled(self._has_default_sink)
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    @staticmethod
    def _set_device_text(widget: QPlainTextEdit, devices: list[str], empty_message: str) -> None:
        widget.setPlainText("\n".join(devices) if devices else empty_message)

    @staticmethod
    def _populate_combo(combo: QComboBox, devices, empty_message: str) -> None:
        combo.clear()
        if not devices:
            combo.addItem(empty_message)
            combo.setEnabled(False)
            return
        for device in devices:
            combo.addItem(device.display_name, device.identifier)
        combo.setEnabled(True)

    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{timestamp}] {message}")

    def shutdown(self) -> None:
        """Stop this page's managed audio processes during application exit."""
        self._stop_recording_timers()
        self.audio_manager.shutdown_recording()
        self.audio_manager.shutdown_playback()
