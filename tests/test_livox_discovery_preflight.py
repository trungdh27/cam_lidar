import asyncio
import json
import unittest
from types import SimpleNamespace

from desktop_app.workers.livox_discovery_worker import LivoxDiscoveryWorker
from devices.livox.profile import load_default_livox_profile

from tests.test_lidar_ping import FAIL_OUTPUT, SUCCESS_OUTPUT


class _FakeConnectionService:
    pass


class _FakeSSH:
    connected = True

    def __init__(
        self,
        ping_output=SUCCESS_OUTPUT,
        ping_exit_status=0,
        helper_installed=True,
        sdk_init_error=False,
        jetson_prefix=24,
    ):
        self.commands = []
        self.ping_output = ping_output
        self.ping_exit_status = ping_exit_status
        self.helper_installed = helper_installed
        self.sdk_init_error = sdk_init_error
        self.jetson_prefix = jetson_prefix

    async def run(self, command, timeout):
        self.commands.append(command)
        if command == "ip -json link":
            return self._result(
                stdout=json.dumps(
                    [
                        {
                            "ifindex": 2,
                            "ifname": "enxec9a0c19f45f",
                            "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
                            "mtu": 1500,
                            "operstate": "UP",
                            "link_type": "ether",
                            "address": "ec:9a:0c:19:f4:5f",
                        }
                    ]
                )
            )
        if command == "ip -json addr":
            return self._result(
                stdout=json.dumps(
                    [
                        {
                            "ifindex": 2,
                            "ifname": "enxec9a0c19f45f",
                            "addr_info": [
                                {
                                    "family": "inet",
                                    "local": "192.168.1.5",
                                    "prefixlen": self.jetson_prefix,
                                }
                            ],
                        }
                    ]
                )
            )
        if command.startswith('printf "%s'):
            return self._result(stdout="10.0.0.2 50000 192.168.1.5 22\n")
        if "/sys/class/net" in command:
            return self._result(stdout="enxec9a0c19f45f|1|1|0\n")
        if command.startswith("LC_ALL=C ping"):
            return self._result(
                exit_status=self.ping_exit_status,
                stdout=self.ping_output,
            )
        if command.startswith("test -x"):
            return self._result(exit_status=0 if self.helper_installed else 1)
        if command.endswith(" --version"):
            return self._payload({"ok": True, "sdk_version": "1.2.3"})
        if "livox_discover --host-ip" in command:
            if self.sdk_init_error:
                return self._payload(
                    {
                        "ok": False,
                        "found": False,
                        "error_type": "sdk_init",
                        "error": "LivoxLidarSdkInit failed",
                        "sdk_version": "1.2.3",
                    },
                    exit_status=3,
                )
            return self._payload(
                {
                    "ok": True,
                    "found": True,
                    "model": "MID360",
                    "serial": "ARMCP4D0032262",
                    "lidar_ip": "192.168.1.162",
                    "sdk_version": "1.2.3",
                }
            )
        raise AssertionError(f"Unexpected command: {command}")

    @staticmethod
    def _result(exit_status=0, stdout="", stderr=""):
        return SimpleNamespace(
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
        )

    @classmethod
    def _payload(cls, payload, exit_status=0):
        return cls._result(
            exit_status=exit_status,
            stdout="LIVOX_DISCOVERY_JSON=" + json.dumps(payload)
        )


def _worker():
    return LivoxDiscoveryWorker(
        connection_service=_FakeConnectionService(),
        network_profile=load_default_livox_profile(),
        model="MID360",
    )


class LivoxDiscoveryPreflightTest(unittest.TestCase):
    def test_network_failure_stops_before_ping_helper_and_discovery(self):
        ssh = _FakeSSH(jetson_prefix=16)

        result = asyncio.run(_worker()._execute(ssh))

        self.assertEqual(result["status"], "JETSON_IP_MISMATCH")
        self.assertFalse(
            any(command.startswith("LC_ALL=C ping") for command in ssh.commands)
        )
        self.assertFalse(
            any(command.startswith("test -x") for command in ssh.commands)
        )
        self.assertFalse(
            any("livox_discover --host-ip" in command for command in ssh.commands)
        )

    def test_flow_is_verify_ping_helper_then_sdk_discovery(self):
        ssh = _FakeSSH()
        progress = []
        stages = []
        worker = _worker()
        worker.progress.connect(
            lambda level, message: progress.append((level, message))
        )
        worker.stage_changed.connect(stages.append)

        result = asyncio.run(worker._execute(ssh))

        ping_index = next(
            index
            for index, command in enumerate(ssh.commands)
            if command.startswith("LC_ALL=C ping")
        )
        helper_check_index = next(
            index
            for index, command in enumerate(ssh.commands)
            if command.startswith("test -x")
        )
        discovery_index = next(
            index
            for index, command in enumerate(ssh.commands)
            if "livox_discover --host-ip" in command
        )
        self.assertLess(ping_index, helper_check_index)
        self.assertLess(helper_check_index, discovery_index)
        self.assertTrue(result["found"])
        self.assertTrue(result["ping"]["reachable"])
        self.assertEqual(result["ping"]["average_rtt_ms"], 0.43)
        self.assertIn(("INFO", "Check Livox SDK2 helper"), progress)
        self.assertIn(("PASS", "MID360 detected"), progress)
        self.assertEqual(
            stages,
            [
                "Network Checking",
                "LiDAR Reachable",
                "SDK Checking",
                "Discovering Device",
                "Device Ready",
            ],
        )

    def test_ping_failure_stops_before_helper_and_discovery(self):
        ssh = _FakeSSH(ping_output=FAIL_OUTPUT, ping_exit_status=1)

        result = asyncio.run(_worker()._execute(ssh))

        self.assertFalse(result["found"])
        self.assertEqual(result["status"], "PING_FAILED")
        self.assertEqual(result["ping"]["received"], 0)
        self.assertFalse(
            any(command.startswith("test -x") for command in ssh.commands)
        )
        self.assertFalse(
            any("livox_discover --host-ip" in command for command in ssh.commands)
        )

    def test_missing_helper_stops_with_specific_status_and_deploy_guidance(self):
        ssh = _FakeSSH(helper_installed=False)
        progress = []
        worker = _worker()
        worker.progress.connect(
            lambda level, message: progress.append((level, message))
        )

        result = asyncio.run(worker._execute(ssh))

        self.assertEqual(result["status"], "SDK_HELPER_MISSING")
        self.assertIn("deploy_livox_helper.sh", result["reason"])
        self.assertFalse(
            any("livox_discover --host-ip" in command for command in ssh.commands)
        )
        self.assertTrue(
            any(level == "FAIL" and "missing on Jetson" in message for level, message in progress)
        )

    def test_sdk_init_error_is_not_reported_as_generic_discovery_error(self):
        result = asyncio.run(
            _worker()._execute(_FakeSSH(sdk_init_error=True))
        )

        self.assertFalse(result["found"])
        self.assertEqual(result["status"], "SDK_INIT_ERROR")
        self.assertIn("LivoxLidarSdkInit failed", result["reason"])
        self.assertEqual(result["environment"]["sdk_version"], "1.2.3")


if __name__ == "__main__":
    unittest.main()
