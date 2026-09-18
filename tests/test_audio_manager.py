import shlex
import unittest

try:
    from desktop_app.audio.audio_manager import AudioManager
except ModuleNotFoundError:
    AudioManager = None

from desktop_app.audio.audio_models import (
    AudioRecordingConfig,
    AudioRecordingVerification,
    AudioPlaybackFile,
    PlaybackSource,
    build_recording_command,
    build_default_sink_command,
    build_default_source_command,
    build_speaker_channel_test_command,
    prepare_speaker_channel_test,
    parse_pulse_sample_spec,
    preferred_recording_configuration,
    build_playback_command,
    normalize_remote_audio_path,
    parse_pactl_devices,
    parse_sink_mute,
    parse_sink_volume,
    parse_alsa_cards,
    parse_alsa_devices,
    parse_pactl_info,
    parse_pactl_short_devices,
    readable_device_name,
    snapshot_from_outputs,
    routing_target_matches,
    validate_playback_path,
    validate_volume,
    resolve_device_description,
    validate_recording_config,
    verify_recording_size,
)


class _Signal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)


class _PlaybackService:
    def __init__(self):
        self.is_connected = True
        self.operation_succeeded = _Signal()
        self.operation_failed = _Signal()
        self.remote_process_started = _Signal()
        self.remote_process_output = _Signal()
        self.remote_process_finished = _Signal()
        self.remote_process_failed = _Signal()
        self.disconnected = _Signal()
        self.started = []
        self.process_calls = []
        self.stopped = []
        self.submitted = []

    def submit_operation(self, name, operation):
        request_id = f"{name}:{len(self.submitted) + 1}"
        self.submitted.append((request_id, operation))
        return request_id

    def start_remote_process(self, name, command):
        request_id = f"audio:{len(self.started) + 1}"
        self.started.append(request_id)
        self.process_calls.append((name, command, request_id))
        return request_id

    def stop_remote_process(self, request_id):
        self.stopped.append(request_id)


@unittest.skipUnless(AudioManager is not None, "PySide6 is unavailable")
class AudioPlaybackManagerTests(unittest.TestCase):
    def test_playback_lifecycle_and_request_id_filtering(self):
        from desktop_app.audio.audio_models import playback_file_metadata

        service = _PlaybackService()
        manager = AudioManager(service)
        manager._validated_file = playback_file_metadata("/tmp/test.wav")
        started = []
        finished = []
        manager.playback_started.connect(started.append)
        manager.playback_finished.connect(lambda path, code: finished.append((path, code)))

        self.assertTrue(manager.play("/tmp/test.wav"))
        request_id = manager.playback_request_id
        self.assertEqual(service.started, [request_id])
        self.assertFalse(manager.play("/tmp/test.wav"))
        service.remote_process_started.emit("unrelated:1")
        self.assertEqual(started, [])
        service.remote_process_started.emit(request_id)
        self.assertEqual(started, ["/tmp/test.wav"])
        service.remote_process_finished.emit("unrelated:1", 0)
        self.assertTrue(manager.playback_active)
        service.remote_process_finished.emit(request_id, 0)
        self.assertFalse(manager.playback_active)
        self.assertEqual(finished, [("/tmp/test.wav", 0)])

    def test_stop_uses_only_the_active_managed_request(self):
        from desktop_app.audio.audio_models import playback_file_metadata

        service = _PlaybackService()
        manager = AudioManager(service)
        manager._validated_file = playback_file_metadata("/tmp/test.wav")
        manager.play("/tmp/test.wav")
        request_id = manager.playback_request_id
        self.assertTrue(manager.stop_playback())
        self.assertEqual(service.stopped, [request_id])
        service.remote_process_finished.emit(request_id, -15)
        self.assertFalse(manager.playback_active)

    def test_recorded_playback_reuses_shared_playback_process(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        path = "/home/user/audio recordings/record.wav"
        manager._last_recorded_file = AudioRecordingVerification(path, True, 320044, True)

        self.assertTrue(manager.play_recorded_file())
        validation_id = service.submitted[-1][0]
        self.assertFalse(manager.play_recorded_file())
        verified_file = AudioPlaybackFile(path, "record.wav", True, 320044)
        service.operation_succeeded.emit("unrelated:validation", verified_file)
        self.assertEqual(service.started, [])
        service.operation_succeeded.emit(validation_id, verified_file)

        request_id = manager.playback_request_id
        self.assertIsNotNone(request_id)
        self.assertEqual(manager.playback_source, PlaybackSource.RECORDED_FILE)
        self.assertEqual(
            service.process_calls,
            [("audio_playback", "paplay '/home/user/audio recordings/record.wav'", request_id)],
        )
        service.remote_process_started.emit("unrelated:playback")
        self.assertTrue(manager.playback_active)
        service.remote_process_started.emit(request_id)
        service.remote_process_finished.emit(request_id, 0)
        self.assertFalse(manager.playback_active)

    def test_missing_recorded_file_fails_without_starting_playback(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        manager._last_recorded_file = AudioRecordingVerification(
            "/home/user/record.wav", True, 320044, True
        )
        failures = []
        manager.playback_failed.connect(failures.append)

        self.assertTrue(manager.play_recorded_file())
        validation_id = service.submitted[-1][0]
        service.operation_failed.emit(validation_id, "Recorded WAV file is no longer available.")

        self.assertFalse(manager.playback_active)
        self.assertEqual(service.started, [])
        self.assertIn("Recorded audio playback failed", failures[-1])

    def test_speaker_channel_test_prevents_duplicates_and_filters_request_ids(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        completed = []
        manager.speaker_test_completed.connect(completed.append)

        self.assertTrue(manager.start_speaker_channel_test())
        preflight_id = service.submitted[-1][0]
        self.assertFalse(manager.start_speaker_channel_test())
        service.operation_succeeded.emit(preflight_id, build_speaker_channel_test_command())
        request_id = manager.speaker_test_request_id
        self.assertIsNotNone(request_id)
        self.assertEqual(service.started, [request_id])

        service.remote_process_started.emit("audio_playback:unrelated")
        self.assertTrue(manager.speaker_test_active)
        service.remote_process_finished.emit("audio_recording:unrelated", 0)
        self.assertTrue(manager.speaker_test_active)
        service.remote_process_started.emit(request_id)
        service.remote_process_finished.emit(request_id, 0)
        self.assertFalse(manager.speaker_test_active)
        self.assertIsNone(manager.speaker_test_request_id)
        self.assertEqual(completed, [0])

    def test_speaker_channel_test_failure_clears_request_id(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        failures = []
        manager.speaker_test_failed.connect(failures.append)

        self.assertTrue(manager.start_speaker_channel_test())
        preflight_id = service.submitted[-1][0]
        service.operation_succeeded.emit(preflight_id, build_speaker_channel_test_command())
        request_id = manager.speaker_test_request_id
        service.remote_process_failed.emit(request_id, "PulseAudio unavailable")

        self.assertFalse(manager.speaker_test_active)
        self.assertIsNone(manager.speaker_test_request_id)
        self.assertIn("Left / Right speaker test failed", failures[-1])

    def test_speaker_channel_test_disconnect_clears_state(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        disconnected = []
        manager.speaker_test_disconnected.connect(lambda: disconnected.append(True))

        self.assertTrue(manager.start_speaker_channel_test())
        preflight_id = service.submitted[-1][0]
        service.operation_succeeded.emit(preflight_id, build_speaker_channel_test_command())
        self.assertIsNotNone(manager.speaker_test_request_id)
        service.disconnected.emit()

        self.assertFalse(manager.speaker_test_active)
        self.assertIsNone(manager.speaker_test_request_id)
        self.assertEqual(disconnected, [True])


@unittest.skipUnless(AudioManager is not None, "PySide6 is unavailable")
class AudioRecordingManagerTests(unittest.TestCase):
    def _config(self):
        return AudioRecordingConfig(
            "alsa_input.usb-mic",
            "/home/user/audio recordings/mic test.wav",
            16000,
            2,
            "S16_LE",
            10,
        )

    def test_recording_lifecycle_filters_request_ids_and_verifies_output(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        completed = []
        failures = []
        manager.recording_completed.connect(completed.append)
        manager.recording_failed.connect(failures.append)

        self.assertTrue(manager.start_recording(self._config()))
        preflight_id = service.submitted[0][0]
        service.operation_succeeded.emit("unrelated:1", self._config())
        self.assertEqual(service.started, [])
        service.operation_succeeded.emit(preflight_id, self._config())
        request_id = manager.recording_request_id
        self.assertIsNotNone(request_id)
        service.remote_process_started.emit("audio_playback:77")
        self.assertEqual(manager.recording_state.value, "Starting")
        service.remote_process_started.emit(request_id)
        self.assertEqual(manager.recording_state.value, "Recording")
        service.remote_process_finished.emit(request_id, -15)
        verification_id = service.submitted[-1][0]
        service.operation_succeeded.emit(
            verification_id,
            AudioRecordingVerification(self._config().output_path, True, 320044, True),
        )
        self.assertEqual(manager.recording_state.value, "Completed")
        self.assertFalse(manager.recording_busy)
        self.assertEqual(len(completed), 1)
        self.assertEqual(failures, [])
        self.assertEqual(manager.last_recorded_file, completed[0])

        previous = manager.last_recorded_file
        manager._complete_recording_verification(
            AudioRecordingVerification(self._config().output_path, True, 44, False, "invalid")
        )
        self.assertEqual(manager.last_recorded_file, previous)

    def test_duplicate_start_does_not_create_second_recording(self):
        service = _PlaybackService()
        manager = AudioManager(service)
        self.assertTrue(manager.start_recording(self._config()))
        self.assertFalse(manager.start_recording(self._config()))
        self.assertEqual(service.started, [])


class AudioParsingTests(unittest.TestCase):
    def test_speaker_channel_test_command_uses_pulse_default_routing(self):
        self.assertEqual(
            build_speaker_channel_test_command(),
            "speaker-test -D pulse -c 2 -t wav -l 1",
        )

    def test_pulse_sample_spec_parsing(self):
        for value, expected_rate in (
            ("s16le 2ch 16000Hz", 16000),
            ("s16le 2ch 44100Hz", 44100),
            ("s16le 2ch 48000Hz", 48000),
            ("float32le 2ch 48000Hz", 48000),
        ):
            spec = parse_pulse_sample_spec(value)
            self.assertIsNotNone(spec)
            self.assertEqual(spec.sample_format, value.split()[0])
            self.assertEqual(spec.channels, 2)
            self.assertEqual(spec.sample_rate_hz, expected_rate)
        self.assertIsNone(parse_pulse_sample_spec("not a sample specification"))
        self.assertIsNone(parse_pulse_sample_spec("s16le 0ch 0Hz"))

    def test_sample_spec_is_stored_on_short_and_full_devices(self):
        short = parse_pactl_short_devices(
            "0\talsa_output.usb-test\tmodule\ts16le 2ch 16000Hz\tSUSPENDED\n"
        )[0]
        self.assertEqual(short.sample_format, "s16le")
        self.assertEqual(short.channels, 2)
        self.assertEqual(short.sample_rate_hz, 16000)
        self.assertEqual(short.state, "SUSPENDED")
        full = parse_pactl_devices(
            "Sink #0\n"
            "\tName: alsa_output.usb-test\n"
            "\tDescription: USB Speaker\n"
            "\tSample Specification: float32le 2ch 48000Hz\n"
            "\tState: IDLE\n"
        )[0]
        self.assertEqual(full.sample_format, "float32le")
        self.assertEqual(full.channels, 2)
        self.assertEqual(full.sample_rate_hz, 48000)
        self.assertEqual(full.state, "IDLE")

    def test_speaker_channel_command_uses_validated_native_rate(self):
        self.assertEqual(
            build_speaker_channel_test_command(16000),
            "speaker-test -D pulse -c 2 -r 16000 -t wav -l 1",
        )
        self.assertIn("-r 44100", build_speaker_channel_test_command(44100))
        self.assertIn("-r 48000", build_speaker_channel_test_command(48000))
        for rate in (16000, 44100, 48000):
            command = build_speaker_channel_test_command(rate)
            self.assertIn("-t wav", command)
            self.assertNotIn("-t sine", command)
            self.assertNotIn("-f 500", command)
        with self.assertRaises(ValueError):
            build_speaker_channel_test_command(480)
        with self.assertRaises(ValueError):
            build_speaker_channel_test_command("16000")

    def test_spoken_prompt_directory_is_quoted_in_command(self):
        command = build_speaker_channel_test_command(
            16000, "/home/test user/.cache/cam_lidar/audio_speaker_test/16000"
        )
        self.assertIn("-r 16000", command)
        self.assertIn("-t wav", command)
        self.assertIn(
            "-W '/home/test user/.cache/cam_lidar/audio_speaker_test/16000'",
            command,
        )
        self.assertNotIn("-t sine", command)

    def test_speaker_preparation_falls_back_without_native_rate(self):
        preparation = prepare_speaker_channel_test(
            "alsa_output.usb-test",
            parse_pactl_short_devices("0\talsa_output.usb-test\tmodule\t\tIDLE\n"),
        )
        self.assertIsNone(preparation.native_sample_rate_hz)
        self.assertEqual(preparation.command, build_speaker_channel_test_command())
        self.assertIn("could not be detected", preparation.warning)

    def test_speaker_preparation_matches_current_default_sink(self):
        devices = parse_pactl_short_devices(
            "0\talsa_output.other\tmodule\ts16le 2ch 48000Hz\tIDLE\n"
            "1\talsa_output.usb-test\tmodule\ts16le 2ch 16000Hz\tSUSPENDED\n"
        )
        preparation = prepare_speaker_channel_test("alsa_output.usb-test", devices)
        self.assertEqual(preparation.native_sample_rate_hz, 16000)
        self.assertEqual(preparation.channels, 2)
        self.assertIn("-r 16000", preparation.command)

    def test_native_input_recommends_recording_configuration(self):
        source_16k = parse_pactl_short_devices(
            "0\talsa_input.usb-test\tmodule\ts16le 2ch 16000Hz\tIDLE\n"
        )[0]
        self.assertEqual(
            preferred_recording_configuration(source_16k),
            (16000, 2, "S16_LE"),
        )
        source_48k = parse_pactl_short_devices(
            "0\talsa_input.usb-test\tmodule\ts16le 2ch 48000Hz\tIDLE\n"
        )[0]
        self.assertEqual(
            preferred_recording_configuration(source_48k),
            (48000, 2, "S16_LE"),
        )

    def test_recording_config_validation(self):
        config = AudioRecordingConfig(
            "alsa_input.usb-mic",
            "/home/user/audio/record.wav",
            16000,
            2,
            "S16_LE",
            10,
        )
        self.assertEqual(validate_recording_config(config), config)
        for invalid in (
            AudioRecordingConfig("", "/home/user/record.wav", 16000, 2, "S16_LE", 10),
            AudioRecordingConfig("source", "", 16000, 2, "S16_LE", 10),
            AudioRecordingConfig("source", "/home/user/record.mp3", 16000, 2, "S16_LE", 10),
            AudioRecordingConfig("source", "/home/user/record.wav", 0, 2, "S16_LE", 10),
            AudioRecordingConfig("source", "/home/user/record.wav", 16000, 0, "S16_LE", 10),
            AudioRecordingConfig("source", "/home/user/record.wav", 16000, 2, "S24_LE", 10),
            AudioRecordingConfig("source", "/home/user/record.wav", 16000, 2, "S16_LE", 0),
            AudioRecordingConfig("source", "/home/user/record.wav", 16000, 2, "S16_LE", -1),
        ):
            with self.assertRaises(ValueError):
                validate_recording_config(invalid)

    def test_recording_command_quotes_source_and_remote_paths(self):
        simple = build_recording_command(
            AudioRecordingConfig("alsa_input.usb-mic", "/home/user/audio/record.wav", 16000, 2, "S16_LE", 10)
        )
        self.assertIn("parecord", simple)
        self.assertIn("--format=s16le", simple)
        self.assertIn("--rate=16000", simple)
        self.assertIn("--channels=2", simple)
        spaced = build_recording_command(
            AudioRecordingConfig(
                "alsa_input.usb microphone's test",
                "/home/user/audio recordings/mic test.wav",
                48000,
                4,
                "S16_LE",
                None,
            )
        )
        self.assertIn("'/home/user/audio recordings/mic test.wav'", spaced)
        self.assertIn("'alsa_input.usb microphone'\"'\"'s test'", spaced)
        injection = build_recording_command(
            AudioRecordingConfig("source;touch /tmp/pwned", "/home/user/record.wav", 16000, 2, "S16_LE", None)
        )
        self.assertEqual(shlex.split(injection)[1], "--device=source;touch /tmp/pwned")

    def test_remote_audio_path_normalization_and_playback_command(self):
        relative = "~/audio_test_logs/manual/record_001.wav"
        absolute = "/home/agx/audio_test_logs/manual/record_001.wav"
        self.assertEqual(normalize_remote_audio_path(relative, "/home/agx"), absolute)
        self.assertEqual(normalize_remote_audio_path(absolute, "/home/agx"), absolute)
        self.assertEqual(
            build_playback_command(relative, "/home/agx"),
            "paplay /home/agx/audio_test_logs/manual/record_001.wav",
        )
        with self.assertRaises(ValueError):
            build_playback_command(relative)
        recording_command = build_recording_command(
            AudioRecordingConfig("source", relative, 48000, 2, "S16_LE", 10),
            "/home/agx",
        )
        self.assertIn("/home/agx/audio_test_logs/manual/record_001.wav", recording_command)
        self.assertNotIn("~/audio_test_logs/manual/record_001.wav", recording_command)

    def test_recording_output_verification(self):
        valid = verify_recording_size("/home/user/record.wav", 320044)
        self.assertTrue(valid.valid)
        self.assertTrue(valid.exists)
        self.assertEqual(valid.size_bytes, 320044)
        self.assertFalse(verify_recording_size("/home/user/record.wav", None).valid)
        self.assertFalse(verify_recording_size("/home/user/record.wav", 0).valid)
        self.assertFalse(verify_recording_size("/home/user/record.wav", 44).valid)

    def test_playback_path_validation_and_safe_command_construction(self):
        self.assertEqual(validate_playback_path(" /home/agx/audio/test.wav "), "/home/agx/audio/test.wav")
        self.assertEqual(build_playback_command("/home/agx/audio files/test speaker.wav"), "paplay '/home/agx/audio files/test speaker.wav'")
        self.assertEqual(build_playback_command("/home/agx/audio/test's.wav"), "paplay '/home/agx/audio/test'\"'\"'s.wav'")
        for path in ("", "/home/agx/test.mp3", "/home/agx/test.wav\ncommand"):
            with self.assertRaises(ValueError):
                validate_playback_path(path)
        self.assertEqual(validate_playback_path("/home/agx/test.WAV"), "/home/agx/test.WAV")
    def test_volume_parsing_uses_maximum_channel_value(self):
        for value in (0, 50, 75, 100):
            self.assertEqual(parse_sink_volume(f"Volume: {value}%"), value)
        self.assertEqual(parse_sink_volume("Volume: front-left: 60%, front-right: 60%"), 60)
        self.assertEqual(parse_sink_volume("Volume: front-left: 40%, front-right: 60%"), 60)
        self.assertIsNone(parse_sink_volume(""))
        self.assertIsNone(parse_sink_volume("Volume: unavailable"))

    def test_mute_parsing_and_volume_validation(self):
        self.assertTrue(parse_sink_mute("Mute: yes\n"))
        self.assertFalse(parse_sink_mute("Mute: no\n"))
        self.assertIsNone(parse_sink_mute(""))
        self.assertIsNone(parse_sink_mute("Mute: maybe"))
        self.assertEqual(validate_volume(0), 0)
        self.assertEqual(validate_volume(100), 100)
        for value in (-1, 101, "50", True):
            with self.assertRaises(ValueError):
                validate_volume(value)

    def test_pactl_info_extracts_defaults_and_accepts_missing_values(self):
        output = """Server String: /run/user/1000/pulse/native
Default Sink: alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo
Default Source: alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo
"""
        self.assertEqual(
            parse_pactl_info(output),
            (
                "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo",
                "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo",
            ),
        )
        self.assertEqual(parse_pactl_info("Default Sink: (null)\n"), (None, None))
        self.assertEqual(parse_pactl_info(""), (None, None))

    def test_pactl_short_lists_keep_identifiers_and_make_labels_readable(self):
        sink_output = """42\talsa_output.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tRUNNING
43\talsa_output.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED
"""
        devices = parse_pactl_short_devices(sink_output)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].display_name, "Seeed Studio reSpeaker XVF3800")
        self.assertEqual(devices[0].identifier.split(".")[0], "alsa_output")
        self.assertEqual(devices[1].display_name, "Built-in Audio")
        source_devices = parse_pactl_short_devices(
            "7\talsa_input.usb-Seeed_Studio_reSpeaker_XVF3800-00.analog-stereo\tPipeWire\n"
        )
        self.assertEqual(source_devices[0].display_name, "Seeed Studio reSpeaker XVF3800")
        self.assertTrue(source_devices[0].identifier.startswith("alsa_input."))
        self.assertEqual(parse_pactl_short_devices(""), [])
        self.assertEqual(readable_device_name("alsa_input.usb-Generic_Mic-00.analog-stereo"), "Generic Mic")

    def test_full_pactl_devices_keep_internal_name_and_description(self):
        output = """Sink #4
\tName: alsa_output.usb-test
\tDescription: USB Speaker
\tState: IDLE
Sink #5
\tName: alsa_output.other
\tDescription: Platform Audio
"""
        devices = parse_pactl_devices(output)
        self.assertEqual(devices[0].name, "alsa_output.usb-test")
        self.assertEqual(devices[0].description, "USB Speaker")
        self.assertEqual(devices[0].index, 4)
        self.assertEqual(resolve_device_description(devices, "alsa_output.usb-test"), "USB Speaker")
        self.assertIsNone(resolve_device_description(devices, "alsa_output.missing"))

    def test_default_routing_commands_quote_internal_names(self):
        sink = "alsa_output.usb speaker's test"
        source = "alsa_input.usb microphone's test"
        self.assertEqual(build_default_sink_command(sink), "pactl set-default-sink 'alsa_output.usb speaker'\"'\"'s test'")
        self.assertEqual(build_default_source_command(source), "pactl set-default-source 'alsa_input.usb microphone'\"'\"'s test'")
        for builder in (build_default_sink_command, build_default_source_command):
            with self.assertRaises(ValueError):
                builder("")
            with self.assertRaises(ValueError):
                builder("alsa_output.bad\ncommand")

    def test_routing_verification_matches_the_requested_default_only(self):
        self.assertTrue(routing_target_matches("sink", "alsa_output.usb-test", "alsa_output.usb-test", "alsa_input.other"))
        self.assertTrue(routing_target_matches("source", "alsa_input.usb-test", "alsa_output.other", "alsa_input.usb-test"))
        self.assertFalse(routing_target_matches("sink", "alsa_output.usb-test", "alsa_output.other", "alsa_input.usb-test"))
        self.assertFalse(routing_target_matches("source", "alsa_input.usb-test", "alsa_output.other", None))

    def test_alsa_card_and_device_parsing(self):
        cards = """ 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0x..., irq 145
 1 [XVF3800        ]: USB-Audio - ReSpeaker XVF3800
                      Seeed Studio ReSpeaker XVF3800 at usb-0000:00:14.0-1, high speed
"""
        devices = """card 1: XVF3800 [ReSpeaker XVF3800], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""
        self.assertEqual(
            parse_alsa_cards(cards),
            ["Card 0: HDA Intel PCH", "Card 1: ReSpeaker XVF3800"],
        )
        self.assertEqual(
            parse_alsa_devices(devices),
            ["Card 1, device 0: ReSpeaker XVF3800 — USB Audio"],
        )
        self.assertEqual(parse_alsa_cards(""), [])
        self.assertEqual(parse_alsa_devices("no soundcards found..."), [])

    def test_snapshot_uses_empty_output_and_errors_safely(self):
        snapshot = snapshot_from_outputs({}, {"pactl info": "pactl command not found"})
        self.assertIsNone(snapshot.default_sink)
        self.assertEqual(snapshot.sinks, [])
        self.assertFalse(snapshot.has_devices)
        self.assertEqual(snapshot.command_errors["pactl info"], "pactl command not found")

    def test_snapshot_prefers_full_pactl_descriptions(self):
        snapshot = snapshot_from_outputs({
            "pactl info": "Default Sink: alsa_output.usb-test\nDefault Source: alsa_input.usb-test\n",
            "pactl list short sinks": "1\talsa_output.usb-test\tmodule\n",
            "pactl list short sources": "2\talsa_input.usb-test\tmodule\n",
            "pactl list sinks": "Sink #1\nName: alsa_output.usb-test\nDescription: USB Speaker\n",
            "pactl list sources": "Source #2\nName: alsa_input.usb-test\nDescription: USB Microphone\n",
        })
        self.assertEqual(snapshot.sinks[0].description, "USB Speaker")
        self.assertEqual(snapshot.sources[0].description, "USB Microphone")


if __name__ == "__main__":
    unittest.main()
