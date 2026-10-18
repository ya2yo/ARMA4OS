"""Versioned contracts shared by collectors, planners, tools and audit artifacts."""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import time
import uuid
from typing import Any

SCHEMA_VERSION = 1


def identifier() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def finite(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"invalid {name}")
    return value


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def dumps(value: Any) -> str:
    return json.dumps(encode(value), ensure_ascii=False, allow_nan=False)


@dataclasses.dataclass(frozen=True)
class Metric:
    value: float | None
    unit: str
    source: str
    timestamp_ms: int
    quality: str = "valid"

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit or not isinstance(self.source, str) or not self.source or self.quality not in {"valid", "missing", "invalid"}:
            raise ValueError("invalid metric metadata")
        if type(self.timestamp_ms) is not int or self.timestamp_ms < 0:
            raise ValueError("metric timestamp_ms must be a nonnegative integer")
        if self.value is not None:
            finite(self.value, "metric value")
        if self.quality == "valid" and self.value is None:
            raise ValueError("valid metric needs a value")

    def usable(self, at_ms: int, max_age_ms: int) -> bool:
        return (self.quality == "valid" and self.value is not None
                and 0 <= at_ms - self.timestamp_ms <= max_age_ms)


@dataclasses.dataclass(frozen=True)
class Observation:
    observation_id: str
    timestamp_ms: int
    window_ms: int
    scope: str
    metrics: dict[str, Metric]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != 1 or not isinstance(self.observation_id, str) or not self.observation_id:
            raise ValueError("invalid observation identity or schema")
        if type(self.timestamp_ms) is not int or self.timestamp_ms < 0 or type(self.window_ms) is not int or self.window_ms < 0:
            raise ValueError("observation timestamp and window must be nonnegative integers")
        if not isinstance(self.scope, str) or not self.scope or not isinstance(self.metrics, dict):
            raise ValueError("invalid observation scope or metrics")

    @classmethod
    def from_dict(cls, data: dict) -> Observation:
        if data.get("schema_version", 1) != 1:
            raise ValueError("unsupported observation schema")
        return cls(**{**data, "metrics": {k: Metric(**v) for k, v in data["metrics"].items()}})


@dataclasses.dataclass(frozen=True)
class Constraint:
    metric: str
    unit: str
    operator: str
    bound: float

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not self.metric or not isinstance(self.unit, str) or not self.unit:
            raise ValueError("constraint metric and unit required")
        if self.operator not in {"<=", ">="}:
            raise ValueError("constraint operator must be <= or >=")
        finite(self.bound, "constraint bound", 0)

    def satisfied(self, value: float) -> bool:
        return value <= self.bound if self.operator == "<=" else value >= self.bound


@dataclasses.dataclass(frozen=True)
class Objective:
    objective_id: str
    scope: str
    metric: str
    unit: str
    direction: str
    expires_at_ms: int
    constraints: tuple[Constraint, ...] = ()
    evaluation_window_s: float = 30.0
    minimum_gain: float = 0.05
    regression_fraction: float = 0.15

    def __post_init__(self) -> None:
        if type(self.expires_at_ms) is not int or self.expires_at_ms < 0:
            raise ValueError("objective expires_at_ms must be a nonnegative integer")
        if self.direction not in {"minimize", "maximize"}:
            raise ValueError("objective direction must be minimize or maximize")
        if not self.scope or not self.metric or not self.unit:
            raise ValueError("objective scope, metric and unit required")
        finite(self.evaluation_window_s, "evaluation_window_s", 0.001)
        finite(self.minimum_gain, "minimum_gain", 0)
        finite(self.regression_fraction, "regression_fraction", 0)
        if self.minimum_gain >= 1 or self.regression_fraction >= 1:
            raise ValueError("gain and regression fractions must be below one")

    @classmethod
    def from_dict(cls, data: dict) -> Objective:
        return cls(**{**data, "constraints": tuple(Constraint(**c) for c in data.get("constraints", []))})


@dataclasses.dataclass(frozen=True)
class Profile:
    label: str
    confidence: float
    features: dict[str, float]
    reason: str


@dataclasses.dataclass(frozen=True)
class Decision:
    decision_id: str
    observation_id: str
    objective_id: str
    expected_policy: str
    policy_id: str
    kind: str
    reason: str
    confidence: float
    expires_at_ms: int
    evidence: tuple[str, ...] = ()
    predicted_gain: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def params(self) -> dict:
        return {"policy_id": self.policy_id, "expected_policy": self.expected_policy,
                "expires_at_unix_ms": self.expires_at_ms, "dry_run": False}


@dataclasses.dataclass(frozen=True)
class ActionResult:
    planned_policy: str
    previous_policy: str
    actual_policy: str
    status: str
    verified: bool
    started_at_unix_ms: int
    finished_at_unix_ms: int
    managed_pid: int | None = None
    error: str | None = None
    recovery: Any = None
    backend: str = "simulated"
