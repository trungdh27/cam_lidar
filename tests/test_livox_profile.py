import copy
import unittest

import yaml

from devices.livox.profile import (
    DEFAULT_PROFILE_PATH,
    LivoxNetworkProfile,
    LivoxProfileError,
    load_default_livox_profile,
)


class LivoxProfileLoaderTest(unittest.TestCase):
    def test_loads_fixed_mid360_family_profile(self):
        profile = load_default_livox_profile()

        self.assertEqual(profile.jetson.interface, "enxec9a0c19f45f")
        self.assertEqual(profile.jetson.cidr, "192.168.1.5/24")
        self.assertEqual(profile.jetson.network, "192.168.1.0/24")
        self.assertEqual(str(profile.lidar.ip), "192.168.1.162")
        self.assertEqual(profile.lidar.expected_serial, "ARMCP4D0032262")
        self.assertFalse(profile.lidar.strict_serial_verification)
        self.assertEqual(profile.lidar.ip_access, "read_only")
        self.assertEqual(profile.discovery_port, 56000)
        self.assertEqual(profile.model("MID360").sdk_profile, "mid360")
        self.assertEqual(profile.model("MID360S").sdk_profile, "mid360s")
        self.assertTrue(
            profile.jetson.matches(
                "enxec9a0c19f45f",
                ["192.168.1.5/24"],
            )
        )
        self.assertFalse(
            profile.jetson.matches(
                "enxec9a0c19f45f",
                ["192.168.1.5/16"],
            )
        )

    def test_sdk_config_uses_validated_profile_ports(self):
        profile = load_default_livox_profile()
        config = profile.sdk_config()[profile.sdk_config_key]

        self.assertEqual(
            config["lidar_net_info"],
            {
                "cmd_data_port": 56100,
                "push_msg_port": 56200,
                "point_data_port": 56300,
                "imu_data_port": 56400,
                "log_data_port": 56500,
            },
        )
        self.assertEqual(
            config["host_net_info"][0],
            {
                "host_ip": "192.168.1.5",
                "multicast_ip": "224.1.1.5",
                "cmd_data_port": 56101,
                "push_msg_port": 56201,
                "point_data_port": 56301,
                "imu_data_port": 56401,
                "log_data_port": 56501,
            },
        )

    def test_rejects_invalid_or_mutable_lidar_ip_profile(self):
        with DEFAULT_PROFILE_PATH.open("r", encoding="utf-8") as stream:
            source = yaml.safe_load(stream)

        invalid_ip = copy.deepcopy(source)
        invalid_ip["jetson"]["ip"] = "not-an-ip"
        with self.assertRaises(LivoxProfileError):
            LivoxNetworkProfile.from_mapping(invalid_ip)

        mutable_lidar_ip = copy.deepcopy(source)
        mutable_lidar_ip["lidar"]["ip_access"] = "read_write"
        with self.assertRaises(LivoxProfileError):
            LivoxNetworkProfile.from_mapping(mutable_lidar_ip)


if __name__ == "__main__":
    unittest.main()
