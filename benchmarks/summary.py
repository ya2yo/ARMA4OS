"""Summary statistics and reconstruction from raw benchmark trial data."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from benchmarks.parsers import ParseError, get_parser

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
            elif k == "wall_time_s":
                metric_units[k] = "s"
            elif k == "throughput_mb_s":
                metric_units[k] = "MB/s"

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


def reconstruct_summary(run_dir: str | Path) -> dict[str, Any]:
    """Reconstruct summary.json by strictly reparsing raw trial outputs from RUN_DIR.

    Does NOT trust cached metrics in metrics.jsonl. Re-runs parsers over raw
    stdout/stderr logs with bounded memory, combines with recorded execution
    metadata (exit code, duration, original failure status), and updates summary.json.

    - Tampering cached metrics.jsonl retains correct raw-derived result.
    - Deleting or corrupting raw stdout yields explicit parse_error.
    - Original timeout / nonzero_exit statuses are preserved.
    """
    path = Path(run_dir)
    if not path.exists():
        raise FileNotFoundError(f"Run directory not found: {path}")

    # Load manifest to discover workload parser configs
    manifest: dict[str, Any] | None = None
    manifest_file = path / "manifest.json"
    if manifest_file.exists():
        with manifest_file.open("r", encoding="utf-8") as f:
            manifest = json.load(f)

    workload_parsers: dict[str, str] = {}
    if manifest and "workload_configs" in manifest:
        for w in manifest["workload_configs"]:
            workload_parsers[w["name"]] = w.get("parser", "elapsed")

    # Discover trials in deterministic order
    # Prioritize sequence recorded in metrics.jsonl or trial_order in manifest
    ordered_trial_ids: list[str] = list(manifest.get("trial_order", [])) if manifest else []
    planned_meta = {p["trial_id"]: p for p in manifest.get("trial_plan", [])} if manifest else {}
    cached_metrics_by_id: dict[str, dict[str, Any]] = {}

    metrics_file = path / "metrics.jsonl"
    if metrics_file.exists():
        with metrics_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        record_dict = json.loads(line)
                        t_id = record_dict.get("trial_id")
                        if t_id:
                            if t_id not in ordered_trial_ids:
                                ordered_trial_ids.append(t_id)
                            cached_metrics_by_id[t_id] = record_dict
                    except json.JSONDecodeError:
                        continue

    # Also discover any trial directories on disk not in metrics.jsonl
    trials_dir = path / "trials"
    if trials_dir.is_dir():
        for sub in sorted(trials_dir.iterdir()):
            if sub.is_dir() and sub.name not in ordered_trial_ids:
                ordered_trial_ids.append(sub.name)

    if not ordered_trial_ids and not metrics_file.exists():
        raise FileNotFoundError(f"No trial records found in run directory: {path}")

    from benchmarks.runner import MAX_OUTPUT_BYTES, read_bounded_file

    reconstructed_records: list[dict[str, Any]] = []

    for trial_id in ordered_trial_ids:
        trial_dir = path / "trials" / trial_id
        record_file = trial_dir / "record.json"
        # The cache can help discover legacy trial IDs but cannot supply trusted
        # execution metadata after raw record.json is deleted or damaged.
        cached_meta = planned_meta.get(trial_id, cached_metrics_by_id.get(trial_id, {}))

        raw_meta: dict[str, Any] = {}
        if record_file.exists():
            try:
                raw_meta = json.loads(record_file.read_text(encoding="utf-8"))
            except Exception:
                raw_meta = {}

        # Merge execution metadata, giving precedence to raw per-trial record.json
        round_index = raw_meta.get("round_index", cached_meta.get("round_index", 0))
        repeat_index = raw_meta.get("repeat_index", cached_meta.get("repeat_index", 0))
        policy = raw_meta.get("policy", cached_meta.get("policy", "default"))
        workload = raw_meta.get("workload", cached_meta.get("workload", "unknown"))
        is_warmup = raw_meta.get("is_warmup", cached_meta.get("is_warmup", False))
        original_status = raw_meta.get("status", cached_meta.get("status", "unknown"))
        exit_code = raw_meta.get("exit_code", cached_meta.get("exit_code"))
        start_time_unix = raw_meta.get("start_time_unix", cached_meta.get("start_time_unix", 0.0))
        end_time_unix = raw_meta.get("end_time_unix", cached_meta.get("end_time_unix", 0.0))
        duration_s = float(raw_meta.get("duration_s", cached_meta.get("duration_s", 0.0)))
        error_message = raw_meta.get("error_message", cached_meta.get("error_message"))

        stdout_rel = raw_meta.get("stdout_path", cached_meta.get("stdout_path", f"trials/{trial_id}/stdout.log"))
        stderr_rel = raw_meta.get("stderr_path", cached_meta.get("stderr_path", f"trials/{trial_id}/stderr.log"))
        stdout_file = path / stdout_rel if stdout_rel else trial_dir / "stdout.log"
        stderr_file = path / stderr_rel if stderr_rel else trial_dir / "stderr.log"

        final_status: str
        metrics: dict[str, Any] = {}

        # Preserve original timeout or non-zero exit failures
        if not raw_meta:
            final_status = "parse_error"
            error_message = "Raw execution metadata missing or invalid: record.json"
        elif original_status == "timeout":
            final_status = "timeout"
            metrics = {}
        elif exit_code is not None and exit_code != 0:
            final_status = "nonzero_exit"
            metrics = {}
        elif original_status == "nonzero_exit" and (exit_code is None or exit_code != 0):
            final_status = "nonzero_exit"
            metrics = {}
        else:
            # Reconstruct by parsing raw trial stdout/stderr output
            if not stdout_file.exists():
                final_status = "parse_error"
                metrics = {}
                error_message = f"Raw stdout file not found: {stdout_rel}"
            else:
                parser_name = workload_parsers.get(workload, "elapsed")
                parser = get_parser(parser_name)
                try:
                    stdout_text = read_bounded_file(stdout_file, max_bytes=MAX_OUTPUT_BYTES)
                    stderr_text = read_bounded_file(stderr_file, max_bytes=MAX_OUTPUT_BYTES) if stderr_file.exists() else ""
                    parsed_result = parser.parse(stdout_text, stderr_text, duration_s, exit_code or 0)
                    metrics = parsed_result.metrics
                    final_status = "success"
                    error_message = None
                except ParseError as pe:
                    final_status = "parse_error"
                    metrics = {}
                    error_message = str(pe)
                except Exception as exc:
                    final_status = "parse_error"
                    metrics = {}
                    error_message = f"Unexpected parser failure: {exc}"

        reconstructed_records.append({
            "trial_id": trial_id,
            "round_index": round_index,
            "repeat_index": repeat_index,
            "policy": policy,
            "workload": workload,
            "is_warmup": is_warmup,
            "status": final_status,
            "exit_code": exit_code,
            "start_time_unix": start_time_unix,
            "end_time_unix": end_time_unix,
            "duration_s": round(duration_s, 6),
            "stdout_path": str(stdout_file.relative_to(path)) if stdout_file.exists() else str(stdout_rel),
            "stderr_path": str(stderr_file.relative_to(path)) if stderr_file.exists() else str(stderr_rel),
            "metrics": metrics,
            "error_message": error_message,
        })

    summary = generate_summary(reconstructed_records, manifest)

    # Write reconstructed summary to RUN_DIR/summary.json
    summary_file = path / "summary.json"
    with summary_file.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return summary


def format_summary_table(summary: dict[str, Any]) -> str:
    """Format summary data into a human-readable text table."""
    lines: list[str] = []
    lines.append("=" * 80)
    if summary.get("executor_backend") == "simulated":
        lines.append("SIMULATED scheduling: timing differences are NOT real scheduler evidence.")
    lines.append(f"{'ARMA4OS Benchmark Run Summary':^80}")
    lines.append("=" * 80)

    groups = summary.get("groups", [])
    if not groups:
        lines.append("No evaluation groups recorded.")
        return "\n".join(lines)

    for g in groups:
        policy = g["policy"]
        workload = g["workload"]
        total = g["total_trials"]
        failed = g["failed_trials"]
        fails = g["failures_by_reason"]
        fail_str = f"{failed} (timeout={fails['timeout']}, nonzero={fails['nonzero_exit']}, parse_error={fails['parse_error']})"

        lines.append(f"\nPolicy: {policy} | Workload: {workload}")
        lines.append(f"Trials: total={total}, successful={g['successful_trials']}, failed={fail_str}")
        lines.append("-" * 80)
        lines.append(f"{'Metric':<20} {'N':<5} {'Mean':<12} {'Std':<12} {'Min':<10} {'Max':<10} {'95% CI':<15}")
        lines.append("-" * 80)

        metrics = g.get("metrics", {})
        if not metrics:
            lines.append("  (No metrics available)")
            continue

        for m_name, m_data in sorted(metrics.items()):
            n = m_data["n"]
            mean = str(m_data["mean"]) if m_data["mean"] is not None else "-"
            std = str(m_data["std"]) if m_data["std"] is not None else "-"
            min_val = str(m_data["min"]) if m_data["min"] is not None else "-"
            max_val = str(m_data["max"]) if m_data["max"] is not None else "-"
            ci = f"[{m_data['ci_95'][0]}, {m_data['ci_95'][1]}]" if m_data["ci_95"] else "-"
            unit = f" ({m_data['unit']})" if m_data.get("unit") else ""
            lines.append(f"{m_name + unit:<20} {n:<5} {mean:<12} {std:<12} {min_val:<10} {max_val:<10} {ci:<15}")

    lines.append("\n" + "=" * 80)
    return "\n".join(lines)
