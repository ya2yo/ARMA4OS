"""Human-readable benchmark summary rendering."""

from typing import Any


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
