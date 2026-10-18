import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.summary import format_summary_table, generate_summary, reconstruct_summary


class EvidenceIntegrityTests(unittest.TestCase):
    def test_simulated_warning_survives_exported_summary(self):
        summary = generate_summary([], {"executor_backend": "simulated", "policy_comparison_valid": False})
        self.assertFalse(summary["policy_comparison_valid"])
        self.assertIn("SIMULATED", format_summary_table(summary))

    def test_missing_execution_metadata_is_invalid_even_if_cache_claims_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / "trials" / "trial_1"
            trial.mkdir(parents=True)
            (trial / "stdout.log").write_text("hello\n")
            (root / "manifest.json").write_text(json.dumps({
                "trial_order": ["trial_1", "trial_2"],
                "trial_plan": [
                    {"trial_id": "trial_1", "policy": "default", "workload": "batch", "is_warmup": False},
                    {"trial_id": "trial_2", "policy": "default", "workload": "batch", "is_warmup": False}],
                "workload_configs": [{"name": "batch", "parser": "elapsed"}]}))
            (root / "metrics.jsonl").write_text(json.dumps({"trial_id": "trial_1", "policy": "default",
                "workload": "batch", "exit_code": 0, "duration_s": 1, "status": "success", "metrics": {"wall_time_s": 1}}) + "\n")
            summary = reconstruct_summary(root)
            group = summary["groups"][0]
            self.assertEqual(group["total_trials"], 2)
            self.assertEqual(group["successful_trials"], 0)
            self.assertEqual(group["failed_trials"], 2)


if __name__ == "__main__":
    unittest.main()
