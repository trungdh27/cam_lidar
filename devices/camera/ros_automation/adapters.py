from abc import ABC, abstractmethod

from devices.camera.models import CameraDevice, UsbSpeed, make_ros_namespace_hint
from devices.camera.ros_automation.models import (
    RosImageProfile,
    RosLaunchSpec,
    RosTopicRequirement,
)


class RosCameraAdapter(ABC):
    driver: str

    @abstractmethod
    def build_launch_spec(self, device: CameraDevice) -> RosLaunchSpec:
        raise NotImplementedError

    def required_package(self, _device: CameraDevice) -> str:
        return self.driver

    def mandatory_topics(self, device: CameraDevice) -> tuple[RosTopicRequirement, ...]:
        return self.build_launch_spec(device).mandatory_topics

    def primary_image_topic(self, device: CameraDevice) -> str:
        return self.build_launch_spec(device).primary_image_topic


def _namespace(device: CameraDevice) -> str:
    return device.ros_namespace_hint or make_ros_namespace_hint(
        device.ros_camera_model or device.normalized_model,
        device.serial,
        device_uid=device.device_uid,
    )


def _namespace_parts(namespace: str) -> tuple[str, str]:
    tokens = [part for part in namespace.strip("/").split("/") if part]
    name = tokens[-1] if tokens else "camera"
    parent = "/".join(tokens[:-1])
    return parent, name


class ZedRosAdapter(RosCameraAdapter):
    driver = "zed_wrapper"

    def build_launch_spec(self, device: CameraDevice) -> RosLaunchSpec:
        if not device.serial:
            raise ValueError("ZED launch requires a physical serial number")
        if device.ros_camera_model not in {"zedxm", "zedxone4k"}:
            raise ValueError(
                f"Unsupported zed_wrapper camera model: {device.ros_camera_model or '-'}"
            )
        namespace = _namespace(device)
        launch_namespace, camera_name = _namespace_parts(namespace)
        if device.ros_camera_model == "zedxm":
            profile = RosImageProfile(
                1920,
                1200,
                30,
                ("bgra8",),
                "installed_zed_wrapper_5.4.0_zedxm",
                {
                    "grab_resolution": "HD1200",
                    "grab_frame_rate": 30,
                    "pub_resolution": "NATIVE",
                },
            )
            requirements = (
                RosTopicRequirement(
                    "rgb/color/rect/image", "sensor_msgs/msg/Image", "color"
                ),
                RosTopicRequirement(
                    "rgb/color/rect/camera_info",
                    "sensor_msgs/msg/CameraInfo",
                    "camera_info",
                ),
                RosTopicRequirement(
                    "depth/depth_registered", "sensor_msgs/msg/Image", "depth"
                ),
                RosTopicRequirement("imu/data", "sensor_msgs/msg/Imu", "imu"),
            )
        else:
            profile = RosImageProfile(
                1920,
                1200,
                30,
                ("bgra8",),
                "installed_zed_wrapper_5.4.0_zedxone4k_safe_profile",
                {
                    "grab_resolution": "HD1200",
                    "grab_frame_rate": 30,
                    "pub_resolution": "NATIVE",
                },
            )
            requirements = (
                RosTopicRequirement(
                    "rgb/color/rect/image", "sensor_msgs/msg/Image", "color"
                ),
                RosTopicRequirement(
                    "rgb/color/rect/camera_info",
                    "sensor_msgs/msg/CameraInfo",
                    "camera_info",
                ),
                RosTopicRequirement("imu/data", "sensor_msgs/msg/Imu", "imu"),
            )
        primary = requirements[0].topic(namespace)
        overrides = (
            f"general.grab_resolution:={profile.configuration['grab_resolution']};"
            f"general.grab_frame_rate:={profile.fps};"
            "general.pub_resolution:=NATIVE;video.enable_24bit_output:=false"
        )
        arguments = (
            f"camera_model:={device.ros_camera_model}",
            f"serial_number:={device.serial}",
            f"camera_name:={camera_name}",
            f"namespace:={launch_namespace}",
            # The installed wrapper waits for its camera-link transforms before
            # publishing sensor data.  camera_name makes the generated frames
            # unique, so keep the wrapper's URDF publisher enabled.
            "publish_urdf:=true",
            "node_log_type:=screen",
            f"param_overrides:={overrides}",
        )
        return RosLaunchSpec(
            driver=self.driver,
            package=self.driver,
            launch_file="zed_camera.launch.py",
            arguments=arguments,
            namespace=namespace,
            expected_node=namespace,
            selected_serial=device.serial,
            serial_parameter="general.serial_number",
            ros_camera_model=device.ros_camera_model,
            requested_profile=profile,
            mandatory_topics=requirements,
            primary_image_topic=primary,
        )


class RealSenseRosAdapter(RosCameraAdapter):
    driver = "realsense2_camera"

    @staticmethod
    def _profile(device: CameraDevice) -> RosImageProfile:
        color_profiles = [
            item
            for item in device.stream_profiles
            if item.stream.lower() in {"color", "rgb"}
            and item.width and item.height and item.fps
        ]
        if device.usb_speed == UsbSpeed.USB_2:
            eligible = [
                item
                for item in color_profiles
                if item.width <= 640 and item.height <= 480 and item.fps <= 30
            ]
            if eligible:
                selected = max(eligible, key=lambda item: (item.fps, item.width * item.height))
                source = "inventory_reported_usb2_profile"
            else:
                selected = None
                source = "usb2_conservative_unvalidated_default"
            width, height, fps = (
                (selected.width, selected.height, selected.fps)
                if selected else (640, 480, 15)
            )
        else:
            eligible = [item for item in color_profiles if item.fps <= 30]
            selected = max(
                eligible,
                key=lambda item: (item.width * item.height, item.fps),
                default=None,
            )
            source = "inventory_reported_profile" if selected else "unvalidated_default"
            width, height, fps = (
                (selected.width, selected.height, selected.fps)
                if selected else (1280, 720, 30)
            )
        return RosImageProfile(
            int(width), int(height), int(fps), ("rgb8", "bgr8"), source,
            {"color_profile": f"{width}x{height}x{fps}"},
        )

    def build_launch_spec(self, device: CameraDevice) -> RosLaunchSpec:
        if not device.serial:
            raise ValueError("RealSense launch requires a physical serial number")
        namespace = _namespace(device)
        launch_namespace, camera_name = _namespace_parts(namespace)
        profile = self._profile(device)
        requirements = (
            RosTopicRequirement("color/image_raw", "sensor_msgs/msg/Image", "color"),
            RosTopicRequirement(
                "color/camera_info", "sensor_msgs/msg/CameraInfo", "camera_info"
            ),
            RosTopicRequirement(
                "depth/image_rect_raw", "sensor_msgs/msg/Image", "depth"
            ),
        )
        arguments = (
            f"serial_no:=_{device.serial}",
            f"camera_name:={camera_name}",
            f"camera_namespace:={launch_namespace}",
            "enable_color:=true",
            "enable_depth:=true",
            f"rgb_camera.color_profile:={profile.configuration['color_profile']}",
        )
        return RosLaunchSpec(
            driver=self.driver,
            package=self.driver,
            launch_file="rs_launch.py",
            arguments=arguments,
            namespace=namespace,
            expected_node=namespace,
            selected_serial=device.serial,
            serial_parameter="serial_no",
            ros_camera_model=device.ros_camera_model or "d435i",
            requested_profile=profile,
            mandatory_topics=requirements,
            primary_image_topic=requirements[0].topic(namespace),
        )
