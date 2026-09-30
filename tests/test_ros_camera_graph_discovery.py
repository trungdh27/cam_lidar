import unittest
from types import SimpleNamespace

from devices.camera.ros_automation.discovery import discover_camera_graph


IMAGE = "sensor_msgs/msg/Image"
INFO = "sensor_msgs/msg/CameraInfo"
IMU = "sensor_msgs/msg/Imu"
POINTS = "sensor_msgs/msg/PointCloud2"
TEMPERATURE = "sensor_msgs/msg/Temperature"


def physical(uid="camera-1", model="Unknown Camera", serial="100", **fields):
    return SimpleNamespace(
        device_uid=uid, model=model, serial=serial, rgb_topic=fields.get("rgb_topic"),
        ros_node=fields.get("ros_node"), device_info_topic=fields.get("device_info_topic"),
        ros_namespace_hint=fields.get("ros_namespace_hint"),
    )


def graph(topics, **extra):
    return {
        "nodes": extra.get("nodes", ()),
        "topics": topics,
        "publisher_counts": {name: 1 for name in topics},
        "publisher_nodes": extra.get("publisher_nodes", {}),
        "publisher_metadata": extra.get("publisher_metadata", {}),
        "camera_metadata": extra.get("camera_metadata", {}),
    }


class RosCameraGraphDiscoveryTests(unittest.TestCase):
    def test_zed_style_topics_group_by_camera_and_associate_by_serial(self):
        snapshot = graph(
            {
                "/zed/rgb/color/rect/image": [IMAGE],
                "/zed/rgb/color/rect/camera_info": [INFO],
                "/zed/depth/depth_registered": [IMAGE],
                "/zed/imu/data": [IMU],
                "/zed/point_cloud/cloud_registered": [POINTS],
            },
            camera_metadata={"/zed": {"serial_number": "53204228", "model": "ZED X Mini"}},
        )
        result = discover_camera_graph(snapshot, [physical(serial="53204228")])
        self.assertEqual(len(result.candidates), 1)
        endpoint = result.candidates[0]
        self.assertTrue(endpoint.capabilities()["depth"])
        self.assertTrue(endpoint.capabilities()["imu"])
        self.assertTrue(result.associations[0].reliable)
        self.assertIn("serial", " ".join(result.associations[0].evidence))

    def test_realsense_style_topics_do_not_require_realsense_package_name(self):
        snapshot = graph({
            "/camera/camera/color/image_raw": [IMAGE],
            "/camera/camera/color/camera_info": [INFO],
            "/camera/camera/aligned_depth_to_color/image_raw": [IMAGE],
            "/camera/camera/imu": [IMU],
        })
        result = discover_camera_graph(snapshot, [physical(rgb_topic="/camera/camera/color/image_raw")])
        self.assertEqual(result.candidates[0].namespace, "/camera/camera")
        self.assertTrue(result.associations[0].reliable)

    def test_custom_and_front_camera_namespaces_are_equivalent(self):
        for prefix in ("/robot/sensors/camera1", "/front_camera", "/abc/robot/foo_cam"):
            with self.subTest(prefix=prefix):
                result = discover_camera_graph(graph({
                    prefix + "/image_raw": [IMAGE], prefix + "/camera_info": [INFO],
                }))
                self.assertEqual(len(result.candidates), 1)
                self.assertEqual(result.candidates[0].namespace, prefix)

    def test_multiple_cameras_remain_separate(self):
        snapshot = graph({
            "/front/image_raw": [IMAGE], "/front/camera_info": [INFO],
            "/rear/image_raw": [IMAGE], "/rear/camera_info": [INFO],
        })
        result = discover_camera_graph(snapshot, [
            physical("front", rgb_topic="/front/image_raw"),
            physical("rear", rgb_topic="/rear/image_raw"),
        ])
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(item.reliable for item in result.associations))

    def test_rgb_only_and_unknown_models_are_valid_generic_cameras(self):
        result = discover_camera_graph(
            graph({"/custom/image": [IMAGE]}),
            [physical(model="Future RGB Sensor", rgb_topic="/custom/image")],
        )
        endpoint = result.candidates[0]
        self.assertTrue(endpoint.capabilities()["image"])
        self.assertFalse(endpoint.capabilities()["depth"])
        self.assertFalse(endpoint.capabilities()["imu"])
        self.assertTrue(result.associations[0].reliable)

    def test_physical_without_ros_and_ros_without_physical_are_preserved(self):
        no_ros = discover_camera_graph(graph({}), [physical()])
        self.assertEqual(len(no_ros.candidates), 0)
        self.assertEqual(no_ros.associations[0].confidence, "UNKNOWN")
        orphan_ros = discover_camera_graph(graph({"/front/image_raw": [IMAGE]}))
        self.assertEqual(len(orphan_ros.candidates), 1)
        self.assertEqual(orphan_ros.associations, ())

    def test_ambiguous_name_hints_never_create_a_reliable_mapping(self):
        result = discover_camera_graph(graph({
            "/left/zed/image": [IMAGE], "/right/zed/image": [IMAGE],
        }), [physical(model="ZED")])
        self.assertFalse(result.associations[0].reliable)
        self.assertIn(result.associations[0].confidence, {"PARTIAL", "UNKNOWN"})

    def test_same_model_cameras_remain_ambiguous_without_identity_evidence(self):
        snapshot = graph({
            "/left/image": [IMAGE],
            "/right/image": [IMAGE],
        }, camera_metadata={
            "/left": {"model": "ZED X Mini"},
            "/right": {"model": "ZED X Mini"},
        })
        result = discover_camera_graph(snapshot, [
            physical("zed-left", model="ZED X Mini"),
            physical("zed-right", model="ZED X Mini"),
        ])
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(item.confidence in {"PARTIAL", "UNKNOWN"} for item in result.associations))
        self.assertFalse(any(item.reliable for item in result.associations))

    def test_unique_runtime_model_is_medium_evidence_not_high_identity(self):
        snapshot = graph({"/x/image": [IMAGE]}, camera_metadata={
            "/x": {"model": "ZED X Mini"},
        })
        result = discover_camera_graph(snapshot, [physical(model="ZED X Mini")])
        association = result.associations[0]
        self.assertEqual(association.confidence, "MEDIUM")
        self.assertFalse(association.reliable)

    def test_topic_with_no_publisher_is_retained_for_test_health_failure(self):
        result = discover_camera_graph({
            "nodes": ["/camera/subscriber"],
            "topics": {"/camera/image": [IMAGE]},
            "publisher_counts": {"/camera/image": 0},
            "publisher_nodes": {"/camera/image": []},
        })
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].image_topics, ("/camera/image",))

    def test_publisher_endpoint_and_qos_metadata_is_preserved(self):
        metadata = [{
            "node_name": "camera_node", "node_namespace": "/robot/camera",
            "reliability": "BEST_EFFORT", "durability": "VOLATILE",
            "history": "KEEP_LAST", "depth": 5,
        }]
        result = discover_camera_graph(graph(
            {"/robot/camera/image": [IMAGE]},
            publisher_nodes={"/robot/camera/image": ["/robot/camera/camera_node"]},
            publisher_metadata={"/robot/camera/image": metadata},
        ))
        self.assertEqual(
            result.candidates[0].publisher_metadata["/robot/camera/image"][0]["depth"],
            5,
        )

    def test_temperature_is_an_optional_discovered_capability(self):
        result = discover_camera_graph(graph({
            "/sensor/image": [IMAGE],
            "/sensor/temperature": [TEMPERATURE],
        }))
        self.assertTrue(result.candidates[0].capabilities()["temperature"])
        self.assertEqual(result.candidates[0].temperature_topics, ("/sensor/temperature",))

    def test_hostname_is_not_an_input_to_grouping_or_association(self):
        snapshot = graph({"/front/image_raw": [IMAGE]})
        first = discover_camera_graph({**snapshot, "hostname": "jetson-alpha"}, [physical(rgb_topic="/front/image_raw")])
        second = discover_camera_graph({**snapshot, "hostname": "jetson-beta"}, [physical(rgb_topic="/front/image_raw")])
        self.assertEqual(first.to_dict(), second.to_dict())
