"""Command Line Interface for ARMA4OS benchmark framework."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from benchmarks.orchestration.engine import run_benchmark
from benchmarks.reporting.formatting import format_summary_table
from benchmarks.reporting.reconstruction import reconstruct_summary


def summarize_run(run_dir: str, output_path: str | None = None) -> int:
    """Reconstruct summary statistics from raw trial data in RUN_DIR."""
    try:
        summary = reconstruct_summary(run_dir)
        print(format_summary_table(summary))

        if output_path:
            out_file = Path(output_path)
            with out_file.open("w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            print(f"Reconstructed summary written to: {out_file}")
        return 0
    except Exception as exc:
        print(f"Summarize error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the command line argument parser."""
    parser = argparse.ArgumentParser(
        prog="benchmarks",
        description="ARMA4OS Benchmark Framework: reproducible scheduling evaluations",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: run
    run_parser = subparsers.add_parser("run", help="Run benchmark suite")
    run_parser.add_argument(
        "--config",
        type=str,
        default="configs/benchmarks.json",
        help="Path to benchmark JSON configuration file (default: configs/benchmarks.json)",
    )
    run_parser.add_argument(
        "--output",
        type=str,
        default="results/benchmarks",
        help="Directory to store trial outputs and metrics (default: results/benchmarks)",
    )
    run_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for randomized trial rounds (default: 42)",
    )
    run_parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="Number of independent repetitions per workload (default: 5)",
    )
    run_parser.add_argument(
        "--executor",
        type=str,
        default=None,
        help="Path to arma-executor binary for active scx switching (optional)",
    )
    run_parser.add_argument(
        "--executor-config",
        type=str,
        default=None,
        help="Path to executor configuration JSON (optional)",
    )

    formal_parser = subparsers.add_parser(
        "formal",
        help="Run kernel_build, schbench, and mixed benchmark scenarios",
    )
    formal_parser.add_argument("--config", required=True)
    formal_parser.add_argument("--output", required=True)
    formal_parser.add_argument("--seed", type=int, default=42)
    formal_parser.add_argument("--repeats", type=int, default=5)
    formal_parser.add_argument("--warmup", type=int, default=1)
    formal_parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["kernel_build", "schbench", "mixed"],
        default=["kernel_build", "schbench", "mixed"],
        help="Benchmark workloads to run (kernel_build, schbench, mixed; default: all three)",
    )
    formal_parser.add_argument("--executor")
    formal_parser.add_argument("--executor-config")

    # Subcommand: summarize
    sum_parser = subparsers.add_parser("summarize", help="Reconstruct summary from RUN_DIR")
    sum_parser.add_argument(
        "run_dir",
        type=str,
        help="Path to benchmark run directory containing metrics.jsonl",
    )
    sum_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional destination path for reconstructed summary.json",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for python3 -m benchmarks."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "formal":
        from benchmarks.orchestration.formal.commands import run_formal
        return run_formal(args)
    if args.command == "run":
        return run_benchmark(
            config_path=args.config,
            output_path=args.output,
            seed=args.seed,
            repeats=args.repeats,
            executor_path=args.executor,
            executor_config_path=args.executor_config,
        )
    elif args.command == "summarize":
        return summarize_run(
            run_dir=args.run_dir,
            output_path=args.output,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
