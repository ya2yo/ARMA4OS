"""Unit tests for ARMA4OS benchmark framework.

Tests cover:
- Schbench output parsing, explicit units, and strict p99 rejection
- Elapsed / batch workload parsing
- Compression workload determinism and verification
- Config schema validation and baseline policy refusal
- Matrix generation, randomized rounds, and seed determinism
- Process group killing on timeout including descendants
- Executor JSONL protocol lifecycle and actions logging
- Summary generation, explicit failure breakdown, CI, and reconstruction
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import unittest

from benchmarks.cli import main, run_benchmark, summarize_run
from benchmarks.config import BenchmarkConfig, WorkloadConfig, load_config
from benchmarks.executor_client import ExecutorClient, ExecutorError
from benchmarks.manifest import generate_manifest
from benchmarks.matrix import generate_trial_plan
from benchmarks.parsers import ParseError, get_parser
from benchmarks.parsers.elapsed import ElapsedParser
from benchmarks.parsers.schbench import SchbenchParser
from benchmarks.runner import is_process_group_alive, kill_process_group, run_trial
from benchmarks.summary import (
    calculate_ci_95,
    calculate_sample_std,
    generate_summary,
    reconstruct_summary,
)
from benchmarks.workloads.compression import (
    generate_deterministic_payload,
    run_compression_workload,
)


class TestSchbenchParser(unittest.TestCase):
    """Test schbench wake latency parsing and verification."""

    def setUp(self) -> None:
        self.parser = SchbenchParser()

    def test_parse_standard_schbench_output(self) -> None:
        stdout = """
Wakeup Latencies percentiles (usec) runtime 30 (s)
\t50.0th: 12
\t75.0th: 15
\t90.0th: 20
\t95.0th: 28
\t*99.0th: 51
\t99.5th: 78
\t99.9th: 198
\tmin=1, max=1023
Requests: 150000 (5000.0/s)
"""
        res = self.parser.parse(stdout=stdout, stderr="", duration_s=30.1, exit_code=0)
        self.assertEqual(res.metrics["p50"], 12.0)
        self.assertEqual(res.metrics["p75"], 15.0)
        self.assertEqual(res.metrics["p90"], 20.0)
        self.assertEqual(res.metrics["p95"], 28.0)
        self.assertEqual(res.metrics["p99"], 51.0)
        self.assertEqual(res.metrics["p99_5"], 78.0)
        self.assertEqual(res.metrics["p99_9"], 198.0)
        self.assertEqual(res.metrics["min"], 1.0)
        self.assertEqual(res.metrics["max"], 1023.0)
        self.assertEqual(res.metrics["latency_unit"], "usec")
        self.assertEqual(res.metrics["requests"], 150000)
        self.assertEqual(res.metrics["rps"], 5000.0)

    def test_parse_four_decimal_schbench_with_samples(self) -> None:
        stdout = """
Wakeup Latencies percentiles (usec) runtime 30 (s)
\t50.0000th: 12 (1000 samples)
\t*99.0000th: 123 (500 samples)
\tmin=1, max=1023
"""
        res = self.parser.parse(stdout=stdout, stderr="", duration_s=30.0, exit_code=0)
        self.assertEqual(res.metrics["p50"], 12.0)
        self.assertEqual(res.metrics["p99"], 123.0)
        self.assertEqual(res.metrics["latency_unit"], "usec")
        # Ensure no invented percentiles
        self.assertNotIn("p75", res.metrics)
        self.assertNotIn("p90", res.metrics)
        self.assertNotIn("p95", res.metrics)
        self.assertNotIn("p99_5", res.metrics)

    def test_reject_ambiguous_latency_unit(self) -> None:
        stdout_no_unit = """
Wakeup Latencies percentiles runtime 10 (s)
50.0th: 12
99.0th: 50
"""
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse(stdout=stdout_no_unit, stderr="", duration_s=10.0, exit_code=0)
        self.assertIn("Ambiguous latency unit", str(ctx.exception))

    def test_reject_conflicting_units(self) -> None:
        stdout_conflict = """
Wakeup Latencies percentiles (usec) runtime 10 (s)
50.0th: 12
99.0th: 50 ms
"""
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse(stdout=stdout_conflict, stderr="", duration_s=10.0, exit_code=0)
        self.assertIn("Conflicting latency units", str(ctx.exception))

    def test_parse_ms_units_schbench_output(self) -> None:
        stdout = """
Wakeup Latencies percentiles (ms) runtime 10 (s)
50.0th: 1.2
90.0th: 2.5
95.0th: 3.1
99.0th: 8.4
min=0.5, max=15.2
"""
        res = self.parser.parse(stdout=stdout, stderr="", duration_s=10.0, exit_code=0)
        self.assertEqual(res.metrics["p99"], 8.4)
        self.assertEqual(res.metrics["latency_unit"], "ms")

    def test_reject_missing_p99(self) -> None:
        stdout = """
Wakeup Latencies percentiles (usec) runtime 10 (s)
50.0th: 12
75.0th: 15
90.0th: 20
min=1, max=50
"""
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse(stdout=stdout, stderr="", duration_s=10.0, exit_code=0)
        self.assertIn("Missing p99", str(ctx.exception))

    def test_reject_malformed_p99(self) -> None:
        stdout = """
Wakeup Latencies percentiles (usec)
50.0th: 12
99.0th: NaN
min=1, max=100
"""
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse(stdout=stdout, stderr="", duration_s=10.0, exit_code=0)
        self.assertIn("p99", str(ctx.exception))

    def test_reject_negative_or_inf_p99(self) -> None:
        stdout_neg = "Wakeup Latencies percentiles (usec)\n50.0th: 12\n99.0th: -5\nmin=1, max=100\n"
        with self.assertRaises(ParseError):
            self.parser.parse(stdout=stdout_neg, stderr="", duration_s=1.0, exit_code=0)

        stdout_inf = "Wakeup Latencies percentiles (usec)\n50.0th: 12\n99.0th: inf\nmin=1, max=100\n"
        with self.assertRaises(ParseError):
            self.parser.parse(stdout=stdout_inf, stderr="", duration_s=1.0, exit_code=0)

    def test_reject_garbage_content(self) -> None:
        with self.assertRaises(ParseError):
            self.parser.parse(stdout="some random unparseable text", stderr="", duration_s=1.0, exit_code=0)

    def test_reject_empty_schbench_output(self) -> None:
        with self.assertRaises(ParseError):
            self.parser.parse(stdout="", stderr="", duration_s=1.0, exit_code=0)

    def test_reject_nonzero_exit_code(self) -> None:
        with self.assertRaises(ParseError):
            self.parser.parse(stdout="some output", stderr="err", duration_s=1.0, exit_code=1)

    def test_multi_block_output_takes_last_block(self) -> None:
        stdout = """
Wakeup Latencies percentiles (usec) runtime 5 (s)
50.0th: 10
99.0th: 20
Wakeup Latencies percentiles (usec) runtime 10 (s)
50.0th: 14
99.0th: 35
"""
        res = self.parser.parse(stdout=stdout, stderr="", duration_s=10.0, exit_code=0)
        self.assertEqual(res.metrics["p50"], 14.0)
        self.assertEqual(res.metrics["p99"], 35.0)


class TestElapsedParser(unittest.TestCase):
    """Test elapsed time and batch workload parsing."""

    def setUp(self) -> None:
        self.parser = ElapsedParser()

    def test_parse_elapsed_basic(self) -> None:
        res = self.parser.parse(stdout="", stderr="", duration_s=1.234567, exit_code=0)
        self.assertEqual(res.metrics["wall_time_s"], 1.234567)
        self.assertEqual(res.metrics["exit_code"], 0)

    def test_parse_elapsed_with_json_metrics(self) -> None:
        stdout = 'Log line 1\n{"workload": "compression", "throughput_mb_s": 55.4, "ratio": 0.45}\nDone\n'
        res = self.parser.parse(stdout=stdout, stderr="", duration_s=0.5, exit_code=0)
        self.assertEqual(res.metrics["wall_time_s"], 0.5)
        self.assertEqual(res.metrics["throughput_mb_s"], 55.4)
        self.assertEqual(res.metrics["ratio"], 0.45)

    def test_reject_nonzero_exit_code(self) -> None:
        with self.assertRaises(ParseError):
            self.parser.parse(stdout="", stderr="failed", duration_s=0.5, exit_code=2)


class TestCompressionWorkload(unittest.TestCase):
    """Test deterministic compression workload."""

    def test_deterministic_payload(self) -> None:
        p1 = generate_deterministic_payload(10000, seed=42)
        p2 = generate_deterministic_payload(10000, seed=42)
        p3 = generate_deterministic_payload(10000, seed=99)
        self.assertEqual(p1, p2)
        self.assertNotEqual(p1, p3)
        self.assertEqual(len(p1), 10000)

    def test_compression_run_zlib(self) -> None:
        res = run_compression_workload(size_bytes=50000, iterations=2, seed=42, algorithm="zlib")
        self.assertEqual(res["workload"], "compression")
        self.assertEqual(res["verified"], True)
        self.assertGreater(res["throughput_mb_s"], 0.0)
        self.assertGreater(res["ratio"], 0.0)

    def test_compression_run_all_algorithms(self) -> None:
        for algo in ["zlib", "gzip", "bz2", "lzma"]:
            res = run_compression_workload(size_bytes=10000, iterations=1, seed=42, algorithm=algo)
            self.assertTrue(res["verified"])


class TestConfigAndValidation(unittest.TestCase):
    """Test configuration parsing and baseline validation."""

    def test_refuse_matrix_without_default_baseline(self) -> None:
        cfg = BenchmarkConfig(
            policies=["scx_bpfland"],  # Missing "default"
            workloads=[
                WorkloadConfig(name="comp", command=["python3", "-c", "pass"], parser="elapsed")
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.validate(has_executor=True)
        self.assertIn("default", str(ctx.exception))

    def test_refuse_multi_policy_without_executor(self) -> None:
        cfg = BenchmarkConfig(
            policies=["default", "scx_bpfland"],
            workloads=[
                WorkloadConfig(name="comp", command=["python3", "-c", "pass"], parser="elapsed")
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.validate(has_executor=False)
        self.assertIn("without an executor", str(ctx.exception))

    def test_valid_default_only_config(self) -> None:
        cfg = BenchmarkConfig(
            policies=["default"],
            workloads=[
                WorkloadConfig(name="comp", command=["python3", "-c", "pass"], parser="elapsed")
            ],
        )
        cfg.validate(has_executor=False)

    def test_reject_shell_command_string(self) -> None:
        with self.assertRaises(ValueError):
            # Command should be list of strings, not string
            w = WorkloadConfig(name="comp", command="echo hello", parser="elapsed")  # type: ignore
            w.validate()

    def test_validate_default_configs_benchmarks_json(self) -> None:
        cfg = load_config("configs/benchmarks.json")
        cfg.validate(has_executor=False)
        self.assertEqual(cfg.policies, ["default"])
        self.assertGreaterEqual(len(cfg.workloads), 1)

    def test_validate_examples_schbench_json(self) -> None:
        cfg = load_config("benchmarks/examples/benchmarks_schbench.json")
        self.assertEqual(cfg.policies, ["default"])
        self.assertEqual(cfg.workloads[0].parser, "schbench")

    def test_reject_duplicate_workload_name(self) -> None:
        cfg = BenchmarkConfig(
            policies=["default"],
            workloads=[
                WorkloadConfig(name="dup_name", command=["python3", "-c", "pass"]),
                WorkloadConfig(name="dup_name", command=["python3", "-c", "pass"]),
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.validate(has_executor=False)
        self.assertIn("Duplicate workload", str(ctx.exception))

    def test_reject_invalid_parser(self) -> None:
        with self.assertRaises(ValueError):
            w = WorkloadConfig(name="bad_parser", command=["python3", "-c", "pass"], parser="invalid_parser")
            w.validate()

    def test_reject_nonexistent_config_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_config("nonexistent_path_to_config.json")

    def test_reject_command_string_in_from_dict(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "workloads": [{"name": "cmd_str", "command": "echo bad"}],
            })
        self.assertIn("must be a list of argv strings", str(ctx.exception))

    def test_reject_policy_string_in_from_dict(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BenchmarkConfig.from_dict({
                "policies": "default",
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })
        self.assertIn("must be a list of strings", str(ctx.exception))

    def test_reject_duplicate_policies(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BenchmarkConfig.from_dict({
                "policies": ["default", "default"],
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })
        self.assertIn("Duplicate policies", str(ctx.exception))

    def test_reject_bool_and_nonfinite_timeout(self) -> None:
        # bool timeout
        with self.assertRaises(ValueError):
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "timeout_sec": True,
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })
        # NaN timeout
        with self.assertRaises(ValueError):
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "timeout_sec": float("nan"),
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })
        # Inf timeout
        with self.assertRaises(ValueError):
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "timeout_sec": float("inf"),
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })

    def test_reject_negative_and_fractional_warmup(self) -> None:
        # negative warmup
        with self.assertRaises(ValueError):
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "warmup": -1,
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })
        # fractional warmup
        with self.assertRaises(ValueError):
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "warmup": 1.5,
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })
        # bool warmup
        with self.assertRaises(ValueError):
            BenchmarkConfig.from_dict({
                "policies": ["default"],
                "warmup": True,
                "workloads": [{"name": "cmd", "command": ["echo", "hi"]}],
            })


class TestMatrixAndRandomization(unittest.TestCase):
    """Test randomized trial scheduling and seed consistency."""

    def test_seed_determinism(self) -> None:
        cfg = BenchmarkConfig(
            policies=["default", "scx_bpfland"],
            workloads=[
                WorkloadConfig(name="w1", command=["python3", "-c", "pass"]),
                WorkloadConfig(name="w2", command=["python3", "-c", "pass"]),
            ],
        )
        plan1 = generate_trial_plan(cfg, repeats=4, seed=42)
        plan2 = generate_trial_plan(cfg, repeats=4, seed=42)
        plan3 = generate_trial_plan(cfg, repeats=4, seed=123)

        self.assertEqual(len(plan1), 16)  # 2 policies x 2 workloads x 4 repeats
        self.assertEqual([p.trial_id for p in plan1], [p.trial_id for p in plan2])
        self.assertEqual(
            [(p.policy, p.workload.name) for p in plan1],
            [(p.policy, p.workload.name) for p in plan2],
        )
        # Different seed yields different round ordering
        self.assertNotEqual(
            [(p.policy, p.workload.name) for p in plan1],
            [(p.policy, p.workload.name) for p in plan3],
        )

    def test_warmup_scheduling(self) -> None:
        cfg = BenchmarkConfig(
            policies=["default"],
            workloads=[
                WorkloadConfig(name="w1", command=["python3", "-c", "pass"], warmup=2),
            ],
            warmup=0,
        )
        plans = generate_trial_plan(cfg, repeats=3, seed=42)
        self.assertEqual(len(plans), 5)  # 2 warmup + 3 formal
        self.assertTrue(plans[0].is_warmup)
        self.assertTrue(plans[1].is_warmup)
        self.assertFalse(plans[2].is_warmup)

    def test_warmup_per_policy(self) -> None:
        cfg = BenchmarkConfig(
            policies=["default", "scx_bpfland"],
            workloads=[
                WorkloadConfig(name="w1", command=["python3", "-c", "pass"], warmup=2),
            ],
            warmup=0,
        )
        plans = generate_trial_plan(cfg, repeats=1, seed=42)
        warmups = [p for p in plans if p.is_warmup]
        self.assertEqual(len(warmups), 4)  # 2 for default + 2 for scx_bpfland
        self.assertEqual([p.policy for p in warmups], ["default", "default", "scx_bpfland", "scx_bpfland"])


class TestProcessExecutionAndSafety(unittest.TestCase):
    """Test process isolation, descendant cleanup, and streaming IO."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="arma_test_exec_")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_kill_process_group_including_descendants_on_timeout(self) -> None:
        # A script that spawns a background grandchild process, then sleeps or exits early
        script = """
import subprocess, time, sys
# Spawn background sleep process in the same process group
child = subprocess.Popen(["sleep", "60"])
# Exit early, leaving grandchild running in the process group!
sys.exit(0)
"""
        workload = WorkloadConfig(
            name="orphan_spawner",
            command=[sys.executable, "-c", script],
            parser="elapsed",
            timeout_sec=0.5,
        )
        plan = generate_trial_plan(
            BenchmarkConfig(workloads=[workload]),
            repeats=1,
            seed=42,
        )[0]

        t0 = time.monotonic()
        record = run_trial(plan, run_dir=Path(self.temp_dir), timeout_sec=0.5)
        duration = time.monotonic() - t0

        self.assertEqual(record.status, "timeout")
        self.assertLess(duration, 3.0)  # Must be killed quickly, not wait 60s

    def test_nonzero_exit_status_tracking(self) -> None:
        workload = WorkloadConfig(
            name="failing_task",
            command=[sys.executable, "-c", "import sys; sys.exit(42)"],
            parser="elapsed",
            timeout_sec=5.0,
        )
        plan = generate_trial_plan(
            BenchmarkConfig(workloads=[workload]),
            repeats=1,
            seed=42,
        )[0]
        record = run_trial(plan, run_dir=Path(self.temp_dir))
        self.assertEqual(record.status, "nonzero_exit")
        self.assertEqual(record.exit_code, 42)

    def test_streaming_io_files_exist(self) -> None:
        workload = WorkloadConfig(
            name="echo_task",
            command=[sys.executable, "-c", "print('stdout message'); import sys; print('stderr message', file=sys.stderr)"],
            parser="elapsed",
        )
        plan = generate_trial_plan(
            BenchmarkConfig(workloads=[workload]),
            repeats=1,
            seed=42,
        )[0]
        record = run_trial(plan, run_dir=Path(self.temp_dir))
        self.assertEqual(record.status, "success")

        stdout_file = Path(self.temp_dir) / record.stdout_path
        stderr_file = Path(self.temp_dir) / record.stderr_path
        self.assertTrue(stdout_file.exists())
        self.assertTrue(stderr_file.exists())
        self.assertIn("stdout message", stdout_file.read_text(encoding="utf-8"))
        self.assertIn("stderr message", stderr_file.read_text(encoding="utf-8"))


class TestExecutorProtocol(unittest.TestCase):
    """Test executor child process JSONL communication per docs/protocol.md using RustExecutor transport."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="arma_test_exec_proto_")
        # Write a mock executor script implementing docs/protocol.md
        self.mock_executor_path = Path(self.temp_dir) / "mock_executor"
        self.mock_config_path = Path(self.temp_dir) / "executor_config.json"
        self.mock_config_path.write_text('{"schema_version":1,"backend":"simulated"}', encoding="utf-8")

        mock_code = """#!/usr/bin/env python3
import sys, json

current_policy = "default"

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue

    op = req.get("operation")
    req_id = req.get("request_id")
    params = req.get("params", {})

    if op == "status":
        resp = {
            "schema_version": 1,
            "request_id": req_id,
            "ok": True,
            "result": {
                "policy_id": current_policy,
                "kernel_state": "disabled" if current_policy == "default" else "enabled",
                "kernel_ops": None if current_policy == "default" else "bpfland",
                "managed_pid": None,
                "healthy": True,
                "backend": "simulated"
            },
            "error": None
        }
    elif op == "apply":
        target = params.get("policy_id")
        current_policy = target
        resp = {
            "schema_version": 1,
            "request_id": req_id,
            "ok": True,
            "result": {
                "planned_policy": target,
                "previous_policy": "default",
                "actual_policy": target,
                "status": "applied",
                "verified": True,
                "started_at_unix_ms": 1000,
                "finished_at_unix_ms": 1010,
                "managed_pid": None,
                "error": None,
                "recovery": None,
                "backend": "simulated"
            },
            "error": None
        }
    elif op == "rollback":
        current_policy = "default"
        resp = {
            "schema_version": 1,
            "request_id": req_id,
            "ok": True,
            "result": {
                "actual_policy": "default",
                "status": "rolled_back",
                "verified": True
            },
            "error": None
        }
    elif op == "shutdown":
        current_policy = "default"
        resp = {
            "schema_version": 1,
            "request_id": req_id,
            "ok": True,
            "result": {"status": "shutdown"},
            "error": None
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
        break
    else:
        resp = {
            "schema_version": 1,
            "request_id": req_id,
            "ok": False,
            "result": None,
            "error": {"code": "unknown_op", "message": f"Unknown op {op}"}
        }

    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""
        self.mock_executor_path.write_text(mock_code, encoding="utf-8")
        self.mock_executor_path.chmod(0o755)
        self.actions_file = Path(self.temp_dir) / "actions.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mock_executor_lifecycle_and_actions(self) -> None:
        client = ExecutorClient(
            executor_path=str(self.mock_executor_path),
            config_path=str(self.mock_config_path),
            actions_log_path=self.actions_file,
        )

        with client:
            status = client.verify_status()
            self.assertEqual(status["policy_id"], "default")
            self.assertTrue(status["healthy"])

            # Verify exact policy match
            verified = client.verify_policy("default")
            self.assertEqual(verified["policy_id"], "default")

            apply_res = client.apply_policy("scx_bpfland")
            self.assertEqual(apply_res["status"], "applied")
            self.assertEqual(client.current_policy, "scx_bpfland")

            client.restore_default()
            self.assertEqual(client.current_policy, "default")

        # Verify actions.jsonl logged operations
        lines = self.actions_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(lines), 3)
        logged_ops = [json.loads(line)["operation"] for line in lines]
        self.assertIn("status", logged_ops)
        self.assertIn("apply", logged_ops)

    def test_mock_executor_unhealthy_status(self) -> None:
        unhealthy_script = """#!/usr/bin/env python3
import sys, json
for line in sys.stdin:
    req = json.loads(line)
    resp = {
        "schema_version": 1,
        "request_id": req["request_id"],
        "ok": True,
        "result": {"policy_id": "default", "healthy": False},
        "error": None
    }
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""
        script_path = Path(self.temp_dir) / "unhealthy_exec"
        script_path.write_text(unhealthy_script, encoding="utf-8")
        script_path.chmod(0o755)

        client = ExecutorClient(
            executor_path=str(script_path),
            config_path=str(self.mock_config_path),
        )

        with self.assertRaises(ExecutorError) as ctx:
            with client:
                pass
        self.assertIn("unhealthy", str(ctx.exception).lower())

    def test_mock_executor_external_scheduler_error(self) -> None:
        external_script = """#!/usr/bin/env python3
import sys, json
for line in sys.stdin:
    req = json.loads(line)
    resp = {
        "schema_version": 1,
        "request_id": req["request_id"],
        "ok": True,
        "result": {"policy_id": "external", "healthy": True},
        "error": None
    }
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""
        script_path = Path(self.temp_dir) / "external_exec"
        script_path.write_text(external_script, encoding="utf-8")
        script_path.chmod(0o755)

        client = ExecutorClient(
            executor_path=str(script_path),
            config_path=str(self.mock_config_path),
        )

        with self.assertRaises(ExecutorError) as ctx:
            with client:
                pass
        self.assertIn("external", str(ctx.exception).lower())

    def test_simulated_executor_wrapper(self) -> None:
        from arma4os.executor import SimulatedExecutor
        sim = SimulatedExecutor()
        client = ExecutorClient(executor=sim, actions_log_path=self.actions_file)
        with client:
            st = client.verify_status()
            self.assertEqual(st["policy_id"], "default")
            client.apply_policy("scx_bpfland")
            self.assertEqual(client.current_policy, "scx_bpfland")
            # Exact policy mismatch test
            with self.assertRaises(ExecutorError):
                client.verify_policy("scx_rusty")


class TestSummaryAndRebuild(unittest.TestCase):
    """Test summary statistics, explicit failure breakdown, and reconstruction."""

    def test_statistics_math(self) -> None:
        # Single sample: std and CI are None (not appropriate)
        self.assertIsNone(calculate_sample_std([5.0]))
        self.assertIsNone(calculate_ci_95([5.0]))

        # Two samples
        vals = [10.0, 12.0]
        std = calculate_sample_std(vals)
        self.assertIsNotNone(std)
        self.assertAlmostEqual(std, 1.4142, places=3)
        ci = calculate_ci_95(vals)
        self.assertIsNotNone(ci)
        self.assertLess(ci[0], 11.0)
        self.assertGreater(ci[1], 11.0)

        # Identical samples (std == 0): CI is None
        self.assertIsNone(calculate_ci_95([5.0, 5.0, 5.0]))

    def test_summary_explicit_failures(self) -> None:
        records = [
            {"policy": "default", "workload": "w1", "status": "success", "is_warmup": False, "metrics": {"wall_time_s": 1.0}},
            {"policy": "default", "workload": "w1", "status": "timeout", "is_warmup": False, "metrics": {}},
            {"policy": "default", "workload": "w1", "status": "nonzero_exit", "is_warmup": False, "metrics": {}},
            {"policy": "default", "workload": "w1", "status": "parse_error", "is_warmup": False, "metrics": {}},
            {"policy": "default", "workload": "w1", "status": "success", "is_warmup": True, "metrics": {"wall_time_s": 0.5}},  # Warmup
        ]
        summary = generate_summary(records)
        group = summary["groups"][0]
        self.assertEqual(group["total_trials"], 4)  # Warmup excluded from formal count
        self.assertEqual(group["successful_trials"], 1)
        self.assertEqual(group["failed_trials"], 3)
        self.assertEqual(group["failures_by_reason"]["timeout"], 1)
        self.assertEqual(group["failures_by_reason"]["nonzero_exit"], 1)
        self.assertEqual(group["failures_by_reason"]["parse_error"], 1)

    def test_reconstruct_summary_from_disk(self) -> None:
        temp_dir = tempfile.mkdtemp(prefix="arma_test_rebuild_")
        try:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "environment_id": "test_env_12345",
                "executor_backend": "kernel-default",
                "workload_configs": [
                    {"name": "taskA", "parser": "schbench"}
                ]
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            # Trial 1
            t1_dir = run_dir / "trials" / "trial_0001"
            t1_dir.mkdir(parents=True)
            (t1_dir / "record.json").write_text(json.dumps({
                "trial_id": "trial_0001",
                "round_index": 0,
                "repeat_index": 0,
                "policy": "default",
                "workload": "taskA",
                "is_warmup": False,
                "status": "success",
                "exit_code": 0,
                "duration_s": 10.0,
                "stdout_path": "trials/trial_0001/stdout.log",
                "stderr_path": "trials/trial_0001/stderr.log",
            }), encoding="utf-8")
            (t1_dir / "stdout.log").write_text("Wakeup Latencies percentiles (usec)\n50.0th: 10\n99.0th: 25.0\n", encoding="utf-8")
            (t1_dir / "stderr.log").write_text("", encoding="utf-8")

            # Trial 2
            t2_dir = run_dir / "trials" / "trial_0002"
            t2_dir.mkdir(parents=True)
            (t2_dir / "record.json").write_text(json.dumps({
                "trial_id": "trial_0002",
                "round_index": 0,
                "repeat_index": 1,
                "policy": "default",
                "workload": "taskA",
                "is_warmup": False,
                "status": "success",
                "exit_code": 0,
                "duration_s": 10.0,
                "stdout_path": "trials/trial_0002/stdout.log",
                "stderr_path": "trials/trial_0002/stderr.log",
            }), encoding="utf-8")
            (t2_dir / "stdout.log").write_text("Wakeup Latencies percentiles (usec)\n50.0th: 10\n99.0th: 27.0\n", encoding="utf-8")
            (t2_dir / "stderr.log").write_text("", encoding="utf-8")

            metrics_file = run_dir / "metrics.jsonl"
            lines = [
                json.dumps({"trial_id": "trial_0001", "policy": "default", "workload": "taskA", "status": "success", "is_warmup": False, "metrics": {"p99": 25.0, "latency_unit": "usec"}}),
                json.dumps({"trial_id": "trial_0002", "policy": "default", "workload": "taskA", "status": "success", "is_warmup": False, "metrics": {"p99": 27.0, "latency_unit": "usec"}}),
            ]
            metrics_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            summary = reconstruct_summary(run_dir)
            self.assertTrue((run_dir / "summary.json").exists())
            self.assertEqual(len(summary["groups"]), 1)
            p99_summary = summary["groups"][0]["metrics"]["p99"]
            self.assertEqual(p99_summary["mean"], 26.0)
            self.assertEqual(p99_summary["unit"], "usec")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_reconstruct_tampered_metrics_retains_raw_result(self) -> None:
        """Tampering cached metrics.jsonl must not alter rebuilt result from raw outputs."""
        temp_dir = tempfile.mkdtemp(prefix="arma_test_tamper_")
        try:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "workload_configs": [{"name": "taskA", "parser": "schbench"}]
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            t_dir = run_dir / "trials" / "t1"
            t_dir.mkdir(parents=True)
            (t_dir / "record.json").write_text(json.dumps({
                "trial_id": "t1", "policy": "default", "workload": "taskA",
                "is_warmup": False, "status": "success", "exit_code": 0, "duration_s": 5.0,
                "stdout_path": "trials/t1/stdout.log", "stderr_path": "trials/t1/stderr.log"
            }), encoding="utf-8")
            (t_dir / "stdout.log").write_text("Wakeup Latencies percentiles (usec)\n50.0th: 10\n99.0th: 20.0\n", encoding="utf-8")

            # Tampered metrics.jsonl with fabricated 99999.0 score
            (run_dir / "metrics.jsonl").write_text(
                json.dumps({"trial_id": "t1", "policy": "default", "workload": "taskA", "status": "success", "metrics": {"p99": 99999.0}}) + "\n",
                encoding="utf-8"
            )

            rebuilt = reconstruct_summary(run_dir)
            # Rebuilt must ignore tampered 99999.0 and retain raw 20.0
            self.assertEqual(rebuilt["groups"][0]["metrics"]["p99"]["mean"], 20.0)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_reconstruct_deleted_or_malformed_raw_output(self) -> None:
        """Deleting or corrupting raw stdout yields explicit parse_error, not cached scores."""
        temp_dir = tempfile.mkdtemp(prefix="arma_test_corrupt_")
        try:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "workload_configs": [{"name": "taskA", "parser": "schbench"}]
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            # Trial 1: missing stdout
            t1_dir = run_dir / "trials" / "t1"
            t1_dir.mkdir(parents=True)
            (t1_dir / "record.json").write_text(json.dumps({
                "trial_id": "t1", "policy": "default", "workload": "taskA",
                "is_warmup": False, "status": "success", "exit_code": 0, "duration_s": 5.0,
                "stdout_path": "trials/t1/stdout.log"
            }), encoding="utf-8")

            # Trial 2: malformed stdout
            t2_dir = run_dir / "trials" / "t2"
            t2_dir.mkdir(parents=True)
            (t2_dir / "record.json").write_text(json.dumps({
                "trial_id": "t2", "policy": "default", "workload": "taskA",
                "is_warmup": False, "status": "success", "exit_code": 0, "duration_s": 5.0,
                "stdout_path": "trials/t2/stdout.log"
            }), encoding="utf-8")
            (t2_dir / "stdout.log").write_text("Corrupted garbage with no p99", encoding="utf-8")

            (run_dir / "metrics.jsonl").write_text(
                json.dumps({"trial_id": "t1", "policy": "default", "workload": "taskA", "status": "success", "metrics": {"p99": 10.0}}) + "\n" +
                json.dumps({"trial_id": "t2", "policy": "default", "workload": "taskA", "status": "success", "metrics": {"p99": 12.0}}) + "\n",
                encoding="utf-8"
            )

            rebuilt = reconstruct_summary(run_dir)
            group = rebuilt["groups"][0]
            self.assertEqual(group["successful_trials"], 0)
            self.assertEqual(group["failed_trials"], 2)
            self.assertEqual(group["failures_by_reason"]["parse_error"], 2)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_reconstruct_preserves_original_failures(self) -> None:
        """Preserve original timeout and nonzero_exit failure statuses."""
        temp_dir = tempfile.mkdtemp(prefix="arma_test_failures_")
        try:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "workload_configs": [{"name": "taskA", "parser": "elapsed"}]
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            # Trial 1: timeout
            t1_dir = run_dir / "trials" / "t1"
            t1_dir.mkdir(parents=True)
            (t1_dir / "record.json").write_text(json.dumps({
                "trial_id": "t1", "policy": "default", "workload": "taskA",
                "is_warmup": False, "status": "timeout", "exit_code": None, "duration_s": 30.0,
                "stdout_path": "trials/t1/stdout.log"
            }), encoding="utf-8")
            (t1_dir / "stdout.log").write_text("partial text", encoding="utf-8")

            # Trial 2: nonzero_exit
            t2_dir = run_dir / "trials" / "t2"
            t2_dir.mkdir(parents=True)
            (t2_dir / "record.json").write_text(json.dumps({
                "trial_id": "t2", "policy": "default", "workload": "taskA",
                "is_warmup": False, "status": "nonzero_exit", "exit_code": 42, "duration_s": 1.0,
                "stdout_path": "trials/t2/stdout.log"
            }), encoding="utf-8")
            (t2_dir / "stdout.log").write_text("crash log", encoding="utf-8")

            rebuilt = reconstruct_summary(run_dir)
            group = rebuilt["groups"][0]
            self.assertEqual(group["failures_by_reason"]["timeout"], 1)
            self.assertEqual(group["failures_by_reason"]["nonzero_exit"], 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestEndToEndBenchmarkRun(unittest.TestCase):
    """Test full end-to-end benchmark execution via CLI."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="arma_test_e2e_")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_run_and_summarize_e2e(self) -> None:
        run_dir = Path(self.temp_dir) / "run_out"
        ret = run_benchmark(
            config_path="configs/benchmarks.json",
            output_path=str(run_dir),
            seed=42,
            repeats=2,
        )
        self.assertEqual(ret, 0)

        # Verify all expected artifacts exist
        self.assertTrue((run_dir / "manifest.json").exists())
        self.assertTrue((run_dir / "metrics.jsonl").exists())
        self.assertTrue((run_dir / "actions.jsonl").exists())
        self.assertTrue((run_dir / "summary.json").exists())
        self.assertTrue((run_dir / "trials").is_dir())

        # Verify manifest content
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["parameters"]["seed"], 42)
        self.assertEqual(manifest["parameters"]["repeats"], 2)
        self.assertIn("system", manifest)
        self.assertIn("python_version", manifest["system"])

        # Test summarize subcommand
        reconstructed_summary_path = run_dir / "reconstructed.json"
        ret_sum = summarize_run(str(run_dir), output_path=str(reconstructed_summary_path))
        self.assertEqual(ret_sum, 0)
        self.assertTrue(reconstructed_summary_path.exists())

    def test_reject_already_existing_output_dir(self) -> None:
        """Reject already existing output directory to preserve previous evidence."""
        existing_dir = Path(self.temp_dir) / "existing_dir"
        existing_dir.mkdir(parents=True)
        ret = run_benchmark(
            config_path="configs/benchmarks.json",
            output_path=str(existing_dir),
            seed=42,
            repeats=1,
        )
        self.assertEqual(ret, 1)

    def test_manifest_fields_and_binary_hashes(self) -> None:
        """Verify environment_id, executor_backend, binary_hashes, and trial_order in manifest."""
        run_dir = Path(self.temp_dir) / "manifest_test_out"
        ret = run_benchmark(
            config_path="configs/benchmarks.json",
            output_path=str(run_dir),
            seed=42,
            repeats=1,
        )
        self.assertEqual(ret, 0)

        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest.get("environment_id"))
        self.assertEqual(manifest.get("executor_backend"), "kernel-default")
        self.assertIn("python", manifest.get("binary_hashes", {}))
        self.assertTrue(manifest["binary_hashes"]["python"])
        self.assertGreater(len(manifest.get("trial_order", [])), 0)
        self.assertIn("affinity", manifest["system"])

    def test_calibrator_compatibility(self) -> None:
        """Verify that calibrator build_registry is fully compatible with benchmark output."""
        from arma4os.calibration import build_registry

        run_dir = Path(self.temp_dir) / "calibrator_out"
        ret = run_benchmark(
            config_path="configs/benchmarks.json",
            output_path=str(run_dir),
            seed=42,
            repeats=5,  # 5 repeats required by calibration minimum
        )
        self.assertEqual(ret, 0)

        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        context = {
            "environment_id": manifest["environment_id"],
            "workload": "compression_smoke",
            "profile": "cpu_contended",
            "features": {"cpu.utilization": [0.5, 1.0], "cpu.pressure": [0.0, 0.5]},
            "metric_mapping": {
                "wall_time_s": {"name": "app.wall_time", "unit": "s"}
            },
            "switch_cost_s": {"default": 0.0},
            "minimum_samples": 5,
        }

        registry_dict = build_registry(run_dir, context, validity_hours=24.0)
        self.assertEqual(registry_dict["environment_id"], manifest["environment_id"])
        self.assertFalse(registry_dict["synthetic"])
        self.assertEqual(len(registry_dict["evidence"]), 1)
        evidence = registry_dict["evidence"][0]
        self.assertEqual(evidence["policy_id"], "default")
        self.assertIn("app.wall_time", evidence["metrics"])
        self.assertEqual(evidence["metrics"]["app.wall_time"]["unit"], "s")

    def test_probe_binary_version_never_reports_stderr(self) -> None:
        """Never report unknown --version stderr error as a real version."""
        from benchmarks.manifest import probe_binary_version
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('error: unknown option --version\\n'); sys.exit(1)"]
        ver = probe_binary_version(cmd)
        self.assertIsNone(ver)

    def test_bounded_file_read_rejects_oversized_file(self) -> None:
        """Bounded memory reading rejects files exceeding cap with ParseError."""
        from benchmarks.runner import read_bounded_file
        oversized = Path(self.temp_dir) / "oversized.log"
        # Test with a smaller cap
        oversized.write_text("A" * 1024, encoding="utf-8")
        with self.assertRaises(ParseError) as ctx:
            read_bounded_file(oversized, max_bytes=512)
        self.assertIn("exceeds limit", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
