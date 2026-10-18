"""Command Line Interface for ARMA4OS benchmark framework."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any, Sequence

from benchmarks.config import BenchmarkConfig, load_config
from benchmarks.executor_client import ExecutorClient, ExecutorError
from benchmarks.manifest import generate_manifest, verify_kernel_sched_ext_state
from benchmarks.matrix import generate_trial_plan
from benchmarks.runner import TrialRecord, run_trial
from benchmarks.summary import format_summary_table, generate_summary, reconstruct_summary


def run_benchmark(
    config_path: str,
    output_path: str,
    seed: int = 42,
    repeats: int = 5,
    executor_path: str | None = None,
    executor_config_path: str | None = None,
) -> int:
    """Execute the full benchmark suite according to the provided configuration."""
    output_dir = Path(output_path)
    # Reject already existing output directory to protect previous evidence
    if output_dir.exists():
        print(f"Error: Output directory '{output_dir}' already exists. Refusing to overwrite previous evidence.", file=sys.stderr)
        return 1

    # 1. Load and validate benchmark configuration
    try:
        config = load_config(config_path)
        has_executor = bool(executor_path)
        config.validate(has_executor=has_executor)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # Validate executor arguments
    if executor_path and not executor_config_path:
        print("Error: --executor-config is required when --executor is provided.", file=sys.stderr)
        return 1

    # 2. In default-only mode, verify kernel sched_ext state immediately
    if not executor_path:
        try:
            verify_kernel_sched_ext_state()
        except RuntimeError as exc:
            print(f"Kernel sched_ext verification failed: {exc}", file=sys.stderr)
            return 1

    output_dir.mkdir(parents=True, exist_ok=False)

    actions_file = output_dir / "actions.jsonl"
    metrics_file = output_dir / "metrics.jsonl"
    manifest_file = output_dir / "manifest.json"
    summary_file = output_dir / "summary.json"

    metrics_file.write_text("", encoding="utf-8")
    actions_file.write_text("", encoding="utf-8")

    # 3. Generate randomized serial round plan
    try:
        plans = generate_trial_plan(config=config, repeats=repeats, seed=seed)
    except Exception as exc:
        print(f"Matrix generation error: {exc}", file=sys.stderr)
        return 1

    total_trials = len(plans)
    trial_order = [p.trial_id for p in plans]

    # 4. Executor initialization
    executor: ExecutorClient | None = None
    executor_backend = "kernel-default"
    if executor_path and executor_config_path:
        try:
            executor = ExecutorClient(
                executor_path=executor_path,
                config_path=executor_config_path,
                actions_log_path=actions_file,
            )
            executor.start()
            init_status = executor.verify_status()
            executor_backend = init_status.get("backend", "simulated")
        except Exception as exc:
            print(f"Failed to connect to executor: {exc}", file=sys.stderr)
            if executor:
                executor.shutdown()
            return 1

    # 5. Write initial manifest
    manifest = generate_manifest(
        config=config,
        seed=seed,
        repeats=repeats,
        output_dir=output_dir,
        executor_path=executor_path,
        executor_config_path=executor_config_path,
        executor_backend=executor_backend,
        trial_order=trial_order,
    )
    manifest["trial_plan"] = [
        {"trial_id": p.trial_id, "policy": p.policy, "workload": p.workload.name,
         "round_index": p.round_index, "repeat_index": p.repeat_index, "is_warmup": p.is_warmup}
        for p in plans
    ]
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Starting ARMA4OS Benchmark Run: {manifest['run_id']}")
    print(f"Output directory: {output_dir}")
    print(f"Policies: {config.policies} | Workloads: {[w.name for w in config.workloads]}")
    print(f"Rounds: {repeats} repeats | Total scheduled trials: {total_trials}")
    if executor_path:
        print(f"Executor: {executor_path} (config: {executor_config_path}, backend: {executor_backend})")
    else:
        print(f"Running in default-only mode (verified kernel backend: {executor_backend})")
    print("-" * 80)

    # 6. Set up signal handling for clean cancellation
    interrupted = False

    def _sig_handler(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        print(f"\nBenchmark received signal {signum}, initiating graceful cleanup...", file=sys.stderr)
        raise KeyboardInterrupt()

    old_sigint = signal.signal(signal.SIGINT, _sig_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _sig_handler)

    records: list[TrialRecord] = []

    try:
        with metrics_file.open("a", encoding="utf-8") as metrics_f:
            for idx, plan in enumerate(plans, start=1):
                warmup_tag = " [WARMUP]" if plan.is_warmup else ""
                print(f"[{idx}/{total_trials}]{warmup_tag} Trial {plan.trial_id} | Policy: {plan.policy:<12} | Workload: {plan.workload.name:<18}", end="", flush=True)

                # Executor management and status verification immediately BEFORE trial
                if executor:
                    if plan.policy != executor.current_policy:
                        try:
                            executor.apply_policy(plan.policy)
                        except ExecutorError as exc:
                            print(f" -> EXECUTOR APPLY ERROR: {exc}")
                            record = TrialRecord(
                                trial_id=plan.trial_id,
                                round_index=plan.round_index,
                                repeat_index=plan.repeat_index,
                                policy=plan.policy,
                                workload=plan.workload.name,
                                is_warmup=plan.is_warmup,
                                status="nonzero_exit",
                                exit_code=-1,
                                start_time_unix=time.time(),
                                end_time_unix=time.time(),
                                duration_s=0.0,
                                stdout_path="",
                                stderr_path="",
                                metrics={},
                                error_message=f"Executor failed to apply policy: {exc}",
                            )
                            records.append(record)
                            t_dir = output_dir / "trials" / plan.trial_id
                            t_dir.mkdir(parents=True, exist_ok=True)
                            (t_dir / "record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
                            metrics_f.write(json.dumps(record.to_dict()) + "\n")
                            metrics_f.flush()
                            continue

                    # Exact policy verification immediately before trial (even for held policies)
                    try:
                        executor.verify_policy(plan.policy)
                    except ExecutorError as exc:
                        print(f" -> PRE-TRIAL POLICY VERIFICATION FAILED: {exc}")
                        record = TrialRecord(
                            trial_id=plan.trial_id,
                            round_index=plan.round_index,
                            repeat_index=plan.repeat_index,
                            policy=plan.policy,
                            workload=plan.workload.name,
                            is_warmup=plan.is_warmup,
                            status="nonzero_exit",
                            exit_code=-1,
                            start_time_unix=time.time(),
                            end_time_unix=time.time(),
                            duration_s=0.0,
                            stdout_path="",
                            stderr_path="",
                            metrics={},
                            error_message=f"Pre-trial executor verification failed: {exc}",
                        )
                        records.append(record)
                        t_dir = output_dir / "trials" / plan.trial_id
                        t_dir.mkdir(parents=True, exist_ok=True)
                        (t_dir / "record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
                        metrics_f.write(json.dumps(record.to_dict()) + "\n")
                        metrics_f.flush()
                        continue
                else:
                    # In default-only mode, verify kernel state immediately before trial
                    try:
                        verify_kernel_sched_ext_state()
                    except RuntimeError as exc:
                        print(f" -> PRE-TRIAL KERNEL VERIFICATION FAILED: {exc}")
                        record = TrialRecord(
                            trial_id=plan.trial_id,
                            round_index=plan.round_index,
                            repeat_index=plan.repeat_index,
                            policy=plan.policy,
                            workload=plan.workload.name,
                            is_warmup=plan.is_warmup,
                            status="nonzero_exit",
                            exit_code=-1,
                            start_time_unix=time.time(),
                            end_time_unix=time.time(),
                            duration_s=0.0,
                            stdout_path="",
                            stderr_path="",
                            metrics={},
                            error_message=f"Pre-trial kernel sched_ext verification failed: {exc}",
                        )
                        records.append(record)
                        t_dir = output_dir / "trials" / plan.trial_id
                        t_dir.mkdir(parents=True, exist_ok=True)
                        (t_dir / "record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
                        metrics_f.write(json.dumps(record.to_dict()) + "\n")
                        metrics_f.flush()
                        continue

                # Run trial with streaming IO and process group isolation
                record = run_trial(plan, run_dir=output_dir)

                # Status verification immediately AFTER trial
                if executor:
                    try:
                        executor.verify_policy(plan.policy)
                    except ExecutorError as exc:
                        print(f" -> POST-TRIAL POLICY VERIFICATION FAILED: {exc}")
                        record.status = "nonzero_exit"
                        record.metrics = {}
                        record.error_message = f"Post-trial executor verification failed: {exc}"
                        t_dir = output_dir / "trials" / plan.trial_id
                        (t_dir / "record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
                else:
                    try:
                        verify_kernel_sched_ext_state()
                    except RuntimeError as exc:
                        print(f" -> POST-TRIAL KERNEL VERIFICATION FAILED: {exc}")
                        record.status = "nonzero_exit"
                        record.metrics = {}
                        record.error_message = f"Post-trial kernel sched_ext verification failed: {exc}"
                        t_dir = output_dir / "trials" / plan.trial_id
                        (t_dir / "record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")

                records.append(record)
                metrics_f.write(json.dumps(record.to_dict()) + "\n")
                metrics_f.flush()

                status_str = record.status.upper()
                if record.status == "success":
                    summary_detail = ""
                    if "p99" in record.metrics:
                        summary_detail = f" (p99={record.metrics['p99']} {record.metrics.get('latency_unit', '')})"
                    elif "throughput_mb_s" in record.metrics:
                        summary_detail = f" ({record.metrics['throughput_mb_s']} MB/s)"
                    print(f" -> {status_str} in {record.duration_s:.3f}s{summary_detail}")
                else:
                    print(f" -> {status_str}: {record.error_message}")

    except (KeyboardInterrupt, BaseException) as exc:
        if not interrupted:
            print(f"\nBenchmark interrupted or encountered error: {exc}", file=sys.stderr)
        interrupted = True
    finally:
        # Restore previous signal handlers
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)

        # Restore executor default policy and shut down
        if executor:
            print("Cleaning up executor connection and restoring default policy...")
            executor.shutdown()

        # Do not drop scheduled failures: record remaining scheduled trials
        executed_ids = {r.trial_id for r in records}
        with metrics_file.open("a", encoding="utf-8") as metrics_f:
            for plan in plans:
                if plan.trial_id not in executed_ids:
                    record = TrialRecord(
                        trial_id=plan.trial_id,
                        round_index=plan.round_index,
                        repeat_index=plan.repeat_index,
                        policy=plan.policy,
                        workload=plan.workload.name,
                        is_warmup=plan.is_warmup,
                        status="nonzero_exit",
                        exit_code=-1,
                        start_time_unix=0.0,
                        end_time_unix=0.0,
                        duration_s=0.0,
                        stdout_path="",
                        stderr_path="",
                        metrics={},
                        error_message="Scheduled trial cancelled or interrupted before execution",
                    )
                    records.append(record)
                    t_dir = output_dir / "trials" / plan.trial_id
                    t_dir.mkdir(parents=True, exist_ok=True)
                    (t_dir / "record.json").write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
                    metrics_f.write(json.dumps(record.to_dict()) + "\n")
                    metrics_f.flush()

        # Write complete summary.json
        raw_records = [r.to_dict() for r in records]
        summary = generate_summary(raw_records, manifest)
        summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

        # Finalize manifest.json with completion metadata
        manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["interrupted"] = interrupted
        manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        if records:
            print("\n" + format_summary_table(summary))
            print(f"\nRun artifacts saved to: {output_dir}")

    # Return nonzero if any formal trial failed or if execution was interrupted
    if interrupted:
        return 1

    has_formal_failure = any(not r.is_warmup and r.status != "success" for r in records)
    if has_formal_failure:
        print("\nNotice: One or more formal trials failed during execution.", file=sys.stderr)
        return 1

    return 0


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
