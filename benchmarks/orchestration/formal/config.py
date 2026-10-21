"""Formal scenario suite runner: CLI configuration adapter for formal evaluations.

Provides the orchestration entry point for formal benchmarks (kernel_build, schbench,
and concurrent mixed evaluation), freezing configuration hashes, validating source metadata,
and composing WorkloadConfig instances for the primary benchmark engine.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from benchmarks.orchestration.models import BenchmarkConfig, WorkloadConfig, validate_timeout, validate_warmup
from benchmarks.evidence.provenance import sha256_file, source_metadata, probe

VALID_SCENARIOS = ("kernel_build", "schbench", "mixed")


def load_formal_config(path: str, scenarios: list[str], warmup: int) -> BenchmarkConfig:
    config_path = Path(path).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("formal config requires schema_version=1")
    if not scenarios:
        raise ValueError("scenarios list must not be empty")
    normalized = []
    for s in scenarios:
        if s not in VALID_SCENARIOS:
            raise ValueError(f"Unknown scenario {s!r}, must be one of: {list(VALID_SCENARIOS)}")
        if s in normalized:
            raise ValueError(f"Duplicate scenario: {s}")
        normalized.append(s)
    validate_warmup(warmup, "warmup")
    timeout = validate_timeout(raw.get("timeout_sec", 3600), "timeout_sec")
    spec: dict[str, Any] = {"schema_version": 1, "cache_policy": "fresh-build-output; OS page cache retained; no cache dropping"}

    def local(value: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("input path must be a nonempty string")
        candidate = Path(value).expanduser()
        return (config_path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    if set(normalized) & {"kernel_build", "mixed"}:
        kernel = raw["kernel"]
        if not isinstance(kernel, dict):
            raise ValueError("kernel must be an object")
        source, config = local(kernel["source"]), local(kernel["config"])
        jobs = kernel["jobs"]
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs <= 0:
            raise ValueError("kernel.jobs must be a positive integer")
        if not source.is_dir() or not (source / "Makefile").is_file() or not config.is_file():
            raise ValueError("kernel.source must contain Makefile and kernel.config must exist")
        make = kernel.get("make", "make")
        if not isinstance(make, str) or not make:
            raise ValueError("kernel.make must be an executable path or name")
        make_path = shutil.which(str(local(make))) if "/" in make else shutil.which(make)
        if not make_path:
            raise ValueError(f"make executable missing: {make}")
        spec["kernel"] = {"source": str(source), "config": str(config), "jobs": jobs,
                          "make": make_path, "make_sha256": sha256_file(Path(make_path)),
                          "input_config_sha256": sha256_file(config), "source_metadata": source_metadata(source)}
        spec["toolchain"] = {name: probe([name, "--version"]) for name in ("cc", "ld", "as")}
    if set(normalized) & {"schbench", "mixed"}:
        sch = raw["schbench"]
        if not isinstance(sch, dict):
            raise ValueError("schbench must be an object")
        binary = local(sch["binary"])
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"schbench binary is missing or not executable: {binary}")
        args = sch.get("args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise ValueError("schbench.args must be an argv string list")
        # Runtime belongs to the protocol. Reject short-option clusters as well.
        if any(a.startswith("--runtime") or (a.startswith("-") and not a.startswith("--") and "r" in a[1:])
               or a in ("--help", "--version", "-h", "-v") for a in args):
            raise ValueError("schbench.args must not override runtime or select help/version")
        runtime = sch.get("runtime_sec", 30)
        if isinstance(runtime, bool) or not isinstance(runtime, int) or runtime <= 0:
            raise ValueError("schbench.runtime_sec must be a positive integer (upstream -r uses seconds)")
        spec["schbench"] = {"binary": str(binary), "sha256": sha256_file(binary), "args": args, "runtime_sec": runtime}
    mixed = raw.get("mixed", {})
    if not isinstance(mixed, dict):
        raise ValueError("mixed must be an object")
    overlap = mixed.get("min_overlap_fraction", 0.9)
    if isinstance(overlap, bool) or not isinstance(overlap, (int, float)) or not math.isfinite(overlap) or not 0 < overlap <= 1:
        raise ValueError("mixed.min_overlap_fraction must be finite and in (0, 1]")
    spec["mixed"] = {"min_overlap_fraction": overlap}
    # Capture the build environment actually inherited, including affinity in the manifest.
    spec["build_environment"] = {k: os.environ.get(k) for k in (
        "PATH", "CC", "CXX", "HOSTCC", "CROSS_COMPILE", "ARCH", "LLVM", "MAKEFLAGS",
        "KCFLAGS", "KCPPFLAGS", "KBUILD_BUILD_TIMESTAMP", "SOURCE_DATE_EPOCH", "CCACHE_DISABLE")}
    workloads = [WorkloadConfig(
        name=scenario,
        command=[sys.executable, "-m", "benchmarks.orchestration.formal", "--scenario", scenario,
                 "--spec", json.dumps(spec, separators=(",", ":"))],
        parser="formal", timeout_sec=timeout, warmup=warmup,
        metadata={"scenario": scenario, "formal_spec": spec, "config_path": str(config_path),
                  "config_sha256": sha256_file(config_path), "config_input": raw},
    ) for scenario in normalized]
    return BenchmarkConfig(workloads=workloads, policies=raw.get("policies", ["default"]),
                           timeout_sec=timeout, warmup=warmup,
                           description="kernel_build, schbench, and concurrent mixed evaluation")
