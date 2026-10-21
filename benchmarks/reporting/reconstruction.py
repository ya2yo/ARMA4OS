"""Rebuild summaries from retained raw metadata and workload outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.evidence.files import MAX_OUTPUT_BYTES, read_bounded_file
from benchmarks.evidence.parsers.base import ParseError
from benchmarks.evidence.parsers.dispatch import parse_trial_evidence
from benchmarks.evidence.store import write_json
from benchmarks.reporting.statistics import generate_summary


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
    workload_metadata: dict[str, dict[str, Any]] = {}
    if manifest and "workload_configs" in manifest:
        for w in manifest["workload_configs"]:
            workload_parsers[w["name"]] = w.get("parser", "elapsed")
            workload_metadata[w["name"]] = w.get("metadata", {})

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
        elif original_status == "nonzero_exit":
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
                try:
                    stdout_text = read_bounded_file(stdout_file, max_bytes=MAX_OUTPUT_BYTES)
                    stderr_text = read_bounded_file(stderr_file, max_bytes=MAX_OUTPUT_BYTES) if stderr_file.exists() else ""
                    parsed_result = parse_trial_evidence(
                        parser_name, trial_dir, stdout_text, stderr_text, duration_s,
                        exit_code or 0, workload_metadata.get(workload, {}),
                    )
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
    write_json(path / "summary.json", summary)

    return summary
