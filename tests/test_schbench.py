"""Regression coverage for upstream schbench's stderr distributions."""

import unittest

from benchmarks.evidence.parsers import ParseError
from benchmarks.evidence.parsers.schbench import SchbenchParser


class TestUpstreamSchbenchOutput(unittest.TestCase):
    def test_stderr_wakeup_does_not_use_request_or_rps_percentiles(self):
        stderr = """
Wakeup Latencies percentiles (usec) runtime 30 (s) (1000 total samples)
  50.0th: 10 (500 samples)
  *99.0th: 75 (10 samples)
  min=1, max=90
Request Latencies percentiles (usec) runtime 30 (s) (1000 total samples)
  50.0th: 100
  *99.0th: 900
  min=90, max=1000
RPS percentiles (requests) runtime 30 (s) (1000 total samples)
  50.0th: 700
  90.0th: 800
  min=600, max=900
average rps: 733.25
"""
        metrics = SchbenchParser().parse("", stderr, 30.1, 0).metrics
        self.assertEqual(metrics["p99"], 75)
        self.assertEqual(metrics["min"], 1)
        self.assertEqual(metrics["max"], 90)
        self.assertEqual(metrics["latency_unit"], "usec")
        self.assertEqual(metrics["rps"], 733.25)
        self.assertNotIn("requests", metrics)

    def test_final_wakeup_block_requires_its_own_p99(self):
        stderr = """
Wakeup Latencies percentiles (usec)
  99.0th: 30
Request Latencies percentiles (usec)
  99.0th: 800
Wakeup Latencies percentiles (usec)
  50.0th: 10
Request Latencies percentiles (usec)
  99.0th: 900
"""
        with self.assertRaisesRegex(ParseError, "Missing p99"):
            SchbenchParser().parse("", stderr, 30, 0)

    def test_request_only_output_is_not_wakeup_latency(self):
        stderr = "Request Latencies percentiles (usec)\n99.0th: 900\n"
        with self.assertRaisesRegex(ParseError, "Missing wakeup"):
            SchbenchParser().parse("", stderr, 30, 0)

    def test_unsupported_latency_unit_is_rejected(self):
        with self.assertRaisesRegex(ParseError, "Unsupported latency unit"):
            SchbenchParser().parse("", "Wakeup Latencies percentiles (bananas)\n99.0th: 12\n", 10, 0)

    def test_overflowed_auxiliary_metric_is_rejected(self):
        output = "Wakeup Latencies percentiles (usec)\n99.0th: 12\nmin=1, max=" + "9" * 400
        with self.assertRaisesRegex(ParseError, "Non-finite"):
            SchbenchParser().parse("", output, 10, 0)

    def test_no_throughput_is_invented_for_legacy_wakeup_mode(self):
        stderr = "Wakeup Latencies percentiles (usec)\n99.0th: 12\n"
        metrics = SchbenchParser().parse("", stderr, 10, 0).metrics
        self.assertEqual(metrics["p99"], 12)
        self.assertNotIn("rps", metrics)


if __name__ == "__main__":
    unittest.main()
