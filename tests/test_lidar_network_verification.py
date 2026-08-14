import unittest

from core.network.models import (
    ManagementConnection,
    NetworkInterface,
    NetworkSnapshot,
    NetworkVerificationStatus,
)
from core.network.network_manager import NetworkManager


INTERFACE_NAME = "enxec9a0c19f45f"
JETSON_CIDR = "192.168.1.5/24"
LIDAR_IP = "192.168.1.162"


def _snapshot(**overrides):
    values = {
        "name": INTERFACE_NAME,
        "mac_address": "ec:9a:0c:19:f4:5f",
        "operstate": "UP",
        "carrier": True,
        "physical": True,
        "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
        "ipv4_addresses": [JETSON_CIDR],
    }
    values.update(overrides)
    return NetworkSnapshot(
        management=ManagementConnection(),
        interfaces=[NetworkInterface(**values)],
    )


def _verify(snapshot, lidar_ip=LIDAR_IP):
    return NetworkManager.verify_lidar_network(
        snapshot=snapshot,
        interface_name=INTERFACE_NAME,
        expected_jetson_cidr=JETSON_CIDR,
        expected_lidar_ip=lidar_ip,
    )


class LidarNetworkVerificationTest(unittest.TestCase):
    def test_fixed_profile_is_network_ready_without_gateway_or_default_route(self):
        result = _verify(_snapshot())

        self.assertTrue(result.ready)
        self.assertEqual(result.status, NetworkVerificationStatus.NETWORK_READY)
        self.assertEqual(result.mac_address, "ec:9a:0c:19:f4:5f")
        self.assertEqual(result.expected_network, "192.168.1.0/24")
        self.assertEqual(result.to_dict()["gateway"], None)
        self.assertFalse(result.to_dict()["gateway_required"])
        self.assertFalse(result.to_dict()["default_route_required"])

    def test_missing_or_nonphysical_interface_is_not_accepted(self):
        missing = NetworkSnapshot(
            management=ManagementConnection(),
            interfaces=[],
        )
        nonphysical = _snapshot(physical=False)

        self.assertEqual(
            _verify(missing).status,
            NetworkVerificationStatus.INTERFACE_NOT_FOUND,
        )
        self.assertEqual(
            _verify(nonphysical).status,
            NetworkVerificationStatus.INTERFACE_NOT_FOUND,
        )

    def test_link_requires_carrier_and_up_or_lower_up(self):
        no_carrier = _snapshot(carrier=False)
        down = _snapshot(operstate="DOWN", flags=["BROADCAST"])
        lower_up = _snapshot(operstate="UNKNOWN", flags=["UP", "LOWER_UP"])

        self.assertEqual(
            _verify(no_carrier).status,
            NetworkVerificationStatus.LINK_DOWN,
        )
        self.assertEqual(
            _verify(down).status,
            NetworkVerificationStatus.LINK_DOWN,
        )
        self.assertEqual(
            _verify(lower_up).status,
            NetworkVerificationStatus.NETWORK_READY,
        )

    def test_wrong_jetson_cidr_is_reported(self):
        result = _verify(_snapshot(ipv4_addresses=["192.168.1.5/16"]))

        self.assertFalse(result.ready)
        self.assertEqual(
            result.status,
            NetworkVerificationStatus.JETSON_IP_MISMATCH,
        )

    def test_lidar_outside_expected_network_is_reported(self):
        result = _verify(_snapshot(), lidar_ip="192.168.2.162")

        self.assertFalse(result.ready)
        self.assertEqual(
            result.status,
            NetworkVerificationStatus.SUBNET_MISMATCH,
        )


if __name__ == "__main__":
    unittest.main()
