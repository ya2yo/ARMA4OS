"""Prepare fresh out-of-tree Linux builds and retain their original outputs.

Configuration is deliberately outside the measured build interval. Child
processes inherit the caller's process group so a trial timeout also terminates
make's compiler descendants.
"""

from __future__ import annotations

from benchmarks.evidence.provenance import sha256_file, probe, source_metadata

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from typing import Any, BinaryIO





def prepare_build(
    source: Path, config: Path, output: Path, jobs: int, make: str = "make",
) -> dict[str, Any]:
    """Configure a new O= directory without modifying or cleaning the source.

    Existing output directories are refused, including empty ones. The retained
    input and effective config hashes distinguish the supplied configuration
    from changes introduced by olddefconfig.
    """
    source, config, output = Path(source).resolve(), Path(config).resolve(), Path(output).resolve()
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs <= 0:
        raise ValueError("jobs must be a positive integer")
    if not source.is_dir() or not (source / "Makefile").is_file():
        raise ValueError(f"Kernel source must contain a Makefile: {source}")
    if not config.is_file():
        raise ValueError(f"Kernel configuration is missing: {config}")
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("Build output must be outside the kernel source tree")
    for marker in (".config", "include/config/auto.conf", "include/generated/autoconf.h", "vmlinux"):
        if (source / marker).exists():
            raise ValueError(f"Refusing configured in-tree source ({marker}); use a clean source tree")
    if output.exists() or output.is_symlink():
        raise ValueError(f"Build output already exists: {output}")
    make_path = shutil.which(make)
    if make_path is None:
        raise ValueError(f"make executable not found: {make}")

    # Exclusive mkdir is the final collision check; failed preparation is kept.
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(config, output / "input.config")
    shutil.copyfile(config, output / ".config")
    command = [make_path, "-C", str(source), f"O={output}"]
    config_command = command + ["olddefconfig"]
    cc_command = shlex.split(os.environ.get("CC", "cc"))
    prepared: dict[str, Any] = {
        "schema_version": 1, "source": str(source), "output": str(output), "jobs": jobs,
        "command": command + [f"-j{jobs}"], "config_command": config_command,
        "artifact": str(output / "vmlinux"),
        "stdout_path": str(output / "build.stdout.log"),
        "stderr_path": str(output / "build.stderr.log"),
        "config_stdout_path": str(output / "config.stdout.log"),
        "config_stderr_path": str(output / "config.stderr.log"),
        "input_config": str(config), "input_config_sha256": sha256_file(output / "input.config"),
        "source_metadata": source_metadata(source),
        "make_version": probe([make_path, "--version"]),
        "compiler_version": probe(cc_command + ["--version"]) if cc_command else {"error": "empty CC"},
        "build_environment": {name: os.environ.get(name) for name in
                              ("CC", "CXX", "HOSTCC", "CROSS_COMPILE", "ARCH", "LLVM", "MAKEFLAGS",
                               "KCFLAGS", "KCPPFLAGS", "KBUILD_BUILD_TIMESTAMP", "SOURCE_DATE_EPOCH")},
        "preparation_start_unix": time.time(),
    }
    preparation_path = output / "preparation.json"
    preparation_path.write_text(json.dumps(prepared, indent=2) + "\n", encoding="utf-8")
    with (output / "config.stdout.log").open("wb") as stdout, (output / "config.stderr.log").open("wb") as stderr:
        result = subprocess.run(config_command, stdout=stdout, stderr=stderr, check=False)
    prepared["config_exit_code"] = result.returncode
    prepared["preparation_end_unix"] = time.time()
    if (output / ".config").is_file():
        prepared["effective_config_sha256"] = sha256_file(output / ".config")
    prepared["prepared"] = result.returncode == 0 and (output / ".config").is_file()
    preparation_path.write_text(json.dumps(prepared, indent=2) + "\n", encoding="utf-8")
    if not prepared["prepared"]:
        raise RuntimeError(f"Kernel olddefconfig failed (exit {result.returncode}); see {output}")
    if (output / "vmlinux").exists():
        raise RuntimeError("Configuration unexpectedly produced vmlinux; refusing an incremental build")
    return prepared


def start_build(prepared: dict[str, Any]) -> tuple[subprocess.Popen[bytes], BinaryIO, BinaryIO]:
    """Start the measured build, inheriting the caller's process group.

    Caller closes both returned handles after waiting. No policy or Agent is
    started here; the enclosing benchmark owns scheduler lifecycle and timeout.
    """
    output = Path(prepared["output"])
    if not prepared.get("prepared"):
        raise ValueError("Build was not successfully prepared")
    if (output / "vmlinux").exists():
        raise ValueError("vmlinux already exists; prepare a fresh output for every trial")
    if sha256_file(output / ".config") != prepared["effective_config_sha256"]:
        raise ValueError("Prepared kernel configuration changed")
    original_source = prepared["source_metadata"]
    current_source = source_metadata(Path(prepared["source"]))
    if current_source != original_source:
        raise ValueError("Kernel source provenance changed after preparation")
    stdout = Path(prepared["stdout_path"]).open("xb")
    try:
        stderr = Path(prepared["stderr_path"]).open("xb")
        try:
            prepared["_build_start_time_unix"] = time.time()
            prepared["_build_start_monotonic"] = time.monotonic()
            process = subprocess.Popen(prepared["command"], stdout=stdout, stderr=stderr)
        except BaseException:
            stderr.close()
            raise
    except BaseException:
        stdout.close()
        raise
    return process, stdout, stderr


def collect_build_result(
    prepared: dict[str, Any], start_time_unix: float, start_monotonic: float, exit_code: int,
) -> dict[str, Any]:
    """Collect a completed build, verifying its actual nonempty vmlinux artifact."""
    end_monotonic, end_time_unix = time.monotonic(), time.time()
    artifact = Path(prepared["artifact"])
    artifact_valid = artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_size > 0
    success = exit_code == 0 and artifact_valid
    result = {
        "workload": "kernel_build", "success": success,
        "start_time_unix": start_time_unix, "end_time_unix": end_time_unix,
        "elapsed_s": max(0.0, end_monotonic - start_monotonic), "exit_code": exit_code,
        "artifact": str(artifact), "artifact_valid": artifact_valid,
        "artifact_bytes": artifact.stat().st_size if artifact_valid else None,
        "stdout_path": prepared["stdout_path"], "stderr_path": prepared["stderr_path"],
        "command": prepared["command"],
        "error": None if success else ("nonzero_exit" if exit_code else "missing_build_artifact"),
    }
    (Path(prepared["output"]) / "build.result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def run_build(prepared: dict[str, Any]) -> dict[str, Any]:
    """Run only the compilation in the measured interval, retaining raw logs."""
    process, stdout, stderr = start_build(prepared)
    try:
        exit_code = process.wait()
    finally:
        stdout.close()
        stderr.close()
    return collect_build_result(
        prepared, prepared["_build_start_time_unix"], prepared["_build_start_monotonic"], exit_code,
    )
