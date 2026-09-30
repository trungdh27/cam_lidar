import asyncio
import unittest
from dataclasses import replace

from devices.camera.discovery import (
    AdapterDiscoveryResult,
    normalize_realsense_device,
    normalize_ros_camera_device,
    normalize_zed_device,
)
from devices.camera.inventory import (
    CameraInventory,
    CameraTargetSelection,
    deduplicate_camera_devices,
)
from devices.camera.profiles import profile_id_for_camera
from devices.camera.models import (
    CameraAccessMode,
    CameraPhysicalStatus,
    CameraRosReadiness,
    UsbSpeed,
    make_camera_device_uid,
    make_ros_namespace_hint,
    normalize_usb_speed,
)
from devices.camera.ros_registry import RosCameraDriverRegistry


class _Adapter:
    def __init__(self, name, devices=(), error=None):
        self.name = name
        self.devices = tuple(devices)
        self.error = error

    async def discover(self, _ssh):
        if self.error:
            raise RuntimeError(self.error)
        return AdapterDiscoveryResult(self.devices)


class _DriverProbe:
    def __init__(self, availability=None, ros2_available=True):
        self.availability = availability or {}
        self.ros2_available = ros2_available

    async def probe(self, _ssh, _packages):
        return dict(self.availability), self.ros2_available


def _zed(model, serial):
    return normalize_zed_device({
        "model": model,
        "serial_number": serial,
        "interface": "GMSL2",
        "device_path": "/dev/video-zed",
        "api": "Camera",
    })


def _d435i(serial="123456789", usb="2.1", profiles=None):
    return normalize_realsense_device({
        "model": "Intel RealSense D435I",
        "serial": serial,
        "firmware": "5.16.0",
        "physical_port": "2-1.3",
        "usb_type_descriptor": usb,
        "backend": "pyrealsense2",
        "profile_status": "AVAILABLE",
        "sensors": ["RGB Camera", "Stereo Module", "Motion Module"],
        "stream_profiles": profiles or [],
    })


class CameraNormalizationTests(unittest.TestCase):
    def test_direct_sdk_camera_keeps_direct_access_mode(self):
        device = _zed("ZED X Mini", "12345678")
        self.assertEqual(device.access_mode, CameraAccessMode.DIRECT_SDK)
        self.assertTrue(device.direct_sdk_available)

    def test_explorer_not_available_is_physical_busy_not_missing(self):
        device = normalize_zed_device({
            "model": "ZED X Mini", "serial_number": "53204228",
            "state": "NOT AVAILABLE", "device_path": "/dev/i2c-9", "port": "1",
            "interface": "GMSL", "api": "ZED_Explorer --all",
        })
        self.assertTrue(device.physical_detected)
        self.assertFalse(device.direct_sdk_available)
        self.assertTrue(device.busy)
        self.assertEqual(device.device_uid, "stereolabs:53204228")

    def test_explorer_available_xone_uhd_retains_direct_physical_device(self):
        device = normalize_zed_device({
            "model": "ZED XOne UHD", "serial_number": "315369161",
            "state": "AVAILABLE", "device_path": "/dev/i2c-10", "port": "3",
            "interface": "GMSL", "api": "ZED_Explorer --all",
        })
        self.assertEqual(device.model, "ZED X One 4K")
        self.assertEqual(device.metadata["sdk_model"], "ZED XOne UHD")
        self.assertTrue(device.direct_sdk_available)
        self.assertEqual(device.access_mode, CameraAccessMode.DIRECT_SDK)

    def test_hardware_model_maps_to_compatible_profile_not_normalized_id(self):
        self.assertEqual(profile_id_for_camera(_d435i()), "realsense_d435i")
        self.assertEqual(profile_id_for_camera(_zed("ZED X One 4K", "1")), "zed_x_one_4k")
        self.assertIsNone(profile_id_for_camera(_zed("ZED X One", "2")))

    def test_production_ros_device_info_is_runtime_authority(self):
        device = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X Mini",
            "serial_number": "53204228", "driver_name": "ZED SDK",
            "driver_version": "5.4.1", "input_type": "GMSL",
            "resolution": "1920x1080", "target_fps": "60.000000",
            "firmware_version": "2001", "capabilities": "left,right,depth",
            "ros_node": "/sensors/zed_x_mini",
            "device_info_topic": "/sensors/camera/zed_x_mini/device_info",
            "rgb_topic": "/sensors/camera/zed_x_mini/rgb",
            "stream_active": True,
        })
        self.assertEqual(device.device_uid, "stereolabs:53204228")
        self.assertEqual(device.access_mode, CameraAccessMode.ROS_READ_ONLY)
        self.assertTrue(device.busy)
        self.assertTrue(device.stream_active)
        self.assertEqual(device.metadata["resolution"], "1920x1080")
        self.assertEqual(device.metadata["target_fps"], "60.000000")

    def test_ros_topic_without_messages_is_discovered_but_not_monitorable(self):
        device = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "53204228",
            "ros_node": "/sensors/zed_x_mini", "rgb_topic": "/sensors/camera/zed_x_mini/rgb",
            "stream_active": False,
        })
        self.assertEqual(device.access_mode, CameraAccessMode.UNAVAILABLE)
        self.assertFalse(device.stream_active)
    def test_zed_x_mini_normalization_and_ros_mapping(self):
        device = _zed("ZED X Mini", "12345678")
        self.assertEqual(device.model, "ZED X Mini")
        self.assertEqual(device.normalized_model, "zed_x_mini")
        mapped = RosCameraDriverRegistry().map_device(
            device, {"zed_wrapper": True}
        )
        self.assertEqual(mapped.ros_driver, "zed_wrapper")
        self.assertEqual(mapped.ros_camera_model, "zedxm")
        self.assertEqual(mapped.ros_readiness, CameraRosReadiness.READY)

    def test_zed_x_one_4k_normalization_and_ros_mapping(self):
        device = _zed("CAMERA_MODEL_ONE.ZED_XONE_UHD", "319328083")
        self.assertEqual(device.model, "ZED X One 4K")
        self.assertEqual(device.normalized_model, "zed_x_one_4k")
        mapped = RosCameraDriverRegistry().map_device(
            device, {"zed_wrapper": True}
        )
        self.assertEqual(mapped.ros_camera_model, "zedxone4k")
        self.assertEqual(mapped.ros_namespace_hint, "/cameras/zedxone4k_319328083")

    def test_unknown_zed_is_kept_but_ros_model_is_unsupported(self):
        mapped = RosCameraDriverRegistry().map_device(
            _zed("Future ZED Camera", "99"), {"zed_wrapper": True}
        )
        self.assertEqual(mapped.physical_status, CameraPhysicalStatus.DETECTED)
        self.assertEqual(mapped.ros_driver, "zed_wrapper")
        self.assertEqual(mapped.ros_readiness, CameraRosReadiness.UNSUPPORTED_MODEL)

    def test_d435i_normalization_profiles_and_ros_mapping(self):
        device = _d435i(profiles=[{
            "stream": "depth", "format": "z16", "width": 848,
            "height": 480, "fps": 30,
        }, {
            "stream": "gyro", "format": "motion_xyz32f", "fps": 200,
        }])
        self.assertEqual(device.vendor, "Intel RealSense")
        self.assertEqual(device.model, "D435i")
        self.assertEqual(device.normalized_model, "d435i")
        self.assertEqual(device.usb_speed, UsbSpeed.USB_2)
        self.assertEqual(device.physical_status, CameraPhysicalStatus.DETECTED)
        self.assertEqual(device.stream_profiles[1].width, None)
        mapped = RosCameraDriverRegistry().map_device(
            device, {"realsense2_camera": True}
        )
        self.assertEqual(mapped.ros_driver, "realsense2_camera")
        self.assertEqual(mapped.ros_camera_model, "d435i")
        self.assertEqual(mapped.ros_readiness, CameraRosReadiness.READY)

    def test_usb_only_realsense_is_retained_as_partial_detail_not_failure(self):
        device = normalize_realsense_device({
            "model": "Intel(R) RealSense(TM) Depth Camera 435i",
            "serial": "42",
            "physical_port": "1-2",
            "usb_sysfs_speed": "480",
            "backend": "usb_sysfs",
            "profile_status": "UNAVAILABLE",
            "warnings": ["Detailed stream profiles unavailable."],
        })
        self.assertEqual(device.model, "D435i")
        self.assertEqual(device.usb_speed, UsbSpeed.USB_2)
        self.assertEqual(device.physical_status, CameraPhysicalStatus.DETECTED)
        self.assertEqual(device.profile_status.value, "UNAVAILABLE")
        mapped = RosCameraDriverRegistry().map_device(
            device, {"realsense2_camera": False}
        )
        self.assertEqual(mapped.physical_status, CameraPhysicalStatus.DETECTED)
        self.assertEqual(mapped.ros_readiness, CameraRosReadiness.DRIVER_MISSING)

    def test_usb_speed_normalization(self):
        for raw in ("USB 2.0", "2.1", "480", "480M", "High-Speed"):
            self.assertEqual(normalize_usb_speed(raw), UsbSpeed.USB_2, raw)
        for raw in ("USB 3.0", "3.2", "5000", "10000", "SuperSpeed"):
            self.assertEqual(normalize_usb_speed(raw), UsbSpeed.USB_3, raw)
        self.assertEqual(normalize_usb_speed("n/a"), UsbSpeed.UNKNOWN)

    def test_stable_device_uid_and_namespace_sanitization(self):
        self.assertEqual(
            make_camera_device_uid("Stereolabs", "319328083"),
            "stereolabs:319328083",
        )
        self.assertEqual(
            make_camera_device_uid("Intel RealSense", "123456789"),
            "intel-realsense:123456789",
        )
        self.assertEqual(
            make_ros_namespace_hint("ZED X Mini", "SN 12/3"),
            "/cameras/zed_x_mini_sn_12_3",
        )

    def test_deduplication_keeps_one_physical_camera_and_merges_profiles(self):
        first = _d435i()
        second = _d435i(profiles=[{
            "stream": "depth", "format": "z16", "width": 640,
            "height": 480, "fps": 30,
        }])
        result = deduplicate_camera_devices([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].stream_profiles), 1)

    def test_physical_and_production_ros_observations_reconcile_by_runtime_serial(self):
        physical = _zed("ZED X Mini", "53204228")
        ros = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "53204228",
            "ros_node": "/sensors/zed_x_mini", "rgb_topic": "/sensors/camera/zed_x_mini/rgb",
            "stream_active": True,
        })
        merged = deduplicate_camera_devices((physical, ros))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].serial, "53204228")
        self.assertEqual(merged[0].access_mode, CameraAccessMode.ROS_READ_ONLY)
        self.assertFalse(merged[0].direct_sdk_available)

    def test_current_robot_physical_zeds_and_optional_ros_enrichment_make_three_devices(self):
        mini_physical = normalize_zed_device({
            "model": "ZED X Mini", "serial_number": "53204228", "state": "NOT AVAILABLE",
            "device_path": "/dev/i2c-9", "port": "1", "interface": "GMSL",
        })
        mini_ros = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "53204228",
            "ros_node": "/sensors/zed_x_mini", "device_info_topic": "/sensors/camera/zed_x_mini/device_info",
            "rgb_topic": "/sensors/camera/zed_x_mini/rgb", "stream_active": True,
        })
        one_physical = normalize_zed_device({
            "model": "ZED XOne UHD", "serial_number": "315369161", "state": "AVAILABLE",
            "device_path": "/dev/i2c-10", "port": "3", "interface": "GMSL",
        })
        # A device_info topic without a bounded message is only namespace
        # enrichment; it cannot replace the physical X One identity.
        one_ros_incomplete = normalize_ros_camera_device({
            "ros_node": "/sensors/zed_x_one_s",
            "ros_camera_name": "zed_x_one_s",
            "device_info_topic": "/sensors/camera/zed_x_one_s/device_info",
            "rgb_topic": "/sensors/camera/zed_x_one_s/rgb", "stream_active": True,
        })
        devices = deduplicate_camera_devices((mini_physical, mini_ros, one_physical, one_ros_incomplete, _d435i("242322076751")))
        self.assertEqual(len(devices), 3)
        by_serial = {item.serial: item for item in devices}
        self.assertEqual(by_serial["53204228"].access_mode, CameraAccessMode.ROS_READ_ONLY)
        self.assertEqual(by_serial["53204228"].ros_node, "/sensors/zed_x_mini")
        self.assertEqual(by_serial["315369161"].access_mode, CameraAccessMode.DIRECT_SDK)
        self.assertEqual(by_serial["315369161"].physical_port, "/dev/i2c-10")

    def test_two_same_model_cameras_with_different_serials_remain_distinct(self):
        devices = deduplicate_camera_devices((
            normalize_ros_camera_device({
                "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "111",
                "rgb_topic": "/sensors/camera/one/rgb", "stream_active": True,
            }),
            normalize_ros_camera_device({
                "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "222",
                "rgb_topic": "/sensors/camera/two/rgb", "stream_active": True,
            }),
        ))
        self.assertEqual({device.serial for device in devices}, {"111", "222"})


class CameraInventoryTests(unittest.TestCase):
    def test_multiple_cameras_are_returned(self):
        devices = (_zed("ZED X Mini", "1"), _zed("ZED XOne UHD", "2"), _d435i("3"))
        inventory = CameraInventory(
            adapters=[_Adapter("mock", devices)],
            driver_probe=_DriverProbe({
                "zed_wrapper": True, "realsense2_camera": True,
            }),
        )
        snapshot = asyncio.run(inventory.discover_all(object()))
        self.assertEqual(len(snapshot.devices), 3)
        self.assertEqual({item.serial for item in snapshot.devices}, {"1", "2", "3"})
        self.assertEqual(snapshot.raw_candidate_count, 3)
        self.assertEqual(len(snapshot.adapter_candidates["mock"]), 3)

    def test_zed_ros_and_realsense_candidates_remain_separate(self):
        zed_mini = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X Mini", "serial_number": "111",
            "ros_node": "/sensors/zed_x_mini", "rgb_topic": "/sensors/camera/zed_x_mini/rgb",
            "stream_active": True,
        })
        zed_one = normalize_ros_camera_device({
            "vendor": "Stereolabs", "model": "ZED X One 4K", "serial_number": "222",
            "ros_node": "/sensors/zed_x_one_s", "rgb_topic": "/sensors/camera/zed_x_one_s/rgb",
            "stream_active": True,
        })
        inventory = CameraInventory(
            adapters=[_Adapter("production_ros", (zed_mini, zed_one)), _Adapter("realsense", (_d435i("333"),))],
            driver_probe=_DriverProbe(),
        )
        snapshot = asyncio.run(inventory.discover_all(object()))
        self.assertEqual(len(snapshot.devices), 3)
        self.assertEqual({item.serial for item in snapshot.devices}, {"111", "222", "333"})

    def test_one_adapter_failure_does_not_discard_other_vendor(self):
        inventory = CameraInventory(
            adapters=[
                _Adapter("zed", [_zed("ZED X Mini", "1")]),
                _Adapter("realsense", error="CLI missing"),
            ],
            driver_probe=_DriverProbe({"zed_wrapper": True}),
        )
        snapshot = asyncio.run(inventory.discover_all(object()))
        self.assertEqual(len(snapshot.devices), 1)
        self.assertIn("realsense", snapshot.adapter_errors)

    def test_ros_graph_empty_does_not_prevent_physical_inventory(self):
        inventory = CameraInventory(
            adapters=[_Adapter("realsense", [_d435i()])],
            driver_probe=_DriverProbe(
                {"zed_wrapper": None, "realsense2_camera": None},
                ros2_available=False,
            ),
        )
        snapshot = asyncio.run(inventory.discover_all(object()))
        self.assertEqual(len(snapshot.devices), 1)
        self.assertEqual(snapshot.devices[0].physical_status, CameraPhysicalStatus.DETECTED)
        self.assertEqual(snapshot.devices[0].ros_readiness, CameraRosReadiness.UNKNOWN)
        self.assertFalse(snapshot.ros2_available)

    def test_target_selection_survives_refresh_and_falls_back_safely(self):
        first, second = _zed("ZED X Mini", "1"), _d435i("2")
        target = CameraTargetSelection()
        target.select(second.device_uid)
        self.assertEqual(target.retain_after_refresh([first, second]), second.device_uid)
        refreshed_second = replace(second, physical_port="3-2")
        self.assertEqual(
            target.retain_after_refresh([first, refreshed_second]), second.device_uid
        )
        self.assertIsNone(target.retain_after_refresh([first]))
        self.assertEqual(target.selected_devices([first]), (first,))


if __name__ == "__main__":
    unittest.main()
