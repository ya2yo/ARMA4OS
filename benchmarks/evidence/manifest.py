"""Manifest generation capturing system environment, binary versions, and benchmark parameters."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any
import uuid

from benchmarks.orchestration.models import BenchmarkConfig


def get_git_commit() -> str | None:
    """Retrieve current git commit hash if in a git repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2.0,
        )
        return res.stdout.strip()
    except Exception:
        return None


def probe_binary_version(cmd: list[str]) -> str | None:
    """Attempt to probe version string from a binary command. Never reports stderr errors."""
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode != 0:
            return None
        out = (res.stdout or "").strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


def compute_file_sha256(path: str | Path | None) -> str | None:
    """Compute hex SHA-256 digest of a local file."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def get_environment_id() -> str:
    """Return the stable machine identity used by the ARMA4OS control plane."""
    identity = {
        "node": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
    }
    try:
        identity["cpuinfo"] = next(s for s in Path("/proc/cpuinfo").read_text().splitlines() if s.startswith("model name"))
    except (OSError, StopIteration):
        pass
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]


def generate_manifest(
    config: BenchmarkConfig,
    seed: int,
    repeats: int,
    output_dir: Path,
    executor_path: str | None = None,
    executor_config_path: str | None = None,
    executor_backend: str | None = None,
    trial_order: list[str] | None = None,
) -> dict[str, Any]:
    """Generate comprehensive manifest recording system state, binary hashes, and trial parameters."""
    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    env_id = get_environment_id()

    # The session verifies policy state before assembling evidence metadata.
    backend = executor_backend or ("simulated" if executor_path else "kernel-default")

    binary_versions: dict[str, Any] = {
        "python": sys.version,
        "python_executable": sys.executable,
    }

    binary_hashes: dict[str, str | None] = {
        "python": compute_file_sha256(sys.executable),
    }

    # Probe schbench if used in workloads
    has_schbench = any(w.parser == "schbench" or (w.command and "schbench" in w.command[0]) for w in config.workloads)
    if has_schbench:
        schbench_path = shutil.which("schbench")
        binary_versions["schbench_path"] = schbench_path
        if schbench_path:
            binary_versions["schbench_version"] = probe_binary_version([schbench_path, "-v"]) or "unknown"
            binary_hashes["schbench"] = compute_file_sha256(schbench_path)
        else:
            binary_versions["schbench_version"] = "not_found"

    # Probe executor if provided
    executor_config_content: dict[str, Any] | None = None
    executor_config_sha256: str | None = None
    if executor_path:
        binary_versions["executor_path"] = executor_path
        binary_versions["executor_version"] = probe_binary_version([executor_path, "--version"]) or "unknown"
        binary_hashes["executor"] = compute_file_sha256(executor_path)

        if executor_config_path:
            cfg_p = Path(executor_config_path)
            if cfg_p.is_file():
                raw_cfg = cfg_p.read_text(encoding="utf-8")
                executor_config_sha256 = hashlib.sha256(raw_cfg.encode("utf-8")).hexdigest()
                try:
                    executor_config_content = json.loads(raw_cfg)
                except Exception:
                    pass

    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None

    # Mark simulated scheduler matrix performance invalid for real policy comparisons
    policy_comparison_valid = backend == "real"

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "environment_id": env_id,
        "executor_backend": backend,
        "policy_comparison_valid": policy_comparison_valid,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "system": {
            "uname": platform.uname()._asdict(),
            "platform": platform.platform(),
            "python_version": sys.version,
            "python_executable": sys.executable,
            "cpu_count": os.cpu_count(),
            "affinity": affinity,
        },
        "git_commit": get_git_commit(),
        "binary_versions": binary_versions,
        "binary_hashes": binary_hashes,
        "parameters": {
            "seed": seed,
            "repeats": repeats,
            "policies": config.policies,
            "executor": executor_path,
            "executor_config": executor_config_path,
            "executor_config_sha256": executor_config_sha256,
        },
        "executor_config": executor_config_content,
        "trial_order": trial_order or [],
        "workload_configs": [
            {
                "name": w.name,
                "command": w.command,
                "parser": w.parser,
                "timeout_sec": w.timeout_sec,
                "warmup": w.warmup,
                "metadata": w.metadata,
            }
            for w in config.workloads
        ],
    }

    if backend == "simulated":
        manifest["simulation_note"] = (
            "Simulated scheduler matrix performance invalid for real policy comparisons "
            "(workload timings still real)"
        )

    return manifest
