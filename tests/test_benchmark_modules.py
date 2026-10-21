"""Behavioral checks for benchmark module boundaries and retained evidence."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from benchmarks.orchestration import engine
from benchmarks.orchestration.models import BenchmarkConfig, TrialPlan, WorkloadConfig
from benchmarks.execution.executor import ExecutorError
from benchmarks.execution.trial import run_trial
from benchmarks.reporting.reconstruction import reconstruct_summary


class BenchmarkLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "run"
        self.config = BenchmarkConfig(workloads=[WorkloadConfig(
            name="safe", command=[sys.executable, "-c", "print('retained output')"],
            timeout_sec=5,
        )])
        self.executor = MagicMock()
        self.executor.current_policy = "default"
        self.executor.verify_status.return_value = {"backend": "simulated"}

    def run_benchmark(self, **kwargs: object) -> int:
        options = dict(
            config_path="unused.json", output_path=str(self.output), repeats=2,
            executor_path="/nonexistent/arma-executor", executor_config_path="unused.json",
            config_override=self.config,
        )
        options.update(kwargs)
        with patch.object(engine, "ExecutorClient", return_value=self.executor), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return engine.run_benchmark(**options)

    def records(self) -> list[dict]:
        records = [json.loads(line) for line in (self.output / "metrics.jsonl").read_text().splitlines()]
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertCountEqual([r["trial_id"] for r in records], manifest["trial_order"])
        self.assertEqual(len(records), len(manifest["trial_order"]))
        for record in records:
            raw = json.loads((self.output / "trials" / record["trial_id"] / "record.json").read_text())
            self.assertEqual(raw["status"], record["status"])
        return records

    def test_pre_trial_verification_failure_is_retained_without_launch(self) -> None:
        self.executor.verify_policy.side_effect = [ExecutorError("policy unavailable"), None, None]
        with patch.object(engine, "run_trial", wraps=run_trial) as launch:
            self.assertEqual(self.run_benchmark(), 1)
        self.assertEqual(launch.call_count, 1)
        records = self.records()
        self.assertEqual([r["status"] for r in records], ["nonzero_exit", "success"])
        self.assertIn("Pre-trial", records[0]["error_message"])
        self.assertEqual(records[0]["metrics"], {})
        self.executor.shutdown.assert_called_once()

    def test_apply_failure_keeps_all_planned_policies(self) -> None:
        self.config.policies.append("scx_test")
        self.executor.apply_policy.side_effect = ExecutorError("load refused")
        with patch.object(engine, "run_trial", wraps=run_trial) as launch:
            self.assertEqual(self.run_benchmark(repeats=1), 1)
        self.assertEqual(launch.call_count, 1)
        records = {r["policy"]: r for r in self.records()}
        self.assertEqual(records["default"]["status"], "success")
        self.assertEqual(records["scx_test"]["status"], "nonzero_exit")
        self.assertIn("Executor apply failed", records["scx_test"]["error_message"])

    def test_post_trial_verification_failure_preserves_process_exit_and_raw_logs(self) -> None:
        self.executor.verify_policy.side_effect = [None, ExecutorError("policy disappeared")]
        self.assertEqual(self.run_benchmark(repeats=1), 1)
        record, = self.records()
        self.assertEqual(record["status"], "nonzero_exit")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["metrics"], {})
        self.assertIn("retained output", (self.output / record["stdout_path"]).read_text())
        summary = reconstruct_summary(self.output)
        self.assertEqual(summary["groups"][0]["failed_trials"], 1)
        self.assertEqual(summary["groups"][0]["successful_trials"], 0)

    def test_interruption_and_cleanup_failure_keep_warmup_and_formal_plan(self) -> None:
        original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.config.workloads[0].warmup = 1
        for cleanup_error in (RuntimeError("cleanup failed"), KeyboardInterrupt()):
            with self.subTest(cleanup_error=type(cleanup_error).__name__):
                self.output = Path(self.temp.name) / type(cleanup_error).__name__
                self.executor.reset_mock()
                self.executor.shutdown.side_effect = cleanup_error
                launched = 0

                def interrupt_second(plan: TrialPlan, run_dir: Path):
                    nonlocal launched
                    launched += 1
                    if launched == 2:
                        raise KeyboardInterrupt()
                    return run_trial(plan, run_dir)

                with patch.object(engine, "run_trial", side_effect=interrupt_second):
                    self.assertEqual(self.run_benchmark(repeats=3), 1)
                records = self.records()
                self.assertEqual(len(records), 4)
                self.assertEqual(sum(r["is_warmup"] for r in records), 1)
                self.assertEqual(sum(r["status"] == "success" for r in records), 1)
                self.assertEqual(sum("cancelled" in (r["error_message"] or "") for r in records), 3)
                manifest = json.loads((self.output / "manifest.json").read_text())
                self.assertTrue(manifest["interrupted"])
                self.executor.shutdown.assert_called_once()
                for sig, handler in original_handlers.items():
                    self.assertEqual(signal.getsignal(sig), handler)

    def test_existing_result_directory_is_untouched_before_any_work(self) -> None:
        self.output.mkdir()
        sentinel = self.output / "original-evidence"
        sentinel.write_bytes(b"original\x00evidence")
        with patch.object(engine, "load_config") as load, \
                patch.object(engine, "run_trial") as launch:
            self.assertEqual(self.run_benchmark(config_override=None), 1)
        load.assert_not_called()
        launch.assert_not_called()
        self.executor.start.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"original\x00evidence")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_trial_interrupt_still_cleans_its_process_group(self) -> None:
        plan = TrialPlan("interrupted", 0, 0, "default", self.config.workloads[0])
        process = MagicMock(pid=123456, returncode=-9)
        process.wait.side_effect = [KeyboardInterrupt(), -9]
        with patch("benchmarks.execution.trial.subprocess.Popen", return_value=process) as spawn, \
                patch("benchmarks.execution.trial.kill_process_group") as kill:
            with self.assertRaises(KeyboardInterrupt):
                run_trial(plan, self.output)
        kill.assert_called_once_with(123456)
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertTrue((self.output / "trials/interrupted/stdout.log").exists())
        self.assertTrue((self.output / "trials/interrupted/stderr.log").exists())


class BenchmarkModuleBoundaryTests(unittest.TestCase):
    def test_public_cli_entry_protects_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "retained-run"
            output.mkdir()
            sentinel = output / "sentinel.log"
            sentinel.write_text("original evidence")
            result = subprocess.run(
                [sys.executable, "-m", "benchmarks", "run", "--config", "missing.json", "--output", str(output)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertEqual(sentinel.read_text(), "original evidence")
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_cli_forwards_run_options_to_orchestration(self) -> None:
        from benchmarks import cli
        self.assertIs(cli.run_benchmark, engine.run_benchmark)
        with patch.object(cli, "run_benchmark", return_value=7) as run:
            code = cli.main([
                "run", "--config", "input.json", "--output", "artifacts", "--seed", "17",
                "--repeats", "3", "--executor", "/trusted/executor", "--executor-config", "exec.json",
            ])
        self.assertEqual(code, 7)
        run.assert_called_once_with(
            config_path="input.json", output_path="artifacts", seed=17, repeats=3,
            executor_path="/trusted/executor", executor_config_path="exec.json",
        )

    def test_pure_contracts_and_evidence_reading_do_not_import_execution(self) -> None:
        for module in (
            "benchmarks.orchestration.models", "benchmarks.orchestration.config",
            "benchmarks.reporting.reconstruction", "benchmarks.orchestration.formal.parsing",
        ):
            with self.subTest(module=module):
                script = (
                    "import importlib, sys; importlib.import_module(sys.argv[1]); "
                    "loaded = [m for m in sys.modules if m.startswith('benchmarks.execution') "
                    "or m.endswith('.formal.scenario')]; assert not loaded, loaded"
                )
                result = subprocess.run([sys.executable, "-c", script, module], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_responsibility_module_entrypoints_expose_expected_options(self) -> None:
        entrypoints = (
            ("benchmarks", "summarize"),
            ("benchmarks.deployment.checks", "--check-only"),
            ("benchmarks.deployment.vm", "--remote-root"),
            ("benchmarks.orchestration.formal", "--spec"),
            ("benchmarks.execution.workloads.ffmpeg", "--input"),
            ("benchmarks.execution.workloads.compression", "--iterations"),
        )
        for module, expected_option in entrypoints:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-m", module, "--help"], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected_option, result.stdout)

    def test_generated_media_configuration_targets_an_executable_workload(self) -> None:
        from scripts.prepare_media_bench import generate_ffmpeg_config
        from benchmarks.orchestration.config import load_config

        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "media.json"
            generate_ffmpeg_config(config_path, "/srv/bench/input.mp4")
            config = load_config(str(config_path))
            command = config.workloads[0].command
            self.assertEqual(command[:3], [sys.executable, "-m", "benchmarks.execution.workloads.ffmpeg"])
            result = subprocess.run(command + ["--help"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--input", result.stdout)


if __name__ == "__main__":
    unittest.main()
