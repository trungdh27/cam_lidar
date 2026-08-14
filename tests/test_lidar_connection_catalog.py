import unittest
from pathlib import Path

import yaml

from desktop_app.testing.lidar_tests import build_lidar_test_registry
from devices.livox.profile import load_default_livox_profile


CATALOG_PATH = (
    Path(__file__).parents[1]
    / "testcases"
    / "lidar"
    / "connection"
    / "connection.yaml"
)

EXPECTED_CASES = [
    ("LID-CON-001", "Ethernet Interface Detection"),
    ("LID-CON-002", "Jetson LiDAR Network Verification"),
    ("LID-CON-003", "LiDAR Ping"),
    ("LID-CON-004", "Livox SDK2 Environment Check"),
    ("LID-CON-005", "Device Discovery"),
    ("LID-CON-006", "LiDAR IP Verification"),
    ("LID-CON-007", "Model and Serial Identification"),
]

RESULT_SCHEMA = [
    "test_id",
    "status",
    "expected",
    "actual",
    "message",
    "metrics",
    "timestamp",
]


class LidarConnectionCatalogTest(unittest.TestCase):
    def test_yaml_catalog_contains_only_fixed_profile_connection_cases(self):
        with CATALOG_PATH.open("r", encoding="utf-8") as stream:
            catalog = yaml.safe_load(stream)

        self.assertEqual(catalog["mode"], "VERIFY_ONLY")
        self.assertEqual(catalog["result_schema"], RESULT_SCHEMA)
        self.assertEqual(
            [(item["id"], item["name"]) for item in catalog["tests"]],
            EXPECTED_CASES,
        )
        self.assertEqual(
            catalog["tests"][1]["expected"]["jetson_cidr"],
            "192.168.1.5/24",
        )
        self.assertEqual(
            catalog["tests"][1]["expected"]["lidar_ip"],
            "192.168.1.162",
        )
        self.assertEqual(
            catalog["tests"][6]["expected"]["serial"],
            "ARMCP4D0032262",
        )
        self.assertTrue(
            all(
                item.get("operation", "VERIFY_ONLY") == "VERIFY_ONLY"
                for item in catalog["tests"]
            )
        )

    def test_shared_registry_defines_priority_automation_cases(self):
        registry = build_lidar_test_registry(load_default_livox_profile())
        definitions = registry.get_tests("lidar")
        ids = [item.id for item in definitions]

        self.assertEqual(len(definitions), 10)
        self.assertEqual(
            ids[:3],
            ["LID-CON-001", "LID-CON-002", "LID-CON-003"],
        )
        self.assertIn("LID-STR-001", ids)
        self.assertIn("LID-TIM-001", ids)
        self.assertIn("LID-ROB-001", ids)
        self.assertTrue(all(item.executor for item in definitions))
        self.assertTrue(all(item.timeout_sec > 0 for item in definitions))


if __name__ == "__main__":
    unittest.main()
