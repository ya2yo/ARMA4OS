"""ARMA4OS Benchmark Framework.

This package provides an independent, reproducible benchmark harness for Linux
scheduling evaluations (including sched_ext / scx experts and default scheduler baselines).

Configuration Schema:
--------------------
Configurations are defined in JSON files adhering to schema_version 1:

{
  "schema_version": 1,
  "description": "ARMA4OS Benchmark Suite",
  "policies": ["default", "scx_bpfland"],       // Baseline 'default' is mandatory
  "timeout_sec": 60.0,                          // Global timeout per trial
  "warmup": 0,                                  // Global warmup rounds per workload
  "workloads": [
    {
      "name": "workload_identifier",            // Unique workload name
      "command": ["python3", "-m", "..."],      // Direct argv list (no shell)
      "parser": "elapsed",                      // "elapsed" or "schbench"
      "timeout_sec": 30.0,                      // Optional workload timeout
      "warmup": 1,                              // Optional workload warmup count
      "metadata": {}                            // Optional user metadata
    }
  ]
}

CLI Commands:
-------------
Run benchmark matrix:
  python3 -m benchmarks run --config configs/benchmarks.json \\
                            --output results/benchmarks \\
                            --seed 42 \\
                            --repeats 5 \\
                            [--executor PATH --executor-config PATH]

Reconstruct summary from existing run directory:
  python3 -m benchmarks summarize results/benchmarks [--output summary.json]
"""

from __future__ import annotations

from benchmarks.config import BenchmarkConfig, WorkloadConfig, load_config
from benchmarks.executor_client import ExecutorClient, ExecutorError
from benchmarks.manifest import generate_manifest
from benchmarks.matrix import TrialPlan, generate_trial_plan
from benchmarks.parsers import ParseError, Parser, get_parser
from benchmarks.parsers.elapsed import ElapsedParser
from benchmarks.parsers.schbench import SchbenchParser
from benchmarks.runner import TrialRecord, kill_process_group, run_trial
from benchmarks.summary import (
    calculate_ci_95,
    calculate_sample_std,
    format_summary_table,
    generate_summary,
    reconstruct_summary,
)

__all__ = [
    "BenchmarkConfig",
    "WorkloadConfig",
    "load_config",
    "ExecutorClient",
    "ExecutorError",
    "generate_manifest",
    "TrialPlan",
    "generate_trial_plan",
    "Parser",
    "ParseError",
    "get_parser",
    "ElapsedParser",
    "SchbenchParser",
    "TrialRecord",
    "kill_process_group",
    "run_trial",
    "calculate_ci_95",
    "calculate_sample_std",
    "format_summary_table",
    "generate_summary",
    "reconstruct_summary",
]
