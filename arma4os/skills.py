"""Executable skills with explicit goals, preconditions, tool steps and recovery."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import identifier


@dataclass(frozen=True)
class OptimizationSkill:
    name: str
    version: str
    description: str
    required_tools: tuple[str, ...]
    success_criterion: str = "verified action or explained hold; application improvement requires controlled evaluation"

    def run(self, registry, objective, dry_run=False) -> dict:
        if self.name == "mixed_workload_tuning" and not objective.constraints:
            raise ValueError("mixed workload tuning requires explicit foreground/background constraints")
        if self.name == "latency_optimization" and objective.direction != "minimize":
            raise ValueError("latency skill requires a minimize objective with a latency unit")
        if self.name == "latency_optimization" and objective.unit not in {"ns", "us", "ms", "s"}:
            raise ValueError("latency objective must specify a time unit")
        missing = set(self.required_tools) - set(registry.tools)
        if missing:
            raise ValueError(f"missing skill capabilities: {sorted(missing)}")
        steps = []

        def call(name, params=None):
            response = registry.invoke({"schema_version": 1, "request_id": identifier(), "scope": "global",
                                        "timeout_ms": 10000, "tool": name, "params": params or {}})
            steps.append({"tool": name, "response": response})
            if not response["ok"]:
                raise RuntimeError(response["error"]["message"])
            return response["result"]

        call("system.capabilities")
        before = call("scheduler.status")
        if not before["healthy"]:
            return {"skill": self.name, "status": "blocked", "reason": "executor unhealthy", "steps": steps}
        call("workload.profile")
        plan = call("scheduler.plan")
        if plan["kind"] == "hold":
            return {"skill": self.name, "status": "held", "reason": plan["reason"], "steps": steps}
        result = call("scheduler.apply", {"decision_id": plan["decision_id"], "dry_run": dry_run})
        after = call("scheduler.status")
        if not result["verified"] or (not dry_run and after["policy_id"] != plan["policy_id"]):
            # Rust performs immediate load recovery; this handles subsequent verification failure.
            already_restored = after["healthy"] and after["policy_id"] == before["policy_id"]
            recovery = result.get("recovery") if already_restored else call("scheduler.rollback") if not dry_run and after["policy_id"] != "external" else None
            return {"skill": self.name, "status": "failed", "recovery": recovery, "steps": steps}
        return {"skill": self.name, "status": result["status"], "steps": steps,
                "next": "continue online trial monitoring through Agent.tick"}


SKILLS = {name: OptimizationSkill(name, "1.0", description,
          ("system.capabilities", "workload.profile", "scheduler.status", "scheduler.plan", "scheduler.apply", "scheduler.rollback"))
          for name, description in {
              "latency_optimization": "Apply calibrated latency-oriented policy under an explicit objective",
              "batch_optimization": "Optimize useful batch throughput or completion time",
              "mixed_workload_tuning": "Route with foreground SLO and background progress constraints",
          }.items()}
