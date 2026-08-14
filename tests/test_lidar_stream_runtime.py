import json
import unittest

from PySide6.QtCore import QObject, Signal

from desktop_app.services.lidar_stream_service import LidarStreamService
from desktop_app.state.lidar_runtime_state import (
    LidarRuntimeState,
    LidarStreamStatus,
)
from devices.livox.profile import load_default_livox_profile


class _FakeConnectionService(QObject):
    disconnected = Signal()
    remote_process_started = Signal(str)
    remote_process_output = Signal(str, str, str)
    remote_process_finished = Signal(str, int)
    remote_process_failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.is_connected = True
        self.commands = []
        self.stopped = []

    def start_remote_process(self, name, command):
        request_id = f"{name}:1"
        self.commands.append((request_id, command))
        return request_id

    def stop_remote_process(self, request_id):
        self.stopped.append(request_id)


class LidarStreamRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.connection = _FakeConnectionService()
        self.state = LidarRuntimeState()
        self.service = LidarStreamService(
            self.connection,
            self.state,
            load_default_livox_profile(),
        )

    def test_metrics_drive_streaming_only_after_real_point_data(self):
        self.assertTrue(self.service.start("MID360S"))
        request_id, command = self.connection.commands[-1]
        self.assertEqual(self.state.stream_status, LidarStreamStatus.STARTING)
        self.assertIn("--model MID360S", command)
        self.assertIn("--host-ip 192.168.1.5", command)
        self.assertIn("--expected-lidar-ip 192.168.1.162", command)

        payload = {
            "state": "STREAMING",
            "cloud_rate_hz": 10.0,
            "point_count": 200000,
            "point_count_unit": "pts/s",
            "point_packet_rate_hz": 144.0,
            "imu_status": "ACTIVE",
            "imu_rate_hz": 200.0,
            "packet_loss_percent": 0.0,
            "packet_loss_supported": True,
            "lidar_timestamp": 123456789,
            "lidar_time_type": 3,
            "last_packet_age_ms": 2.0,
            "packet_counter": 172,
            "point_counter": 200000,
            "uptime_sec": 1.0,
        }
        self.connection.remote_process_output.emit(
            request_id,
            "stdout",
            "LIVOX_STREAM_METRICS=" + json.dumps(payload),
        )

        self.assertEqual(self.state.stream_status, LidarStreamStatus.STREAMING)
        self.assertEqual(self.state.point_count, 200000)
        self.assertEqual(self.state.imu_status, "ACTIVE")
        self.assertEqual(self.state.lidar_timestamp, 123456789)

        self.assertTrue(self.service.stop())
        self.assertEqual(self.state.stream_status, LidarStreamStatus.STOPPING)
        self.assertEqual(self.connection.stopped, [request_id])
        self.connection.remote_process_output.emit(
            request_id,
            "stdout",
            'LIVOX_STREAM_EVENT={"event":"sdk_uninitialized"}',
        )
        self.connection.remote_process_finished.emit(request_id, 0)
        self.assertEqual(self.state.stream_status, LidarStreamStatus.IDLE)
        self.assertEqual(self.state.point_count, 200000)

    def test_duplicate_start_is_rejected_and_mid360_is_preserved(self):
        self.assertTrue(self.service.start("MID360"))
        self.assertFalse(self.service.start("MID360S"))
        self.assertEqual(len(self.connection.commands), 1)
        self.assertIn("--model MID360", self.connection.commands[0][1])

    def test_process_failure_sets_error(self):
        self.assertTrue(self.service.start("MID360S"))
        request_id = self.service.request_id
        self.connection.remote_process_output.emit(
            request_id,
            "stderr",
            "helper failure",
        )
        self.connection.remote_process_finished.emit(request_id, 3)
        self.assertEqual(self.state.stream_status, LidarStreamStatus.ERROR)
        self.assertIn("helper failure", self.state.last_error)

    def test_clean_exit_without_stop_is_still_an_error(self):
        self.assertTrue(self.service.start("MID360S"))
        request_id = self.service.request_id
        self.connection.remote_process_finished.emit(request_id, 0)
        self.assertEqual(self.state.stream_status, LidarStreamStatus.ERROR)
        self.assertIn("unexpectedly", self.state.last_error)

    def test_stale_and_missing_imu_are_reported_without_fake_values(self):
        logs = []
        self.service.log.connect(lambda level, message: logs.append((level, message)))
        self.assertTrue(self.service.start("MID360S"))
        request_id = self.service.request_id
        payload = {
            "state": "STALE",
            "cloud_rate_hz": 0.0,
            "point_count": 0,
            "point_count_unit": "pts/s",
            "point_packet_rate_hz": 0.0,
            "imu_status": "IDLE",
            "imu_rate_hz": 0.0,
            "packet_loss_percent": None,
            "packet_loss_supported": False,
            "lidar_timestamp": None,
            "last_packet_age_ms": None,
            "uptime_sec": 5.0,
        }
        self.connection.remote_process_output.emit(
            request_id,
            "stdout",
            "LIVOX_STREAM_METRICS=" + json.dumps(payload),
        )
        self.assertEqual(self.state.stream_status, LidarStreamStatus.STALE)
        self.assertEqual(self.state.imu_status, "IDLE")
        self.assertFalse(self.state.packet_loss_supported)
        self.assertIsNone(self.state.packet_loss_percent)
        self.assertIn(("WARNING", "Point cloud stream is stale"), logs)

    def test_disconnected_start_idle_stop_and_active_disconnect_are_safe(self):
        self.connection.is_connected = False
        self.assertFalse(self.service.start("MID360S"))
        self.assertEqual(self.state.stream_status, LidarStreamStatus.ERROR)
        self.assertFalse(self.service.stop())

        self.connection.is_connected = True
        self.assertTrue(self.service.start("MID360S"))
        self.connection.disconnected.emit()
        self.assertFalse(self.service.active)
        self.assertEqual(self.state.stream_status, LidarStreamStatus.ERROR)
        self.assertIn("disconnected", self.state.last_error.lower())


if __name__ == "__main__":
    unittest.main()
