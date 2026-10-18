"""Trial matrix generation and randomized round scheduling."""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Iterator

from benchmarks.config import BenchmarkConfig, WorkloadConfig


@dataclass
class TrialPlan:
    """Execution plan for an individual trial."""

    trial_id: str
    round_index: int
    repeat_index: int
    policy: str
    workload: WorkloadConfig
    is_warmup: bool = False


def generate_trial_plan(
    config: BenchmarkConfig,
    repeats: int,
    seed: int,
) -> list[TrialPlan]:
    """Generate a serialized, randomized trial matrix according to policy x workload x repeats.

    Rounds are organized such that each round covers every (policy, workload) combination
    in a deterministic pseudo-random order seeded by (seed + round_index).
    Warmup trials (if configured) precede formal evaluation rounds and are executed
    per policy.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats <= 0:
        raise ValueError(f"repeats must be a positive integer, got {repeats}")

    plans: list[TrialPlan] = []
    trial_counter = 1

    # 1. Warmup trials (per policy if enabled)
    # Warmup runs under each configured policy to prime caches and schedulers
    for policy in config.policies:
        for workload in config.workloads:
            if workload.warmup > 0:
                for w_idx in range(workload.warmup):
                    plans.append(
                        TrialPlan(
                            trial_id=f"warmup_{trial_counter:04d}",
                            round_index=-1,
                            repeat_index=w_idx,
                            policy=policy,
                            workload=workload,
                            is_warmup=True,
                        )
                    )
                    trial_counter += 1

    # 2. Formal evaluation rounds
    # Track per (policy, workload) repeat counter
    repeat_counts: dict[tuple[str, str], int] = {}

    for round_idx in range(repeats):
        rng = random.Random(seed + round_idx)
        # Cartesian product of policies x workloads
        round_items: list[tuple[str, WorkloadConfig]] = [
            (policy, workload)
            for policy in config.policies
            for workload in config.workloads
        ]
        # Shuffle round items deterministically
        rng.shuffle(round_items)

        for policy, workload in round_items:
            key = (policy, workload.name)
            current_repeat = repeat_counts.get(key, 0)
            repeat_counts[key] = current_repeat + 1

            plans.append(
                TrialPlan(
                    trial_id=f"trial_{trial_counter:04d}",
                    round_index=round_idx,
                    repeat_index=current_repeat,
                    policy=policy,
                    workload=workload,
                    is_warmup=False,
                )
            )
            trial_counter += 1

    return plans
