"""Shared experiment contracts; independent of execution and storage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any

def validate_timeout(val: Any, field_name: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{field_name} must be a number, got {type(val).__name__} ({val!r})")
    val_float = float(val)
    if math.isnan(val_float) or math.isinf(val_float) or val_float <= 0:
        raise ValueError(f"{field_name} must be finite and positive, got {val}")
    return val_float

def validate_warmup(val: Any, field_name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or isinstance(val, float):
        raise ValueError(f"{field_name} must be a non-negative integer, got {type(val).__name__} ({val!r})")
    if val < 0:
        raise ValueError(f"{field_name} must be non-negative, got {val}")
    return val

@dataclass
class WorkloadConfig:
    """Configuration for an individual benchmark workload."""

    name: str
    command: list[str]
    parser: str = "elapsed"
    timeout_sec: float = 60.0
    warmup: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError(f"Workload name must be a non-empty string, got {self.name!r}")
        if isinstance(self.command, str) or not isinstance(self.command, list) or len(self.command) == 0:
            raise ValueError(f"Workload '{self.name}' command must be a non-empty list of argv strings")
        for idx, arg in enumerate(self.command):
            if not isinstance(arg, str):
                raise ValueError(f"Workload '{self.name}' command argument at index {idx} must be a string, got {type(arg)}")
        if self.parser not in ("elapsed", "schbench", "formal", "ffmpeg"):
            raise ValueError(f"Workload '{self.name}' parser must be 'elapsed', 'schbench', 'formal', or 'ffmpeg', got {self.parser!r}")
        validate_timeout(self.timeout_sec, f"Workload '{self.name}' timeout_sec")
        validate_warmup(self.warmup, f"Workload '{self.name}' warmup")

@dataclass
class BenchmarkConfig:
    """Top-level configuration for benchmark runs."""

    workloads: list[WorkloadConfig]
    policies: list[str] = field(default_factory=lambda: ["default"])
    schema_version: int = 1
    description: str = ""
    timeout_sec: float = 60.0
    warmup: int = 0

    def validate(self, has_executor: bool = False) -> None:
        if self.schema_version != 1:
            raise ValueError(f"Unsupported schema_version: {self.schema_version}, expected 1")
        if isinstance(self.policies, str) or not isinstance(self.policies, list) or len(self.policies) == 0:
            raise ValueError("Policies list must be a non-empty list of policy names")
        for idx, pol in enumerate(self.policies):
            if not isinstance(pol, str) or not pol.strip():
                raise ValueError(f"Policy at index {idx} must be a non-empty string, got {pol!r}")
        if len(self.policies) != len(set(self.policies)):
            raise ValueError(f"Duplicate policies found in configuration: {self.policies}")

        validate_timeout(self.timeout_sec, "BenchmarkConfig timeout_sec")
        validate_warmup(self.warmup, "BenchmarkConfig warmup")

        # Policy baseline check
        if "default" not in self.policies:
            raise ValueError(
                "Refusing benchmark matrix without 'default' baseline policy. "
                f"Configured policies: {self.policies}"
            )

        # Default-only check when no executor is provided
        if not has_executor and set(self.policies) != {"default"}:
            raise ValueError(
                "Cannot execute non-default policies without an executor. "
                f"Configured policies: {self.policies}. Only ['default'] is allowed without --executor."
            )

        if not self.workloads:
            raise ValueError("At least one workload must be defined in the configuration")

        seen_names: set[str] = set()
        for w in self.workloads:
            w.validate()
            if w.name in seen_names:
                raise ValueError(f"Duplicate workload name found: '{w.name}'")
            seen_names.add(w.name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkConfig:
        if not isinstance(data, dict):
            raise ValueError(f"Configuration root must be a dict, got {type(data).__name__}")
        schema_version = data.get("schema_version", 1)
        description = data.get("description", "")

        policies_raw = data.get("policies", ["default"])
        if isinstance(policies_raw, str):
            raise ValueError(f"Config field 'policies' must be a list of strings, got str: {policies_raw!r}")
        if not isinstance(policies_raw, list):
            raise ValueError(f"Config field 'policies' must be a list of strings, got {type(policies_raw).__name__}")
        for p in policies_raw:
            if not isinstance(p, str) or not p.strip():
                raise ValueError(f"Each policy in 'policies' must be a non-empty string, got {p!r}")
        if len(policies_raw) != len(set(policies_raw)):
            raise ValueError(f"Duplicate policies in configuration: {policies_raw}")

        global_timeout = validate_timeout(data.get("timeout_sec", 60.0), "timeout_sec")
        global_warmup = validate_warmup(data.get("warmup", 0), "warmup")

        workloads_raw = data.get("workloads", [])
        if not isinstance(workloads_raw, list):
            raise ValueError("Config field 'workloads' must be a list")

        workloads: list[WorkloadConfig] = []
        for item in workloads_raw:
            if not isinstance(item, dict):
                raise ValueError(f"Each item in 'workloads' must be a dictionary, got {type(item)}")
            if "name" not in item:
                raise ValueError("Workload dictionary missing required 'name' field")
            w_name = item["name"]
            cmd_raw = item.get("command")
            if isinstance(cmd_raw, str):
                raise ValueError(f"Workload '{w_name}' command must be a list of argv strings, got string: {cmd_raw!r}")
            if not isinstance(cmd_raw, list) or len(cmd_raw) == 0:
                raise ValueError(f"Workload '{w_name}' command must be a non-empty list of argv strings")
            for idx, arg in enumerate(cmd_raw):
                if not isinstance(arg, str):
                    raise ValueError(f"Workload '{w_name}' command argument at index {idx} must be a string, got {type(arg)}")

            timeout_sec = validate_timeout(item.get("timeout_sec", global_timeout), f"Workload '{w_name}' timeout_sec")
            warmup = validate_warmup(item.get("warmup", global_warmup), f"Workload '{w_name}' warmup")
            workloads.append(
                WorkloadConfig(
                    name=w_name,
                    command=list(cmd_raw),
                    parser=item.get("parser", "elapsed"),
                    timeout_sec=timeout_sec,
                    warmup=warmup,
                    metadata=item.get("metadata", {}),
                )
            )

        return cls(
            workloads=workloads,
            policies=list(policies_raw),
            schema_version=schema_version,
            description=description,
            timeout_sec=global_timeout,
            warmup=global_warmup,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "description": self.description,
            "policies": self.policies,
            "timeout_sec": self.timeout_sec,
            "warmup": self.warmup,
            "workloads": [
                {
                    "name": w.name,
                    "command": w.command,
                    "parser": w.parser,
                    "timeout_sec": w.timeout_sec,
                    "warmup": w.warmup,
                    "metadata": w.metadata,
                }
                for w in self.workloads
            ],
        }

@dataclass
class TrialPlan:
    """Execution plan for an individual trial."""

    trial_id: str
    round_index: int
    repeat_index: int
    policy: str
    workload: WorkloadConfig
    is_warmup: bool = False

@dataclass
class TrialRecord:
    """Execution record and measurements for a single trial."""

    trial_id: str
    round_index: int
    repeat_index: int
    policy: str
    workload: str
    is_warmup: bool
    status: str  # "success" | "timeout" | "nonzero_exit" | "parse_error"
    exit_code: int | None
    start_time_unix: float
    end_time_unix: float
    duration_s: float
    stdout_path: str
    stderr_path: str
    metrics: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
