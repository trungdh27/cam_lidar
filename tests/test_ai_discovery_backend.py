import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from devices.ai.jetson_ai_manager import JETSON_AI_MANAGER


class AiDiscoveryBackendTests(unittest.TestCase):
    def discover_with_ps(self, rows, configured_modules=()):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root); bin_dir = root_path / "bin"; bin_dir.mkdir()
            ps = bin_dir / "ps"; ps.write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(repr(item) for item in rows) + "\n", encoding="utf-8"); ps.chmod(0o755)
            config = root_path / ".config" / "cam_lidar"; config.mkdir(parents=True)
            (config / "ai_modules.json").write_text(json.dumps(list(configured_modules)), encoding="utf-8")
            env = {**os.environ, "HOME": root, "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}
            result = subprocess.run([sys.executable, "-c", JETSON_AI_MANAGER, json.dumps({"action": "discover"})], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(item for item in result.stdout.splitlines() if item.startswith("CAMERA_AI_JSON="))
            return json.loads(line.split("=", 1)[1])["environment"]

    def test_generic_utilities_with_ai_in_arguments_are_not_candidates(self):
        environment = self.discover_with_ps((
            "900 1 900 tee tee /home/huu/ai_direct_discovery.log",
            "901 1 901 grep grep ai",
            "902 1 902 bash bash -lc echo ai",
        ))
        self.assertEqual(environment["candidate_processes"], [])
        self.assertEqual(environment["module_count"], 0)
        self.assertFalse(environment["module_running"])

    def test_configured_executable_is_a_candidate_without_making_config_running(self):
        module = {"module_uid": "configured", "display_name": "Configured", "process_name": "vision_runner", "status": "CONFIGURED"}
        environment = self.discover_with_ps(("903 1 903 vision_runner vision_runner --config /opt/vision/config.yaml",), (module,))
        self.assertEqual(environment["candidate_processes"][0]["process_name"], "vision_runner")
        self.assertEqual(environment["candidate_processes"][0]["classification"], "configured_executable")
        self.assertFalse(environment["module_running"])

    @staticmethod
    def confirmed_module():
        return {
            "module_uid": "robot:vision", "display_name": "Robot Vision", "function_type": "object_detection",
            "runtime": "TensorRT", "package": "vd_vision", "process_name": "vd_vision_infer",
            "model_path": "/opt/vindynamics/controls/models/vision.engine",
            "input_topics": [{"name": "/camera/image", "message_type": "sensor_msgs/msg/Image", "role": "input"}],
            "output_topics": [{"name": "/vision/detections", "message_type": "example_msgs/msg/Detections", "role": "output"}],
            "launch_spec": {"method": "ros2_launch", "package": "vd_vision", "launch_file": "vision.launch.py", "command": ["ros2", "launch", "vd_vision", "vision.launch.py"], "approved": True},
        }

    def test_explicit_linked_package_model_and_endpoints_registers_inactive_module(self):
        environment = self.discover_with_ps((), (self.confirmed_module(),))
        self.assertEqual(environment["module_count"], 1)
        self.assertTrue(environment["module_discovered"])
        self.assertFalse(environment["module_running"])
        module = environment["modules"][0]
        self.assertEqual(module["status"], "READY_TO_LAUNCH")
        self.assertEqual(module["metadata"]["confidence"], "CONFIRMED")
        self.assertTrue(module["launch_spec"]["launch_ready"])
        self.assertEqual(module["input_topics"][0]["name"], "/camera/image")

    def test_running_confirmed_executable_updates_only_its_runtime_state(self):
        environment = self.discover_with_ps(("904 1 904 vd_vision_infer vd_vision_infer --engine /opt/vision.engine",), (self.confirmed_module(),))
        self.assertEqual(environment["module_count"], 1)
        self.assertTrue(environment["module_running"])
        self.assertEqual(environment["modules"][0]["status"], "RUNNING")
        self.assertEqual(environment["modules"][0]["metadata"]["running_evidence"]["process_name"], "vd_vision_infer")

    def test_incomplete_config_model_or_yaml_is_candidate_not_module(self):
        incomplete = {"module_uid": "yaml-only", "display_name": "Object Detection YAML", "package": "zed_wrapper", "model_path": "/opt/ros/zed/object_detection.yaml"}
        environment = self.discover_with_ps((), (incomplete,))
        self.assertEqual(environment["module_count"], 0)
        self.assertFalse(environment["module_discovered"])
        self.assertTrue(environment["registration_required"])
        self.assertEqual(environment["module_candidates"][0]["confidence"], "STRONG_CANDIDATE")

    def test_arbitrary_shell_launch_is_rejected_as_a_registration_candidate(self):
        unsafe = self.confirmed_module(); unsafe["launch_spec"] = {"method": "shell", "command": "ros2 launch vd_vision vision.launch.py", "approved": True}
        environment = self.discover_with_ps((), (unsafe,))
        self.assertEqual(environment["module_count"], 0)
        self.assertTrue(environment["registration_required"])

    def test_missing_executable_or_model_cannot_be_ready_to_launch(self):
        missing_executable = self.confirmed_module(); missing_executable.pop("process_name")
        missing_model = self.confirmed_module(); missing_model.pop("model_path")
        for module in (missing_executable, missing_model):
            environment = self.discover_with_ps((), (module,))
            self.assertEqual(environment["module_count"], 0)
            self.assertFalse(environment["module_running"])
            self.assertTrue(environment["registration_required"])


if __name__ == "__main__":
    unittest.main()
