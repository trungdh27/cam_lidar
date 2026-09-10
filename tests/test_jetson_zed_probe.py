import types
import unittest

from devices.camera.jetson_zed_probe import JETSON_ZED_PROBE


REAL_EXPLORER_OUTPUT = '''## Cam  0  ##
 Model :  "ZED X Mini"
 S/N :  58651554
 State :  "AVAILABLE"
 Path :  /dev/i2c-9
 ID :  0
 Port :  0
 Type :  "GMSL"
********************
'''


def _probe_namespace():
    namespace = {"__name__": "jetson_zed_probe_test"}
    exec(JETSON_ZED_PROBE, namespace)
    return namespace


def _candidate(model, serial, **extra):
    namespace = _probe_namespace()
    record = {
        "model": model,
        "raw_model": model,
        "normalized_model": namespace["normalize_model"](model),
        "model_family": namespace["model_family"](model),
        "serial_number": serial,
        "camera_id": "-",
        "state": "-",
        "firmware": "-",
        "interface": "GMSL2",
        "device_path": "-",
        "port": "-",
        "sdk_driver": "ZED SDK 5.0",
        "api": "Camera",
    }
    record.update(extra)
    return record


class JetsonZedExplorerParserTests(unittest.TestCase):
    def test_exact_real_explorer_block(self):
        namespace = _probe_namespace()
        namespace["shutil"] = types.SimpleNamespace(
            which=lambda command: "/usr/local/zed/tools/ZED_Explorer"
        )
        namespace["subprocess"] = types.SimpleNamespace(
            run=lambda *args, **kwargs: types.SimpleNamespace(
                stdout=REAL_EXPLORER_OUTPUT, stderr="", returncode=0
            )
        )

        devices, error = namespace["explorer_devices"]("5.0")

        self.assertIsNone(error)
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device["model"], "ZED X Mini")
        self.assertEqual(device["raw_model"], "ZED X Mini")
        self.assertEqual(device["serial_number"], "58651554")
        self.assertEqual(device["state"], "AVAILABLE")
        self.assertEqual(device["device_path"], "/dev/i2c-9")
        self.assertEqual(device["camera_id"], "0")
        self.assertEqual(device["port"], "0")
        self.assertEqual(device["interface"], "GMSL")
        self.assertEqual(device["model_family"], "zed_x_mini")
        self.assertEqual(device["sdk_driver"], "ZED SDK 5.0")
        self.assertEqual(device["api"], "ZED_Explorer --all")

    def test_supported_serial_id_and_transport_field_variants(self):
        namespace = _probe_namespace()
        variants = (
            ("S/N", "ID", "Type"),
            ("SN", "Camera ID", "Interface"),
            ("Serial", "ID", "Type"),
            ("Serial Number", "Camera ID", "Interface"),
        )
        for serial_key, id_key, interface_key in variants:
            with self.subTest(serial_key=serial_key, id_key=id_key, interface_key=interface_key):
                output = (
                    "## Cam 0 ##\n"
                    "Model: ZED X Mini\n"
                    f"{serial_key}: 58651554\n"
                    f"{id_key}: 0\n"
                    f"{interface_key}: GMSL\n"
                    "********************\n"
                )
                namespace["shutil"] = types.SimpleNamespace(which=lambda command: "ZED_Explorer")
                namespace["subprocess"] = types.SimpleNamespace(
                    run=lambda *args, **kwargs: types.SimpleNamespace(
                        stdout=output, stderr="", returncode=0
                    )
                )
                devices, error = namespace["explorer_devices"]("5.0")
                self.assertIsNone(error)
                self.assertEqual(devices[0]["serial_number"], "58651554")
                self.assertEqual(devices[0]["camera_id"], "0")
                self.assertEqual(devices[0]["interface"], "GMSL")

    def test_unframed_fields_do_not_emit_partial_records(self):
        namespace = _probe_namespace()
        namespace["shutil"] = types.SimpleNamespace(which=lambda command: "ZED_Explorer")
        namespace["subprocess"] = types.SimpleNamespace(
            run=lambda *args, **kwargs: types.SimpleNamespace(
                stdout="Model: ZED X Mini\nS/N: 58651554\n",
                stderr="",
                returncode=0,
            )
        )

        devices, error = namespace["explorer_devices"]("5.0")

        self.assertIsNone(error)
        self.assertEqual(devices, [])


class JetsonZedReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.deduplicate = _probe_namespace()["deduplicate_devices"]

    def test_sdk_and_explorer_same_serial_merge_with_specific_model(self):
        sdk = {
            "model": "ZED X",
            "serial_number": "58651554",
            "sdk_driver": "ZED SDK 5.0",
            "api": "Camera",
        }
        explorer = {
            "model": "ZED X Mini",
            "serial_number": "58651554",
            "device_path": "/dev/i2c-9",
            "camera_id": "0",
            "port": "0",
            "state": "AVAILABLE",
            "interface": "GMSL",
            "api": "ZED_Explorer --all",
        }

        devices = self.deduplicate([sdk, explorer])

        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device["model"], "ZED X Mini")
        self.assertEqual(device["model_family"], "zed_x_mini")
        self.assertEqual(device["serial_number"], "58651554")
        self.assertEqual(device["device_path"], "/dev/i2c-9")
        self.assertEqual(device["camera_id"], "0")
        self.assertEqual(device["port"], "0")
        self.assertEqual(device["state"], "AVAILABLE")
        self.assertEqual(device["interface"], "GMSL")
        self.assertEqual(device["sdk_driver"], "ZED SDK 5.0")

    def test_same_model_with_different_serials_remains_two_devices(self):
        devices = self.deduplicate([
            _candidate("ZED X Mini", "111"),
            _candidate("ZED X Mini", "222"),
        ])
        self.assertEqual(len(devices), 2)

    def test_different_models_with_different_serials_remain_two_devices(self):
        devices = self.deduplicate([
            _candidate("ZED X Mini", "111"),
            _candidate("ZED X One", "222"),
        ])
        self.assertEqual(len(devices), 2)

    def test_no_serial_record_merges_only_on_unique_physical_identity(self):
        known = _candidate(
            "ZED X", "58651554", device_path="/dev/i2c-9", camera_id="0", port="0"
        )
        fallback = _candidate(
            "ZED X Mini", "-", device_path="/dev/i2c-9", camera_id="0", port="0",
            interface="GMSL", api="ZED_Explorer --all",
        )
        devices = self.deduplicate([fallback, known])
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["serial_number"], "58651554")
        self.assertEqual(devices[0]["model"], "ZED X Mini")

    def test_no_serial_record_does_not_merge_by_model_alone(self):
        devices = self.deduplicate([
            _candidate("ZED X Mini", "111"),
            _candidate("ZED X Mini", "-"),
        ])
        self.assertEqual(len(devices), 2)


if __name__ == "__main__":
    unittest.main()
