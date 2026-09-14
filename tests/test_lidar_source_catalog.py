import ast
import csv
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from devices.livox.profile import load_default_livox_profile
from devices.livox.testing.lidar_tests import build_lidar_test_registry
from devices.livox.testing.source_catalog import (
    LidarSourceCatalogError,
    SOURCE_FIELDS,
    _load_source_rows,
)
from devices.livox.testing.test_result import TestResult, TestStatus


EXPECTED_IDS = [
    "TC-ENV-001", "TC-NET-001",
    *[f"TC-ROS-{value:03d}" for value in range(1, 8)],
    *[f"TC-PCD-{value:03d}" for value in range(1, 5)],
    *[f"TC-IMU-{value:03d}" for value in range(1, 5)],
    "TC-ROB-001", "TC-ROB-002",
    "TC-SYS-001", "TC-SYS-002", "TC-SYS-003",
    *[f"TC-SLAM-{value:03d}" for value in range(1, 5)],
]


class LidarSourceCatalogTest(unittest.TestCase):
    def setUp(self):
        definitions = build_lidar_test_registry(load_default_livox_profile()).get_tests("lidar")
        self.native = [item for item in definitions if item.id.startswith("LID-")]
        self.source = [item for item in definitions if item.source_test_id]

    def test_exact_source_ids_and_native_compatibility(self):
        self.assertEqual([item.id for item in self.source], EXPECTED_IDS)
        self.assertEqual(len(self.native), 10)
        self.assertEqual(len({item.id for item in self.native + self.source}), 36)

    def test_source_status_never_initializes_runtime_status(self):
        historical = {item.id: item.source_status for item in self.source}
        self.assertEqual(historical["TC-PCD-003"], "FAIL")
        self.assertEqual(TestResult("TC-PCD-003").status, TestStatus.NOT_RUN)
        self.assertTrue(all(TestResult(item.id).status is TestStatus.NOT_RUN for item in self.source))

    def test_groups_levels_and_details_metadata(self):
        groups = {item.group for item in self.source}
        self.assertEqual(groups, {
            "Environment & Setup", "Network & IP", "ROS Driver & Topics",
            "Point Cloud Quality", "IMU & Timestamp", "Reliability & Recovery",
            "System Stability", "Robot Motion", "System Initialization",
            "SLAM & Mapping Integration",
        })
        levels = Counter(item.automation_level.value for item in self.source)
        self.assertEqual(levels, {"AUTO": 13, "GUIDED": 13})
        self.assertEqual(next(item for item in self.source if item.id == "TC-ROB-002").automation_level.value, "GUIDED")
        for item in self.source:
            self.assertTrue(item.purpose)
            self.assertTrue(item.precondition)
            self.assertTrue(item.requirements)
            self.assertTrue(item.procedure)
            self.assertTrue(item.expected_result)
            self.assertTrue(item.parameters)
            self.assertTrue(item.implemented_by)

    def test_duplicate_csv_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.csv"
            row = ["TC-X-001", "Group", "Name", "Purpose", "Pre", "Req", "Proc", "Expected", "PASS", "Actual", "By", "Date", "Note"]
            with path.open("w", encoding="cp1252", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(SOURCE_FIELDS)
                writer.writerow(row)
                writer.writerow(row)
            with self.assertRaisesRegex(LidarSourceCatalogError, "Duplicate"):
                _load_source_rows(path, "cp1252")

    def test_new_lidar_modules_have_no_camera_or_core_testing_imports(self):
        root = Path(__file__).resolve().parents[1]
        paths = [
            root / "devices/livox/testing/source_catalog.py",
            root / "devices/livox/testing/ros2_profile.py",
            root / "devices/livox/testing/ros2_worker.py",
            root / "devices/livox/testing/ros2_executors.py",
            root / "devices/livox/testing/guided.py",
            root / "devices/livox/testing/guided_executor.py",
            root / "desktop_app/services/lidar_ros2_service.py",
            root / "desktop_app/ui/lidar_test_dialogs.py",
        ]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            self.assertFalse(any("camera" in value for value in imports), path)
            self.assertFalse(any(value.startswith("core.testing") for value in imports), path)


if __name__ == "__main__":
    unittest.main()
