"""Tests for prepare_kernel_bench script without invoking real SSH."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.prepare_kernel_bench import (
    build_guest_setup_script,
    build_remote_bash_command,
    generate_local_config,
    probe_guest_environment,
    main,
)


class PrepareKernelBenchTests(unittest.TestCase):
    def test_build_guest_setup_script_structure(self) -> None:
        script = build_guest_setup_script(
            dest_root="/srv/test_bench",
            kernel_version="v6.14",
            kernel_repo="https://example.com/linux.git",
            fallback_repo="https://kernel.org/linux.git",
            depth=1,
            install_deps=True,
            build_schbench=True,
            kconfig_type="tinyconfig",
        )
        self.assertIn("mkdir -p /srv/test_bench", script)
        self.assertIn("dnf install -y", script)
        self.assertIn("git clone --depth 1 --branch 'v6.14'", script)
        self.assertIn("Secondary clone failed, attempting upstream kernel.org", script)
        self.assertIn("/srv/test_bench/linux-6.14", script)
        self.assertIn("/srv/test_bench/kernel-tinyconfig.config", script)
        self.assertIn("schbench", script)
        self.assertIn("make -C '/srv/test_bench/linux-6.14' O=\"$TEMP_O\" tinyconfig", script)
        # Verify clean tree check exists
        self.assertIn("rm -f '/srv/test_bench/linux-6.14/.config'", script)
        self.assertIn("Existing directory /srv/test_bench/linux-6.14 is incomplete or corrupted", script)
        self.assertIn("rm -rf '/srv/test_bench/linux-6.14'", script)

    def test_build_guest_setup_script_kconfig_types(self) -> None:
        script_def = build_guest_setup_script(
            dest_root="/srv/bench",
            kernel_version="v6.14",
            kernel_repo="https://example.com/linux.git",
            fallback_repo="https://kernel.org/linux.git",
            depth=1,
            install_deps=False,
            build_schbench=False,
            kconfig_type="defconfig",
        )
        self.assertIn("make -C '/srv/bench/linux-6.14' O=\"$TEMP_O\" defconfig", script_def)

        script_curr = build_guest_setup_script(
            dest_root="/srv/bench",
            kernel_version="v6.14",
            kernel_repo="https://example.com/linux.git",
            fallback_repo="https://kernel.org/linux.git",
            depth=1,
            install_deps=False,
            build_schbench=False,
            kconfig_type="current",
        )
        self.assertIn("/boot/config-$(uname -r)", script_curr)

    def test_build_guest_setup_script_skip_flags(self) -> None:
        script = build_guest_setup_script(
            dest_root="/srv/bench",
            kernel_version="v6.12",
            kernel_repo="https://example.com/linux.git",
            fallback_repo="https://kernel.org/linux.git",
            depth=1,
            install_deps=False,
            build_schbench=False,
        )
        self.assertIn("Skipping dependency installation", script)
        self.assertNotIn("dnf install", script)
        self.assertIn("Skipping schbench build", script)
        self.assertNotIn("schbench.git", script)

    def test_generate_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "test_config.json"
            generate_local_config(
                output_path=out_path,
                dest_root="/srv/custom",
                kernel_version="v6.14",
                jobs=8,
                kconfig_type="tinyconfig",
            )
            self.assertTrue(out_path.is_file())
            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["kernel"]["source"], "/srv/custom/linux-6.14")
            self.assertEqual(data["kernel"]["config"], "/srv/custom/kernel-tinyconfig.config")
            self.assertEqual(data["kernel"]["jobs"], 8)
            self.assertEqual(data["schbench"]["binary"], "/usr/local/bin/schbench")

    @patch("scripts.prepare_kernel_bench.run_remote_ssh")
    def test_probe_guest_environment_parses_nproc(self, mock_ssh: MagicMock) -> None:
        mock_ssh.return_value = (0, "NPROC=16\n6.6.0-oe2403\nNAME=openEuler", "")
        conn = MagicMock()
        conn.host = "test_vm"
        res = probe_guest_environment(conn)
        self.assertEqual(res["jobs"], "16")

    @patch("scripts.prepare_kernel_bench.run_remote_ssh")
    def test_probe_guest_environment_raises_on_failure(self, mock_ssh: MagicMock) -> None:
        mock_ssh.return_value = (-1, "", "Connection refused")
        conn = MagicMock()
        conn.host = "unreachable_vm"
        with self.assertRaises(RuntimeError) as ctx:
            probe_guest_environment(conn)
        self.assertIn("Cannot connect to guest VM", str(ctx.exception))

    def test_build_remote_bash_command(self) -> None:
        raw_script = "echo '=== [3/5] (v6.14) ==='\nexit 0"
        cmd = build_remote_bash_command(raw_script)
        self.assertTrue(cmd.startswith("printf %s "))
        self.assertTrue(cmd.endswith(" | base64 -d | bash"))
        # Verify base64 decoding reproduces the exact script
        parts = cmd.split(" ")
        b64_payload = parts[2]
        import base64
        decoded = base64.b64decode(b64_payload).decode("utf-8")
        self.assertEqual(decoded, raw_script)

    @patch("scripts.prepare_kernel_bench.probe_guest_environment", return_value={"jobs": "4", "probe_output": ""})
    def test_main_cli_dry_run(self, mock_probe: MagicMock) -> None:
        ret = main(["--host", "mock_host", "--dry-run"])
        self.assertEqual(ret, 0)

    @patch("scripts.prepare_kernel_bench.run_remote_streaming", return_value=0)
    @patch("scripts.prepare_kernel_bench.probe_guest_environment", return_value={"jobs": "4", "probe_output": ""})
    def test_main_e2e_dry_run(self, mock_probe: MagicMock, mock_stream: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_out = Path(tmpdir) / "benchmarks.local.json"
            ret = main([
                "--host", "mock_host",
                "--transport", "native",
                "--output-config", str(cfg_out),
                "--jobs", "2",
            ])
            self.assertEqual(ret, 0)
            self.assertTrue(cfg_out.is_file())
            cfg = json.loads(cfg_out.read_text())
            self.assertEqual(cfg["kernel"]["jobs"], 2)


if __name__ == "__main__":
    unittest.main()
