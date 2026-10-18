"""Post-switch health checks trigger recovery, without claiming causal attribution."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Objective, Observation


@dataclass
class Trial:
    previous_policy: str
    policy_id: str
    objective: Objective
    started_at_ms: int
    baseline: float


class Evaluator:
    def __init__(self, max_age_ms=5000):
        self.max_age_ms = max_age_ms
        self.trial: Trial | None = None
        self.guard: Trial | None = None

    def begin(self, observation: Observation, objective: Objective, previous: str, target: str, at_ms: int):
        self.trial = Trial(previous, target, objective, at_ms, observation.metrics[objective.metric].value)
        self.guard = None

    def evaluate(self, observation: Observation, status: dict, at_ms: int) -> dict:
        trial = self.trial or self.guard
        if trial is None:
            return {"status": "idle", "rollback": False}
        objective = trial.objective
        rollback, reason = False, "trial monitoring"
        if status["policy_id"] != trial.policy_id or not status["healthy"]:
            rollback, reason = True, "scheduler health or actual policy mismatch"
        elif at_ms >= objective.expires_at_ms or observation.scope != objective.scope:
            rollback, reason = True, "trial objective expired or scope mismatch"
        else:
            required = [(objective.metric, objective.unit)] + [(c.metric, c.unit) for c in objective.constraints]
            missing = any(k not in observation.metrics or observation.metrics[k].unit != u
                          or not observation.metrics[k].usable(at_ms, self.max_age_ms) for k, u in required)
            if missing:
                rollback, reason = True, "trial KPI missing, stale or wrong unit"
            elif any(not c.satisfied(observation.metrics[c.metric].value) for c in objective.constraints):
                rollback, reason = True, "application constraint violated during trial"
            else:
                value = observation.metrics[objective.metric].value
                worse = trial.baseline > 0 and (value > trial.baseline * (1 + objective.regression_fraction) if objective.direction == "minimize" else value < trial.baseline * (1 - objective.regression_fraction))
                if worse:
                    rollback, reason = True, "application KPI regression guard triggered"
        complete = self.trial is not None and at_ms - trial.started_at_ms >= objective.evaluation_window_s * 1000
        outcome = {"status": "rollback" if rollback else "completed" if complete else "monitoring",
                   "rollback": rollback, "reason": reason, "causal_improvement_claim": False,
                   "previous_policy": trial.previous_policy, "trial_policy": trial.policy_id}
        # Keep a failed trial until recovery is confirmed. A transport/recovery failure
        # must not erase the reason to retry on the next observation.
        if complete and not rollback:
            self.guard = self.trial
            self.trial = None
        return outcome
