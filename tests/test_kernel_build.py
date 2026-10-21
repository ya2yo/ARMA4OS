"""Kernel-build task tests use a tiny Makefile, never a real kernel build."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from benchmarks.execution.workloads.kernel_build.build import prepare_build, run_build, start_build


@unittest.skipUnless(shutil.which("make"), "GNU make is needed for build fixtures")
class KernelBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="arma kernel build ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "linux source"
        self.source.mkdir()
        self.config = self.root / "fixed.config"
        self.config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
        self.output = self.root / "build output"
        self.write_makefile()

    def write_makefile(self, build: str = 'printf kernel > "$(O)/vmlinux"', configure: str = 'printf configured') -> None:
        (self.source / "Makefile").write_text(
            "VERSION = 6\nPATCHLEVEL = 14\nSUBLEVEL = 0\nEXTRAVERSION = -fixture\n"
            ".PHONY: all olddefconfig\n"
            f"all:\n\t@printf 'build stdout\\n'\n\t@printf 'build stderr\\n' >&2\n\t@{build}\n"
            f"olddefconfig:\n\t@{configure}\n", encoding="utf-8")

    def prepare(self) -> dict:
        return prepare_build(self.source, self.config, self.output, 2)

    def test_fresh_build_preserves_raw_outputs_and_metadata(self) -> None:
        prepared = self.prepare()
        self.assertFalse((self.source / ".config").exists())
        self.assertFalse((self.output / "vmlinux").exists())
        self.assertEqual((self.output / "input.config").read_bytes(), self.config.read_bytes())
        self.assertEqual(prepared["input_config_sha256"], prepared["effective_config_sha256"])
        self.assertEqual(prepared["source_metadata"]["version_fields"]["PATCHLEVEL"], "14")
        self.assertIsNone(prepared["source_metadata"]["dirty"])
        self.assertEqual(prepared["make_version"]["exit_code"], 0)
        self.assertIn("compiler_version", prepared)
        self.assertIn("configured", (self.output / "config.stdout.log").read_text())
        self.assertEqual(prepared["command"][-1], "-j2")
        self.assertEqual(prepared["command"][2], str(self.source))
        recorded = json.loads((self.output / "preparation.json").read_text())
        self.assertTrue(recorded["prepared"])
        result = run_build(prepared)
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_code"], 0)
        self.assertGreater(result["elapsed_s"], 0)
        self.assertGreaterEqual(result["start_time_unix"], prepared["preparation_end_unix"])
        self.assertIn("build stdout", Path(result["stdout_path"]).read_text())
        self.assertIn("build stderr", Path(result["stderr_path"]).read_text())
        self.assertEqual(json.loads((self.output / "build.result.json").read_text()), result)
        with self.assertRaises(ValueError):
            run_build(prepared)

    def test_nonzero_build_is_failure_even_with_an_artifact(self) -> None:
        self.write_makefile(build='printf kernel > "$(O)/vmlinux"; exit 7')
        result = run_build(self.prepare())
        self.assertFalse(result["success"])
        self.assertTrue(result["artifact_valid"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["error"], "nonzero_exit")

    def test_exit_zero_without_artifact_is_failure(self) -> None:
        self.write_makefile(build="true")
        result = run_build(self.prepare())
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "missing_build_artifact")

    def test_failed_config_keeps_preparation_and_logs(self) -> None:
        self.write_makefile(configure="printf configure-error >&2; exit 3")
        with self.assertRaises(RuntimeError):
            self.prepare()
        recorded = json.loads((self.output / "preparation.json").read_text())
        self.assertFalse(recorded["prepared"])
        self.assertNotEqual(recorded["config_exit_code"], 0)
        self.assertIn("configure-error", (self.output / "config.stderr.log").read_text())
        self.assertFalse((self.source / ".config").exists())

    def test_refuse_source_output_missing_config_and_reuse(self) -> None:
        for output in (self.source, self.source / "build", self.root):
            with self.subTest(output=output), self.assertRaises(ValueError):
                prepare_build(self.source, self.config, output, 2)
        with self.assertRaises(ValueError):
            prepare_build(self.source, self.root / "missing.config", self.output, 2)
        for jobs in (0, -1, True, 1.5):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                prepare_build(self.source, self.config, self.output, jobs)
        self.output.mkdir()
        with self.assertRaises(ValueError):
            self.prepare()

    def test_refuse_configured_source(self) -> None:
        (self.source / ".config").write_text("existing config")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual((self.source / ".config").read_text(), "existing config")
        self.assertFalse(self.output.exists())

    def test_modified_effective_config_refused(self) -> None:
        prepared = self.prepare()
        (self.output / ".config").write_text("CONFIG_CHANGED=y\n")
        with self.assertRaises(ValueError):
            run_build(prepared)

    def test_source_makefile_change_after_preparation_refused(self) -> None:
        prepared = self.prepare()
        self.write_makefile(build="true")
        with self.assertRaisesRegex(ValueError, "source provenance changed"):
            run_build(prepared)

    def test_child_inherits_trial_process_group(self) -> None:
        self.write_makefile(build='printf kernel > "$(O)/vmlinux"')
        prepared = self.prepare()
        process, stdout, stderr = start_build(prepared)
        try:
            self.assertEqual(os.getpgid(process.pid), os.getpgrp())
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            stdout.close()
            stderr.close()
            if process.poll() is None:
                process.kill()
                process.wait()

    @unittest.skipUnless(shutil.which("git"), "Git needed for provenance fixture")
    def test_git_dirty_source_is_recorded(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        prepared = self.prepare()
        self.assertTrue(prepared["source_metadata"]["dirty"])
        self.assertIn("Makefile", prepared["source_metadata"]["git_status"]["stdout"])


if __name__ == "__main__":
    unittest.main()
