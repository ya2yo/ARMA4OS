"""Pure benchmark aggregation and independent-run summary statistics."""

from __future__ import annotations

import math
from typing import Any

# Two-tailed Student's t critical values for 95% confidence (alpha = 0.05)
# Degrees of freedom df = n - 1 (1 to 30)
STUDENT_T_95: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def calculate_sample_std(values: list[float]) -> float | None:
    """Compute sample standard deviation with Bessel's correction (n - 1).

    Returns None if n < 2.
    """
    n = len(values)
    if n < 2:
        return None
    mean_val = sum(values) / n
    variance = sum((x - mean_val) ** 2 for x in values) / (n - 1)
    return math.sqrt(max(variance, 0.0))


def calculate_ci_95(values: list[float]) -> list[float] | None:
    """Compute 95% confidence interval using Student's t distribution.

    Only appropriate and computed when n >= 2 and sample standard deviation > 0.
    Returns None otherwise, avoiding fabricated or meaningless intervals.
    """
    n = len(values)
    if n < 2:
        return None

    std = calculate_sample_std(values)
    if std is None or std <= 0:
        return None

    df = n - 1
    t_val = STUDENT_T_95.get(df, 1.960)  # Standard normal 1.960 for df > 30
    half_width = t_val * std / math.sqrt(n)
    mean_val = sum(values) / n

    lower = round(mean_val - half_width, 4)
    upper = round(mean_val + half_width, 4)
    return [lower, upper]


def compute_metric_summary(values: list[float], unit: str | None = None) -> dict[str, Any]:
    """Compute n, mean, std, min, max, and CI for a list of numerical metric values."""
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "ci_95": None,
            "unit": unit,
        }

    mean_val = sum(values) / n
    std_val = calculate_sample_std(values)
    ci = calculate_ci_95(values)

    return {
        "n": n,
        "mean": round(mean_val, 4),
        "std": round(std_val, 4) if std_val is not None else None,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "ci_95": ci,
        "unit": unit,
    }


def compute_group_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics for a collection of trial records belonging to one group."""
    if not records:
        return {}

    first = records[0]
    policy = first["policy"]
    workload = first["workload"]

    # Filter out warmup runs from formal statistical evaluations
    formal_records = [r for r in records if not r.get("is_warmup", False)]
    total = len(formal_records)

    success_records = [r for r in formal_records if r.get("status") == "success"]
    successful_count = len(success_records)
    failed_count = total - successful_count

    # Explicit breakdown of failure types
    timeouts = sum(1 for r in formal_records if r.get("status") == "timeout")
    nonzero_exits = sum(1 for r in formal_records if r.get("status") == "nonzero_exit")
    parse_errors = sum(1 for r in formal_records if r.get("status") == "parse_error")

    # Collect numerical metrics
    metric_values: dict[str, list[float]] = {}
    metric_units: dict[str, str | None] = {}

    for r in success_records:
        m = r.get("metrics", {})
        unit = m.get("latency_unit")
        for k, v in m.items():
            if k in ("latency_unit", "exit_code") or not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            metric_values.setdefault(k, []).append(float(v))
            if unit and k in ("p50", "p75", "p90", "p95", "p99", "p99_5", "p99_9", "min", "max"):
                metric_units[k] = unit
            elif k.endswith("_s") and k != "throughput_mb_s":
                metric_units[k] = "s"
            elif k == "throughput_mb_s":
                metric_units[k] = "MB/s"
            elif k == "rps":
                metric_units[k] = "requests/s"
            elif k.startswith("overlap_fraction"):
                metric_units[k] = "ratio"
            elif k == "requests":
                metric_units[k] = "requests"
            elif k == "fps":
                metric_units[k] = "fps"
            elif k == "speed":
                metric_units[k] = "ratio"
            elif k == "bitrate_kbps":
                metric_units[k] = "kbps"
            elif k == "encoded_frames":
                metric_units[k] = "frames"

    metrics_summary: dict[str, Any] = {}
    for metric_name, vals in metric_values.items():
        metrics_summary[metric_name] = compute_metric_summary(vals, metric_units.get(metric_name))

    return {
        "policy": policy,
        "workload": workload,
        "total_trials": total,
        "successful_trials": successful_count,
        "failed_trials": failed_count,
        "failures_by_reason": {
            "timeout": timeouts,
            "nonzero_exit": nonzero_exits,
            "parse_error": parse_errors,
        },
        "metrics": metrics_summary,
    }


def generate_summary(
    records: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate all trials into a structured summary with per-group statistics."""
    # Group by (policy, workload)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        key = (r["policy"], r["workload"])
        groups.setdefault(key, []).append(r)

    group_summaries: list[dict[str, Any]] = []
    for key in sorted(groups.keys()):
        group_summaries.append(compute_group_summary(groups[key]))

    return {
        "schema_version": 1,
        "run_id": manifest.get("run_id") if manifest else None,
        "baseline_policy": "default",
        "executor_backend": manifest.get("executor_backend", "unknown") if manifest else "unknown",
        "policy_comparison_valid": manifest.get("policy_comparison_valid", False) if manifest else False,
        "groups": group_summaries,
    }
