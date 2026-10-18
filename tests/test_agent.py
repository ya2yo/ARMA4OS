from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from arma4os.agent import Agent
from arma4os.audit import RunStore
from arma4os.cli import demo_observation, demo_registry
from arma4os.collector import ProcCollector
from arma4os.contracts import Constraint, Metric, Objective, dumps, identifier, now_ms
from arma4os.evaluator import Evaluator
from arma4os.executor import ExecutorError, SimulatedExecutor
from arma4os.hooks import CpuWeightHook, HookRegistry, SimulatedCounterHook
from arma4os.profiler import RuleProfiler
from arma4os.registry import PolicyRegistry
from arma4os.selector import Selector, SelectorConfig
from arma4os.skills import SKILLS
from arma4os.tools import build_tools


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.stamp = now_ms()
        self.registry = demo_registry()
        self.selector = Selector(self.registry)
        self.objective = Objective("test", "global", "app.throughput", "ops/s", "maximize", self.stamp + 60000,
                                   (Constraint("app.p99", "ms", "<=", 10),))

    def choose(self, obs=None, objective=None, current="default", stamp=None):
        obs = obs or demo_observation()
        stamp = stamp if stamp is not None else now_ms()
        return self.selector.select(obs, objective or self.objective, RuleProfiler().profile(obs, stamp), current, stamp)

    def stabilize(self, objective=None):
        return [self.choose(objective=objective) for _ in range(3)][-1]

    def test_same_load_different_goal_changes_expert(self):
        bounded = self.stabilize()
        self.assertEqual((bounded.kind, bounded.policy_id), ("switch", "scx_bpfland"))
        relaxed = dataclasses.replace(self.objective, objective_id="relaxed", constraints=())
        self.assertEqual(self.stabilize(relaxed).policy_id, "scx_rusty")

    def test_latency_goal_uses_conservative_minimum(self):
        goal = dataclasses.replace(self.objective, metric="app.p99", unit="ms", direction="minimize", constraints=())
        self.assertEqual(self.stabilize(goal).policy_id, "scx_bpfland")

    def test_short_phase_and_high_cost_hold(self):
        goal = dataclasses.replace(self.objective, evaluation_window_s=1)
        self.assertEqual(self.stabilize(goal).kind, "hold")
        self.registry.evidence = [dataclasses.replace(e, switch_cost_s=50) for e in self.registry.evidence]
        self.assertEqual(self.stabilize().kind, "hold")

    def test_stale_kpi_wrong_unit_and_unknown_load_hold(self):
        for change in [
            {"app.p99": Metric(20, "ms", "test", self.stamp - 10000)},
            {"app.p99": Metric(20, "us", "test", self.stamp)},
            {"cpu.pressure": Metric(None, "fraction", "test", self.stamp, "missing")},
        ]:
            obs = demo_observation()
            obs.metrics.update(change)
            self.assertEqual(self.choose(obs).kind, "hold")

    def test_empty_evidence_and_machine_mismatch_hold(self):
        self.selector = Selector(PolicyRegistry("empty", []))
        self.assertEqual(self.stabilize().kind, "hold")
        self.registry.environment_id = "different-machine"
        self.selector = Selector(self.registry)
        self.assertEqual(self.stabilize().kind, "hold")

    def test_insufficient_samples_and_overlap_hold(self):
        self.registry.evidence = [dataclasses.replace(e, samples=1) for e in self.registry.evidence]
        self.assertEqual(self.stabilize().kind, "hold")
        self.registry = demo_registry()
        self.registry.evidence.append(dataclasses.replace(self.registry.evidence[0], evidence_id="overlap"))
        self.selector = Selector(self.registry)
        self.assertIn("overlapping", self.stabilize().reason)

    def test_expired_objective_and_scope_mismatch(self):
        for goal in [dataclasses.replace(self.objective, expires_at_ms=0), dataclasses.replace(self.objective, scope="other")]:
            self.assertEqual(self.stabilize(goal).kind, "hold")

    def test_duplicate_window_cannot_trigger_switch(self):
        obs = demo_observation()
        self.choose(obs)
        for _ in range(5):
            self.assertEqual(self.choose(obs).kind, "hold")

    def test_alternating_replayed_windows_cannot_supply_stability(self):
        a, b = demo_observation(), demo_observation()
        self.choose(a)
        self.choose(b)
        self.assertEqual(self.choose(a).kind, "hold")
        self.assertEqual(self.choose(b).kind, "hold")

    def test_cooldown_and_external_ownership(self):
        self.selector.mark_switch(self.stamp)
        self.assertIn("cooldown", self.stabilize().reason)
        self.assertIn("external", self.choose(current="external").reason)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.temp.name) / "run", {"synthetic": True})
        self.executor = SimulatedExecutor()
        objective = Objective("test", "global", "app.throughput", "ops/s", "maximize", now_ms() + 60000,
                              (Constraint("app.p99", "ms", "<=", 10),))
        self.agent = Agent(None, Selector(demo_registry()), self.executor, self.store, objective)

    def tearDown(self):
        self.agent.close()
        self.temp.cleanup()

    def switch(self):
        for _ in range(3):
            result = self.agent.tick(demo_observation())
        return result

    def test_feedback_constraint_violation_restores_previous(self):
        self.assertEqual(self.switch()["status"]["policy_id"], "scx_bpfland")
        self.assertEqual(self.agent.tick(demo_observation(1250, 4))["feedback"]["status"], "monitoring")
        recovery = self.agent.tick(demo_observation(1250, 30))
        self.assertTrue(recovery["feedback"]["rollback"])
        self.assertEqual(recovery["status"]["policy_id"], "default")
        self.assertFalse(recovery["feedback"]["causal_improvement_claim"])
        self.agent.rollback()
        self.assertEqual(self.executor.current, "default")

    def test_missing_trial_kpi_rolls_back(self):
        self.switch()
        obs = demo_observation()
        del obs.metrics["app.p99"]
        self.assertTrue(self.agent.tick(obs)["feedback"]["rollback"])
        self.assertEqual(self.executor.current, "default")

    def test_constraint_monitoring_continues_after_initial_trial(self):
        self.agent.objective = dataclasses.replace(self.agent.objective, evaluation_window_s=15)
        self.switch()
        stamp = self.agent.evaluator.trial.started_at_ms + 15000
        obs = demo_observation(1250, 4)
        obs = dataclasses.replace(obs, timestamp_ms=stamp,
                                  metrics={k: dataclasses.replace(v, timestamp_ms=stamp) for k, v in obs.metrics.items()})
        feedback = self.agent.evaluator.evaluate(obs, self.executor.request("status"), stamp)
        self.assertEqual(feedback["status"], "completed")
        self.assertIsNone(self.agent.evaluator.trial)
        self.assertIsNotNone(self.agent.evaluator.guard)
        result = self.agent.tick(demo_observation(1250, 30))
        self.assertTrue(result["feedback"]["rollback"])
        self.assertEqual(self.executor.current, "default")

    def test_zero_latency_baseline_uses_absolute_constraint_without_false_regression(self):
        objective = dataclasses.replace(self.agent.objective, metric="app.p99", unit="ms", direction="minimize")
        self.executor.current = "scx_bpfland"
        evaluator = Evaluator()
        evaluator.begin(demo_observation(1000, 0), objective, "default", "scx_bpfland", now_ms())
        healthy = evaluator.evaluate(demo_observation(1000, 0.05), self.executor.request("status"), now_ms())
        self.assertFalse(healthy["rollback"])
        violated = evaluator.evaluate(demo_observation(1000, 20), self.executor.request("status"), now_ms())
        self.assertTrue(violated["rollback"])

    def test_failed_apply_never_starts_trial(self):
        self.executor.fail_policy = "scx_bpfland"
        result = self.switch()
        self.assertEqual(result["action"]["status"], "failed")
        self.assertIsNone(self.agent.evaluator.trial)
        self.assertEqual(self.executor.current, "default")
        for _ in range(4):
            self.assertIsNone(self.agent.tick(demo_observation())["action"])

    def test_failed_recovery_keeps_trial_for_retry(self):
        self.switch()
        self.executor.fail_policy = "default"
        result = self.agent.tick(demo_observation(1250, 30))
        self.assertEqual(result["action"]["status"], "failed")
        self.assertIsNotNone(self.agent.evaluator.trial)
        self.executor.fail_policy = None
        retry = self.agent.tick(demo_observation(1250, 30))
        self.assertEqual(retry["status"]["policy_id"], "default")
        self.assertIsNone(self.agent.evaluator.trial)

    def test_monitor_only_no_mutations(self):
        for _ in range(5):
            self.agent.tick(demo_observation(), act=False)
        self.assertEqual(self.executor.current, "default")
        self.assertEqual((self.store.path / "actions.jsonl").read_text(), "")

    def test_close_restores_and_summary_rebuilds(self):
        self.switch()
        self.agent.close()
        self.assertEqual(self.executor.current, "default")
        summary = self.store.summarize()
        self.assertEqual(summary["observations"], 3)
        self.assertEqual(summary["actions"], 2)

    def test_plan_expires_or_expected_state_changes(self):
        for _ in range(3):
            plan = self.agent.plan(demo_observation())[0]
        self.executor.current = "scx_rusty"
        with self.assertRaises(ExecutorError):
            self.agent.apply(plan.decision_id)
        self.executor.current = "default"
        expired = dataclasses.replace(plan, expires_at_ms=0)
        self.agent.plans[plan.decision_id] = (expired, demo_observation())
        with self.assertRaises(ValueError):
            self.agent.apply(plan.decision_id)

    def test_tool_idempotency_types_scope_and_structured_errors(self):
        hooks = HookRegistry()
        hooks.register("counter", SimulatedCounterHook())
        tools = build_tools(self.agent, hooks)
        request = {"schema_version": 1, "request_id": "same", "scope": "test", "timeout_ms": 1000,
                   "tool": "hook.attach", "params": {"hook_id": "counter"}}
        result = tools.invoke(request)
        self.assertTrue(result["ok"])
        self.assertEqual(tools.invoke(request), result)
        self.assertFalse(tools.invoke({**request, "params": {"hook_id": "missing"}})["ok"])
        self.assertFalse(tools.invoke({**request, "request_id": "bad", "params": {"hook_id": "counter", "dry_run": "false"}})["ok"])
        self.assertFalse(tools.invoke({**request, "request_id": "scope", "tool": "scheduler.rollback", "params": {}})["ok"])
        self.assertFalse(tools.invoke({**request, "request_id": "unknown", "tool": "unknown"})["ok"])

    def test_skill_rejects_implicit_mixed_goal(self):
        hooks = HookRegistry()
        tools = build_tools(self.agent, hooks)
        with self.assertRaises(ValueError):
            SKILLS["mixed_workload_tuning"].run(tools, dataclasses.replace(self.agent.objective, constraints=()))

    def test_monitoring_reads_cannot_exhaust_mutation_idempotency(self):
        hooks = HookRegistry()
        hooks.register("counter", SimulatedCounterHook())
        tools = build_tools(self.agent, hooks)
        tools.read_capacity, tools.mutation_capacity = 2, 2
        base = {"schema_version": 1, "scope": "global", "timeout_ms": 1000, "params": {}}
        for i in range(5):
            self.assertTrue(tools.invoke({**base, "request_id": f"read-{i}", "tool": "scheduler.status"})["ok"])
        self.assertEqual(len(tools.seen), 0)
        request = {**base, "request_id": "attach", "tool": "hook.attach", "params": {"hook_id": "counter"}}
        self.assertTrue(tools.invoke(request)["ok"])
        self.assertEqual(len(tools.seen), 1)
        self.assertTrue(tools.invoke(request)["ok"])

    def test_synthetic_rows_cannot_be_merged_into_a_real_registry(self):
        registry = PolicyRegistry("synthetic-demo", demo_registry().evidence, synthetic=False)
        self.assertTrue(registry.synthetic)

        class PretendReal(SimulatedExecutor):
            backend = "real"

        with self.assertRaises(ValueError):
            Agent(None, Selector(registry), PretendReal(), self.store, self.agent.objective)

    def test_profile_then_plan_uses_one_sampling_window(self):
        from unittest.mock import Mock
        self.agent.collector = Mock()
        self.agent.collector.snapshot.return_value = demo_observation()
        tools = build_tools(self.agent, HookRegistry())
        base = {"schema_version": 1, "scope": "global", "timeout_ms": 1000, "params": {}}
        tools.invoke({**base, "request_id": "profile", "tool": "workload.profile"})
        tools.invoke({**base, "request_id": "plan", "tool": "scheduler.plan"})
        self.assertEqual(self.agent.collector.snapshot.call_count, 1)

    def test_skill_does_not_rollback_again_after_executor_recovery(self):
        from unittest.mock import patch
        hooks = HookRegistry()
        tools = build_tools(self.agent, hooks)
        self.executor.fail_policy = "scx_bpfland"
        for _ in range(2):
            self.agent.plan(demo_observation())
        # Skill's own plan call sees a measured fixture, not a future stage label.
        with patch.object(self.agent, "collector") as collector:
            collector.snapshot.side_effect = [demo_observation(), demo_observation()]
            result = SKILLS["mixed_workload_tuning"].run(tools, self.agent.objective)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("scheduler.rollback", [step["tool"] for step in result["steps"]])


class CollectorAndHookTests(unittest.TestCase):
    def test_counter_deltas_and_metric_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pressure").mkdir()
            (root / "stat").write_text("cpu 10 0 10 80 0 0 0 0 0 0\nctxt 100\n")
            (root / "pressure/cpu").write_text("some avg10=20.00 avg60=10 avg300=1 total=10\n")
            (root / "loadavg").write_text("1 1 1 2/10 999")
            app = root / "kpi.json"
            app.write_text(dumps({"scope": "global", "metrics": {"app.p99": Metric(4, "ms", "app", 1)}}))
            collector = ProcCollector(root, application_metrics=app)
            first = collector.snapshot()
            self.assertIsNone(first.metrics["cpu.utilization"].value)
            (root / "stat").write_text("cpu 40 0 30 130 0 0 0 0 0 0\nctxt 200\n")
            second = collector.snapshot()
            self.assertAlmostEqual(second.metrics["cpu.utilization"].value, 0.5)
            self.assertAlmostEqual(second.metrics["cpu.pressure"].value, 0.2)
            self.assertEqual(second.metrics["app.p99"].timestamp_ms, 1)
            self.assertFalse(second.metrics["app.p99"].usable(now_ms(), 5000))

    def test_cgroup_authorization_dry_run_and_external_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cpu.weight").write_text("100")
            hook = CpuWeightHook({"service": root})
            hook.attach("service", {"weight": 200}, dry_run=True)
            self.assertEqual(int((root / "cpu.weight").read_text()), 100)
            hook.attach("service", {"weight": 200})
            hook.detach("service")
            self.assertEqual(int((root / "cpu.weight").read_text()), 100)
            for params in [{"weight": 0}, {"weight": True}, {"weight": 10001}]:
                with self.assertRaises(ValueError):
                    hook.attach("service", params)
            with self.assertRaises(ValueError):
                hook.attach("../other", {"weight": 100})
            hook.attach("service", {"weight": 200})
            (root / "cpu.weight").write_text("300")
            with self.assertRaises(ValueError):
                hook.detach("service")

    def test_contract_rejects_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            Metric(float("nan"), "ms", "test", now_ms())
        with self.assertRaises(ValueError):
            SelectorConfig(vote_decay=0)


if __name__ == "__main__":
    unittest.main()
