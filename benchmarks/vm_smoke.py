"""Deploy the current source to an already running SSH guest and run test-real."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import uuid

from benchmarks.real_smoke import require, write_json

SOURCE_ROOTS = {"arma4os", "benchmarks", "crates", "configs", "schemas", "hooks", "tests", "scripts", "docs", ".github"}
SOURCE_FILES = {"Makefile", "Cargo.toml", "Cargo.lock", "pyproject.toml", "README.md", "AGENTS.md", "LICENSE", ".gitignore"}
SOURCE_SUFFIXES = {".py", ".rs", ".toml", ".lock", ".json", ".c", ".h", ".md", ".yaml", ".yml", ".sh"}
EXCLUDED_PARTS = {"target", "build", "results", "__pycache__", ".git", ".venv", "node_modules"}
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]


def source_files(root: Path) -> list[Path]:
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
    selected = []
    for raw in names.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts or EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if relative.as_posix() not in SOURCE_FILES and not (
                relative.parts[0] in SOURCE_ROOTS and relative.suffix in SOURCE_SUFFIXES):
            continue
        path = root / relative
        require(not any(p.is_symlink() for p in [path, *path.parents] if p != root.parent),
                f"Source symlink is not supported: {relative}")
        if path.is_file():
            selected.append(relative)
    require(Path("benchmarks/vm_smoke.py") in selected, "Remote harness missing from source snapshot")
    return sorted(set(selected))


def snapshot(root: Path, archive: Path) -> dict:
    # Hash and archive the same bytes even if an editor saves during packing.
    metadata = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                "working_tree": subprocess.check_output(["git", "status", "--short"], cwd=root, text=True),
                "files": {}}
    with tarfile.open(archive, "w:gz") as tar:
        for name in source_files(root):
            data = (root / name).read_bytes()
            metadata["files"][name.as_posix()] = hashlib.sha256(data).hexdigest()
            entry = tarfile.TarInfo(name.as_posix())
            entry.size = len(data)
            entry.mode = (root / name).stat().st_mode & 0o777
            tar.addfile(entry, io.BytesIO(data))
        data = (json.dumps(metadata, indent=2) + "\n").encode()
        entry = tarfile.TarInfo("deployment-source.json")
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))
    return metadata


def extract_files(archive: Path, destination: Path) -> None:
    """Only regular files and directories; reject links and path traversal."""
    with tarfile.open(archive) as tar:
        seen = set()
        for entry in tar.getmembers():
            path = PurePosixPath(entry.name)
            require(not path.is_absolute() and ".." not in path.parts and entry.name not in seen and
                    (entry.isfile() or entry.isdir()), f"Unsafe archive entry: {entry.name}")
            seen.add(entry.name)
        for entry in tar.getmembers():
            target = destination / entry.name
            require(not any(p.is_symlink() for p in [target, *target.parents]), "Archive destination contains a symlink")
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def powershell_command(program: str, args: list[str]) -> list[str]:
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); & " + program + " " + " ".join(map(quote, args)) + "; exit $LASTEXITCODE"
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode()]


class Connection:
    def __init__(self, host: str, transport: str):
        self.host = host
        self.transport = ("windows" if os.environ.get("WSL_DISTRO_NAME") and shutil.which("powershell.exe") else "native") if transport == "auto" else transport

    def command(self, program: str, args: list[str]) -> list[str]:
        return powershell_command(program, args) if self.transport == "windows" else [program, *args]

    def ssh(self, command: str) -> list[str]:
        return self.command("ssh", [*SSH_OPTIONS, self.host, command])

    def local_path(self, path: Path) -> str:
        if self.transport == "windows":
            return subprocess.check_output(["wslpath", "-w", str(path.resolve())], text=True).strip()
        return str(path.resolve())

    def copy(self, local: Path, remote: str, upload: bool) -> list[str]:
        pair = [self.local_path(local), self.host + ":" + remote]
        return self.command("scp", [*SSH_OPTIONS, *(pair if upload else pair[::-1])])


def execute(command: list[str], output: Path, name: str, timeout: float) -> int:
    with (output / (name + ".stdout")).open("wb") as stdout, (output / (name + ".stderr")).open("wb") as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout)
    for suffix, stream in [("stdout", sys.stdout), ("stderr", sys.stderr)]:
        print((output / (name + "." + suffix)).read_text(errors="replace"), end="", file=stream, flush=True)
    return result.returncode


# This short bootstrap is sent through SSH before the harness is installed.
BOOTSTRAP = r'''
import hashlib,json,pathlib,tarfile
archive=pathlib.Path(ARCHIVE)
if hashlib.sha256(archive.read_bytes()).hexdigest()!=DIGEST:raise RuntimeError("Source archive hash mismatch")
root=pathlib.Path(DESTINATION);root.mkdir(parents=True,exist_ok=False)
with tarfile.open(archive) as t:
 for e in t.getmembers():
  p=pathlib.PurePosixPath(e.name)
  if p.is_absolute() or ".." in p.parts or not e.isfile():raise RuntimeError("Unsafe source archive")
  dest=root/e.name;dest.parent.mkdir(parents=True,exist_ok=True)
  with t.extractfile(e) as src,dest.open("xb") as out:out.write(src.read())
meta=json.loads((root/"deployment-source.json").read_text())
for name,digest in meta["files"].items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise RuntimeError("Source file mismatch: "+name)
print("Verified source files:",len(meta["files"]))
'''


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
        command = ["python3", "-m", "benchmarks.vm_smoke", "--guest-run", "--scheduler", args.scheduler,
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
