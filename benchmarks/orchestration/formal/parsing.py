"""Reconstruct formal metrics from component logs and execution metadata."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from benchmarks.evidence.parsers.base import ParseError, ParserResult
from benchmarks.evidence.files import read_bounded_file
from benchmarks.evidence.parsers.schbench import SchbenchParser
from benchmarks.evidence.provenance import sha256_file


def parse_evidence(root: Path, metadata: dict[str, Any]) -> ParserResult:
    # Never trust wrapper stdout, metrics.jsonl, or cached percentiles.

    def load(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ParseError(f"Missing component evidence: {path.name}")
        data = json.loads(read_bounded_file(path))
        if not isinstance(data, dict):
            raise ParseError(f"Invalid component record: {path.name}")
        return data

    def require(path: Path) -> None:
        if not path.is_file():
            raise ParseError(f"Missing component evidence: {path.name}")

    scenario, spec = metadata["scenario"], metadata["formal_spec"]
    if load(root / "spec.json") != {"scenario": scenario, "spec": spec}:
        raise ParseError("Component specification differs from manifest")
    metrics: dict[str, Any] = {}
    executions = {}
    for name in (["kernel"] if scenario == "kernel_build" else ["schbench"] if scenario == "schbench" else ["kernel", "schbench"]):
        execution = load(root / f"{name}.execution.json")
        started = load(root / f"{name}.started.json")
        for key in ("start_time_unix", "start_monotonic", "command"):
            if execution[key] != started[key]:
                raise ParseError(f"Inconsistent component start metadata: {name}")
        for key in ("start_monotonic", "end_monotonic", "start_time_unix", "end_time_unix", "elapsed_s", "end_lower_bound_monotonic"):
            val = execution[key]
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ParseError(f"Invalid component timing: {name}.{key}")
        elapsed = execution["end_monotonic"] - execution["start_monotonic"]
        if elapsed <= 0 or abs(elapsed - execution["elapsed_s"]) > 1e-6 or execution["exit_code"] != 0:
            raise ParseError(f"Failed or inconsistent component execution: {name}")
        if not execution["start_monotonic"] <= execution["end_lower_bound_monotonic"] <= execution["end_monotonic"]:
            raise ParseError(f"Invalid component completion bounds: {name}")
        executions[name] = execution
        metrics[f"{name}_completion_uncertainty_s"] = execution["end_monotonic"] - execution["end_lower_bound_monotonic"]
        if name == "kernel":
            kernel = root / "kernel"
            for filename in ("build.stdout.log", "build.stderr.log", "config.stdout.log", "config.stderr.log", "input.config", ".config"):
                require(kernel / filename)
            preparation = load(kernel / "preparation.json")
            result = load(kernel / "build.result.json")
            artifact = kernel / "vmlinux"
            if not result["success"] or result["exit_code"] != 0 or not preparation["prepared"] or preparation["config_exit_code"] != 0:
                raise ParseError("Kernel compilation or preparation failed")
            if not artifact.is_file() or artifact.is_symlink() or artifact.stat().st_size <= 0:
                raise ParseError("Missing or invalid kernel build artifact")
            if preparation["input_config_sha256"] != spec["kernel"]["input_config_sha256"] or sha256_file(kernel / "input.config") != preparation["input_config_sha256"]:
                raise ParseError("Kernel input config hash mismatch")
            if sha256_file(kernel / ".config") != preparation["effective_config_sha256"] or preparation["source_metadata"] != spec["kernel"]["source_metadata"]:
                raise ParseError("Kernel effective configuration or source mismatch")
            if execution["command"] != preparation["command"] or result["command"] != execution["command"]:
                raise ParseError("Kernel command mismatch")
            metrics["compile_time_s"] = elapsed
        else:
            require(root / "schbench.stdout.log")
            require(root / "schbench.stderr.log")
            expected = [spec["schbench"]["binary"], *spec["schbench"]["args"], "-r", str(spec["schbench"]["runtime_sec"])]
            if execution["command"] != expected:
                raise ParseError("schbench command mismatch")
            if elapsed + 0.01 < spec["schbench"]["runtime_sec"]:
                raise ParseError("schbench exited before configured runtime")
            parsed = SchbenchParser().parse(read_bounded_file(root / "schbench.stdout.log"),
                                            read_bounded_file(root / "schbench.stderr.log"), elapsed, 0)
            metrics.update(parsed.metrics)
            metrics["schbench_time_s"] = elapsed
    if scenario == "mixed":
        a, b = executions["kernel"], executions["schbench"]
        overlap = max(0.0, min(a["end_monotonic"], b["end_monotonic"]) - max(a["start_monotonic"], b["start_monotonic"]))
        fraction = overlap / b["elapsed_s"]
        # A last-alive observation is a lower bound on completion. Use actual
        # observations rather than assuming a fixed polling/scheduling delay.
        conservative_overlap = max(0.0, min(a["end_lower_bound_monotonic"], b["end_lower_bound_monotonic"]) -
                                   max(a["start_monotonic"], b["start_monotonic"]))
        conservative = conservative_overlap / b["elapsed_s"]
        if conservative < spec["mixed"]["min_overlap_fraction"]:
            raise ParseError(f"Insufficient build/schbench overlap: conservative fraction={conservative:.4f}")
        metrics.update(overlap_s=overlap, overlap_fraction=fraction, overlap_fraction_conservative=conservative)
    metrics["metric_source"] = "component raw stdout/stderr and monotonic execution records"
    metrics["data_quality"] = "validated"
    return ParserResult(metrics=metrics)
