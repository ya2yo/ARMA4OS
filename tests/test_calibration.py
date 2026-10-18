import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arma4os.calibration import build_registry


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = {"schema_version": 1, "environment_id": "machine-A", "executor_backend": "real"}
        self.write_manifest()
        self.context = {"environment_id": "machine-A", "profile": "cpu_contended", "minimum_samples": 5,
                        "workload": "batch", "features": {"cpu.utilization": [0.7, 1], "cpu.pressure": [0.05, 1]},
                        "metric_mapping": {"wall_time_s": {"name": "app.completion_time", "unit": "s"}},
                        "switch_cost_s": {"default": 0.1}}
        self.group = {"workload": "batch", "policy": "default", "failed_trials": 0, "successful_trials": 5,
                      "metrics": {"wall_time_s": {"n": 5, "mean": 1, "ci_95": [0.9, 1.1], "unit": "s"}}}

    def write_manifest(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        with patch("benchmarks.summary.reconstruct_summary", return_value={"groups": [self.group]}):
            return build_registry(self.root, self.context)

    def test_simulated_matrix_retains_synthetic_marker(self):
        self.manifest["executor_backend"] = "simulated"
        self.write_manifest()
        result = self.build()
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["evidence"][0]["samples"], 5)
        self.assertIn("manifest-sha256=", result["evidence"][0]["source"])

    def test_failed_trials_cannot_be_removed_to_improve_registry(self):
        self.group["failed_trials"] = 1
        with self.assertRaises(ValueError):
            self.build()

    def test_environment_and_units_must_match(self):
        self.context["environment_id"] = "other-machine"
        with self.assertRaises(ValueError):
            self.build()
        self.context["environment_id"] = "machine-A"
        self.context["metric_mapping"]["wall_time_s"]["unit"] = "ms"
        with self.assertRaises(ValueError):
            self.build()

    def test_unknown_backend_insufficient_repeats_and_missing_interval_refused(self):
        self.manifest["executor_backend"] = "unknown"
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.build()
        self.manifest["executor_backend"] = "real"
        self.write_manifest()
        self.group["successful_trials"] = 4
        with self.assertRaises(ValueError):
            self.build()
        self.group["successful_trials"] = 5
        self.group["metrics"]["wall_time_s"]["ci_95"] = None
        with self.assertRaises(ValueError):
            self.build()


if __name__ == "__main__":
    unittest.main()
