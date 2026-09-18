"""Manual audio discovery, routing, playback, and microphone recording page."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from desktop_app.audio.audio_manager import AudioManager, DISCOVERY_COMMANDS
from desktop_app.audio.audio_evidence import AudioEvidenceManager, SessionState
from desktop_app.audio.audio_models import (
    AudioRecordingConfig,
    AudioPlaybackFile,
    AudioDeviceSnapshot,
    RECORDING_CHANNELS,
    RECORDING_FORMATS,
    RECORDING_SAMPLE_RATES,
    PlaybackSource,
    RecordingState,
    readable_device_name,
    resolve_device_description,
    preferred_recording_configuration,
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
        self.evidence_manager = AudioEvidenceManager()
        self._last_connected: bool | None = None
        self._has_default_sink = False
        self._last_confirmed_volume: int | None = None
        self._volume_busy = False
        self._current_default_sink: str | None = None
        self._current_default_source: str | None = None
        self._recording_native_signature: tuple[object, ...] | None = None
        self._routing_busy_kind: str | None = None
        self._file_validated = False
        self._validated_path: str | None = None
        self._validated_input_path: str | None = None
        self._session_baseline_pending = False
        self._session_volume_baseline_pending = False
        self._pending_routing_evidence: dict[str, object] = {}
        self._pending_volume_evidence: dict[str, object] | None = None
        self._pending_volume_refresh = False
        self._pending_mute_evidence: str | None = None
        self._pending_playback_evidence: dict[str, object] = {}
        self._pending_recording_evidence: dict[str, object] = {}
        self._pending_speaker_test_evidence = False
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
        self.audio_manager.speaker_test_state_changed.connect(self._on_speaker_test_state_changed)
        self.audio_manager.speaker_test_prepared.connect(self._on_speaker_test_prepared)
        self.audio_manager.speaker_test_started.connect(self._on_speaker_test_started)
        self.audio_manager.speaker_test_completed.connect(self._on_speaker_test_completed)
        self.audio_manager.speaker_test_failed.connect(self._on_speaker_test_failed)
        self.audio_manager.speaker_test_output.connect(self._on_speaker_test_output)
        self.audio_manager.speaker_test_disconnected.connect(self._on_speaker_test_disconnected)
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

        evidence_bar = QFrame()
        evidence_bar.setObjectName("AudioEvidenceBar")
        evidence_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        evidence_layout = QHBoxLayout(evidence_bar)
        evidence_layout.setContentsMargins(4, 2, 4, 2)
        evidence_layout.setSpacing(8)
        evidence_layout.addWidget(self._key_label("Evidence:"))
        self.session_status_chip = StatusChip("Off", "idle")
        evidence_layout.addWidget(self.session_status_chip)
        self.session_id_label = self._value_label()
        self.session_id_label.setText("-")
        evidence_layout.addWidget(self.session_id_label)
        evidence_layout.addStretch(1)
        self.start_session_button = QPushButton("Start Session")
        self.start_session_button.setObjectName("PrimaryButton")
        self.start_session_button.clicked.connect(self.start_audio_session)
        self.end_session_button = QPushButton("End")
        self.end_session_button.setObjectName("SmallButton")
        self.end_session_button.setToolTip("End Audio Evidence Session")
        self.end_session_button.clicked.connect(self.end_audio_session)
        self.copy_evidence_button = QPushButton("Copy Path")
        self.copy_evidence_button.setObjectName("SmallButton")
        self.copy_evidence_button.clicked.connect(self.copy_evidence_path)
        self.add_note_button = QPushButton("Add Note")
        self.add_note_button.setObjectName("SmallButton")
        self.add_note_button.clicked.connect(self.add_session_note)
        evidence_layout.addWidget(self.start_session_button)
        evidence_layout.addWidget(self.add_note_button)
        evidence_layout.addWidget(self.end_session_button)
        evidence_layout.addWidget(self.copy_evidence_button)
        root.addWidget(evidence_bar)

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
        volume_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
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
        volume_grid.addWidget(self._key_label("State"), 2, 0)
        volume_grid.addWidget(self.mute_status_chip, 2, 1)
        volume_card.body_layout.addLayout(volume_grid)
        volume_buttons = QHBoxLayout()
        self.mute_button = QPushButton("Mute")
        self.mute_button.setObjectName("OutlineButton")
        self.unmute_button = QPushButton("Unmute")
        self.unmute_button.setObjectName("OutlineButton")
        self.refresh_volume_button = QPushButton("Refresh Volume")
        self.refresh_volume_button.setObjectName("SmallButton")
        self.mute_button.clicked.connect(self.mute_speaker)
        self.unmute_button.clicked.connect(self.unmute_speaker)
        self.refresh_volume_button.clicked.connect(self.refresh_volume)
        volume_buttons.addWidget(self.mute_button)
        volume_buttons.addWidget(self.unmute_button)
        volume_buttons.addStretch()
        volume_buttons.addWidget(self.refresh_volume_button)
        volume_card.body_layout.addLayout(volume_buttons)
        self.speaker_test_button = QPushButton("Test Left / Right")
        self.speaker_test_button.setObjectName("SmallButton")
        self.speaker_test_button.clicked.connect(self.start_speaker_channel_test)
        volume_card.body_layout.addWidget(self.speaker_test_button)
        volume_card.body_layout.addWidget(self.high_volume_warning)
        volume_card.body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        content_layout.addWidget(volume_card, 1, 0, Qt.AlignmentFlag.AlignTop)

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
        recording_grid.addWidget(self._key_label("Last Recording"), 6, 0)
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
        recorded_playback_buttons = QHBoxLayout()
        recorded_playback_buttons.addStretch()
        self.play_recording_button = QPushButton("Play Recording")
        self.play_recording_button.setObjectName("PrimaryButton")
        self.play_recording_button.clicked.connect(self.play_recording)
        self.stop_recording_playback_button = QPushButton("Stop")
        self.stop_recording_playback_button.setObjectName("DangerButton")
        self.stop_recording_playback_button.clicked.connect(self.stop_recording_playback)
        recorded_playback_buttons.addWidget(self.play_recording_button)
        recorded_playback_buttons.addWidget(self.stop_recording_playback_button)
        recording_card.body_layout.addLayout(recorded_playback_buttons)
        recorded_playback_status = QHBoxLayout()
        recorded_playback_status.addWidget(self._key_label("Playback"))
        self.recorded_playback_status_chip = StatusChip("Idle", "idle")
        self.recorded_playback_status_label = self.recorded_playback_status_chip.text_label
        recorded_playback_status.addWidget(self.recorded_playback_status_chip)
        recorded_playback_status.addStretch()
        recording_card.body_layout.addLayout(recorded_playback_status)
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
        self._update_session_ui()

    def _audio_operation_active(self) -> bool:
        return bool(
            self.audio_manager.busy
            or self.audio_manager.playback_busy
            or self.audio_manager.recording_busy
            or self.audio_manager.speaker_test_active
        )

    def _update_session_ui(self) -> None:
        state = self.evidence_manager.state
        labels = {
            SessionState.NONE: ("idle", "Off"),
            SessionState.ACTIVE: ("ok", "Active"),
            SessionState.COMPLETED: ("ok", "Saved"),
            SessionState.INTERRUPTED: ("warning", "Interrupted"),
        }
        chip_state, chip_text = labels.get(state, ("idle", "Off"))
        self.session_status_chip.set_state(chip_state, chip_text)
        has_session = state != SessionState.NONE
        self.session_id_label.setText(self.evidence_manager.session_id or "-")
        self.session_id_label.setVisible(has_session)
        self.session_id_label.setToolTip(
            str(self.evidence_manager.evidence_path)
            if self.evidence_manager.evidence_path
            else ""
        )
        active = self.evidence_manager.is_active
        connected = self.jetson_service.is_connected
        self.start_session_button.setEnabled(not active and connected and not self._audio_operation_active())
        self.start_session_button.setVisible(not active)
        self.end_session_button.setEnabled(active and not self._audio_operation_active())
        self.end_session_button.setVisible(active)
        has_evidence = self.evidence_manager.evidence_path is not None
        self.copy_evidence_button.setEnabled(has_evidence)
        self.copy_evidence_button.setVisible(has_evidence)
        self.add_note_button.setEnabled(active)
        self.add_note_button.setVisible(active)

    def start_audio_session(self) -> None:
        if self.evidence_manager.is_active:
            return
        if not self.jetson_service.is_connected:
            self.append_log("ERROR: Connect Jetson before starting an Audio session.")
            return
        session_id = self.evidence_manager.start_session(
            host=self.jetson_state.host,
            jetson_info=self.jetson_state.jetson_info,
            jetson_connected=True,
            default_output=self._current_default_sink,
            default_input=self._current_default_source,
            default_output_display=self.default_output_label.text(),
            default_input_display=self.default_input_label.text(),
            speaker_volume=self.volume_slider.value() if self._has_default_sink else None,
            speaker_muted=self.mute_status_label.text() == "Muted" if self._has_default_sink else None,
        )
        self._session_baseline_pending = True
        self._session_volume_baseline_pending = True
        self._update_session_ui()
        self.append_log(f"Evidence session started: {session_id}")
        self.append_log(f"Evidence path: {self.evidence_manager.local_display_path}")
        if not self.audio_manager.discover_devices():
            self._session_baseline_pending = False
            self._session_volume_baseline_pending = False
            self.evidence_manager.record_operation(
                "DEVICE_SNAPSHOT", "FAILED", error="Unable to start baseline Audio discovery."
            )
            self.append_log("ERROR: Initial Audio device snapshot could not be started.")
        self._update_session_ui()

    def end_audio_session(self) -> None:
        if not self.evidence_manager.is_active:
            return
        if self._audio_operation_active():
            self.append_log("Stop the active Audio operation before ending the session.")
            self._update_session_ui()
            return
        self.evidence_manager.update_state(
            default_output=self._current_default_sink,
            default_input=self._current_default_source,
            speaker_volume=self.volume_slider.value() if self._has_default_sink else None,
            speaker_muted=self.mute_status_label.text() == "Muted" if self._has_default_sink else None,
        )
        self.evidence_manager.end_session()
        self.append_log("Audio session ended")
        self._update_session_ui()

    def add_session_note(self) -> None:
        if not self.evidence_manager.is_active:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Evidence Note")
        dialog.setModal(True)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(12, 12, 12, 12)
        note_edit = QTextEdit()
        note_edit.setPlaceholderText("Manual observation or evidence note")
        note_edit.setMinimumHeight(64)
        note_edit.setMaximumHeight(100)
        dialog_layout.addWidget(note_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog_layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        note = note_edit.toPlainText().strip()
        if not note:
            return
        self.evidence_manager.record_note(note)
        self.append_log(f"Evidence note saved: {note}")

    def copy_evidence_path(self) -> None:
        path = self.evidence_manager.evidence_path
        if path is None:
            return
        QApplication.clipboard().setText(str(path))
        self.append_log(f"Copied local evidence path: {path}")

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
        if not connected and self.evidence_manager.is_active and not self.evidence_manager.is_interrupted:
            self.evidence_manager.mark_interrupted("Jetson disconnected during Audio session.")
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
        self._update_session_ui()

    def _clear_devices(self, message: str) -> None:
        self._current_default_sink = None
        self._current_default_source = None
        self._recording_native_signature = None
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
        if self.evidence_manager.is_active:
            if self._session_baseline_pending:
                self.evidence_manager.record_baseline(snapshot)
                self._session_baseline_pending = False
                self.append_log("Device snapshot saved")
            else:
                self.evidence_manager.record_operation(
                    "DEVICE_REFRESH",
                    "SUCCESS",
                    details={
                        "default_output": snapshot.default_sink,
                        "default_input": snapshot.default_source,
                        "output_count": len(snapshot.sinks),
                        "input_count": len(snapshot.sources),
                        "command_errors": snapshot.command_errors,
                    },
                )
            self.evidence_manager.update_state(
                default_output=snapshot.default_sink,
                default_input=snapshot.default_source,
                default_output_display=self._device_description(snapshot.sinks, snapshot.default_sink, "Not reported"),
                default_input_display=self._device_description(snapshot.sources, snapshot.default_source, "Not reported"),
            )
        self._current_default_sink = snapshot.default_sink
        self._current_default_source = snapshot.default_source
        self.default_output_label.setText(self._device_description(snapshot.sinks, snapshot.default_sink, "Not reported"))
        output_device = self._find_device(snapshot.sinks, snapshot.default_sink)
        self.default_output_label.setToolTip(
            self._device_tooltip(snapshot.default_sink, output_device)
        )
        self.default_input_label.setText(self._device_description(snapshot.sources, snapshot.default_source, "Not reported"))
        input_device = self._find_device(snapshot.sources, snapshot.default_source)
        self.default_input_label.setToolTip(
            self._device_tooltip(snapshot.default_source, input_device)
        )
        self.recording_input_label.setText(
            self._device_description(snapshot.sources, snapshot.default_source, "No input audio device available.")
        )
        self.recording_input_label.setToolTip(
            self._device_tooltip(snapshot.default_source, input_device)
        )
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
            self.volume_output_label.setToolTip(
                self._device_tooltip(snapshot.default_sink, output_device)
            )
            self._set_volume_controls_enabled(True)
        else:
            self._clear_volume("No output audio device available.")

        self._apply_native_recording_configuration(input_device)

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
        if self.evidence_manager.is_active:
            self.evidence_manager.record_operation("DEVICE_REFRESH", "FAILED", error=error)
            self._session_baseline_pending = False
        self.append_log(f"ERROR: {error}")

    @staticmethod
    def _device_description(devices, name: str | None, fallback: str) -> str:
        if not name:
            return fallback
        return resolve_device_description(devices, name) or readable_device_name(name)

    @staticmethod
    def _find_device(devices, name: str | None):
        if not name:
            return None
        return next((device for device in devices if device.identifier == name), None)

    @staticmethod
    def _device_tooltip(name: str | None, device) -> str:
        if not name:
            return ""
        if device and all(
            value is not None
            for value in (device.sample_format, device.channels, device.sample_rate_hz)
        ):
            return (
                f"{name}\n"
                f"Native format: {device.sample_format} / "
                f"{device.channels} ch / {device.sample_rate_hz} Hz"
            )
        return name

    def _apply_native_recording_configuration(self, device) -> None:
        signature = None
        if device:
            signature = (
                device.identifier,
                device.sample_format,
                device.channels,
                device.sample_rate_hz,
            )
        if signature == self._recording_native_signature:
            return
        self._recording_native_signature = signature
        if not device:
            return
        sample_rate, channels, sample_format = preferred_recording_configuration(device)
        if sample_rate is not None:
            self._select_combo_data(self.recording_sample_rate_combo, sample_rate)
        if channels is not None:
            self._select_combo_data(self.recording_channels_combo, channels)
        if sample_format:
            self._select_combo_data(self.recording_format_combo, sample_format.lower())

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
        self._validated_input_path = None
        if not self.audio_manager.playback_active:
            self.current_file_label.setText("-")
            self._set_playback_status("Idle")
        self._set_playback_controls_enabled()

    def validate_playback_file(self) -> None:
        if self.audio_manager.playback_active:
            return
        self._validated_input_path = self.playback_file_edit.text().strip()
        self.append_log("Validating audio file...")
        self.audio_manager.validate_file(self.playback_file_edit.text())

    def play_audio(self) -> None:
        path = self.playback_file_edit.text().strip()
        if not self._file_validated or path != self._validated_input_path:
            self.append_log("ERROR: Validate this WAV file before playback.")
            return
        self.current_file_label.setText(path.rsplit("/", 1)[-1] or path)
        self.append_log("Starting speaker playback...")
        self.append_log(f"Remote file: {self._validated_path or path}")
        self._pending_playback_evidence = {
            "operation": "PLAY_WAV",
            "path": self._validated_path or path,
            "output": self.volume_output_label.text(),
            "volume": self._last_confirmed_volume,
            "muted": self.mute_status_label.text() == "Muted",
        }
        self.evidence_manager.record_operation(
            "PLAY_WAV", "STARTED", details=dict(self._pending_playback_evidence)
        )
        if not self.audio_manager.play(path):
            self.evidence_manager.record_operation(
                "PLAY_WAV", "FAILED", details=dict(self._pending_playback_evidence),
                error="Unable to start speaker playback.",
            )
            self._pending_playback_evidence = {}

    def stop_audio(self) -> None:
        if not self.audio_manager.playback_active:
            return
        self.append_log("Stopping playback...")
        self.evidence_manager.record_operation(
            "STOP_WAV", "STARTED", details={"path": self._pending_playback_evidence.get("path")}
        )
        self.audio_manager.stop_playback()

    def play_recording(self) -> None:
        recording = self.audio_manager.last_recorded_file
        if recording is None or not recording.valid:
            self._set_recorded_playback_controls_enabled()
            return
        filename = self._recording_filename(recording.path)
        self.append_log("Playing recorded audio...")
        self.append_log(f"Remote file: {recording.path}")
        if self.mute_status_chip.text_label.text() == "Muted":
            self.append_log("Speaker output is currently muted.")
        self._pending_playback_evidence = {
            "operation": "PLAY_RECORDING",
            "path": recording.path,
            "output": self.volume_output_label.text(),
            "volume": self._last_confirmed_volume,
            "muted": self.mute_status_label.text() == "Muted",
            "file_size_bytes": recording.size_bytes,
        }
        self.evidence_manager.record_operation(
            "PLAY_RECORDING", "STARTED", details=dict(self._pending_playback_evidence)
        )
        if not self.audio_manager.play_recorded_file():
            self.evidence_manager.record_operation(
                "PLAY_RECORDING", "FAILED", details=dict(self._pending_playback_evidence),
                error="Unable to start recorded audio playback.",
            )
            self._pending_playback_evidence = {}

    def stop_recording_playback(self) -> None:
        if (
            not self.audio_manager.playback_active
            or self.audio_manager.playback_source != PlaybackSource.RECORDED_FILE
        ):
            return
        self.append_log("Stopping recorded audio playback...")
        self.evidence_manager.record_operation(
            "STOP_RECORDED_PLAYBACK", "STARTED",
            details={"path": self._pending_playback_evidence.get("path")},
        )
        self.audio_manager.stop_playback()

    def start_speaker_channel_test(self) -> None:
        if not self._has_default_sink:
            self.append_log("No default output device available.")
            self._set_speaker_test_controls_enabled()
            return
        if self.audio_manager.playback_active or self.audio_manager.recording_busy:
            return
        self.append_log("Starting Left / Right speaker test...")
        self.append_log(f"Output device: {self.volume_output_label.text()}")
        if self.mute_status_chip.text_label.text() == "Muted":
            self.append_log("Speaker is currently muted.")
        self._pending_speaker_test_evidence = True
        self.evidence_manager.record_operation(
            "SPEAKER_LEFT_RIGHT_TEST",
            "STARTED",
            details={
                "default_output": self._current_default_sink,
                "output_display": self.volume_output_label.text(),
                "volume": self._last_confirmed_volume,
                "muted": self.mute_status_label.text() == "Muted",
            },
        )
        if not self.audio_manager.start_speaker_channel_test():
            self.evidence_manager.record_operation(
                "SPEAKER_LEFT_RIGHT_TEST", "FAILED",
                error="Unable to start the Left / Right speaker test.",
            )
            self._pending_speaker_test_evidence = False

    def _on_speaker_test_prepared(self, preparation) -> None:
        details = preparation.as_dict()
        self.evidence_manager.record_operation(
            "SPEAKER_LEFT_RIGHT_TEST_PREPARED", "INFO", details=details
        )
        output = preparation.output_display_name or self.volume_output_label.text()
        self.append_log("Left / Right speaker test")
        self.append_log(f"Output: {output}")
        if preparation.sample_format:
            self.append_log(f"Native format: {preparation.sample_format}")
        self.append_log(f"Channels: {preparation.channels or 2}")
        self.append_log("Test type: Spoken WAV")
        if preparation.prompt_directory:
            self.append_log(f"Prompt directory: {preparation.prompt_directory}")
        if preparation.native_sample_rate_hz is None:
            self.append_log(
                "WARNING: Output native sample rate could not be detected; "
                "using speaker-test default rate."
            )
            self.append_log("Native rate: unknown")
            self.append_log("Command rate: default")
        else:
            self.append_log(
                f"Native rate: {preparation.native_sample_rate_hz} Hz"
            )
            self.append_log(
                f"Command rate: {preparation.command_rate_hz} Hz"
            )
        if preparation.warning and preparation.native_sample_rate_hz is not None:
            self.append_log(f"WARNING: {preparation.warning}")

    def _set_speaker_test_controls_enabled(self) -> None:
        if not hasattr(self, "speaker_test_button"):
            return
        active = self.audio_manager.speaker_test_active
        self.speaker_test_button.setText("Testing L / R..." if active else "Test Left / Right")
        self.speaker_test_button.setEnabled(
            self.jetson_service.is_connected
            and self._has_default_sink
            and not active
            and not self.audio_manager.playback_active
            and not self.audio_manager.recording_busy
            and not self.audio_manager.busy
        )

    def _on_speaker_test_state_changed(self, _state: str) -> None:
        self._set_speaker_test_controls_enabled()
        self._set_volume_controls_enabled(self._has_default_sink)
        self._set_playback_controls_enabled()
        self._set_recording_controls_enabled()
        self._update_session_ui()

    def _on_speaker_test_started(self) -> None:
        preparation = self.audio_manager.speaker_test_preparation
        self.evidence_manager.record_operation(
            "SPEAKER_LEFT_RIGHT_TEST", "SUCCESS",
            details={
                "process_state": "started",
                **(preparation.as_dict() if preparation else {}),
            },
        )
        self.append_log("Speaker Left / Right test started")
        self._set_speaker_test_controls_enabled()

    def _on_speaker_test_completed(self, _exit_code: int) -> None:
        self.evidence_manager.record_operation(
            "SPEAKER_LEFT_RIGHT_TEST_COMPLETED", "SUCCESS", details={"exit_code": _exit_code}
        )
        self._pending_speaker_test_evidence = False
        self.append_log("Speaker Left / Right test completed")
        self._set_speaker_test_controls_enabled()

    def _on_speaker_test_failed(self, error: str) -> None:
        self.evidence_manager.record_operation(
            "SPEAKER_LEFT_RIGHT_TEST", "FAILED", error=error
        )
        self._pending_speaker_test_evidence = False
        self.append_log(f"ERROR: {error}")
        self._set_speaker_test_controls_enabled()

    def _on_speaker_test_output(self, message: str) -> None:
        self.append_log(message)

    def _on_speaker_test_disconnected(self) -> None:
        self.evidence_manager.record_operation(
            "SPEAKER_LEFT_RIGHT_TEST", "FAILED",
            error="Jetson disconnected during Left / Right speaker test.",
        )
        self._pending_speaker_test_evidence = False
        self._set_speaker_test_controls_enabled()
        self.append_log("Jetson disconnected during Left / Right speaker test.")

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

    def _set_recorded_playback_status(self, status: str) -> None:
        states = {
            "Playing": "ok",
            "Completed": "ok",
            "Failed": "error",
            "Disconnected": "idle",
            "Starting": "warning",
            "Stopping": "warning",
            "Idle": "idle",
        }
        self.recorded_playback_status_chip.set_state(states.get(status, "idle"), status)

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
            and self.playback_file_edit.text().strip() == self._validated_input_path
        )
        self.stop_button.setEnabled(
            connected
            and active
            and self.audio_manager.playback_source != PlaybackSource.RECORDED_FILE
        )
        self._set_recorded_playback_controls_enabled()
        self._set_speaker_test_controls_enabled()

    def _set_recorded_playback_controls_enabled(self) -> None:
        if not hasattr(self, "play_recording_button"):
            return
        recording = self.audio_manager.last_recorded_file
        has_valid_recording = bool(recording and recording.valid)
        playback_active = self.audio_manager.playback_active
        self.play_recording_button.setEnabled(
            self.jetson_service.is_connected
            and self._has_default_sink
            and has_valid_recording
            and not playback_active
            and not self.audio_manager.recording_busy
            and not self.audio_manager.busy
        )
        self.stop_recording_playback_button.setEnabled(
            self.jetson_service.is_connected
            and playback_active
            and self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE
        )

    def _on_validation_started(self) -> None:
        self._set_playback_status("Validating")
        self._set_playback_controls_enabled()

    def _on_file_validated(self, audio_file: AudioPlaybackFile) -> None:
        if not self.jetson_service.is_connected:
            self._reset_playback_for_disconnect()
            self.append_log("Audio file validation result ignored because Jetson disconnected.")
            return
        self._validated_path = audio_file.path
        self._file_validated = (
            bool(self._validated_input_path)
            and self.playback_file_edit.text().strip() == self._validated_input_path
        )
        if not self._file_validated:
            self._validated_path = None
            self._validated_input_path = None
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
        self._validated_input_path = None
        self._set_playback_status("Failed" if self.jetson_service.is_connected else "Disconnected")
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    def _on_playback_state_changed(self, state: str) -> None:
        if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE:
            self._set_recorded_playback_status(state)
        else:
            self._set_playback_status(state)
        self._set_playback_controls_enabled()
        self._update_session_ui()

    def _current_playback_evidence_operation(self) -> str:
        pending = self._pending_playback_evidence.get("operation")
        if pending in {"PLAY_WAV", "PLAY_RECORDING"}:
            return str(pending)
        return (
            "PLAY_RECORDING"
            if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE
            else "PLAY_WAV"
        )

    def _on_playback_started(self, path: str) -> None:
        operation = self._current_playback_evidence_operation()
        details = dict(self._pending_playback_evidence)
        details["path"] = path
        details["process_state"] = "started"
        self.evidence_manager.record_operation(operation, "SUCCESS", details=details)
        if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE:
            self._set_recorded_playback_status("Playing")
            self.append_log("Recorded audio playback started")
        else:
            self.current_file_label.setText(path.rsplit("/", 1)[-1] or path)
            self.append_log("Playback started")
        self._set_playback_controls_enabled()

    def _on_playback_finished(self, _path: str, _exit_code: int) -> None:
        operation = (
            "PLAY_RECORDING_COMPLETED"
            if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE
            else "PLAY_WAV_COMPLETED"
        )
        self.evidence_manager.record_operation(
            operation, "SUCCESS", details={"path": _path, "exit_code": _exit_code}
        )
        self._pending_playback_evidence = {}
        if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE:
            self._set_recorded_playback_status("Completed")
            self.append_log("Recorded audio playback completed")
        else:
            self.current_file_label.setText("-")
            self.append_log("Playback completed")
        self._set_playback_controls_enabled()

    def _on_playback_stopped(self) -> None:
        operation = (
            "STOP_RECORDED_PLAYBACK"
            if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE
            else "STOP_WAV"
        )
        self.evidence_manager.record_operation(operation, "SUCCESS", details={"process_state": "stopped"})
        self._pending_playback_evidence = {}
        if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE:
            self._set_recorded_playback_status("Idle")
            self.append_log("Recorded audio playback stopped")
        else:
            self.current_file_label.setText("-")
            self.append_log("Playback stopped")
        self._set_playback_controls_enabled()

    def _on_playback_failed(self, error: str) -> None:
        operation = self._current_playback_evidence_operation()
        self.evidence_manager.record_operation(
            operation, "FAILED", details=dict(self._pending_playback_evidence), error=error
        )
        self._pending_playback_evidence = {}
        if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE:
            self._set_recorded_playback_status(
                "Failed" if self.jetson_service.is_connected else "Disconnected"
            )
        else:
            self.current_file_label.setText("-")
            self._set_playback_status("Failed" if self.jetson_service.is_connected else "Disconnected")
        self._set_playback_controls_enabled()
        self.append_log(f"ERROR: {error}")

    def _on_playback_output(self, message: str) -> None:
        self.append_log(message)

    def _on_playback_disconnected(self) -> None:
        operation = self._current_playback_evidence_operation()
        self.evidence_manager.record_operation(
            operation, "FAILED", details=dict(self._pending_playback_evidence),
            error="Jetson disconnected during audio playback.",
        )
        self._pending_playback_evidence = {}
        if self.audio_manager.playback_source == PlaybackSource.RECORDED_FILE:
            self._set_recorded_playback_status("Disconnected")
            self.append_log("Jetson disconnected during recorded audio playback.")
        else:
            self.current_file_label.setText("-")
            self._file_validated = False
            self._validated_path = None
            self._validated_input_path = None
            self._set_playback_status("Disconnected")
            self.append_log("Jetson disconnected during audio playback.")
        self._set_playback_controls_enabled()

    def _reset_playback_for_disconnect(self) -> None:
        self._file_validated = False
        self._validated_path = None
        self._validated_input_path = None
        self.current_file_label.setText("-")
        self._set_playback_status("Disconnected")
        self._set_playback_controls_enabled()

    @staticmethod
    def _recording_filename(path: str) -> str:
        return path.rsplit("/", 1)[-1] or path

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
        self.recording_elapsed_label.setText("00:00")
        self.append_log("Preparing microphone recording...")
        self.append_log(f"Input: {self.recording_input_label.text()}")
        self.append_log(f"Sample rate: {config.sample_rate} Hz")
        self.append_log(f"Channels: {config.channels}")
        self.append_log(f"Format: {config.sample_format}")
        self.append_log(f"Output: {config.output_path}")
        self._pending_recording_evidence = {
            "input_source": config.source_name,
            "input_display": self.recording_input_label.text(),
            "sample_rate": config.sample_rate,
            "channels": config.channels,
            "format": config.sample_format,
            "duration_seconds": config.duration_seconds,
            "requested_output_path": config.output_path,
        }
        self.evidence_manager.record_operation(
            "START_RECORDING", "STARTED", details=dict(self._pending_recording_evidence)
        )
        if not self.audio_manager.start_recording(config):
            self.evidence_manager.record_operation(
                "START_RECORDING", "FAILED", details=dict(self._pending_recording_evidence),
                error="Unable to start microphone recording.",
            )
            self._pending_recording_evidence = {}
        self._set_recording_controls_enabled()
        self._set_playback_controls_enabled()

    def stop_recording(self) -> None:
        if not self.audio_manager.recording_active:
            return
        self.append_log("Stopping microphone recording...")
        self.evidence_manager.record_operation("STOP_RECORDING", "STARTED")
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
        self._set_recorded_playback_controls_enabled()
        self._set_speaker_test_controls_enabled()

    def _on_recording_timer_tick(self) -> None:
        self._recording_elapsed_seconds += 1
        minutes, seconds = divmod(self._recording_elapsed_seconds, 60)
        self.recording_elapsed_label.setText(f"{minutes:02d}:{seconds:02d}")

    def _on_recording_state_changed(self, state: str) -> None:
        self._set_recording_status(state)
        self._set_recording_controls_enabled()
        self._set_routing_controls_enabled(self.jetson_service.is_connected)
        self._set_playback_controls_enabled()
        self._update_session_ui()

    def _on_recording_started(self, path: str) -> None:
        self._recording_elapsed_seconds = 0
        self.recording_elapsed_label.setText("00:00")
        self._recording_elapsed_timer.start()
        if not self.recording_manual_check.isChecked():
            self._recording_duration_timer.start(self.recording_duration_spin.value() * 1000)
        details = dict(self._pending_recording_evidence)
        details["remote_path"] = path
        self.evidence_manager.record_operation("START_RECORDING", "SUCCESS", details=details)
        self.append_log("Recording started")

    def _stop_recording_timers(self) -> None:
        self._recording_elapsed_timer.stop()
        self._recording_duration_timer.stop()

    def _on_recording_completed(self, result) -> None:
        self._stop_recording_timers()
        filename = self._recording_filename(result.path)
        self.recorded_file_label.setText(filename)
        self.recorded_file_label.setToolTip(result.path)
        self.recorded_file_size_label.setText(
            f"{result.size_bytes:,} bytes ({result.size_bytes / 1000:.0f} KB)"
        )
        self.append_log("Recording completed")
        self.append_log("Recorded WAV verified")
        self.append_log(f"Display file: {filename}")
        self.append_log(f"Remote path: {result.path}")
        self.append_log(f"Size: {result.size_bytes} bytes")
        recording_details = dict(self._pending_recording_evidence)
        recording_details.update(
            {"remote_path": result.path, "size_bytes": result.size_bytes, "verified": result.valid}
        )
        self.evidence_manager.record_operation(
            "RECORDING_COMPLETED", "SUCCESS", details=recording_details
        )
        self.evidence_manager.record_operation(
            "STOP_RECORDING", "SUCCESS", details={"remote_path": result.path, "size_bytes": result.size_bytes}
        )
        self._pending_recording_evidence = {}
        self._set_recording_controls_enabled()

    def _on_recording_failed(self, error: str) -> None:
        details = dict(self._pending_recording_evidence)
        self.evidence_manager.record_operation("START_RECORDING", "FAILED", details=details, error=error)
        self._pending_recording_evidence = {}
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
        self.evidence_manager.record_operation(
            "START_RECORDING", "FAILED", details=dict(self._pending_recording_evidence),
            error="Jetson disconnected during microphone recording.",
        )
        self._pending_recording_evidence = {}
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
        self._pending_routing_evidence["sink"] = {
            "previous": self._current_default_sink,
            "requested": str(sink_name),
            "requested_display": description,
        }
        if not self.audio_manager.set_default_sink(str(sink_name)):
            self.evidence_manager.record_operation(
                "SET_DEFAULT_OUTPUT", "FAILED", details=self._pending_routing_evidence.pop("sink", {}),
                error="Unable to start default output routing operation.",
            )

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
        self._pending_routing_evidence["source"] = {
            "previous": self._current_default_source,
            "requested": str(source_name),
            "requested_display": description,
        }
        if not self.audio_manager.set_default_source(str(source_name)):
            self.evidence_manager.record_operation(
                "SET_DEFAULT_INPUT", "FAILED", details=self._pending_routing_evidence.pop("source", {}),
                error="Unable to start default input routing operation.",
            )

    def _on_routing_operation_started(self, kind: str) -> None:
        self._routing_busy_kind = kind
        if kind == "sink":
            self.set_default_output_button.setText("Setting Default Output...")
        else:
            self.set_default_input_button.setText("Setting Default Input...")
        self._set_routing_controls_enabled(False)
        self._set_volume_controls_enabled(False)
        self._set_playback_controls_enabled()
        self._update_session_ui()

    def _on_routing_succeeded(self, result) -> None:
        operation = "SET_DEFAULT_OUTPUT" if result.kind == "sink" else "SET_DEFAULT_INPUT"
        details = dict(self._pending_routing_evidence.pop(result.kind, {}))
        details.update(
            {
                "confirmed_default_output": result.default_sink,
                "confirmed_default_input": result.default_source,
            }
        )
        self._routing_busy_kind = None
        self.set_default_output_button.setText("Set Default")
        self.set_default_input_button.setText("Set Default")
        if not self.jetson_service.is_connected:
            self.evidence_manager.record_operation(
                operation, "FAILED", details=details,
                error="Routing result ignored because Jetson disconnected.",
            )
            self.append_log("ERROR: Routing result ignored because Jetson disconnected.")
            return
        label = "output" if result.kind == "sink" else "input"
        self.evidence_manager.record_operation(operation, "SUCCESS", details=details)
        self.evidence_manager.update_state(
            default_output=result.default_sink,
            default_input=result.default_source,
            default_output_display=self.output_device_combo.currentText() if result.kind == "sink" else self.default_output_label.text(),
            default_input_display=self.input_device_combo.currentText() if result.kind == "source" else self.default_input_label.text(),
        )
        self.append_log(f"Default {label} updated successfully")
        self.append_log("Refreshing device state...")
        self.refresh_devices()

    def _on_routing_failed(self, error: str) -> None:
        kind = self._routing_busy_kind or ""
        self._routing_busy_kind = None
        self.set_default_output_button.setText("Set Default")
        self.set_default_input_button.setText("Set Default")
        self._set_routing_controls_enabled(self.jetson_service.is_connected)
        self._set_volume_controls_enabled(self._has_default_sink)
        self._set_playback_controls_enabled()
        if kind in {"sink", "source"}:
            operation = "SET_DEFAULT_OUTPUT" if kind == "sink" else "SET_DEFAULT_INPUT"
            self.evidence_manager.record_operation(
                operation,
                "FAILED",
                details=self._pending_routing_evidence.pop(kind, {}),
                error=error,
            )
        self.append_log(f"ERROR: {error}")

    def _clear_volume(self, message: str) -> None:
        self._has_default_sink = False
        self._volume_busy = False
        self._last_confirmed_volume = None
        self.volume_output_label.setText(message)
        self.volume_output_label.setToolTip("")
        self.volume_slider.setEnabled(False)
        self.mute_button.setEnabled(False)
        self.unmute_button.setEnabled(False)
        self.refresh_volume_button.setEnabled(False)
        self.mute_status_chip.set_state("idle", "Unavailable")
        self.high_volume_warning.setVisible(False)
        self._set_speaker_test_controls_enabled()

    def _set_volume_controls_enabled(self, enabled: bool) -> None:
        enabled = bool(
            enabled
            and self.jetson_service.is_connected
            and not self._volume_busy
            and not self.audio_manager.speaker_test_active
        )
        self.volume_slider.setEnabled(enabled)
        self.mute_button.setEnabled(enabled)
        self.unmute_button.setEnabled(enabled)
        self.refresh_volume_button.setEnabled(enabled)
        self.refresh_button.setEnabled(
            self.jetson_service.is_connected
            and not self.audio_manager.busy
            and not self.audio_manager.recording_busy
        )
        self._set_recorded_playback_controls_enabled()
        self._set_speaker_test_controls_enabled()

    def _on_slider_value_changed(self, value: int) -> None:
        self.volume_percent_label.setText(f"{value} %")
        self.high_volume_warning.setVisible(value >= 80)

    def _apply_slider_volume(self) -> None:
        if not self._has_default_sink or self._volume_busy:
            return
        percent = self.volume_slider.value()
        self.append_log(f"Setting speaker volume to {percent}%")
        self._pending_volume_evidence = {
            "previous": self._last_confirmed_volume,
            "requested": percent,
        }
        if not self.audio_manager.set_volume(percent):
            self.evidence_manager.record_operation(
                "SET_VOLUME", "FAILED", details=self._pending_volume_evidence or {},
                error="Unable to start volume operation.",
            )
            self._pending_volume_evidence = None

    def refresh_volume(self) -> None:
        if not self._has_default_sink or not self.jetson_service.is_connected or self._volume_busy:
            return
        self.append_log("Reading speaker volume...")
        self._pending_volume_refresh = True
        if not self.audio_manager.get_volume_state():
            if self._pending_volume_refresh:
                self._pending_volume_refresh = False
                self.evidence_manager.record_operation(
                    "REFRESH_VOLUME", "FAILED", error="Unable to start volume refresh operation."
                )

    def mute_speaker(self) -> None:
        if not self._has_default_sink or self._volume_busy:
            return
        self.append_log("Muting speaker...")
        self._pending_mute_evidence = "MUTE"
        if not self.audio_manager.mute():
            self.evidence_manager.record_operation("MUTE", "FAILED", error="Unable to start mute operation.")
            self._pending_mute_evidence = None

    def unmute_speaker(self) -> None:
        if not self._has_default_sink or self._volume_busy:
            return
        self.append_log("Unmuting speaker...")
        self._pending_mute_evidence = "UNMUTE"
        if not self.audio_manager.unmute():
            self.evidence_manager.record_operation("UNMUTE", "FAILED", error="Unable to start unmute operation.")
            self._pending_mute_evidence = None

    def _on_volume_operation_started(self, _kind: str) -> None:
        self._volume_busy = True
        self._set_volume_controls_enabled(False)
        self._set_playback_controls_enabled()
        self._update_session_ui()

    def _on_volume_state_ready(self, state) -> None:
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
            return
        self._volume_busy = False
        self._last_confirmed_volume = state.volume_percent
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(state.volume_percent)
        self.volume_slider.blockSignals(False)
        self._on_slider_value_changed(state.volume_percent)
        self._set_mute_status(state.muted)
        self._set_volume_controls_enabled(True)
        self._set_playback_controls_enabled()
        self.evidence_manager.update_state(
            speaker_volume=state.volume_percent,
            speaker_muted=state.muted,
        )
        if self.evidence_manager.is_active:
            self.evidence_manager.record_operation(
                "REFRESH_VOLUME",
                "SUCCESS",
                details={
                    "confirmed_volume": state.volume_percent,
                    "muted": state.muted,
                    "automatic": self._session_volume_baseline_pending,
                },
            )
            self._session_volume_baseline_pending = False
        self._pending_volume_refresh = False
        self.append_log(f"Current volume: {state.volume_percent}%")
        self.append_log(f"Speaker state: {'Muted' if state.muted else 'Unmuted'}")

    def _on_volume_changed(self, percent: int) -> None:
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
            return
        self._volume_busy = False
        self._last_confirmed_volume = percent
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(percent)
        self.volume_slider.blockSignals(False)
        self._on_slider_value_changed(percent)
        self._set_volume_controls_enabled(True)
        self._set_playback_controls_enabled()
        volume_details = dict(self._pending_volume_evidence or {})
        volume_details["confirmed"] = percent
        if percent >= 80:
            volume_details["warning"] = "High volume – speaker distortion may occur."
        self.evidence_manager.update_state(speaker_volume=percent)
        self.evidence_manager.record_operation("SET_VOLUME", "SUCCESS", details=volume_details)
        self._pending_volume_evidence = None
        self.append_log(f"Volume updated successfully: {percent}%")

    def _on_mute_changed(self, muted: bool) -> None:
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
            return
        self._volume_busy = False
        self._set_mute_status(muted)
        self._set_volume_controls_enabled(True)
        self._set_playback_controls_enabled()
        operation = self._pending_mute_evidence or ("MUTE" if muted else "UNMUTE")
        self.evidence_manager.update_state(speaker_muted=muted)
        self.evidence_manager.record_operation(
            operation, "SUCCESS", details={"muted": muted}
        )
        self._pending_mute_evidence = None
        self.append_log(f"Speaker {'muted' if muted else 'unmuted'}")

    def _set_mute_status(self, muted: bool) -> None:
        self.mute_status_chip.set_state("warning" if muted else "ok", "Muted" if muted else "Unmuted")

    def _on_volume_failed(self, error: str) -> None:
        pending_volume = self._pending_volume_evidence
        pending_mute = self._pending_mute_evidence
        pending_refresh = self._pending_volume_refresh
        self._pending_volume_evidence = None
        self._pending_mute_evidence = None
        self._pending_volume_refresh = False
        self._volume_busy = False
        if not self.jetson_service.is_connected:
            self._clear_volume("Unavailable — Jetson is not connected.")
        else:
            self._set_volume_controls_enabled(self._has_default_sink)
        self._set_playback_controls_enabled()
        if pending_volume is not None:
            self.evidence_manager.record_operation("SET_VOLUME", "FAILED", details=pending_volume, error=error)
        elif pending_mute is not None:
            self.evidence_manager.record_operation(pending_mute, "FAILED", error=error)
        elif pending_refresh:
            self.evidence_manager.record_operation("REFRESH_VOLUME", "FAILED", error=error)
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
        self.evidence_manager.log_message(message)
        self._update_session_ui()

    def shutdown(self) -> None:
        """Stop this page's managed audio processes during application exit."""
        had_active_operation = self._audio_operation_active()
        self._stop_recording_timers()
        self.audio_manager.shutdown_recording()
        self.audio_manager.shutdown_playback()
        self.audio_manager.shutdown_speaker_channel_test()
        if self.evidence_manager.is_active:
            if had_active_operation and not self.evidence_manager.is_interrupted:
                self.evidence_manager.mark_interrupted(
                    "Application closed while an Audio operation was active."
                )
            self.evidence_manager.end_session(application_closed=True)
        self._update_session_ui()
