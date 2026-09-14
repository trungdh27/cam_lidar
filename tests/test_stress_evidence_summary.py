import unittest
from datetime import timedelta, timezone

from desktop_app.stress.evidence_summary import (
    SystemMetricsSummary,
    format_bytes,
    format_local_timestamp,
)


def snapshot(
    timestamp: str,
    aggregate: str,
    *cores: str,
    memory: str = "Mem: 65893445632 6302867456 1000 0 0 57575608320",
    swap: str = "Swap: 32942342144 524288 32941817856",
) -> str:
    return "\n".join(
        [
            f"[stdout] {timestamp}",
            f"[stdout] cpu {aggregate}",
            *(f"[stdout] {core}" for core in cores),
            "[stdout] 10:32:54 up 1:19, 1 user, load average: 17.46, 16.30, 14.76",
            f"[stdout] {memory}",
            f"[stdout] {swap}",
            "",
        ]
    )


class SystemMetricsSummaryTests(unittest.TestCase):
    def test_first_snapshot_waits_and_second_calculates_cpu_deltas(self):
        parser = SystemMetricsSummary()
        parser.feed(snapshot("2026-09-10T10:32:54+00:00", "100 0 50 800 50 0 0 0"))
        self.assertIsNone(parser.current_cpu)
        self.assertIn("Waiting for next sample", parser.render("RUNNING"))

        parser.feed(snapshot("2026-09-10T10:32:59+00:00", "140 0 60 840 60 0 0 0"))
        # delta_total=100; delta idle+iowait=50, so utilization is 50%.
        self.assertAlmostEqual(parser.current_cpu, 50.0)

    def test_per_core_delta_and_missing_core_are_safe(self):
        parser = SystemMetricsSummary()
        parser.feed(
            snapshot(
                "2026-09-10T10:32:54+00:00",
                "100 0 50 800 50 0 0 0",
                "cpu0 50 0 25 400 25 0 0 0",
                "cpu1 50 0 25 400 25 0 0 0",
            )
        )
        parser.feed(
            snapshot(
                "2026-09-10T10:32:59+00:00",
                "140 0 60 840 60 0 0 0",
                "cpu0 70 0 30 420 30 0 0 0",
            )
        )
        self.assertAlmostEqual(parser.state.cpu_usage["cpu0"], 50.0)
        self.assertNotIn("cpu1", parser.state.cpu_usage)
        parser.feed(
            snapshot(
                "2026-09-10T10:33:04+00:00",
                "180 0 70 880 70 0 0 0",
                "cpu0 90 0 35 440 35 0 0 0",
                "cpu1 90 0 35 440 35 0 0 0",
            )
        )
        self.assertNotIn("cpu1", parser.state.cpu_usage)

    def test_malformed_cpu_sample_does_not_raise_or_use_stale_counters(self):
        parser = SystemMetricsSummary()
        parser.feed(snapshot("2026-09-10T10:32:54+00:00", "100 0 50 800 50 0 0 0"))
        parser.feed(snapshot("2026-09-10T10:32:59+00:00", "not valid"))
        self.assertIsNone(parser.current_cpu)
        parser.feed(snapshot("2026-09-10T10:33:04+00:00", "180 0 70 880 70 0 0 0"))
        self.assertIsNone(parser.current_cpu)

    def test_memory_swap_conversion_and_ram_percentage(self):
        parser = SystemMetricsSummary()
        parser.feed(snapshot("2026-09-10T10:32:54+00:00", "100 0 50 800 50 0 0 0"))
        self.assertEqual(format_bytes(parser.state.ram_total), "61.37 GiB")
        self.assertEqual(format_bytes(parser.state.swap_used), "0.50 MiB")
        self.assertAlmostEqual(parser.state.ram_usage_percent, 9.5652, places=3)
        rendered = parser.render("RUNNING")
        self.assertIn("RAM Available  53.62 GiB", rendered)
        self.assertIn("RAM Usage      9.6 %", rendered)

    def test_target_current_average_minimum_and_maximum_tracking(self):
        parser = SystemMetricsSummary(target_cpu_percent=30)
        parser.feed(snapshot("2026-09-10T10:32:54+00:00", "100 0 50 800 50 0 0 0"))
        parser.feed(snapshot("2026-09-10T10:32:59+00:00", "140 0 60 840 60 0 0 0"))  # 50%
        parser.feed(snapshot("2026-09-10T10:33:04+00:00", "150 0 75 900 75 0 0 0"))  # 25%
        self.assertAlmostEqual(parser.current_cpu, 25.0)
        self.assertAlmostEqual(parser.average_cpu, 37.5)
        self.assertAlmostEqual(parser.minimum_cpu, 25.0)
        self.assertAlmostEqual(parser.maximum_cpu, 50.0)
        rendered = parser.render("RUNNING")
        self.assertIn("Target CPU     30 %", rendered)
        self.assertIn("Average CPU    37.5 %", rendered)
        self.assertNotIn("tolerance", rendered.lower())

    def test_timestamp_is_presented_in_requested_local_timezone(self):
        local = timezone(timedelta(hours=7))
        self.assertEqual(
            format_local_timestamp("2026-09-10T10:32:54+00:00", local),
            "2026-09-10 17:32:54 +07:00",
        )


if __name__ == "__main__":
    unittest.main()
