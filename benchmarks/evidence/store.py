"""Persist original execution records separately from derived metric caches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from benchmarks.orchestration.models import TrialRecord


def _record_dict(record: TrialRecord | dict[str, Any]) -> dict[str, Any]:
    return dict(record) if isinstance(record, dict) else record.to_dict()


def write_json(path: Path, value: Any) -> None:
    """Write one JSON artifact without changing its schema or directory layout."""
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def write_trial_record(run_dir: Path, record: TrialRecord | dict[str, Any]) -> None:
    """Save original execution metadata; parsed metrics remain a disposable cache."""
    data = _record_dict(record)
    data.pop("metrics", None)
    trial_dir = Path(run_dir) / "trials" / data["trial_id"]
    trial_dir.mkdir(parents=True, exist_ok=True)
    write_json(trial_dir / "record.json", data)


def append_trial_record(metrics_path: Path, record: TrialRecord | dict[str, Any]) -> None:
    """Append the complete trial result, including derived metrics, to its cache."""
    with Path(metrics_path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_record_dict(record), ensure_ascii=False) + "\n")
        stream.flush()


class RunStore:
    """Own the stable on-disk layout of a single benchmark run."""

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.manifest_path = self.run_dir / "manifest.json"
        self.metrics_path = self.run_dir / "metrics.jsonl"
        self.actions_path = self.run_dir / "actions.jsonl"
        self.summary_path = self.run_dir / "summary.json"

    def initialize(self) -> None:
        """Create a fresh run; refusing an existing directory protects evidence."""
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.metrics_path.write_text("", encoding="utf-8")
        self.actions_path.write_text("", encoding="utf-8")

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        write_json(self.manifest_path, manifest)

    def write_record(self, record: TrialRecord | dict[str, Any], append_metrics: bool = True) -> None:
        write_trial_record(self.run_dir, record)
        if append_metrics:
            append_trial_record(self.metrics_path, record)

    def write_summary(self, summary: dict[str, Any]) -> None:
        write_json(self.summary_path, summary)
