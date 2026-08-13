from dataclasses import dataclass

from devices.camera.models import StreamProfile


@dataclass(frozen=True)
class CameraProfile:
    profile_id: str
    display_name: str
    vendor: str
    backend: str
    interface_hint: str
    sdk_driver: str
    execution_hosts: tuple[str, ...]
    stream_profiles: tuple[StreamProfile, ...]


CAMERA_PROFILES = (
    CameraProfile(
        profile_id="zed_x_one_4k",
        display_name="ZED X One 4K",
        vendor="Stereolabs",
        backend="zed",
        interface_hint="GMSL2",
        sdk_driver="ZED SDK",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("HD4K", "HD4K", "3840 x 2160", (15,), ("RGBA", "BGRA")),
            StreamProfile("QHDPLUS", "QHD+", "3200 x 1800", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("HD1200", "HD1200", "1920 x 1200", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("HD1080", "HD1080", "1920 x 1080", (15, 30, 60), ("RGBA", "BGRA")),
        ),
    ),
    CameraProfile(
        profile_id="zed_x_mini",
        display_name="ZED X Mini",
        vendor="Stereolabs",
        backend="zed",
        interface_hint="GMSL2",
        sdk_driver="ZED SDK",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("HD1200", "HD1200", "1920 x 1200", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("HD1080", "HD1080", "1920 x 1080", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("SVGA", "SVGA", "960 x 600", (15, 30, 60, 120), ("RGBA", "BGRA")),
        ),
    ),
    CameraProfile(
        profile_id="zed_x",
        display_name="ZED X",
        vendor="Stereolabs",
        backend="zed",
        interface_hint="GMSL2",
        sdk_driver="ZED SDK",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("HD1200", "HD1200", "1920 x 1200", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("HD1080", "HD1080", "1920 x 1080", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("SVGA", "SVGA", "960 x 600", (15, 30, 60, 120), ("RGBA", "BGRA")),
        ),
    ),
    CameraProfile(
        profile_id="zed_x_one_gs",
        display_name="ZED X One GS",
        vendor="Stereolabs",
        backend="zed",
        interface_hint="GMSL2",
        sdk_driver="ZED SDK",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("HD1200", "HD1200", "1920 x 1200", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("HD1080", "HD1080", "1920 x 1080", (15, 30, 60), ("RGBA", "BGRA")),
            StreamProfile("SVGA", "SVGA", "960 x 600", (15, 30, 60, 120), ("RGBA", "BGRA")),
        ),
    ),
    CameraProfile(
        profile_id="realsense_d405",
        display_name="Intel RealSense D405",
        vendor="Intel",
        backend="realsense",
        interface_hint="USB",
        sdk_driver="librealsense",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("1280X720", "1280x720", "1280 x 720", (5, 15, 30), ("RGB8", "Z16")),
            StreamProfile("848X480", "848x480", "848 x 480", (30, 60), ("RGB8", "Z16")),
        ),
    ),
    CameraProfile(
        profile_id="realsense_d435i",
        display_name="Intel RealSense D435i",
        vendor="Intel",
        backend="realsense",
        interface_hint="USB 3",
        sdk_driver="librealsense",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("1280X720", "1280x720", "1280 x 720", (6, 15, 30), ("RGB8", "Z16")),
            StreamProfile("848X480", "848x480", "848 x 480", (30, 60, 90), ("RGB8", "Z16")),
        ),
    ),
    CameraProfile(
        profile_id="generic_v4l2",
        display_name="Generic UVC / V4L2 Camera",
        vendor="Generic",
        backend="v4l2",
        interface_hint="USB / V4L2",
        sdk_driver="V4L2 / UVC",
        execution_hosts=("Local Host", "Jetson"),
        stream_profiles=(
            StreamProfile("1920X1080", "1920x1080", "1920 x 1080", (15, 30), ("MJPG", "YUYV")),
            StreamProfile("1280X720", "1280x720", "1280 x 720", (30, 60), ("MJPG", "YUYV")),
            StreamProfile("640X480", "640x480", "640 x 480", (30, 60), ("MJPG", "YUYV")),
        ),
    ),
)


def get_camera_profile(profile_id: str) -> CameraProfile:
    for profile in CAMERA_PROFILES:
        if profile.profile_id == profile_id:
            return profile
    raise KeyError(f"Unknown camera profile: {profile_id}")
