"""Opt-in target-host sched_ext environment and executor lifecycle checks.

Run on the target host with administrator-provided BPF privileges. Read-only
checks do not establish load permissions or scheduler ABI compatibility.
"""
from __future__ import annotations

import argparse
import collections
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from arma4os.executor import RustExecutor
from benchmarks.orchestration.config import load_config
from benchmarks.reporting.reconstruction import reconstruct_summary
from benchmarks.deployment._support import require, write_json

SYSFS = Path("/sys/kernel/sched_ext")

BTF = Path("/sys/kernel/btf/vmlinux")

STATE_DIR = "/run/arma4os"

def check_environment(scheduler: Path) -> dict:
    require(sys.version_info >= (3, 11), "Python >= 3.11 is required")
    require(scheduler.is_absolute(), "Scheduler path must be absolute")
    require(scheduler.is_file() and os.access(scheduler, os.X_OK),
            f"Scheduler is missing or not executable: {scheduler}")
    require(BTF.is_file() and os.access(BTF, os.R_OK), f"BTF is not readable: {BTF}")
    require((SYSFS / "state").is_file(), "sched_ext sysfs is absent; run on the target guest")
    state = (SYSFS / "state").read_text().strip()
    require(state == "disabled", f"sched_ext state is {state!r}; external scheduler must exit first")
    return {"kernel": os.uname().release, "effective_uid": os.geteuid(),
            "state": state, "scheduler": str(scheduler), "state_dir": STATE_DIR,
            "scheduler_sha256": hashlib.sha256(scheduler.read_bytes()).hexdigest(),
            "note": "Read-only checks do not prove BPF load permission or ABI compatibility"}

def run_command(argv: list[str], output: Path, name: str, timeout_s: float) -> int:
    """Preserve complete stdout/stderr; interruption requests child cleanup first."""
    print("Running:", " ".join(argv), flush=True)
    with (output / f"{name}.stdout").open("w") as stdout, (output / f"{name}.stderr").open("w") as stderr:
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
        try:
            code = process.wait(timeout=timeout_s)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
    print((output / f"{name}.stdout").read_text(), end="", flush=True)
    if code:
        print((output / f"{name}.stderr").read_text(), file=sys.stderr, end="", flush=True)
    return code

@contextmanager
def executor_diagnostics(path: Path):
    """RustExecutor inherits OS stderr; retain its cleanup diagnostics verbatim."""
    saved = os.dup(2)
    try:
        with path.open("w") as log:
            os.dup2(log.fileno(), 2)
            yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)

def verify_matrix(run_dir: Path, repeats: int) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    require(manifest.get("executor_backend") == "real" and manifest.get("policy_comparison_valid") is True,
            "Matrix must report a real, valid policy comparison")
    records = [json.loads(line) for line in (run_dir / "metrics.jsonl").read_text().splitlines()]
    plans = manifest["trial_plan"]
    require(collections.Counter(r["trial_id"] for r in records) ==
            collections.Counter(p["trial_id"] for p in plans), "Missing or duplicate trial records")
    require(all(r["status"] == "success" for r in records), "Warmup or formal trial failed")
    for record in records:
        trial = run_dir / "trials" / record["trial_id"]
        require(all((trial / name).is_file() for name in ("record.json", "stdout.log", "stderr.log")),
                f"Raw trial evidence is missing: {record['trial_id']}")
    counts = collections.Counter(r["policy"] for r in records if not r["is_warmup"])
    require(counts == {"default": repeats, "scx_simple": repeats}, "Incorrect independent repeat counts")
    original = json.loads((run_dir / "summary.json").read_text())
    write_json(run_dir.parent / "summary-original.json", original)
    rebuilt = reconstruct_summary(run_dir)
    write_json(run_dir.parent / "summary-rebuilt.json", rebuilt)
    require(rebuilt == original,
            "Summary differs from reconstruction of original output")
    require(all(g["failed_trials"] == 0 and g["successful_trials"] == repeats for g in rebuilt["groups"]),
            "Reconstructed trial results contain failures")
    return {"total_trials": len(records), "formal_counts": dict(counts), "summary_matches_raw": True}

def wait_recovered(managed_pid: int, timeout_s: float = 12) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if (SYSFS / "state").read_text().strip() == "disabled" and not Path(f"/proc/{managed_pid}").exists():
            return
        time.sleep(0.05)
    raise RuntimeError(f"Recovery failed: state must be disabled and owned PID {managed_pid} must exit")

def lifecycle_case(binary: Path, config: Path, mode: str, record: dict) -> None:
    executor = RustExecutor(binary, config)
    managed_pid = None
    try:
        record["before"] = executor.request("status")
        require(record["before"].get("policy_id") == "default" and record["before"].get("healthy"),
                "Executor must initially report healthy default")
        record["apply"] = executor.request("apply", {
            "policy_id": "scx_simple", "expected_policy": "default",
            "expires_at_unix_ms": int(time.time() * 1000) + 30000}, timeout_ms=15000)
        action = record["apply"]
        managed_pid = action.get("managed_pid")
        require(action.get("status") == "applied" and action.get("verified") is True and
                action.get("backend") == "real" and action.get("actual_policy") == "scx_simple",
                "Real expert apply failed")
        record["during"] = executor.request("status")
        status = record["during"]
        require(status.get("healthy") and status.get("backend") == "real" and
                status.get("policy_id") == "scx_simple" and status.get("kernel_state") == "enabled" and
                status.get("kernel_ops") == "simple" and isinstance(managed_pid, int) and managed_pid > 0 and
                status.get("managed_pid") == managed_pid, "Active sysfs identity or ownership mismatch")
        ops = SYSFS / "root/ops"
        if not ops.exists():
            ops = SYSFS / "ops"
        record["sysfs_ops"] = ops.read_text().strip()
        require(record["sysfs_ops"] == "simple", "Kernel ops identity mismatch")
        if mode == "rollback":
            record["rollback"] = executor.request("rollback", {
                "policy_id": "default", "expected_policy": "scx_simple",
                "expires_at_unix_ms": int(time.time() * 1000) + 30000}, timeout_ms=15000)
            result = record["rollback"]
            require(result.get("status") == "rolled_back" and result.get("verified") is True and
                    result.get("actual_policy") == "default", "Rollback was not verified")
            wait_recovered(managed_pid)
            executor.close()
        elif mode == "eof":
            executor.process.stdin.close()
            executor.closed = True
        elif mode == "sigterm":
            executor.process.send_signal(signal.SIGTERM)
        else:
            raise ValueError(f"Unknown lifecycle mode: {mode}")
        record["exit_code"] = executor.process.wait(timeout=20)
        require(record["exit_code"] == 0, "Executor exited with a nonzero cleanup status")
        wait_recovered(managed_pid)
        record["managed_pid_gone"] = True
        record["after_state"] = (SYSFS / "state").read_text().strip()
    finally:
        try:
            executor.close()
        finally:
            if isinstance(managed_pid, int) and managed_pid > 0:
                wait_recovered(managed_pid)
            record["after_cleanup_state"] = (SYSFS / "state").read_text().strip()
    record["passed"] = True

def interrupted(signum: int, frame: object) -> None:
    raise InterruptedError(f"Received signal {signum}; requesting owned process cleanup")

def run_smoke(args: argparse.Namespace) -> int:
    environment = check_environment(args.scheduler)
    binary = args.executor.resolve()
    require(binary.is_file() and os.access(binary, os.X_OK), f"Executor is missing: {binary}")
    output = args.output or Path("results") / ("real-smoke-" +
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    print(f"Evidence directory: {output}", flush=True)
    report = {"ok": False, "environment": environment, "lifecycle": []}
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        write_json(output / "environment.json", environment)
        config = {"schema_version": 1, "backend": "real", "state_dir": STATE_DIR,
                  "sysfs_root": str(SYSFS), "policies": [
                      {"id": "default", "command": [], "kernel_ops": None},
                      {"id": "scx_simple", "command": [str(args.scheduler)], "kernel_ops": "simple"}]}
        config_path = output / "executor.json"
        write_json(config_path, config)
        bench = load_config("configs/benchmarks.json").to_dict()
        bench["policies"] = ["default", "scx_simple"]
        matrix = output / "matrix.json"
        write_json(matrix, bench)
        report["executor_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        report["benchmark_argv"] = [
            sys.executable, "-m", "benchmarks", "run", "--config", str(matrix),
            "--output", str(output / "matrix"), "--repeats", str(args.repeats), "--seed", str(args.seed),
            "--executor", str(binary), "--executor-config", str(config_path)]
        write_json(output / "verification.json", report)
        report["benchmark_returncode"] = run_command(
            report["benchmark_argv"], output, "benchmark", timeout_s=120 + 120 * (args.repeats + 1))
        require(report["benchmark_returncode"] == 0, "Benchmark failed; see benchmark.stderr and matrix trials")
        report["matrix"] = verify_matrix(output / "matrix", args.repeats)
        require((SYSFS / "state").read_text().strip() == "disabled", "Matrix did not restore default")
        for mode in ("rollback", "eof", "sigterm"):
            record = {"mode": mode, "passed": False}
            report["lifecycle"].append(record)
            write_json(output / "verification.json", report)
            try:
                with executor_diagnostics(output / f"{mode}.stderr"):
                    lifecycle_case(binary, config_path, mode, record)
            except Exception as exc:
                record["error"] = str(exc)
                raise
            finally:
                write_json(output / "verification.json", report)
            print(f"Recovery {mode}: passed", flush=True)
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc) or type(exc).__name__
        print(f"Real smoke failed: {report['error']}", file=sys.stderr)
    finally:
        report["final_state"] = (SYSFS / "state").read_text().strip()
        report["ok"] = report["ok"] and report["final_state"] == "disabled"
        write_json(output / "verification.json", report)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"Real smoke {'PASSED' if report['ok'] else 'FAILED'}: {output}")
    return 0 if report["ok"] else 1

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Read-only host checks; no scheduler load")
    parser.add_argument("--scheduler", type=Path, required=True, help="Trusted absolute scx_simple executable")
    parser.add_argument("--executor", type=Path, default=Path("target/release/arma-executor"))
    parser.add_argument("--output", type=Path, help="New output directory; default generates a unique name")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        require(args.repeats > 0, "Repeat count must be positive")
        if args.check_only:
            print(json.dumps(check_environment(args.scheduler), indent=2))
            return 0
        return run_smoke(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Real smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
