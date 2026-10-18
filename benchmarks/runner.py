"""Trial execution, process management, and streaming IO."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any

from benchmarks.matrix import TrialPlan
from benchmarks.parsers import ParseError, get_parser


@dataclass
class TrialRecord:
    """Execution record and measurements for a single trial."""

    trial_id: str
    round_index: int
    repeat_index: int
    policy: str
    workload: str
    is_warmup: bool
    status: str  # "success" | "timeout" | "nonzero_exit" | "parse_error"
    exit_code: int | None
    start_time_unix: float
    end_time_unix: float
    duration_s: float
    stdout_path: str
    stderr_path: str
    metrics: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MAX_OUTPUT_BYTES: int = 16 * 1024 * 1024  # 16 MiB memory safety cap


def read_bounded_file(path: Path, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """Read file content with strict size cap, preventing unbounded memory consumption."""
    if not path.exists():
        return ""
    size = path.stat().st_size
    if size > max_bytes:
        raise ParseError(f"Output file '{path.name}' ({size} bytes) exceeds limit ({max_bytes} bytes)")
    with path.open("r", encoding="utf-8", errors="replace") as f:
        content = f.read(max_bytes + 1)
        if len(content.encode("utf-8", errors="replace")) > max_bytes:
            raise ParseError(f"Output file '{path.name}' exceeds limit ({max_bytes} bytes)")
        return content


def kill_process_group(pgid: int) -> None:
    """Kill an entire POSIX process group, including any spawned descendants."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass


def is_process_group_alive(pgid: int) -> bool:
    """Check if any processes in the given process group are still alive."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


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
            )
            pgid = proc.pid
        except OSError as exc:
            t_end_mono = time.monotonic()
            t_end_unix = time.time()
            record_dict = {
                "trial_id": plan.trial_id,
                "round_index": plan.round_index,
                "repeat_index": plan.repeat_index,
                "policy": plan.policy,
                "workload": plan.workload.name,
                "is_warmup": plan.is_warmup,
                "status": "nonzero_exit",
                "exit_code": -1,
                "start_time_unix": t_start_unix,
                "end_time_unix": t_end_unix,
                "duration_s": round(t_end_mono - t_start_mono, 6),
                "stdout_path": str(stdout_file.relative_to(run_dir)),
                "stderr_path": str(stderr_file.relative_to(run_dir)),
                "error_message": f"Failed to spawn process: {exc}",
            }
            with (trial_dir / "record.json").open("w", encoding="utf-8") as rf:
                json.dump(record_dict, rf, indent=2, ensure_ascii=False)

            return TrialRecord(
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

            parser = get_parser(plan.workload.parser)
            parsed_result = parser.parse(stdout_text, stderr_text, duration_s, exit_code)
            metrics = parsed_result.metrics
            status = "success"
        except ParseError as parse_exc:
            status = "parse_error"
            error_message = str(parse_exc)
        except Exception as exc:
            status = "parse_error"
            error_message = f"Unexpected parser error: {exc}"

    # Write per-trial raw execution metadata record.json
    raw_meta = {
        "trial_id": plan.trial_id,
        "round_index": plan.round_index,
        "repeat_index": plan.repeat_index,
        "policy": plan.policy,
        "workload": plan.workload.name,
        "is_warmup": plan.is_warmup,
        "status": status,
        "exit_code": exit_code,
        "start_time_unix": t_start_unix,
        "end_time_unix": t_end_unix,
        "duration_s": round(duration_s, 6),
        "stdout_path": str(stdout_file.relative_to(run_dir)),
        "stderr_path": str(stderr_file.relative_to(run_dir)),
        "error_message": error_message,
    }
    with (trial_dir / "record.json").open("w", encoding="utf-8") as rf:
        json.dump(raw_meta, rf, indent=2, ensure_ascii=False)

    return TrialRecord(
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
