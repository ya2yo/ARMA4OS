"""Exercise formal benchmark orchestration (kernel_build, schbench, mixed) using fixtures.

These fixtures verify evidence and lifecycle behavior, not kernel performance.
"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from benchmarks.cli import main
from benchmarks.reporting.reconstruction import reconstruct_summary


class TestFormalBenchmarks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "linux-fixture"
        self.source.mkdir()
        self.kernel_config = self.root / "fixed.config"
        self.kernel_config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
        self.schbench = self.root / "schbench-fixture"
        self.schbench.write_text(
            f"#!{sys.executable}\n"
            "import sys, time\n"
            "print('schbench fixture started', flush=True)\n"
            "runtime = float(sys.argv[sys.argv.index('-r') + 1])\n"
            "time.sleep(runtime)\n"
            "print('Wakeup Latencies percentiles (usec)\\n99.0th: 42', file=sys.stderr)\n",
            encoding="utf-8",
        )
        self.schbench.chmod(0o755)
        self.write_makefile(1.5)
        self.config = self.root / "formal.json"
        self.write_config()

    def write_makefile(self, duration):
        (self.source / "Makefile").write_text(
            "VERSION = 6\nPATCHLEVEL = 14\nSUBLEVEL = 0\n"
            ".PHONY: all olddefconfig\n"
            "all:\n\t@echo build-fixture-started\n"
            f"\t@sleep {duration}\n"
            '\t@printf fixture-vmlinux > "$(O)/vmlinux"\n'
            "olddefconfig:\n\t@echo config-fixture-complete\n",
            encoding="utf-8",
        )

    def write_config(self, timeout=10):
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "policies": ["default"],
            "timeout_sec": timeout,
            "kernel": {
                # Relative paths must resolve against the configuration file.
                "source": self.source.name,
                "config": self.kernel_config.name,
                "jobs": 2,
                "make": "make",
            },
            "schbench": {
                "binary": str(self.schbench), "args": [], "runtime_sec": 1,
            },
            "mixed": {"min_overlap_fraction": 0.9},
        }), encoding="utf-8")

    def run_formal(self, output, scenarios):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main([
                "formal", "--config", str(self.config), "--output", str(output),
                "--repeats", "1", "--warmup", "0", "--seed", "42",
                "--scenarios", *scenarios,
            ])

    @staticmethod
    def load_summary(output):
        return json.loads((output / "summary.json").read_text(encoding="utf-8"))

    def test_formal_success_raw_evidence_and_reconstruction(self):
        output = self.root / "formal_out"
        self.assertEqual(self.run_formal(output, ["kernel_build", "schbench", "mixed"]), 0)
        summary = self.load_summary(output)
        self.assertEqual(len(summary["groups"]), 3)
        for group in summary["groups"]:
            self.assertEqual(group["successful_trials"], 1, group)
            self.assertEqual(group["failed_trials"], 0, group)
        self.assertEqual(len(list(output.rglob("build.stdout.log"))), 2)
        self.assertEqual(len(list(output.rglob("build.stderr.log"))), 2)
        self.assertEqual(len(list(output.rglob("build.result.json"))), 2)
        logs = list(output.rglob("*.log"))
        measured_schbench_logs = [p for p in logs if "99.0th: 42" in p.read_text(errors="replace")]
        self.assertGreaterEqual(len(measured_schbench_logs), 2)
        self.assertEqual(reconstruct_summary(output), summary)

        cached = output / "metrics.jsonl"
        rows = [json.loads(line) for line in cached.read_text().splitlines() if line.strip()]
        for row in rows:
            row["metrics"] = {"p99": 999999, "wall_time_s": 999999}
        cached.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(reconstruct_summary(output), summary)

        # The existing evidence directory must never be reused or overwritten.
        manifest_before = (output / "manifest.json").read_bytes()
        self.assertNotEqual(self.run_formal(output, ["schbench"]), 0)
        self.assertEqual((output / "manifest.json").read_bytes(), manifest_before)

    def test_missing_component_log_or_record_invalidates_mixed_trial(self):
        original = self.root / "mixed"
        self.assertEqual(self.run_formal(original, ["mixed"]), 0)
        for filename in ("build.stderr.log", "build.result.json"):
            with self.subTest(missing=filename):
                component = list(original.rglob(filename))
                self.assertEqual(len(component), 1)
                contents = component[0].read_bytes()
                component[0].unlink()
                try:
                    group = reconstruct_summary(original)["groups"][0]
                    self.assertEqual(group["successful_trials"], 0, group)
                    self.assertEqual(group["failed_trials"], 1, group)
                finally:
                    component[0].write_bytes(contents)

    def test_mixed_rejects_background_ending_before_measurement(self):
        self.write_makefile(0.05)
        output = self.root / "insufficient-overlap"
        self.assertNotEqual(self.run_formal(output, ["mixed"]), 0)
        group = self.load_summary(output)["groups"][0]
        self.assertEqual(group["successful_trials"], 0, group)
        self.assertEqual(group["failed_trials"], 1, group)
        self.assertTrue(list(output.rglob("build.stdout.log")))

    def test_timeout_keeps_started_task_output(self):
        self.write_config(timeout=0.3)
        output = self.root / "timeout"
        self.assertNotEqual(self.run_formal(output, ["schbench"]), 0)
        group = self.load_summary(output)["groups"][0]
        self.assertEqual(group["successful_trials"], 0, group)
        self.assertEqual(group["failures_by_reason"]["timeout"], 1, group)
        logs = list(output.rglob("*.log"))
        self.assertTrue(any("schbench fixture started" in p.read_text(errors="replace") for p in logs))
        self.assertEqual(reconstruct_summary(output), self.load_summary(output))


if __name__ == "__main__":
    unittest.main()
