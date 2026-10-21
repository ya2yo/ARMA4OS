"""Load benchmark JSON configuration; validation lives with the contracts."""
from __future__ import annotations

import json
from pathlib import Path
from benchmarks.orchestration.models import BenchmarkConfig, WorkloadConfig

def load_config(path: str | Path) -> BenchmarkConfig:
    """Load and parse benchmark configuration from a JSON file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid JSON configuration root: expected object, got {type(data)}")

    return BenchmarkConfig.from_dict(data)
