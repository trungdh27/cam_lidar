import asyncio
import shlex
import unittest
from types import SimpleNamespace

from core.network.network_manager import NetworkManager


SUCCESS_OUTPUT = """PING 192.168.1.162 (192.168.1.162) 56(84) bytes of data.
64 bytes from 192.168.1.162: icmp_seq=1 ttl=64 time=0.41 ms
64 bytes from 192.168.1.162: icmp_seq=2 ttl=64 time=0.43 ms
64 bytes from 192.168.1.162: icmp_seq=3 ttl=64 time=0.45 ms

--- 192.168.1.162 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2034ms
rtt min/avg/max/mdev = 0.410/0.430/0.450/0.016 ms
"""

FAIL_OUTPUT = """PING 192.168.1.162 (192.168.1.162) 56(84) bytes of data.

--- 192.168.1.162 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2045ms
"""


class _FakeSSH:
    connected = True

    def __init__(self, exit_status=0, stdout=SUCCESS_OUTPUT, stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    async def run(self, command, timeout):
        self.calls.append((command, timeout))
        return SimpleNamespace(
            exit_status=self.exit_status,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class LidarPingTest(unittest.TestCase):
    def test_parses_packet_counts_loss_and_average_rtt(self):
        result = NetworkManager.parse_ping_result(
            target_ip="192.168.1.162",
            interface_name="enxec9a0c19f45f",
            command="ping",
            exit_code=0,
            stdout=SUCCESS_OUTPUT,
            stderr="",
        )

        self.assertTrue(result.reachable)
        self.assertEqual(result.transmitted, 3)
        self.assertEqual(result.received, 3)
        self.assertEqual(result.packet_loss_percent, 0.0)
        self.assertEqual(result.average_rtt_ms, 0.43)

    def test_no_response_is_structured_failure(self):
        result = NetworkManager.parse_ping_result(
            target_ip="192.168.1.162",
            interface_name="enxec9a0c19f45f",
            command="ping",
            exit_code=1,
            stdout=FAIL_OUTPUT,
            stderr="",
        )

        self.assertFalse(result.reachable)
        self.assertEqual(result.transmitted, 3)
        self.assertEqual(result.received, 0)
        self.assertEqual(result.packet_loss_percent, 100.0)
        self.assertEqual(result.average_rtt_ms, None)

    def test_ping_runs_on_jetson_and_forces_fixed_interface(self):
        ssh = _FakeSSH()

        result = asyncio.run(
            NetworkManager(ssh).ping_lidar(
                interface_name="enxec9a0c19f45f",
                lidar_ip="192.168.1.162",
            )
        )

        command, timeout = ssh.calls[0]
        self.assertEqual(
            shlex.split(command),
            [
                "LC_ALL=C",
                "ping",
                "-I",
                "enxec9a0c19f45f",
                "-c",
                "3",
                "-W",
                "1",
                "192.168.1.162",
            ],
        )
        self.assertEqual(timeout, 6.0)
        self.assertTrue(result.reachable)


if __name__ == "__main__":
    unittest.main()
