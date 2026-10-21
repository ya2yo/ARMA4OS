"""Run one trial with bounded process lifetime and retained raw evidence."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any

from benchmarks.orchestration.models import TrialPlan, TrialRecord
from benchmarks.evidence.files import read_bounded_file
from benchmarks.evidence.parsers.base import ParseError
from benchmarks.evidence.parsers.dispatch import parse_trial_evidence
from benchmarks.evidence.store import write_trial_record
from benchmarks.execution.processes import is_process_group_alive, kill_process_group


def run_trial(
    plan: TrialPlan,
    run_dir: Path,
    timeout_sec: float | None = None,
) -> TrialRecord:
    """Execute a single workload trial with streaming IO and process group isolation.

    No shell is used (direct argv execution).
    stdout and stderr are streamed directly to disk files to maintain bounded memory.
    On timeout, process groups are killed including descendants even if the leader exits early.
    """
    trial_dir = run_dir / "trials" / plan.trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)

    stdout_file = trial_dir / "stdout.log"
    stderr_file = trial_dir / "stderr.log"

    effective_timeout = timeout_sec if timeout_sec is not None else plan.workload.timeout_sec
    cmd = plan.workload.command
    child_env = os.environ.copy()
    child_env["ARMA_BENCHMARK_TRIAL_DIR"] = str(trial_dir.resolve())

    status: str = "success"
    exit_code: int | None = None
    metrics: dict[str, Any] = {}
    error_message: str | None = None
    timed_out = False

    t_start_mono = time.monotonic()
    t_start_unix = time.time()
    deadline = t_start_mono + effective_timeout

    # Stream stdout and stderr directly to disk files with bounded memory overhead
    with stdout_file.open("wb") as out_f, stderr_file.open("wb") as err_f:
        try:
            # start_new_session=True creates a new POSIX session and process group (pgid == proc.pid)
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
                env=child_env,
            )
            pgid = proc.pid
        except OSError as exc:
            t_end_mono = time.monotonic()
            t_end_unix = time.time()
            record = TrialRecord(
                trial_id=plan.trial_id,
                round_index=plan.round_index,
                repeat_index=plan.repeat_index,
                policy=plan.policy,
                workload=plan.workload.name,
                is_warmup=plan.is_warmup,
                status="nonzero_exit",
                exit_code=-1,
                start_time_unix=t_start_unix,
                end_time_unix=t_end_unix,
                duration_s=round(t_end_mono - t_start_mono, 6),
                stdout_path=str(stdout_file.relative_to(run_dir)),
                stderr_path=str(stderr_file.relative_to(run_dir)),
                metrics={},
                error_message=f"Failed to spawn process: {exc}",
            )
            write_trial_record(run_dir, record)
            return record

        try:
            # 1. Wait for process group leader
            try:
                proc.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_group(pgid)
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

            # 2. If leader exited early, check if any descendants in process group are still alive
            if not timed_out:
                while is_process_group_alive(pgid):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        kill_process_group(pgid)
                        break
                    time.sleep(min(0.05, remaining))
        finally:
            # Always ensure entire process group is killed, even on KeyboardInterrupt
            kill_process_group(pgid)
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            exit_code = proc.returncode

    t_end_mono = time.monotonic()
    t_end_unix = time.time()
    duration_s = max(t_end_mono - t_start_mono, 0.0)

    # Determine status and parse output
    if timed_out:
        status = "timeout"
        exit_code = None
        error_message = f"Trial timed out after {effective_timeout:.2f}s"
    elif exit_code != 0:
        status = "nonzero_exit"
        error_message = f"Process exited with non-zero exit code: {exit_code}"
    else:
        # Exit code 0, parse outputs with bounded memory
        try:
            stdout_text = read_bounded_file(stdout_file)
            stderr_text = read_bounded_file(stderr_file)

            parsed_result = parse_trial_evidence(
                plan.workload.parser, trial_dir, stdout_text, stderr_text,
                duration_s, exit_code, plan.workload.metadata,
            )
            metrics = parsed_result.metrics
            status = "success"
        except ParseError as parse_exc:
            status = "parse_error"
            error_message = str(parse_exc)
        except Exception as exc:
            status = "parse_error"
            error_message = f"Unexpected parser error: {exc}"

    record = TrialRecord(
        trial_id=plan.trial_id,
        round_index=plan.round_index,
        repeat_index=plan.repeat_index,
        policy=plan.policy,
        workload=plan.workload.name,
        is_warmup=plan.is_warmup,
        status=status,
        exit_code=exit_code,
        start_time_unix=t_start_unix,
        end_time_unix=t_end_unix,
        duration_s=round(duration_s, 6),
        stdout_path=str(stdout_file.relative_to(run_dir)),
        stderr_path=str(stderr_file.relative_to(run_dir)),
        metrics=metrics,
        error_message=error_message,
    )
    write_trial_record(run_dir, record)
    return record
