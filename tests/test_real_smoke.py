"""Portable checks for the opt-in real smoke harness; never load real BPF."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from benchmarks import real_smoke
from benchmarks.cli import run_benchmark


class RealSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sysfs = self.root / "sysfs"
        self.sysfs.mkdir()
        (self.sysfs / "state").write_text("disabled\n")
        self.btf = self.root / "vmlinux"
        self.btf.write_bytes(b"btf")
        self.scheduler = self.root / "scx_simple"
        self.scheduler.write_text("fixture")
        self.scheduler.chmod(0o755)
        self.enterContext(patch.object(real_smoke, "SYSFS", self.sysfs))
        self.enterContext(patch.object(real_smoke, "BTF", self.btf))

    def test_check_only_does_not_start_process_or_create_output(self):
        with patch.object(real_smoke, "RustExecutor") as executor, contextlib.redirect_stdout(io.StringIO()):
            code = real_smoke.main(["--check-only", "--scheduler", str(self.scheduler)])
        self.assertEqual(code, 0)
        executor.assert_not_called()

    def test_external_scheduler_and_absent_sysfs_are_rejected(self):
        (self.sysfs / "state").write_text("enabled\n")
        with self.assertRaisesRegex(RuntimeError, "external scheduler"):
            real_smoke.check_environment(self.scheduler)
        (self.sysfs / "state").unlink()
        with self.assertRaisesRegex(RuntimeError, "sysfs is absent"):
            real_smoke.check_environment(self.scheduler)

    def matrix_fixture(self):
        """Exercise the real runner and reconstruction over a safe default-only run."""
        config = self.root / "bench.json"
        config.write_text(json.dumps({"schema_version": 1, "policies": ["default"],
            "workloads": [{"name": "smoke", "command": ["/bin/echo", "{}"],
                           "parser": "elapsed", "warmup": 1}]}))
        run = self.root / "matrix"
        with patch("benchmarks.cli.verify_kernel_sched_ext_state", return_value="kernel-default"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_benchmark(str(config), str(run), repeats=2), 0)
        # Label one formal trial as the second policy for harness verification.
        # This fixture is local test data, not deployable or performance evidence.
        manifest = json.loads((run / "manifest.json").read_text())
        manifest.update(executor_backend="real", policy_comparison_valid=True)
        manifest["trial_plan"][-1]["policy"] = "scx_simple"
        (run / "manifest.json").write_text(json.dumps(manifest))
        records = [json.loads(x) for x in (run / "metrics.jsonl").read_text().splitlines()]
        records[-1]["policy"] = "scx_simple"
        for record in records:
            (run / "trials" / record["trial_id"] / "record.json").write_text(json.dumps(record))
        (run / "metrics.jsonl").write_text("".join(json.dumps(x) + "\n" for x in records))
        real_smoke.reconstruct_summary(run)
        return run, records

    def test_summary_comparison_preserves_original_before_reconstruction(self):
        run, _ = self.matrix_fixture()
        self.assertTrue(real_smoke.verify_matrix(run, 1)["summary_matches_raw"])
        summary = json.loads((run / "summary.json").read_text())
        summary["groups"][0]["metrics"]["wall_time_s"]["mean"] = 9999
        (run / "summary.json").write_text(json.dumps(summary))
        with self.assertRaisesRegex(RuntimeError, "differs from reconstruction"):
            real_smoke.verify_matrix(run, 1)
        self.assertEqual(json.loads((run.parent / "summary-original.json").read_text()), summary)

    def test_missing_warmup_raw_output_is_rejected(self):
        run, records = self.matrix_fixture()
        (run / records[0]["stdout_path"]).unlink()
        with self.assertRaisesRegex(RuntimeError, "Raw trial evidence is missing"):
            real_smoke.verify_matrix(run, 1)

    def test_recovery_failure_cannot_be_hidden_by_fallback_close(self):
        executor = MagicMock()
        executor.request.side_effect = [
            {"policy_id": "default", "healthy": True},
            {"status": "applied", "verified": True, "backend": "real",
             "actual_policy": "scx_simple", "managed_pid": 12345},
            {"policy_id": "scx_simple", "healthy": True, "backend": "real",
             "kernel_state": "enabled", "kernel_ops": "simple", "managed_pid": 12345},
            {"status": "rolled_back", "verified": True, "actual_policy": "default"}]
        (self.sysfs / "ops").write_text("simple")
        record = {"mode": "rollback", "passed": False}
        # Initial recovery fails; fallback close subsequently manages to clean up.
        with patch.object(real_smoke, "RustExecutor", return_value=executor), \
                patch.object(real_smoke, "wait_recovered", side_effect=[RuntimeError("Recovery failed"), None]):
            with self.assertRaisesRegex(RuntimeError, "Recovery failed"):
                real_smoke.lifecycle_case(self.scheduler, self.scheduler, "rollback", record)
        executor.close.assert_called_once()
        self.assertFalse(record["passed"])

    def test_missing_trial_and_failed_warmup_are_rejected(self):
        run, records = self.matrix_fixture()
        (run / "metrics.jsonl").write_text(json.dumps(records[0]) + "\n")
        with self.assertRaisesRegex(RuntimeError, "Missing or duplicate"):
            real_smoke.verify_matrix(run, 1)
        records[0]["status"] = "timeout"
        (run / "metrics.jsonl").write_text("".join(json.dumps(x) + "\n" for x in records))
        with self.assertRaisesRegex(RuntimeError, "Warmup or formal"):
            real_smoke.verify_matrix(run, 1)

    def test_missing_raw_output_cannot_pass_from_cached_summary(self):
        run, records = self.matrix_fixture()
        (run / records[-1]["stdout_path"]).unlink()
        with self.assertRaisesRegex(RuntimeError, "Raw trial evidence is missing"):
            real_smoke.verify_matrix(run, 1)

    def test_benchmark_failure_preserves_failed_verification(self):
        output = self.root / "evidence"
        args = argparse.Namespace(scheduler=self.scheduler, executor=self.scheduler,
                                  output=output, repeats=1, seed=42)
        with patch.object(real_smoke, "run_command", return_value=1), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(real_smoke.run_smoke(args), 1)
        report = json.loads((output / "verification.json").read_text())
        self.assertFalse(report["ok"])
        self.assertEqual(report["benchmark_returncode"], 1)
        self.assertIn("Benchmark failed", report["error"])
        self.assertEqual(report["final_state"], "disabled")
        # Reusing output must preserve the evidence.
        with self.assertRaises(FileExistsError):
            real_smoke.run_smoke(args)
        self.assertEqual(json.loads((output / "verification.json").read_text()), report)


if __name__ == "__main__":
    unittest.main()
