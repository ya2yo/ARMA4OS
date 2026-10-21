"""Deploy fresh source snapshots to SSH guests and retrieve verification evidence."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import uuid

from benchmarks.deployment._support import require, write_json
from benchmarks.deployment.snapshot import BOOTSTRAP, extract_files, snapshot
from benchmarks.deployment.transport import Connection

def execute(command: list[str], output: Path, name: str, timeout: float) -> int:
    with (output / (name + ".stdout")).open("wb") as stdout, (output / (name + ".stderr")).open("wb") as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout)
    for suffix, stream in [("stdout", sys.stdout), ("stderr", sys.stderr)]:
        print((output / (name + "." + suffix)).read_text(errors="replace"), end="", file=stream, flush=True)
    return result.returncode

def guest_run(args: argparse.Namespace) -> int:
    """Executed inside the fresh guest checkout; bound the job even after SSH loss."""
    output = Path("results")
    output.mkdir(exist_ok=False)
    report = {"ok": False, "returncode": None}
    process = None
    def interrupt(signum, frame):
        raise InterruptedError(f"Guest received signal {signum}")
    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        env = dict(os.environ, SCX_SIMPLE=args.scheduler, REAL_REPEATS=str(args.repeats),
                   REAL_SEED=str(args.seed), REAL_OUTPUT="results/real", PYTHON=sys.executable)
        with (output / "make.stdout").open("w") as out, (output / "make.stderr").open("w") as err:
            process = subprocess.Popen(["make", "test-real"], env=env, stdout=out, stderr=err, start_new_session=True)
            report["returncode"] = process.wait(timeout=args.timeout)
        verification = output / "real/verification.json"
        report["ok"] = report["returncode"] == 0 and verification.is_file() and json.loads(verification.read_text()).get("ok") is True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc) or type(exc).__name__
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        state = Path("/sys/kernel/sched_ext/state")
        report["final_state"] = state.read_text().strip() if state.exists() else "unsupported"
        report["ok"] = bool(report["ok"] and report["final_state"] == "disabled")
        write_json(output / "guest-result.json", report)
        with tarfile.open("evidence.tar.gz", "w:gz") as tar:
            tar.add(output, arcname="results")
            tar.add("deployment-source.json", arcname="deployment-source.json")
        Path("evidence.sha256").write_text(hashlib.sha256(Path("evidence.tar.gz").read_bytes()).hexdigest())
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps(report), flush=True)
    return 0 if report["ok"] else 1

def run_vm(args: argparse.Namespace) -> int:
    require(bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", args.host)), "Invalid SSH host")
    for value in (args.remote_root, args.scheduler):
        require(value.startswith("/") and bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", value)) and
                ".." not in PurePosixPath(value).parts, "Guest paths must be absolute without shell metacharacters")
    require(args.repeats > 0 and args.timeout > 0, "Repeats and timeout must be positive")
    run_id = "vm-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = args.output or Path("results/vmware") / run_id
    output.mkdir(parents=True, exist_ok=False)
    connection = Connection(args.host, args.transport)
    remote_dir = args.remote_root.rstrip("/") + "/" + run_id
    remote_archive = "/tmp/arma4os-" + run_id + ".tar.gz"
    report = {"ok": False, "host": args.host, "transport": connection.transport, "remote_dir": remote_dir, "steps": []}
    print(f"VM: {args.host} via {connection.transport} | Guest directory: {remote_dir} | Local results: {output}", flush=True)
    def step(name, command, timeout=90):
        print(f"[{name}]", flush=True)
        code = execute(command, output, name, timeout)
        report["steps"].append({"name": name, "returncode": code})
        write_json(output / "vm-result.json", report)
        return code
    try:
        require(step("connect", connection.ssh("python3 --version")) == 0, "Cannot connect; check VM network and SSH key authentication")
        source = output / "source.tar.gz"
        meta = snapshot(Path.cwd(), source)
        write_json(output / "deployment-source.json", meta)
        require(step("upload", connection.copy(source, remote_archive, True)) == 0, "Source upload failed")
        bootstrap = "ARCHIVE=" + repr(remote_archive) + "\nDESTINATION=" + repr(remote_dir) + "\nDIGEST=" + repr(hashlib.sha256(source.read_bytes()).hexdigest()) + "\n" + BOOTSTRAP
        encoded = base64.b64encode(bootstrap.encode()).decode()
        require(step("deploy", connection.ssh(
            "printf %s " + encoded + " | base64 -d | python3")) == 0, "Source deployment failed")
        command = ["python3", "-m", "benchmarks.deployment.vm", "--guest-run", "--scheduler", args.scheduler,
                   "--repeats", str(args.repeats), "--seed", str(args.seed), "--timeout", str(args.timeout)]
        remote_command = "cd " + shlex.quote(remote_dir) + " && " + shlex.join(command)
        try:
            code = step("test", connection.ssh(remote_command), args.timeout + 180)
        except Exception as exc:
            code = -1
            report["test_transport_error"] = str(exc)
        # A failed test still has valuable build and cleanup evidence.
        require(step("download", connection.copy(output / "evidence.tar.gz", remote_dir + "/evidence.tar.gz", False)) == 0, "Evidence download failed; guest directory retained")
        require(step("download-hash", connection.copy(output / "evidence.sha256", remote_dir + "/evidence.sha256", False)) == 0, "Evidence hash download failed")
        require(hashlib.sha256((output / "evidence.tar.gz").read_bytes()).hexdigest() ==
                (output / "evidence.sha256").read_text().strip(), "Evidence archive hash mismatch")
        extract_files(output / "evidence.tar.gz", output / "guest")
        guest = json.loads((output / "guest/results/guest-result.json").read_text())
        report["guest"] = guest
        require(code == 0 and guest.get("ok") is True, "Guest test failed; see guest/results/make.stderr and verification.json")
        print((output / "guest/results/make.stdout").read_text(), end="", flush=True)
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc) or type(exc).__name__
        print(f"VM test failed: {report['error']}", file=sys.stderr)
    finally:
        write_json(output / "vm-result.json", report)
    print(f"VM test {'PASSED' if report['ok'] else 'FAILED'}: {output}", flush=True)
    return 0 if report["ok"] else 1

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="root@192.168.66.128")
    parser.add_argument("--transport", choices=["auto", "windows", "native"], default="auto")
    parser.add_argument("--remote-root", default="/root/arma4os-vm-tests")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scheduler", default="/root/sched-ext-dev/tools/sched_ext/build/bin/scx_simple")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=int, default=1200, help="Guest build + test deadline in seconds")
    parser.add_argument("--guest-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return guest_run(args) if args.guest_run else run_vm(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"VM test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
