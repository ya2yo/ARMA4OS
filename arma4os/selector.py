"""Goal-aware conservative expert routing with decayed votes and explicit switching cost."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .contracts import Decision, Objective, Observation, Profile, identifier
from .registry import Evidence, PolicyRegistry


@dataclass(frozen=True)
class SelectorConfig:
    max_age_ms: int = 5000
    plan_ttl_ms: int = 3000
    stable_windows: int = 3
    cooldown_s: float = 20.0
    minimum_residence_s: float = 15.0
    vote_decay: float = 0.7
    confidence_threshold: float = 0.8
    monitor_cost_s: float = 0.01
    failure_backoff_s: float = 60.0

    def __post_init__(self):
        if self.stable_windows < 1 or self.max_age_ms <= 0 or self.plan_ttl_ms <= 0:
            raise ValueError("invalid selector window settings")
        if not 0 < self.vote_decay <= 1 or not 0 < self.confidence_threshold <= 1:
            raise ValueError("invalid selector voting settings")
        from .contracts import finite
        for name in ["cooldown_s", "minimum_residence_s", "monitor_cost_s", "failure_backoff_s"]:
            finite(getattr(self, name), name, 0)


class Selector:
    def __init__(self, registry: PolicyRegistry, config: SelectorConfig | None = None):
        self.registry = registry
        self.config = config or SelectorConfig()
        self.votes: deque[str] = deque(maxlen=max(8, self.config.stable_windows))
        self.last_switch_ms: int | None = None
        self.last_observation_id: str | None = None
        self.observation_ids: dict[str, int] = {}
        self.context: tuple | None = None
        self.quarantine: dict[str, int] = {}

    def mark_switch(self, at_ms: int) -> None:
        self.last_switch_ms = at_ms
        self.votes.clear()

    def mark_failure(self, policy_id: str, at_ms: int) -> None:
        self.quarantine[policy_id] = at_ms + int(self.config.failure_backoff_s * 1000)
        self.votes.clear()

    @staticmethod
    def feasible(evidence: Evidence, objective: Objective) -> bool:
        for constraint in objective.constraints:
            m = evidence.metrics.get(constraint.metric)
            if m is None or m.unit != constraint.unit:
                return False
            worst = m.mean + m.uncertainty if constraint.operator == "<=" else m.mean - m.uncertainty
            if not constraint.satisfied(worst):
                return False
        return True

    def select(self, obs: Observation, objective: Objective, profile: Profile,
               current_policy: str, at_ms: int) -> Decision:
        cfg = self.config

        def decision(kind: str, reason: str, target=current_policy, confidence=0.0, evidence=(), gain=0.0):
            return Decision(identifier(), obs.observation_id, objective.objective_id, current_policy, target,
                            kind, reason, confidence, min(at_ms + cfg.plan_ttl_ms, objective.expires_at_ms),
                            tuple(evidence), gain)

        def hold(reason: str):
            self.votes.clear()
            return decision("hold", reason)

        context = (objective, current_policy, profile.label)
        if context != self.context:
            self.votes.clear()
            self.context = context
        if current_policy == "external":
            return hold("external scheduler conflict")
        if objective.expires_at_ms <= at_ms or objective.scope != obs.scope:
            return hold("objective expired or scope mismatch")
        if not 0 <= at_ms - obs.timestamp_ms <= cfg.max_age_ms or obs.window_ms <= 0:
            return hold("observation stale, future-dated or not yet a complete window")
        for key, unit in [(objective.metric, objective.unit)] + [(c.metric, c.unit) for c in objective.constraints]:
            metric = obs.metrics.get(key)
            if metric is None or metric.unit != unit or not metric.usable(at_ms, cfg.max_age_ms):
                return hold(f"required application KPI missing, stale or wrong unit: {key}")
        if profile.label != "cpu_contended" or profile.confidence < cfg.confidence_threshold:
            return hold("unknown, low-confidence or uncontended workload")
        rows = self.registry.matching(profile, at_ms)
        # Ambiguous calibration contexts must be narrowed offline, not cherry-picked online.
        ids = [e.policy_id for e in rows]
        if len(ids) != len(set(ids)):
            return hold("overlapping calibration records; refine feature contexts")
        rows = [e for e in rows if objective.metric in e.metrics and e.metrics[objective.metric].unit == objective.unit]
        current = next((e for e in rows if e.policy_id == current_policy), None)
        if current is None:
            return hold("no trustworthy current-policy calibration")
        candidates = [e for e in rows if self.feasible(e, objective)
                      and self.quarantine.get(e.policy_id, 0) <= at_ms]
        if not candidates:
            return hold("no candidate satisfies conservative objective constraints")
        minimize = objective.direction == "minimize"
        best = min(candidates, key=lambda e: e.metrics[objective.metric].mean + e.metrics[objective.metric].uncertainty) if minimize else max(
            candidates, key=lambda e: e.metrics[objective.metric].mean - e.metrics[objective.metric].uncertainty)
        if best.policy_id == current_policy:
            return hold("current policy is best feasible calibrated candidate")
        a, b = current.metrics[objective.metric], best.metrics[objective.metric]
        if minimize:
            base = a.mean - a.uncertainty
            gain = (base - b.mean - b.uncertainty) / base if base > 0 else 0
        else:
            base = a.mean + a.uncertainty
            gain = (b.mean - b.uncertainty - base) / base if base > 0 else 0
        horizon = objective.evaluation_window_s
        # Compare integrated normalized application utility over a fixed horizon.
        # Switching is conservatively charged as zero useful utility while it runs.
        # For latency this is a declared quality utility proxy, not saved wall-clock time.
        # Each calibrated cost bounds a COMPLETE transition involving that expert;
        # it already includes unload + load, so never add the same transition twice.
        cost = max(current.switch_cost_s, best.switch_cost_s) + cfg.monitor_cost_s
        if horizon < cfg.minimum_residence_s or gain < objective.minimum_gain or gain * horizon <= cost:
            return hold("conservative gain does not cover minimum improvement, residence or switching cost")
        if self.last_switch_ms is not None and at_ms - self.last_switch_ms < max(cfg.cooldown_s, cfg.minimum_residence_s) * 1000:
            return hold("cooldown / minimum residence active")
        self.observation_ids = {key: stamp for key, stamp in self.observation_ids.items()
                                if at_ms - stamp <= cfg.max_age_ms}
        if obs.observation_id in self.observation_ids:
            return decision("hold", "duplicate observation cannot vote")
        if len(self.observation_ids) >= 10000:
            return hold("observation replay cache full; wait for the next sampling window")
        self.last_observation_id = obs.observation_id
        self.observation_ids[obs.observation_id] = obs.timestamp_ms
        self.votes.append(best.policy_id)
        weights = [cfg.vote_decay ** age for age in range(len(self.votes))]
        vote = sum(w for w, p in zip(weights, reversed(self.votes)) if p == best.policy_id) / sum(weights)
        if len(self.votes) < cfg.stable_windows or any(p != best.policy_id for p in list(self.votes)[-cfg.stable_windows:]) or vote < cfg.confidence_threshold:
            return decision("hold", "collecting stable decayed candidate votes", confidence=vote)
        return decision("switch", "constraints satisfied; stable conservative gain exceeds switching cost",
                        best.policy_id, min(vote, profile.confidence), [current.evidence_id, best.evidence_id], gain)
