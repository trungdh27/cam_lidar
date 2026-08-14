import asyncio
import json
import shlex
import unittest
from dataclasses import replace
from types import SimpleNamespace

from devices.livox.models import LivoxDiscoveryStatus
from devices.livox.profile import load_default_livox_profile
from devices.livox.sdk2_backend import (
    LivoxSDK2Backend,
    LivoxSDK2EnvironmentError,
)


def _result(exit_status=0, payload=None, log=""):
    stdout = log
    if payload is not None:
        stdout += "LIVOX_DISCOVERY_JSON=" + json.dumps(payload)
    return SimpleNamespace(
        exit_status=exit_status,
        stdout=stdout,
        stderr="",
    )


class _FakeSSH:
    def __init__(self, discovery_result=None, helper_installed=True):
        self.commands = []
        self.discovery_result = discovery_result or _result(
            payload={
                "ok": True,
                "found": True,
                "mismatch": False,
                "model": "MID360",
                "dev_type": 9,
                "serial": "ARMCP4D0032262",
                "lidar_ip": "192.168.1.162",
                "handle": 42,
                "sdk_version": "1.2.3",
            },
            log="arbitrary SDK log that must not be parsed\n",
        )
        self.helper_installed = helper_installed

    async def run(self, command, timeout):
        self.commands.append((command, timeout))
        if command.startswith("test -x"):
            return _result(exit_status=0 if self.helper_installed else 1)
        if command.endswith(" --version"):
            return _result(payload={"ok": True, "sdk_version": "1.2.3"})
        return self.discovery_result


class LivoxSDK2BackendTest(unittest.TestCase):
    def test_check_environment_reports_helper_sdk_and_fixed_profile(self):
        environment = asyncio.run(
            LivoxSDK2Backend(_FakeSSH()).check_environment()
        )

        self.assertEqual(environment["helper_installed"], True)
        self.assertEqual(environment["sdk_version"], "1.2.3")
        self.assertEqual(environment["host_ip"], "192.168.1.5")
        self.assertEqual(environment["expected_lidar_ip"], "192.168.1.162")

    def test_discovery_uses_profile_cli_and_marker_json(self):
        ssh = _FakeSSH()

        result = asyncio.run(
            LivoxSDK2Backend(ssh).discover(
                expected_model="MID360",
                timeout=8,
            )
        )

        command, timeout = ssh.commands[-1]
        self.assertEqual(
            shlex.split(command),
            [
                "$HOME/.cam_lidar/bin/livox_discover",
                "--host-ip",
                "192.168.1.5",
                "--expected-lidar-ip",
                "192.168.1.162",
                "--model",
                "MID360",
                "--timeout",
                "8",
            ],
        )
        self.assertEqual(timeout, 13.0)
        self.assertTrue(result.found)
        self.assertEqual(result.status, LivoxDiscoveryStatus.FOUND)
        self.assertEqual(result.handle, 42)
        self.assertEqual(result.raw_result["payload"]["handle"], 42)
        self.assertIn("arbitrary SDK log", result.raw_result["stdout"])

    def test_mid360s_discovery_preserves_model_in_option_cli(self):
        ssh = _FakeSSH(
            discovery_result=_result(
                payload={
                    "ok": True,
                    "found": True,
                    "mismatch": False,
                    "model": "MID360S",
                    "dev_type": 35,
                    "serial": "ARMCP4D0032262",
                    "lidar_ip": "192.168.1.162",
                    "handle": 43,
                    "sdk_version": "1.3.1",
                }
            )
        )

        result = asyncio.run(
            LivoxSDK2Backend(ssh).discover(
                expected_model="MID360S",
                timeout=8,
            )
        )

        command, _timeout = ssh.commands[-1]
        self.assertEqual(shlex.split(command)[-4:], ["--model", "MID360S", "--timeout", "8"])
        self.assertTrue(result.found)
        self.assertEqual(result.model, "MID360S")
        self.assertEqual(result.dev_type, 35)

    def test_check_environment_reports_missing_helper_without_raising(self):
        ssh = _FakeSSH(helper_installed=False)

        environment = asyncio.run(LivoxSDK2Backend(ssh).check_environment())

        self.assertEqual(
            environment,
            {
                "helper_installed": False,
                "helper_path": "$HOME/.cam_lidar/bin/livox_discover",
                "sdk_version": None,
                "host_ip": "192.168.1.5",
                "expected_lidar_ip": "192.168.1.162",
            },
        )
        self.assertEqual(
            ssh.commands,
            [('test -x "$HOME/.cam_lidar/bin/livox_discover"', 5)],
        )

    def test_missing_helper_raises_actionable_message(self):
        ssh = _FakeSSH(helper_installed=False)

        with self.assertRaisesRegex(
            LivoxSDK2EnvironmentError,
            "Livox discovery helper is missing on Jetson",
        ) as raised:
            asyncio.run(
                LivoxSDK2Backend(ssh).discover(expected_model="MID360")
            )

        self.assertEqual(
            str(raised.exception),
            "Livox discovery helper is missing on Jetson.\n"
            "Deploy it with:\n"
            "./scripts/deploy_livox_helper.sh <user>@<jetson-ip>",
        )

    def test_exit_code_two_returns_device_not_found(self):
        ssh = _FakeSSH(
            discovery_result=_result(
                exit_status=2,
                payload={
                    "ok": True,
                    "found": False,
                    "mismatch": False,
                    "reason": "timeout",
                    "host_ip": "192.168.1.5",
                    "expected_lidar_ip": "192.168.1.162",
                    "sdk_version": "1.2.3",
                },
            )
        )

        result = asyncio.run(
            LivoxSDK2Backend(ssh).discover(expected_model="MID360")
        )

        self.assertFalse(result.found)
        self.assertEqual(result.status, LivoxDiscoveryStatus.DEVICE_NOT_FOUND)
        self.assertEqual(result.reason, "timeout")
        self.assertEqual(result.exit_code, 2)

    def test_exit_code_two_maps_detected_ip_mismatch(self):
        ssh = _FakeSSH(
            discovery_result=_result(
                exit_status=2,
                payload={
                    "ok": True,
                    "found": False,
                    "mismatch": True,
                    "reason": "detected_device_does_not_match",
                    "detected_model": "MID360",
                    "detected_dev_type": 9,
                    "detected_serial": "OTHER",
                    "detected_lidar_ip": "192.168.1.163",
                    "detected_handle": 7,
                    "sdk_version": "1.2.3",
                },
            )
        )

        result = asyncio.run(
            LivoxSDK2Backend(ssh).discover(expected_model="MID360")
        )

        self.assertFalse(result.found)
        self.assertEqual(result.status, LivoxDiscoveryStatus.IP_MISMATCH)
        self.assertEqual(result.lidar_ip, "192.168.1.163")
        self.assertEqual(result.serial, "OTHER")
        self.assertEqual(result.handle, 7)

    def test_serial_mismatch_is_advisory_unless_strict(self):
        payload = {
            "ok": True,
            "found": True,
            "model": "MID360",
            "serial": "OTHER",
            "lidar_ip": "192.168.1.162",
            "sdk_version": "1.2.3",
        }
        advisory_result = asyncio.run(
            LivoxSDK2Backend(
                _FakeSSH(discovery_result=_result(payload=payload))
            ).discover(expected_model="MID360")
        )

        profile = load_default_livox_profile()
        strict_profile = replace(
            profile,
            lidar=replace(profile.lidar, strict_serial_verification=True),
        )
        strict_result = asyncio.run(
            LivoxSDK2Backend(
                _FakeSSH(discovery_result=_result(payload=payload)),
                network_profile=strict_profile,
            ).discover(expected_model="MID360")
        )

        self.assertTrue(advisory_result.found)
        self.assertFalse(advisory_result.serial_match)
        self.assertEqual(advisory_result.status, LivoxDiscoveryStatus.FOUND)
        self.assertFalse(strict_result.found)
        self.assertEqual(
            strict_result.status,
            LivoxDiscoveryStatus.SERIAL_MISMATCH,
        )


if __name__ == "__main__":
    unittest.main()
