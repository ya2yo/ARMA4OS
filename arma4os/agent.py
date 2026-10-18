"""Online loop shared by CLI, Skills and the tool adapter; no LLM dependency."""

from __future__ import annotations

import time

from .contracts import Objective, Observation, encode, identifier, now_ms
from .evaluator import Evaluator
from .profiler import RuleProfiler
from .selector import Selector


class Agent:
    def __init__(self, collector, selector: Selector, executor, store, objective: Objective):
        self.collector, self.selector, self.executor = collector, selector, executor
        self.store, self.objective = store, objective
        self.profiler = RuleProfiler(selector.config.max_age_ms)
        self.evaluator = Evaluator(selector.config.max_age_ms)
        self.plans = {}
        self.closed = False
        self.latest_observation = None
        self.last_sample_monotonic = 0.0
        if selector.registry.synthetic and executor.request("status")["backend"] != "simulated":
            raise ValueError("synthetic evidence cannot drive a real executor")

    def observe(self):
        # A profile followed immediately by plan must share a real sampling window.
        # Otherwise the profile consumes CPU counters and plan sees dt=0.
        stamp = time.monotonic()
        if self.latest_observation is None or stamp - self.last_sample_monotonic >= 0.5:
            self.latest_observation = self.collector.snapshot()
            self.last_sample_monotonic = stamp
        return self.latest_observation

    def plan(self, observation: Observation | None = None, at_ms: int | None = None):
        obs = observation or self.observe()
        stamp = now_ms() if at_ms is None else at_ms
        self.store.append("metrics", obs)
        status = self.executor.request("status")
        profile = self.profiler.profile(obs, stamp)
        decision = self.selector.select(obs, self.objective, profile, status["policy_id"], stamp)
        if not status["healthy"] and decision.kind == "switch":
            from dataclasses import replace
            decision = replace(decision, kind="hold", policy_id=status["policy_id"], reason="executor unhealthy")
        self.store.append("decisions", decision)
        # Only plans created by this process can be applied through the tool boundary.
        self.plans = {decision.decision_id: (decision, obs)}
        return decision, obs, status, profile

    def apply(self, decision_id: str, dry_run=False, timeout_ms=5000):
        decision, obs = self.plans[decision_id]
        if self.evaluator.trial is not None and not dry_run:
            raise ValueError("an active trial must finish or roll back before another switch")
        if decision.kind != "switch":
            raise ValueError("only switch decisions can be applied")
        if decision.expires_at_ms <= now_ms():
            raise ValueError("decision plan expired")
        if decision.objective_id != self.objective.objective_id or self.objective.expires_at_ms <= now_ms():
            raise ValueError("objective changed or expired")
        params = {**decision.params(), "dry_run": dry_run}
        if self.selector.registry.synthetic and self.executor.request("status")["backend"] != "simulated":
            raise ValueError("synthetic evidence cannot drive a real executor")
        result = self.executor.request("apply", params, timeout_ms=timeout_ms, request_id=decision.decision_id)
        self.store.append("actions", {**result, "request_id": decision.decision_id, "decision_id": decision.decision_id,
                                      "observation_id": obs.observation_id, "objective_id": self.objective.objective_id})
        if not dry_run:
            self.plans.pop(decision_id, None)
        if result["status"] == "applied" and result["verified"] and result["actual_policy"] == decision.policy_id:
            self.selector.mark_switch(result["finished_at_unix_ms"])
            self.evaluator.begin(obs, self.objective, decision.expected_policy, decision.policy_id,
                                 result["finished_at_unix_ms"])
        elif not dry_run and result["status"] == "failed":
            self.selector.mark_failure(decision.policy_id, result["finished_at_unix_ms"])
        return result

    def rollback(self, status=None, timeout_ms=5000):
        status = status or self.executor.request("status")
        guarded = self.evaluator.trial or self.evaluator.guard
        params = {"expected_policy": status["policy_id"], "expires_at_unix_ms": now_ms() + timeout_ms}
        if guarded:
            params["policy_id"] = guarded.previous_policy
        if not status["healthy"] and status["kernel_state"] == "disabled":
            params["policy_id"] = "default"
        request_id = identifier()
        result = self.executor.request("rollback", params, timeout_ms=timeout_ms, request_id=request_id)
        self.store.append("actions", {**result, "request_id": request_id, "objective_id": self.objective.objective_id})
        if result["verified"] and result["status"] in {"rolled_back", "applied", "held"}:
            guarded = self.evaluator.trial or self.evaluator.guard
            if guarded:
                self.selector.mark_failure(guarded.policy_id, result["finished_at_unix_ms"])
            self.evaluator.trial = None
            self.evaluator.guard = None
            self.selector.mark_switch(result["finished_at_unix_ms"])
        return result

    def tick(self, observation: Observation | None = None, act=True, at_ms=None):
        obs = observation or self.observe()
        stamp = now_ms() if at_ms is None else at_ms
        status = self.executor.request("status")
        feedback = self.evaluator.evaluate(obs, status, stamp)
        self.store.append("feedback", feedback)
        if feedback["rollback"]:
            self.store.append("metrics", obs)
            action = self.rollback(status) if act else None
            return {"feedback": feedback, "action": action, "status": self.executor.request("status")}
        decision, obs, status, profile = self.plan(obs, stamp)
        action = self.apply(decision.decision_id) if act and decision.kind == "switch" and self.evaluator.trial is None else None
        return {"observation_id": obs.observation_id, "profile": encode(profile), "decision": encode(decision),
                "action": action, "feedback": feedback, "status": self.executor.request("status")}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            status = self.executor.request("status")
            if status["policy_id"] not in {"default", "external"}:
                action = self.executor.request("apply", {"policy_id": "default", "expected_policy": status["policy_id"],
                                               "expires_at_unix_ms": now_ms() + 10000}, timeout_ms=10000)
                self.store.append("actions", action)
        finally:
            try:
                self.executor.close()
            finally:
                self.store.summarize()
