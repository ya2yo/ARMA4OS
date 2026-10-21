"""Portable remote harness tests. SSH and BPF are never invoked."""

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from benchmarks.deployment import snapshot as vm_snapshot
from benchmarks.deployment import transport
from benchmarks.deployment import vm as vm_smoke


class VmSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_snapshot_includes_uncommitted_source_excludes_results_and_secrets(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for name in ["benchmarks/deployment/vm.py", "arma4os/new.py", "Makefile",
                     "results/secret.json", ".env", "configs/key.pem", "benchmarks/__pycache__/x.py"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original")
        original_check_output = subprocess.check_output
        with patch("benchmarks.deployment.snapshot.subprocess.check_output") as command:
            # Empty fixture repo has no HEAD; source discovery still uses actual Git.
            def output(argv, **kwargs):
                return "fixture-commit\n" if argv[1:3] == ["rev-parse", "HEAD"] else original_check_output(argv, **kwargs)
            command.side_effect = output
            archive = self.root / "source.tar.gz"
            meta = vm_snapshot.snapshot(self.root, archive)
        self.assertEqual(set(meta["files"]), {"Makefile", "arma4os/new.py", "benchmarks/deployment/vm.py"})
        (self.root / "arma4os/new.py").write_text("changed later")
        with tarfile.open(archive) as tar:
            data = tar.extractfile("arma4os/new.py").read()
        self.assertEqual(data, b"original")
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta["files"]["arma4os/new.py"])

    def test_archive_rejects_traversal_and_links(self):
        for name, kind in [("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)]:
            archive = self.root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = "/etc/passwd"
                tar.addfile(entry)
            with self.assertRaisesRegex(RuntimeError, "Unsafe archive"):
                vm_snapshot.extract_files(archive, self.root / "extracted")
        self.assertFalse((self.root / "escape").exists())

    def test_windows_quoting_keeps_arguments_literal(self):
        value = "a'; $(unexpected) `command`"
        argv = transport.powershell_command("ssh", [value])
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertTrue(script.startswith("$ErrorActionPreference='Stop';"))
        self.assertIn("'a''; $(unexpected) `command`'", script)
        self.assertEqual(argv[:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])

    def test_failed_guest_downloads_evidence_and_returns_failure(self):
        args = argparse.Namespace(host="root@192.168.66.128", transport="native",
            remote_root="/root/tests", scheduler="/usr/bin/scx_simple", repeats=1,
            seed=42, timeout=120, output=self.root / "run")
        steps = []
        def execute(command, output, name, timeout):
            steps.append(name)
            if name == "test":
                self.assertIn("python3 -m benchmarks.deployment.vm --guest-run", command[-1])
            if name == "download":
                with tarfile.open(output / "evidence.tar.gz", "w:gz") as tar:
                    data = json.dumps({"ok": False, "returncode": 2}).encode()
                    entry = tarfile.TarInfo("results/guest-result.json")
                    entry.size = len(data)
                    tar.addfile(entry, io.BytesIO(data))
            if name == "download-hash":
                (output / "evidence.sha256").write_text(hashlib.sha256((output / "evidence.tar.gz").read_bytes()).hexdigest())
            return 2 if name == "test" else 0
        def snapshot(root, archive):
            archive.write_bytes(b"source")
            return {"files": {}}
        with patch.object(vm_smoke, "execute", side_effect=execute), \
                patch.object(vm_smoke, "snapshot", side_effect=snapshot), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vm_smoke.run_vm(args), 1)
            with self.assertRaises(FileExistsError):
                vm_smoke.run_vm(args)
        self.assertEqual(steps, ["connect", "upload", "deploy", "test", "download", "download-hash"])
        report = json.loads((args.output / "vm-result.json").read_text())
        self.assertFalse(report["ok"])
        self.assertFalse(report["guest"]["ok"])
        self.assertTrue((args.output / "guest/results/guest-result.json").is_file())

    def test_host_and_guest_paths_reject_shell_arguments(self):
        args = argparse.Namespace(host="-oProxyCommand=bad", remote_root="/root/tests", scheduler="/usr/bin/scx_simple")
        with self.assertRaisesRegex(RuntimeError, "Invalid SSH host"):
            vm_smoke.run_vm(args)
        args.host = "root@host"
        args.remote_root = "/root/../etc"
        with self.assertRaisesRegex(RuntimeError, "Guest paths"):
            vm_smoke.run_vm(args)

    def test_guest_timeout_requests_group_cleanup_and_packages_failure(self):
        args = argparse.Namespace(scheduler="/usr/bin/scx_simple", repeats=1, seed=42, timeout=1)
        process = MagicMock(pid=54321)
        process.wait.side_effect = [subprocess.TimeoutExpired("make", 1), 0]
        process.poll.return_value = None
        (self.root / "deployment-source.json").write_text("{}")
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(vm_smoke.subprocess, "Popen", return_value=process), \
                    patch.object(vm_smoke.os, "killpg") as kill, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(vm_smoke.guest_run(args), 1)
            kill.assert_called_once_with(54321, signal.SIGTERM)
            self.assertEqual(process.wait.call_count, 2)
            report = json.loads((self.root / "results/guest-result.json").read_text())
            self.assertFalse(report["ok"])
            self.assertIn("timed out", report["error"])
            with tarfile.open(self.root / "evidence.tar.gz") as archive:
                self.assertIn("results/guest-result.json", archive.getnames())
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
