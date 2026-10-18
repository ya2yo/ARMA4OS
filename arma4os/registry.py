"""Machine-specific calibration records, loaded separately from executable policy IDs."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from .contracts import finite


@dataclasses.dataclass(frozen=True)
class Estimate:
    mean: float
    uncertainty: float
    unit: str

    def __post_init__(self):
        finite(self.mean, "estimate.mean", 0)
        finite(self.uncertainty, "estimate.uncertainty", 0)
        if self.uncertainty > self.mean or not self.unit:
            raise ValueError("invalid estimate interval")


@dataclasses.dataclass(frozen=True)
class Evidence:
    evidence_id: str
    policy_id: str
    environment_id: str
    profile: str
    samples: int
    source: str
    valid_until_ms: int
    features: dict[str, tuple[float, float]]
    metrics: dict[str, Estimate]
    switch_cost_s: float = 0.0
    synthetic: bool = False

    def __post_init__(self):
        if not self.evidence_id or not self.source or type(self.samples) is not int or self.samples < 1:
            raise ValueError("evidence requires provenance and independent sample count")
        finite(self.switch_cost_s, "switch_cost_s", 0)
        if type(self.synthetic) is not bool:
            raise ValueError("evidence synthetic marker must be boolean")
        for low, high in self.features.values():
            if finite(low, "feature low") > finite(high, "feature high"):
                raise ValueError("inverted feature range")


class PolicyRegistry:
    def __init__(self, environment_id: str, evidence: list[Evidence], minimum_samples: int = 5,
                 synthetic: bool = False):
        self.environment_id = environment_id
        self.evidence = evidence
        self.minimum_samples = minimum_samples
        self.synthetic = synthetic or any(e.synthetic for e in evidence)
        if type(synthetic) is not bool or type(minimum_samples) is not int or minimum_samples < 2 or len({e.evidence_id for e in evidence}) != len(evidence):
            raise ValueError("invalid registry sample threshold or duplicate evidence IDs")

    @classmethod
    def load(cls, path: str | Path) -> PolicyRegistry:
        data = json.loads(Path(path).read_text())
        if data["schema_version"] != 1:
            raise ValueError("unsupported registry schema")
        entries = []
        for row in data["evidence"]:
            entries.append(Evidence(**{**row, "metrics": {k: Estimate(**v) for k, v in row["metrics"].items()}}))
        return cls(data["environment_id"], entries, data.get("minimum_samples", 5), data.get("synthetic", False))

    def matching(self, profile, at_ms: int) -> list[Evidence]:
        return [e for e in self.evidence if e.environment_id == self.environment_id
                and e.profile == profile.label and e.samples >= self.minimum_samples
                and e.valid_until_ms >= at_ms and e.features
                and all(k in profile.features and bounds[0] <= profile.features[k] <= bounds[1]
                        for k, bounds in e.features.items())]
