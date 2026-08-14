import unittest
from pathlib import Path


class LivoxHelperModelRoutingTest(unittest.TestCase):
    def test_mid360_and_mid360s_have_distinct_official_config_routes(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "jetson_tools"
            / "livox_discovery"
            / "main.cpp"
        ).read_text(encoding="utf-8")

        self.assertIn('BuildModelConfig(host_ip, "MID360", true)', source)
        self.assertIn('BuildModelConfig(host_ip, "Mid360s", false)', source)
        self.assertIn('if (options.model == "MID360")', source)
        self.assertIn('if (options.model == "MID360S")', source)
        self.assertIn("case kLivoxLidarTypeMid360: return \"MID360\"", source)
        self.assertIn("case kLivoxLidarTypeMid360s: return \"MID360S\"", source)

    def test_stream_helper_preserves_distinct_model_config_routes(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "jetson_tools"
            / "livox_stream"
            / "main.cpp"
        ).read_text(encoding="utf-8")

        self.assertIn('BuildModelConfig(host_ip, "MID360", true)', source)
        self.assertIn('BuildModelConfig(host_ip, "Mid360s", false)', source)
        self.assertIn('options.model == "MID360"', source)
        self.assertIn('options.model == "MID360S"', source)
        self.assertIn("case kLivoxLidarTypeMid360: return \"MID360\"", source)
        self.assertIn("case kLivoxLidarTypeMid360s: return \"MID360S\"", source)


if __name__ == "__main__":
    unittest.main()
