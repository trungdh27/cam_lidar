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

    def camera_info_requirement(self, device: CameraDevice) -> RosTopicRequirement:
        requirements = self.build_launch_spec(device).mandatory_topics
        return next(item for item in requirements if item.capability == "camera_info")

    def primary_image_requirement(self, device: CameraDevice) -> RosTopicRequirement:
        spec = self.build_launch_spec(device)
        return next(
            item for item in spec.mandatory_topics
            if item.topic(spec.namespace) == spec.primary_image_topic
        )

    def sensor_topics(self, _device: CameraDevice) -> tuple[RosTopicRequirement, ...]:
        return ()

    def qos_topics(self, device: CameraDevice) -> tuple[RosTopicRequirement, ...]:
        topics = [self.primary_image_requirement(device), self.camera_info_requirement(device)]
        sensor = next(
            (item for item in self.sensor_topics(device) if item.availability == "MANDATORY"),
            None,
        )
        if sensor is not None:
            topics.append(sensor)
        return tuple(topics)

    def bag_topics(self, device: CameraDevice) -> tuple[RosTopicRequirement, ...]:
        return self.qos_topics(device)

    def frame_relationship_valid(self, image_frame: str, camera_info_frame: str) -> bool:
        def normalized(value):
            tokens = [
                token for token in str(value or "").strip("/").lower().split("_")
                if token not in {"frame", "optical", "link"}
            ]
            return tuple(tokens)

        if not image_frame or not camera_info_frame:
            return False
        left, right = normalized(image_frame), normalized(camera_info_frame)
        return left == right


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

    def sensor_topics(self, device: CameraDevice) -> tuple[RosTopicRequirement, ...]:
        if device.ros_camera_model == "zedxm":
            return (
                RosTopicRequirement(
                    "imu/data", "sensor_msgs/msg/Imu", "imu",
                    availability="MANDATORY", alternatives=("imu/data_raw",),
                ),
                RosTopicRequirement(
                    "temperature/imu", "sensor_msgs/msg/Temperature", "temperature",
                    availability="OPTIONAL",
                    alternatives=("temperature/left", "temperature/right"),
                ),
                RosTopicRequirement(
                    "imu/mag", "sensor_msgs/msg/MagneticField", "magnetic_field",
                    availability="OPTIONAL", alternatives=("mag",),
                ),
            )
        return (
            RosTopicRequirement(
                "imu/data", "sensor_msgs/msg/Imu", "imu",
                availability="OPTIONAL", alternatives=("imu/data_raw",),
            ),
            RosTopicRequirement(
                "temperature/imu", "sensor_msgs/msg/Temperature", "temperature",
                availability="OPTIONAL",
                alternatives=("temperature/left", "temperature/right"),
            ),
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
            "enable_gyro:=true",
            "enable_accel:=true",
            "unite_imu_method:=2",
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

    def sensor_topics(self, _device: CameraDevice) -> tuple[RosTopicRequirement, ...]:
        return (
            RosTopicRequirement(
                "imu", "sensor_msgs/msg/Imu", "imu", availability="MANDATORY",
                alternatives=("gyro/sample", "accel/sample"),
            ),
            RosTopicRequirement(
                "temperature", "sensor_msgs/msg/Temperature", "temperature",
                availability="OPTIONAL",
                alternatives=("temperature/imu", "temperature/gyro", "temperature/accel"),
            ),
        )
