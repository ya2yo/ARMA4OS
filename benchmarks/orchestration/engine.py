"""Own experiment planning, verified trial sequencing, and finalization.

The CLI and specialized suites call this engine. Process execution, evidence
storage, and statistics remain behind their respective module interfaces.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import signal
import sys
import time
from typing import Any

from benchmarks.orchestration.config import load_config
from benchmarks.orchestration.matrix import generate_trial_plan
from benchmarks.orchestration.models import BenchmarkConfig, TrialPlan, TrialRecord
from benchmarks.execution.executor import ExecutorClient, ExecutorError
from benchmarks.execution.policy import verify_kernel_sched_ext_state
from benchmarks.execution.trial import run_trial
from benchmarks.evidence.manifest import generate_manifest
from benchmarks.evidence.store import RunStore
from benchmarks.reporting.formatting import format_summary_table
from benchmarks.reporting.statistics import generate_summary


def _failed_trial(plan: TrialPlan, message: str, *, cancelled: bool = False) -> TrialRecord:
    timestamp = 0.0 if cancelled else time.time()
    return TrialRecord(
        trial_id=plan.trial_id, round_index=plan.round_index,
        repeat_index=plan.repeat_index, policy=plan.policy,
        workload=plan.workload.name, is_warmup=plan.is_warmup,
        status="nonzero_exit", exit_code=-1,
        start_time_unix=timestamp, end_time_unix=timestamp, duration_s=0.0,
        stdout_path="", stderr_path="", error_message=message,
    )


def _run_verified_trial(
    plan: TrialPlan, output_dir: Path, executor: ExecutorClient | None,
) -> TrialRecord:
    """Require the requested scheduler immediately before and after execution."""
    if executor:
        if plan.policy != executor.current_policy:
            try:
                executor.apply_policy(plan.policy)
            except ExecutorError as exc:
                return _failed_trial(plan, f"Executor apply failed: {exc}")
        try:
            executor.verify_policy(plan.policy)
        except ExecutorError as exc:
            return _failed_trial(plan, f"Pre-trial executor verification failed: {exc}")
    else:
        try:
            verify_kernel_sched_ext_state()
        except RuntimeError as exc:
            return _failed_trial(plan, f"Pre-trial kernel sched_ext verification failed: {exc}")

    record = run_trial(plan, run_dir=output_dir)
    try:
        if executor:
            executor.verify_policy(plan.policy)
        else:
            verify_kernel_sched_ext_state()
    except (ExecutorError, RuntimeError) as exc:
        record.status = "nonzero_exit"
        record.metrics = {}
        source = "executor" if executor else "kernel sched_ext"
        record.error_message = f"Post-trial {source} verification failed: {exc}"
    return record


def _print_trial_result(record: TrialRecord) -> None:
    if record.status != "success":
        print(f" -> {record.status.upper()}: {record.error_message}")
        return
    detail = ""
    if "p99" in record.metrics:
        detail = f" (p99={record.metrics['p99']} {record.metrics.get('latency_unit', '')})"
    elif "throughput_mb_s" in record.metrics:
        detail = f" ({record.metrics['throughput_mb_s']} MB/s)"
    print(f" -> SUCCESS in {record.duration_s:.3f}s{detail}")


def run_benchmark(
    config_path: str,
    output_path: str,
    seed: int = 42,
    repeats: int = 5,
    executor_path: str | None = None,
    executor_config_path: str | None = None,
    config_override: BenchmarkConfig | None = None,
) -> int:
    """Execute a serial experiment, retaining every scheduled trial and failure."""
    output_dir = Path(output_path)
    if output_dir.exists():
        print(f"Error: Output directory '{output_dir}' already exists. Refusing to overwrite previous evidence.", file=sys.stderr)
        return 1
    try:
        config = config_override if config_override is not None else load_config(config_path)
        config.validate(has_executor=bool(executor_path))
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if executor_path and not executor_config_path:
        print("Error: --executor-config is required when --executor is provided.", file=sys.stderr)
        return 1
    if not executor_path:
        try:
            verify_kernel_sched_ext_state()
        except RuntimeError as exc:
            print(f"Kernel sched_ext verification failed: {exc}", file=sys.stderr)
            return 1
    try:
        plans = generate_trial_plan(config=config, repeats=repeats, seed=seed)
    except Exception as exc:
        print(f"Matrix generation error: {exc}", file=sys.stderr)
        return 1

    store = RunStore(output_dir)
    store.initialize()
    executor: ExecutorClient | None = None
    executor_backend = "kernel-default"
    records: list[TrialRecord] = []
    manifest: dict[str, Any] | None = None
    interrupted = False
    old_handlers: dict[signal.Signals, Any] = {}

    def handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        print(f"\nBenchmark received signal {signum}, initiating graceful cleanup...", file=sys.stderr)
        raise KeyboardInterrupt()

    try:
        # Install cancellation handling before starting the managed executor.
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, handle_signal)
        if executor_path and executor_config_path:
            executor = ExecutorClient(
                executor_path=executor_path, config_path=executor_config_path,
                actions_log_path=output_dir / "actions.jsonl",
            )
            executor.start()
            executor_backend = executor.verify_status().get("backend", "simulated")

        manifest = generate_manifest(
            config=config, seed=seed, repeats=repeats, output_dir=output_dir,
            executor_path=executor_path, executor_config_path=executor_config_path,
            executor_backend=executor_backend, trial_order=[p.trial_id for p in plans],
        )
        manifest["trial_plan"] = [
            {"trial_id": p.trial_id, "policy": p.policy, "workload": p.workload.name,
             "round_index": p.round_index, "repeat_index": p.repeat_index, "is_warmup": p.is_warmup}
            for p in plans
        ]
        store.write_manifest(manifest)

        print(f"Starting ARMA4OS Benchmark Run: {manifest['run_id']}")
        print(f"Output directory: {output_dir}")
        print(f"Policies: {config.policies} | Workloads: {[w.name for w in config.workloads]}")
        print(f"Rounds: {repeats} repeats | Total scheduled trials: {len(plans)}")
        print(f"Executor backend: {executor_backend}")
        print("-" * 80)
        for index, plan in enumerate(plans, start=1):
            warmup = " [WARMUP]" if plan.is_warmup else ""
            print(f"[{index}/{len(plans)}]{warmup} Trial {plan.trial_id} | Policy: {plan.policy:<12} | Workload: {plan.workload.name:<18}", end="", flush=True)
            record = _run_verified_trial(plan, output_dir, executor)
            store.write_record(record)
            records.append(record)
            _print_trial_result(record)
    except BaseException as exc:
        if not interrupted:
            print(f"\nBenchmark interrupted or encountered error: {exc}", file=sys.stderr)
        interrupted = True
    finally:
        # Cleanup failures must not prevent recording cancelled planned trials.
        try:
            if executor:
                print("Cleaning up executor connection and restoring default policy...")
                executor.shutdown()
        except BaseException as exc:
            interrupted = True
            print(f"Executor cleanup failed: {exc}", file=sys.stderr)
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)

        executed_ids = {r.trial_id for r in records}
        for plan in plans:
            if plan.trial_id not in executed_ids:
                record = _failed_trial(
                    plan, "Scheduled trial cancelled or interrupted before execution", cancelled=True,
                )
                store.write_record(record)
                records.append(record)
        if manifest is not None:
            summary = generate_summary([r.to_dict() for r in records], manifest)
            store.write_summary(summary)
            manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
            manifest["interrupted"] = interrupted
            store.write_manifest(manifest)
            if records:
                print("\n" + format_summary_table(summary))
                print(f"\nRun artifacts saved to: {output_dir}")

    if interrupted:
        return 1
    if any(not r.is_warmup and r.status != "success" for r in records):
        print("\nNotice: One or more formal trials failed during execution.", file=sys.stderr)
        return 1
    return 0
