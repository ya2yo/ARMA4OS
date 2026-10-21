import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.evidence.store import RunStore
from benchmarks.reporting.formatting import format_summary_table
from benchmarks.reporting.statistics import generate_summary
from benchmarks.reporting.reconstruction import reconstruct_summary


class EvidenceIntegrityTests(unittest.TestCase):
    def test_store_keeps_metrics_out_of_original_execution_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "run")
            store.initialize()
            store.write_manifest({
                "trial_order": ["trial_1"],
                "workload_configs": [{"name": "batch", "parser": "elapsed"}],
            })
            record = {
                "trial_id": "trial_1", "policy": "default", "workload": "batch",
                "is_warmup": False, "status": "success", "exit_code": 0,
                "duration_s": 2.0, "stdout_path": "trials/trial_1/stdout.log",
                "stderr_path": "trials/trial_1/stderr.log",
                "metrics": {"wall_time_s": 999},
            }
            store.write_record(record)
            trial = store.run_dir / "trials" / "trial_1"
            (trial / "stdout.log").write_text("completed\n")
            (trial / "stderr.log").write_text("")

            self.assertNotIn("metrics", json.loads((trial / "record.json").read_text()))
            self.assertEqual(json.loads(store.metrics_path.read_text())["metrics"], {"wall_time_s": 999})
            summary = reconstruct_summary(store.run_dir)
            self.assertEqual(summary["groups"][0]["metrics"]["wall_time_s"]["mean"], 2.0)

    def test_original_postverification_failure_overrides_success_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "run")
            store.initialize()
            store.write_manifest({
                "trial_order": ["trial_1"],
                "workload_configs": [{"name": "batch", "parser": "elapsed"}],
            })
            record = {
                "trial_id": "trial_1", "policy": "default", "workload": "batch",
                "is_warmup": False, "status": "success", "exit_code": 0,
                "duration_s": 2.0, "metrics": {"wall_time_s": 2.0},
            }
            store.write_record(record)
            trial = store.run_dir / "trials" / "trial_1"
            (trial / "stdout.log").write_text("completed\n")
            (trial / "stderr.log").write_text("")
            record.update(status="nonzero_exit", metrics={},
                          error_message="Post-trial policy verification failed")
            store.write_record(record, append_metrics=False)

            self.assertEqual(len(store.metrics_path.read_text().splitlines()), 1)
            self.assertEqual(json.loads(store.metrics_path.read_text())["status"], "success")
            self.assertEqual(json.loads((trial / "record.json").read_text())["exit_code"], 0)
            group = reconstruct_summary(store.run_dir)["groups"][0]
            self.assertEqual(group["successful_trials"], 0)
            self.assertEqual(group["failures_by_reason"]["nonzero_exit"], 1)

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
