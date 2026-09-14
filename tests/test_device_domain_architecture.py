import ast
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop_app.state.device_registry import DeviceRegistry
from desktop_app.testing.evidence_store import LidarEvidenceStore
from desktop_app.ui.main_window import MainWindow
from devices.livox.profile import load_default_livox_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: str) -> set[str]:
    tree = ast.parse((PROJECT_ROOT / path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class DeviceDomainArchitectureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_camera_and_lidar_imports_are_independent(self):
        camera_imports = _imports("desktop_app/ui/camera_page.py") | _imports(
            "desktop_app/workers/camera_test_runner_worker.py"
        )
        lidar_imports = _imports("desktop_app/ui/lidar_page.py")

        self.assertFalse(
            any(name.startswith("devices.livox") for name in camera_imports)
        )
        self.assertFalse(
            any(name.startswith("desktop_app.testing") for name in camera_imports)
        )
        self.assertFalse(
            any(name.startswith("devices.camera") for name in lidar_imports)
        )
        self.assertFalse(any(name.startswith("core.testing") for name in lidar_imports))

    def test_device_registry_accepts_only_summary_fields(self):
        registry = DeviceRegistry()
        registry.update_device(
            {
                "device": "LiDAR",
                "family": "Livox",
                "model": "MID360S",
                "available": "detected",
                "status": "streaming",
                "point_rate": 200_000,
                "packet_loss_percent": 0.0,
            }
        )
        record = registry.device("lidar")

        self.assertEqual(record["family"], "Livox")
        self.assertNotIn("point_rate", record)
        self.assertNotIn("packet_loss_percent", record)
        self.assertEqual(
            set(record).difference(DeviceRegistry.SUMMARY_FIELDS),
            set(),
        )

    def test_lidar_evidence_uses_only_lidar_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = LidarEvidenceStore(root)
            session_id = store.begin_session("LiDAR")

            self.assertTrue((root / "lidar" / session_id).is_dir())
            with self.assertRaises(ValueError):
                store.begin_session("camera")

    def test_livox_family_profiles_remain_available(self):
        profile = load_default_livox_profile()

        self.assertEqual(profile.model("MID360").display_name, "MID-360")
        self.assertEqual(profile.model("MID360S").display_name, "MID-360S")

    def test_main_window_constructs_both_device_pages(self):
        window = MainWindow()
        try:
            self.assertIsNotNone(window.camera_page)
            self.assertIsNotNone(window.lidar_page)
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
