"""Asynchronous discovery of audio devices through the shared Jetson service."""
from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from PySide6.QtCore import QObject, Signal

from desktop_app.audio.audio_models import (
    AudioDeviceSnapshot,
    AudioRecordingConfig,
    AudioRecordingVerification,
    AudioPlaybackFile,
    AudioRoutingResult,
    AudioSpeakerTestPreparation,
    AudioVolumeState,
    PlaybackSource,
    RecordingState,
    build_recording_command,
    build_default_sink_command,
    build_default_source_command,
    parse_sink_mute,
    parse_sink_volume,
    parse_pactl_info,
    parse_pactl_short_devices,
    quote_remote_path,
    build_playback_command,
    playback_file_metadata,
    normalize_remote_audio_path,
    prepare_speaker_channel_test,
    routing_target_matches,
    snapshot_from_outputs,
    verify_recording_size,
    validate_recording_config,
    validate_playback_path,
    validate_volume,
)
from desktop_app.audio.speaker_test_prompts import (
    DEFAULT_PROMPT_SOURCE_DIRECTORY,
    PromptWavPreparationError,
    build_remote_prompt_preparation_command,
    parse_remote_prompt_preparation,
)


DISCOVERY_COMMANDS = (
    ("pactl info", "pactl info"),
    ("pactl list short sinks", "pactl list short sinks"),
    ("pactl list short sources", "pactl list short sources"),
    ("pactl list sinks", "pactl list sinks"),
    ("pactl list sources", "pactl list sources"),
    ("ALSA cards", "cat /proc/asound/cards"),
    ("ALSA playback devices", "aplay -l"),
    ("ALSA capture devices", "arecord -l"),
)


def _command_error(label: str, result) -> str:
    detail = (result.stderr or "").strip()
    output = (result.stdout or "").strip()
    if result.exit_status == 127 or "not found" in detail.casefold():
        executable = str(getattr(result, "command", label)).split()[0]
        return f"{executable} command not found"
    return detail or output or f"command exited with status {result.exit_status}"


def _quote_remote_path(path: str) -> str:
    return quote_remote_path(path)


class AudioManager(QObject):
    """Coordinates audio discovery on the shared Jetson SSH worker."""

    started = Signal()
    completed = Signal(object)
    failed = Signal(str)
    volume_operation_started = Signal(str)
    volume_state_ready = Signal(object)
    volume_changed = Signal(int)
    mute_changed = Signal(bool)
    volume_failed = Signal(str)
    routing_operation_started = Signal(str)
    routing_succeeded = Signal(object)
    routing_failed = Signal(str)
    validation_started = Signal()
    file_validated = Signal(object)
    validation_failed = Signal(str)
    playback_state_changed = Signal(str)
    playback_started = Signal(str)
    playback_finished = Signal(str, int)
    playback_stopped = Signal()
    playback_failed = Signal(str)
    playback_output = Signal(str)
    playback_disconnected = Signal()
    recording_state_changed = Signal(str)
    recording_started = Signal(str)
    recording_completed = Signal(object)
    recording_failed = Signal(str)
    recording_output = Signal(str)
    recording_disconnected = Signal()
    speaker_test_state_changed = Signal(str)
    speaker_test_prepared = Signal(object)
    speaker_test_started = Signal()
    speaker_test_completed = Signal(int)
    speaker_test_failed = Signal(str)
    speaker_test_output = Signal(str)
    speaker_test_disconnected = Signal()

    def __init__(self, jetson_service, parent=None):
        super().__init__(parent)
        self.jetson_service = jetson_service
        self._request_id: str | None = None
        self._request_kind: str | None = None
        self._validated_file: AudioPlaybackFile | None = None
        self._validated_input_path: str | None = None
        self._playback_request_id: str | None = None
        self._playback_file: str | None = None
        self._playback_active = False
        self._stop_requested = False
        self._playback_source: PlaybackSource | None = None
        self._last_recorded_file: AudioRecordingVerification | None = None
        self._recording_request_id: str | None = None
        self._recording_path: str | None = None
        self._recording_config: AudioRecordingConfig | None = None
        self._recording_active = False
        self._recording_stop_requested = False
        self._recording_state = RecordingState.IDLE
        self._speaker_test_request_id: str | None = None
        self._speaker_test_active = False
        self._speaker_test_preparation: AudioSpeakerTestPreparation | None = None
        jetson_service.operation_succeeded.connect(self._on_operation_succeeded)
        jetson_service.operation_failed.connect(self._on_operation_failed)
        if hasattr(jetson_service, "remote_process_started"):
            jetson_service.remote_process_started.connect(self._on_remote_process_started)
            jetson_service.remote_process_output.connect(self._on_remote_process_output)
            jetson_service.remote_process_finished.connect(self._on_remote_process_finished)
            jetson_service.remote_process_failed.connect(self._on_remote_process_failed)
        if hasattr(jetson_service, "disconnected"):
            jetson_service.disconnected.connect(self._on_jetson_disconnected)

    @property
    def busy(self) -> bool:
        return self._request_id is not None or self._speaker_test_active

    @property
    def playback_active(self) -> bool:
        return self._playback_active

    @property
    def playback_request_id(self) -> str | None:
        return self._playback_request_id

    @property
    def playback_source(self) -> PlaybackSource | None:
        return self._playback_source

    @property
    def playback_busy(self) -> bool:
        return self._playback_active or self._request_kind == "recorded_playback_validate"

    @property
    def last_recorded_file(self) -> AudioRecordingVerification | None:
        return self._last_recorded_file

    @property
    def recording_state(self) -> RecordingState:
        return self._recording_state

    @property
    def recording_active(self) -> bool:
        return self._recording_active

    @property
    def recording_busy(self) -> bool:
        return bool(
            self._recording_active
            or self._recording_request_id
            or self._request_kind in {"recording_preflight", "recording_verify"}
        )

    @property
    def recording_request_id(self) -> str | None:
        return self._recording_request_id

    @property
    def speaker_test_request_id(self) -> str | None:
        return self._speaker_test_request_id

    @property
    def speaker_test_active(self) -> bool:
        return self._speaker_test_active

    @property
    def speaker_test_preparation(self) -> AudioSpeakerTestPreparation | None:
        return self._speaker_test_preparation

    @property
    def recording_path(self) -> str | None:
        return self._recording_path

    def discover_devices(self) -> bool:
        if self.busy or self.recording_busy:
            return False
        if not self.jetson_service.is_connected:
            self.failed.emit("Jetson is not connected.")
            return False

        async def operation(ssh):
            outputs: dict[str, str] = {}
            errors: dict[str, str] = {}
            for label, command in DISCOVERY_COMMANDS:
                try:
                    result = await ssh.run(command, timeout=15)
                    outputs[label] = result.stdout or ""
                    if result.exit_status != 0:
                        errors[label] = _command_error(label, result)
                except Exception as exc:
                    outputs[label] = ""
                    errors[label] = f"{type(exc).__name__}: {exc}"
            return snapshot_from_outputs(outputs, errors)

        request_id = self.jetson_service.submit_operation("audio_discovery", operation)
        if request_id is None:
            self.failed.emit("Jetson is not connected.")
            return False
        self._request_id = request_id
        self._request_kind = "discovery"
        self.started.emit()
        return True

    def get_volume_state(self) -> bool:
        """Read default sink volume and mute state asynchronously."""
        async def operation(ssh):
            volume_result = await ssh.run("pactl get-sink-volume @DEFAULT_SINK@", timeout=15)
            self._raise_volume_command_error("pactl get-sink-volume", volume_result)
            mute_result = await ssh.run("pactl get-sink-mute @DEFAULT_SINK@", timeout=15)
            self._raise_volume_command_error("pactl get-sink-mute", mute_result)
            volume = parse_sink_volume(volume_result.stdout or "")
            muted = parse_sink_mute(mute_result.stdout or "")
            if volume is None:
                raise ValueError("Could not parse default sink volume.")
            if muted is None:
                raise ValueError("Could not parse default sink mute state.")
            return AudioVolumeState(volume, muted)

        return self._submit_volume_operation("volume_state", operation)

    def validate_file(self, path: str) -> bool:
        self._validated_file = None
        self._validated_input_path = None
        try:
            path = validate_playback_path(path)
        except ValueError as exc:
            self.validation_failed.emit(str(exc))
            return False
        self._validated_input_path = path
        if self.busy:
            return False
        if not self.jetson_service.is_connected:
            self.validation_failed.emit("Jetson is not connected.")
            return False

        async def operation(ssh):
            home_result = await ssh.run('printf "%s" "$HOME"', timeout=15)
            if home_result.exit_status != 0:
                raise RuntimeError("Remote home directory could not be resolved.")
            try:
                absolute_path = normalize_remote_audio_path(
                    path, (home_result.stdout or "").strip()
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            quoted = shlex.quote(absolute_path)
            exists = await ssh.run(f"test -f {quoted}", timeout=15)
            if exists.exit_status != 0:
                raise RuntimeError("Audio file does not exist on Jetson.")
            availability = await ssh.run("command -v paplay", timeout=15)
            if availability.exit_status != 0:
                raise RuntimeError("Speaker playback is unavailable because paplay was not found.")
            size_result = await ssh.run(f"stat -c %s {quoted}", timeout=15)
            size = None
            try:
                size = int((size_result.stdout or "").strip())
            except (TypeError, ValueError):
                pass
            return playback_file_metadata(absolute_path, size)

        request_id = self.jetson_service.submit_operation("audio_validate_file", operation)
        if request_id is None:
            self.validation_failed.emit("Jetson is not connected.")
            return False
        self._request_id = request_id
        self._request_kind = "validate_file"
        self.validation_started.emit()
        return True

    def play(self, path: str) -> bool:
        try:
            path = validate_playback_path(path)
        except ValueError as exc:
            self.playback_failed.emit(str(exc))
            return False
        if self._playback_active:
            self.playback_failed.emit("Playback is already active.")
            return False
        if self.recording_busy:
            self.playback_failed.emit("Stop microphone recording before playback.")
            return False
        if self._speaker_test_active:
            self.playback_failed.emit("Stop the Left / Right speaker test before playback.")
            return False
        if self.busy:
            self.playback_failed.emit("Another audio operation is already in progress.")
            return False
        if not self.jetson_service.is_connected:
            self.playback_failed.emit("Unable to start playback because Jetson is disconnected.")
            return False
        if self._validated_file is None:
            self.playback_failed.emit("Validate this WAV file before playback.")
            return False
        if self._validated_input_path is not None:
            if self._validated_input_path != path:
                self.playback_failed.emit("Validate this WAV file before playback.")
                return False
        elif self._validated_file.path != path:
            self.playback_failed.emit("Validate this WAV file before playback.")
            return False
        return self._start_playback_process(
            self._validated_file.path, PlaybackSource.NORMAL_FILE
        )

    def play_recorded_file(self) -> bool:
        """Validate the last recording remotely, then use the shared paplay path."""
        if self._playback_active:
            self.playback_failed.emit("Playback is already active.")
            return False
        if self.recording_busy:
            self.playback_failed.emit("Stop microphone recording before playback.")
            return False
        if self._speaker_test_active:
            self.playback_failed.emit("Stop the Left / Right speaker test before playback.")
            return False
        if self.busy:
            self.playback_failed.emit("Another audio operation is already in progress.")
            return False
        if not self.jetson_service.is_connected:
            self.playback_failed.emit("Unable to start playback because Jetson is disconnected.")
            return False
        recording = self._last_recorded_file
        if recording is None or not recording.valid:
            self.playback_failed.emit("Recorded WAV file is no longer available.")
            return False

        path = recording.path
        quoted = quote_remote_path(path)

        async def operation(ssh):
            exists = await ssh.run(f"test -f {quoted}", timeout=15)
            if exists.exit_status != 0:
                raise RuntimeError("Recorded WAV file is no longer available.")
            size_result = await ssh.run(f"stat -c %s {quoted}", timeout=15)
            try:
                size = int((size_result.stdout or "").strip())
            except (TypeError, ValueError):
                size = None
            verified = verify_recording_size(path, size)
            if not verified.valid:
                raise RuntimeError("Recorded WAV file is no longer available.")
            return playback_file_metadata(path, size)

        request_id = self.jetson_service.submit_operation(
            "audio_recorded_playback_validate", operation
        )
        if request_id is None:
            self.playback_failed.emit("Unable to start playback because Jetson is disconnected.")
            return False
        self._playback_source = PlaybackSource.RECORDED_FILE
        self._request_id = request_id
        self._request_kind = "recorded_playback_validate"
        self._set_playback_state("Starting")
        return True

    def _start_playback_process(self, path: str, source: PlaybackSource) -> bool:
        self._playback_source = source
        command = build_playback_command(path)
        request_id = self.jetson_service.start_remote_process("audio_playback", command)
        if request_id is None:
            self._set_playback_state("Failed")
            self.playback_failed.emit("Unable to start playback because Jetson is disconnected.")
            return False
        self._playback_request_id = request_id
        self._playback_file = path
        self._playback_active = True
        self._stop_requested = False
        self._set_playback_state("Starting")
        return True

    def stop_playback(self) -> bool:
        if not self._playback_active or not self._playback_request_id:
            return False
        self._stop_requested = True
        self._set_playback_state("Stopping")
        self.jetson_service.stop_remote_process(self._playback_request_id)
        return True

    def shutdown_playback(self) -> None:
        request_id = self._playback_request_id
        if request_id:
            self.jetson_service.stop_remote_process(request_id)
        if self._request_kind == "recorded_playback_validate":
            self._request_id = None
            self._request_kind = None
        self._clear_playback_state()

    def _set_playback_state(self, state: str) -> None:
        self.playback_state_changed.emit(state)

    def _clear_playback_state(self) -> str:
        path = self._playback_file or ""
        self._playback_request_id = None
        self._playback_file = None
        self._playback_active = False
        self._stop_requested = False
        return path

    def start_speaker_channel_test(self) -> bool:
        """Check speaker-test availability, then run the managed stereo test."""
        if self._speaker_test_active:
            return False
        if self._playback_active:
            self.speaker_test_failed.emit("Stop speaker playback before the Left / Right speaker test.")
            return False
        if self.recording_busy:
            self.speaker_test_failed.emit("Stop microphone recording before the Left / Right speaker test.")
            return False
        if self.busy:
            return False
        if not self.jetson_service.is_connected:
            self.speaker_test_failed.emit("Jetson is not connected.")
            return False

        async def operation(ssh):
            result = await ssh.run("command -v speaker-test", timeout=15)
            if result.exit_status != 0:
                raise RuntimeError("speaker-test is not available on Jetson.")
            default_sink = None
            info = await ssh.run("pactl info", timeout=15)
            if info.exit_status == 0:
                default_sink, _default_source = parse_pactl_info(info.stdout or "")

            sinks = await ssh.run("pactl list short sinks", timeout=15)
            if sinks.exit_status == 0:
                devices = parse_pactl_short_devices(sinks.stdout or "")
            else:
                devices = []
            preliminary = prepare_speaker_channel_test(default_sink, devices)

            home_result = await ssh.run('printf "%s" "$HOME"', timeout=15)
            remote_home = (home_result.stdout or "").strip()
            if home_result.exit_status != 0 or not remote_home.startswith("/"):
                raise RuntimeError(
                    "Spoken Left/Right WAV prompts could not be prepared: "
                    "remote HOME could not be resolved."
                )
            cache_rate = (
                str(preliminary.native_sample_rate_hz)
                if preliminary.native_sample_rate_hz is not None
                else "default"
            )
            cache_directory = str(
                PurePosixPath(remote_home)
                / ".cache"
                / "cam_lidar"
                / "audio_speaker_test"
                / cache_rate
            )
            prompt_command = build_remote_prompt_preparation_command(
                DEFAULT_PROMPT_SOURCE_DIRECTORY,
                cache_directory,
                preliminary.native_sample_rate_hz,
            )
            prompt_result = await ssh.run(prompt_command, timeout=30)
            if prompt_result.exit_status != 0:
                detail = (prompt_result.stderr or prompt_result.stdout or "").strip()
                suffix = f": {detail}" if detail else "."
                raise RuntimeError(
                    "Spoken Left/Right WAV prompts could not be prepared" + suffix
                )
            try:
                prompt_preparation = parse_remote_prompt_preparation(
                    prompt_result.stdout or ""
                )
            except PromptWavPreparationError as exc:
                raise RuntimeError(
                    f"Spoken Left/Right WAV prompts could not be prepared: {exc}"
                ) from exc
            return prepare_speaker_channel_test(
                default_sink,
                devices,
                prompt_directory=prompt_preparation.directory,
                prompt_source_rates=prompt_preparation.source_rates,
                prompt_resampled=prompt_preparation.resampled,
                prompt_cache_reused=prompt_preparation.cache_reused,
            )

        request_id = self.jetson_service.submit_operation("audio_speaker_test_preflight", operation)
        if request_id is None:
            self.speaker_test_failed.emit("Jetson is not connected.")
            return False
        self._request_id = request_id
        self._request_kind = "speaker_test_preflight"
        self._speaker_test_active = True
        self.speaker_test_state_changed.emit("Starting")
        return True

    def shutdown_speaker_channel_test(self) -> None:
        request_id = self._speaker_test_request_id
        if request_id:
            self.jetson_service.stop_remote_process(request_id)
        if self._request_kind == "speaker_test_preflight":
            self._request_id = None
            self._request_kind = None
        self._clear_speaker_test_state()

    def _clear_speaker_test_state(self) -> None:
        self._speaker_test_request_id = None
        self._speaker_test_active = False

    def _fail_speaker_test(self, error: str) -> None:
        self._clear_speaker_test_state()
        self.speaker_test_state_changed.emit("Failed")
        self.speaker_test_failed.emit(error)

    def _start_speaker_test_process(
        self, preparation: AudioSpeakerTestPreparation | str
    ) -> None:
        if isinstance(preparation, str):
            preparation = AudioSpeakerTestPreparation(
                command=preparation,
                output_name=None,
                output_display_name=None,
                sample_format=None,
                channels=None,
                native_sample_rate_hz=None,
                command_rate_hz=None,
                warning="Output native sample rate could not be detected; using speaker-test default rate.",
            )
        self._speaker_test_preparation = preparation
        self.speaker_test_prepared.emit(preparation)
        request_id = self.jetson_service.start_remote_process(
            "audio_speaker_channel_test", preparation.command
        )
        if request_id is None:
            self._fail_speaker_test("Unable to start the Left / Right speaker test because Jetson is disconnected.")
            return
        self._speaker_test_request_id = request_id
        self.speaker_test_state_changed.emit("Starting")

    def start_recording(self, config: AudioRecordingConfig) -> bool:
        """Validate, prepare, and start one managed remote ``parecord`` process."""
        try:
            config = validate_recording_config(config)
        except ValueError as exc:
            self._fail_recording(str(exc))
            return False
        if self.recording_busy:
            self.recording_failed.emit("Microphone recording is already active.")
            return False
        if self.busy:
            self.recording_failed.emit("Another audio operation is already in progress.")
            return False
        if self.playback_active:
            self.recording_failed.emit("Stop speaker playback before recording.")
            return False
        if self._speaker_test_active:
            self.recording_failed.emit("Stop the Left / Right speaker test before recording.")
            return False
        if not self.jetson_service.is_connected:
            self._set_recording_state(RecordingState.DISCONNECTED)
            self.recording_failed.emit("Unable to start recording because Jetson is disconnected.")
            return False

        self._recording_config = config
        self._recording_path = config.output_path
        self._recording_stop_requested = False
        self._set_recording_state(RecordingState.VALIDATING)

        async def operation(ssh):
            availability = await ssh.run("command -v parecord", timeout=15)
            if availability.exit_status != 0:
                raise RuntimeError(
                    "Microphone recording is unavailable because parecord was not found."
                )

            sources = await ssh.run("pactl list short sources", timeout=15)
            if sources.exit_status != 0:
                error = _command_error("pactl list short sources", sources)
                raise RuntimeError(f"Unable to discover the selected input source: {error}")
            source_names = {device.identifier for device in parse_pactl_short_devices(sources.stdout or "")}
            if config.source_name not in source_names:
                raise RuntimeError("No input audio device available.")

            home_result = await ssh.run('printf "%s" "$HOME"', timeout=15)
            if home_result.exit_status != 0:
                raise RuntimeError("Remote home directory could not be resolved.")
            try:
                output_path = normalize_remote_audio_path(
                    config.output_path, (home_result.stdout or "").strip()
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            absolute_config = AudioRecordingConfig(
                source_name=config.source_name,
                output_path=output_path,
                sample_rate=config.sample_rate,
                channels=config.channels,
                sample_format=config.sample_format,
                duration_seconds=config.duration_seconds,
            )
            output_dir = str(PurePosixPath(output_path).parent)
            quoted_dir = _quote_remote_path(output_dir)
            quoted_output = _quote_remote_path(output_path)
            mkdir = await ssh.run(f"mkdir -p -- {quoted_dir}", timeout=15)
            if mkdir.exit_status != 0:
                raise RuntimeError(
                    f"Could not create the remote recording directory: {_command_error('mkdir', mkdir)}"
                )
            exists = await ssh.run(f"test ! -e {quoted_output}", timeout=15)
            if exists.exit_status != 0:
                raise RuntimeError(
                    "Recording output file already exists. Generate a new path before recording."
                )
            return absolute_config

        request_id = self.jetson_service.submit_operation("audio_recording_preflight", operation)
        if request_id is None:
            self._fail_recording("Unable to start recording because Jetson is disconnected.")
            return False
        self._request_id = request_id
        self._request_kind = "recording_preflight"
        return True

    def stop_recording(self) -> bool:
        """Stop only the request ID owned by the active microphone recording."""
        if not self._recording_request_id or not self._recording_active:
            return False
        if self._recording_stop_requested:
            return False
        self._recording_stop_requested = True
        self._set_recording_state(RecordingState.STOPPING)
        self.jetson_service.stop_remote_process(self._recording_request_id)
        return True

    def shutdown_recording(self) -> None:
        request_id = self._recording_request_id
        if request_id:
            self.jetson_service.stop_remote_process(request_id)
        if self._request_kind in {"recording_preflight", "recording_verify"}:
            self._request_id = None
            self._request_kind = None
        self._clear_recording_state()

    def _set_recording_state(self, state: RecordingState | str) -> None:
        self._recording_state = state if isinstance(state, RecordingState) else RecordingState(state)
        self.recording_state_changed.emit(self._recording_state.value)

    def _clear_recording_state(self) -> None:
        self._recording_request_id = None
        self._recording_config = None
        self._recording_active = False
        self._recording_stop_requested = False

    def _fail_recording(self, error: str, state: RecordingState = RecordingState.FAILED) -> None:
        self._clear_recording_state()
        self._set_recording_state(state)
        self.recording_failed.emit(error)

    def _start_recording_process(self, config: AudioRecordingConfig) -> None:
        try:
            command = build_recording_command(config)
        except ValueError as exc:
            self._fail_recording(str(exc))
            return
        request_id = self.jetson_service.start_remote_process("audio_recording", command)
        if request_id is None:
            self._fail_recording("Unable to start recording because Jetson is disconnected.")
            return
        self._recording_request_id = request_id
        self._recording_path = config.output_path
        self._recording_active = True
        self._recording_stop_requested = False
        self._set_recording_state(RecordingState.STARTING)

    def _verify_recording_output(self) -> None:
        path = self._recording_path
        if not path or not self.jetson_service.is_connected:
            self._fail_recording(
                "Jetson disconnected before the recorded WAV could be verified.",
                RecordingState.DISCONNECTED if not self.jetson_service.is_connected else RecordingState.FAILED,
            )
            return

        quoted = _quote_remote_path(path)

        async def operation(ssh):
            exists = await ssh.run(f"test -f {quoted}", timeout=15)
            if exists.exit_status != 0:
                return AudioRecordingVerification(
                    path, False, None, False, "Recorded WAV file was not created."
                )
            size_result = await ssh.run(f"stat -c %s {quoted}", timeout=15)
            try:
                size = int((size_result.stdout or "").strip())
            except (TypeError, ValueError):
                size = None
            return verify_recording_size(path, size)

        request_id = self.jetson_service.submit_operation("audio_recording_verify", operation)
        if request_id is None:
            self._fail_recording("Unable to verify the recorded WAV because Jetson is disconnected.")
            return
        self._request_id = request_id
        self._request_kind = "recording_verify"

    def _complete_recording_verification(self, result: AudioRecordingVerification) -> None:
        self._clear_recording_state()
        if result.valid:
            self._last_recorded_file = result
            self._set_recording_state(RecordingState.COMPLETED)
            self.recording_completed.emit(result)
            return
        self._set_recording_state(RecordingState.FAILED)
        self.recording_failed.emit(result.error or "Recorded WAV file verification failed.")

    def set_volume(self, percent: int) -> bool:
        try:
            percent = validate_volume(percent)
        except ValueError as exc:
            self.volume_failed.emit(str(exc))
            return False

        async def operation(ssh):
            result = await ssh.run(
                f"pactl set-sink-volume @DEFAULT_SINK@ {percent}%", timeout=15
            )
            self._raise_volume_command_error("pactl set-sink-volume", result)
            return percent

        return self._submit_volume_operation("set_volume", operation)

    def mute(self) -> bool:
        return self._set_mute(True)

    def unmute(self) -> bool:
        return self._set_mute(False)

    def set_default_sink(self, sink_name: str) -> bool:
        return self._set_default_device("sink", sink_name)

    def set_default_source(self, source_name: str) -> bool:
        if self.recording_busy:
            self.routing_failed.emit("Stop microphone recording before changing the default input.")
            return False
        return self._set_default_device("source", source_name)

    def _set_default_device(self, kind: str, name: str) -> bool:
        try:
            command = (
                build_default_sink_command(name)
                if kind == "sink"
                else build_default_source_command(name)
            )
        except ValueError as exc:
            self.routing_failed.emit(str(exc))
            return False

        async def operation(ssh):
            result = await ssh.run(command, timeout=15)
            self._raise_routing_command_error(f"pactl set-default-{kind}", result)
            info = await ssh.run("pactl info", timeout=15)
            self._raise_routing_command_error("pactl info", info)
            default_sink, default_source = parse_pactl_info(info.stdout or "")
            if not routing_target_matches(kind, name, default_sink, default_source):
                raise RuntimeError(
                    f"Requested {kind} was not found after routing update."
                )
            return AudioRoutingResult(kind, name, default_sink, default_source)

        return self._submit_routing_operation(kind, operation)

    def _submit_routing_operation(self, kind: str, operation) -> bool:
        if self.busy:
            return False
        if not self.jetson_service.is_connected:
            self.routing_failed.emit("Jetson is not connected.")
            return False
        request_id = self.jetson_service.submit_operation("audio_set_default_" + kind, operation)
        if request_id is None:
            self.routing_failed.emit("Jetson is not connected.")
            return False
        self._request_id = request_id
        self._request_kind = "route_" + kind
        self.routing_operation_started.emit(kind)
        return True

    @staticmethod
    def _raise_routing_command_error(label: str, result) -> None:
        if result.exit_status == 0:
            return
        error = _command_error(label, result)
        if "not found" in error.casefold():
            raise RuntimeError("Audio routing is unavailable because pactl was not found.")
        raise RuntimeError(f"{label} failed: {error}")

    def _set_mute(self, muted: bool) -> bool:
        async def operation(ssh):
            result = await ssh.run(
                f"pactl set-sink-mute @DEFAULT_SINK@ {1 if muted else 0}", timeout=15
            )
            self._raise_volume_command_error("pactl set-sink-mute", result)
            return muted

        return self._submit_volume_operation("mute" if muted else "unmute", operation)

    def _submit_volume_operation(self, kind: str, operation) -> bool:
        if self.busy:
            return False
        if not self.jetson_service.is_connected:
            self.volume_failed.emit("Jetson is not connected.")
            return False
        request_id = self.jetson_service.submit_operation("audio_" + kind, operation)
        if request_id is None:
            self.volume_failed.emit("Jetson is not connected.")
            return False
        self._request_id = request_id
        self._request_kind = kind
        self.volume_operation_started.emit(kind)
        return True

    @staticmethod
    def _raise_volume_command_error(label: str, result) -> None:
        if result.exit_status == 0:
            return
        error = _command_error(label, result)
        if "not found" in error.casefold():
            raise RuntimeError("Audio volume control is unavailable because pactl was not found.")
        if "no such entity" in error.casefold() or "default sink" in error.casefold():
            raise RuntimeError("No default output sink detected.")
        raise RuntimeError(f"{label} failed: {error}")

    def _on_operation_succeeded(self, request_id: str, result: object) -> None:
        if request_id != self._request_id:
            return
        kind = self._request_kind
        self._request_id = None
        self._request_kind = None
        if kind == "recording_preflight" and isinstance(result, AudioRecordingConfig):
            self._start_recording_process(result)
            return
        if kind == "recorded_playback_validate" and isinstance(result, AudioPlaybackFile):
            self._request_id = None
            self._request_kind = None
            self._start_playback_process(result.path, PlaybackSource.RECORDED_FILE)
            return
        if kind == "speaker_test_preflight" and isinstance(
            result, (AudioSpeakerTestPreparation, str)
        ):
            self._start_speaker_test_process(result)
            return
        if kind == "recording_verify" and isinstance(result, AudioRecordingVerification):
            self._complete_recording_verification(result)
            return
        if kind == "discovery" and isinstance(result, AudioDeviceSnapshot):
            self.completed.emit(result)
            return
        if kind == "volume_state" and isinstance(result, AudioVolumeState):
            self.volume_state_ready.emit(result)
            return
        if kind == "validate_file" and isinstance(result, AudioPlaybackFile):
            self._validated_file = result
            self.file_validated.emit(result)
            return
        if kind in {"route_sink", "route_source"} and isinstance(result, AudioRoutingResult):
            self.routing_succeeded.emit(result)
            return
        if kind == "set_volume" and isinstance(result, int):
            self.volume_changed.emit(result)
            return
        if kind in {"mute", "unmute"} and isinstance(result, bool):
            self.mute_changed.emit(result)
            return
        if kind == "discovery":
            self.failed.emit("Audio discovery returned an invalid result.")
        elif kind == "validate_file":
            self.validation_failed.emit("Audio file validation returned an invalid result.")
        elif kind in {"route_sink", "route_source"}:
            self.routing_failed.emit("Audio routing returned an invalid result.")
        else:
            self.volume_failed.emit("Audio volume operation returned an invalid result.")

    def _on_operation_failed(self, request_id: str, error: str) -> None:
        if request_id != self._request_id:
            return
        kind = self._request_kind
        self._request_id = None
        self._request_kind = None
        if kind == "discovery":
            self.failed.emit(error)
        elif kind == "recording_preflight":
            self._fail_recording(error)
        elif kind == "recording_verify":
            self._fail_recording(error)
        elif kind == "recorded_playback_validate":
            self._set_playback_state("Failed")
            self.playback_failed.emit(f"Recorded audio playback failed: {error}")
        elif kind == "speaker_test_preflight":
            self._fail_speaker_test(error)
        elif kind == "validate_file":
            self.validation_failed.emit(error)
        elif kind in {"route_sink", "route_source"}:
            self.routing_failed.emit(error)
        else:
            self.volume_failed.emit(error)

    def _on_remote_process_started(self, request_id: str) -> None:
        if request_id == self._speaker_test_request_id and self._speaker_test_active:
            self.speaker_test_state_changed.emit("Running")
            self.speaker_test_started.emit()
            return
        if request_id == self._playback_request_id and self._playback_active:
            if self._stop_requested:
                self._set_playback_state("Stopping")
                self.jetson_service.stop_remote_process(request_id)
                return
            self._set_playback_state("Playing")
            self.playback_started.emit(self._playback_file or "")
            return
        if request_id != self._recording_request_id or not self._recording_active:
            return
        if self._recording_stop_requested:
            self._set_recording_state(RecordingState.STOPPING)
            self.jetson_service.stop_remote_process(request_id)
            return
        self._set_recording_state(RecordingState.RECORDING)
        self.recording_started.emit(self._recording_path or "")

    def _on_remote_process_output(self, request_id: str, stream: str, line: str) -> None:
        text = str(line).strip()
        if not text:
            return
        if request_id == self._speaker_test_request_id and self._speaker_test_active:
            self.speaker_test_output.emit(f"ERROR: {text}" if stream == "stderr" else text)
            return
        if request_id == self._playback_request_id:
            self.playback_output.emit(f"ERROR: {text}" if stream == "stderr" else text)
            return
        if request_id == self._recording_request_id:
            self.recording_output.emit(f"ERROR: {text}" if stream == "stderr" else text)

    def _on_remote_process_finished(self, request_id: str, exit_code: int) -> None:
        if request_id == self._speaker_test_request_id and self._speaker_test_active:
            self._clear_speaker_test_state()
            if exit_code == 0:
                self.speaker_test_state_changed.emit("Completed")
                self.speaker_test_completed.emit(exit_code)
            else:
                self.speaker_test_state_changed.emit("Failed")
                self.speaker_test_failed.emit(
                    f"Left / Right speaker test failed with exit code {exit_code}"
                )
            return
        if request_id == self._playback_request_id:
            was_stopping = self._stop_requested
            path = self._clear_playback_state()
            if was_stopping:
                self._set_playback_state("Stopped")
                self.playback_stopped.emit()
            elif exit_code == 0:
                self._set_playback_state("Completed")
                self.playback_finished.emit(path, exit_code)
            else:
                self._set_playback_state("Failed")
                self.playback_failed.emit(f"Playback failed with exit code {exit_code}")
            return
        if request_id != self._recording_request_id:
            return
        was_stopping = self._recording_stop_requested
        self._recording_active = False
        if exit_code != 0 and not was_stopping:
            self._fail_recording(f"Recording failed with exit code {exit_code}")
            return
        self._set_recording_state(RecordingState.STOPPING)
        self._verify_recording_output()

    def _on_remote_process_failed(self, request_id: str, error: str) -> None:
        if request_id == self._speaker_test_request_id and self._speaker_test_active:
            self._fail_speaker_test(f"Left / Right speaker test failed: {error}")
            return
        if request_id == self._playback_request_id:
            was_stopping = self._stop_requested
            self._clear_playback_state()
            if was_stopping:
                self._set_playback_state("Stopped")
                self.playback_stopped.emit()
            else:
                self._set_playback_state("Failed")
                self.playback_failed.emit(f"Playback failed: {error}")
            return
        if request_id != self._recording_request_id:
            return
        if self._recording_stop_requested:
            self._recording_active = False
            self._set_recording_state(RecordingState.STOPPING)
            self._verify_recording_output()
            return
        self._fail_recording(f"Recording failed: {error}")

    def _on_jetson_disconnected(self) -> None:
        self._validated_file = None
        self._validated_input_path = None
        if self._request_kind == "recorded_playback_validate":
            self._request_id = None
            self._request_kind = None
            self._set_playback_state("Disconnected")
            self.playback_disconnected.emit()
        speaker_test_was_active = self._speaker_test_active or self._request_kind == "speaker_test_preflight"
        if self._request_kind == "speaker_test_preflight":
            self._request_id = None
            self._request_kind = None
        if speaker_test_was_active:
            self._clear_speaker_test_state()
            self.speaker_test_state_changed.emit("Disconnected")
            self.speaker_test_disconnected.emit()
        if self._playback_active:
            self._clear_playback_state()
            self._set_playback_state("Disconnected")
            self.playback_disconnected.emit()
        if self.recording_busy:
            self._request_id = None
            self._request_kind = None
            self._clear_recording_state()
            self._set_recording_state(RecordingState.DISCONNECTED)
            self.recording_disconnected.emit()
