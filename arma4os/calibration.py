"""Build a draft policy registry from independent benchmark runs.

Contexts and metric mappings are explicit inputs, frozen from exploratory runs.
Drafts require a human review of workload relevance, machine identity and noise.
Only real, verified matrices may produce non-synthetic evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .contracts import dumps, now_ms
from .registry import PolicyRegistry


def build_registry(run_dir: Path, context: dict, validity_hours: float = 24) -> dict:
    from benchmarks.reporting.reconstruction import reconstruct_summary

    if validity_hours <= 0 or validity_hours > 24 * 30:
        raise ValueError("validity_hours must be in (0, 720]")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    summary = reconstruct_summary(run_dir)
    environment = manifest.get("environment_id")
    if not environment or context["environment_id"] != environment:
        raise ValueError("context machine identity must match the recorded benchmark environment")
    if not context.get("features") or context.get("profile") != "cpu_contended":
        raise ValueError("explicit CPU contention feature ranges are required")
    backend = manifest.get("executor_backend", "unknown")
    if backend not in {"real", "simulated", "kernel-default"}:
        raise ValueError("manifest lacks verified executor backend")
    synthetic = backend == "simulated"
    # A default-only run may supply a baseline draft, but cannot invent expert scores.
    minimum = context.get("minimum_samples", 5)
    if type(minimum) is not int or minimum < 5:
        raise ValueError("at least five independent runs required for initial calibration")
    evidence = []
    digest = hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest()
    for group in summary["groups"]:
        if group["workload"] != context["workload"]:
            continue
        if group["failed_trials"] or group["successful_trials"] < minimum:
            raise ValueError("calibration refuses failed or insufficiently repeated groups")
        estimates = {}
        for source, target in context["metric_mapping"].items():
            metric = group["metrics"].get(source)
            if not metric or metric["n"] < minimum or metric["unit"] != target["unit"]:
                raise ValueError(f"missing or incompatible calibrated metric: {source}")
            interval = metric["ci_95"]
            if interval is None:
                raise ValueError("uncertainty interval unavailable; inspect measurement resolution and noise")
            uncertainty = max(metric["mean"] - interval[0], interval[1] - metric["mean"])
            estimates[target["name"]] = {"mean": metric["mean"], "uncertainty": uncertainty, "unit": metric["unit"]}
        cost = context["switch_cost_s"][group["policy"]]
        evidence.append({"evidence_id": f"{digest[:12]}:{group['policy']}:{group['workload']}",
                         "policy_id": group["policy"], "environment_id": environment, "profile": context["profile"],
                         "samples": group["successful_trials"], "source": str(run_dir.resolve()) + f"#manifest-sha256={digest}",
                         "valid_until_ms": now_ms() + int(validity_hours * 3600000), "features": context["features"],
                         "metrics": estimates, "switch_cost_s": cost, "synthetic": synthetic})
    if not evidence:
        raise ValueError("context workload not present in run")
    return {"schema_version": 1, "environment_id": environment, "minimum_samples": minimum,
            "synthetic": synthetic, "evidence": evidence}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validity-hours", type=float, default=24)
    args = parser.parse_args(argv)
    data = build_registry(args.run_dir, json.loads(args.context.read_text()), args.validity_hours)
    with args.output.open("x") as f:
        f.write(dumps(data) + "\n")
    try:
        PolicyRegistry.load(args.output)
    except BaseException:
        args.output.unlink()
        raise
    print(f"Draft registry: {args.output}; review contexts, units, noise and measured switch costs before deployment")


if __name__ == "__main__":
    main()
