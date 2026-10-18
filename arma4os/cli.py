"""CLI and JSONL tool transport. Real scheduling is always an explicit opt-in."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from .agent import Agent
from .audit import RunStore
from .collector import ProcCollector
from .contracts import Constraint, Metric, Objective, Observation, dumps, identifier, now_ms
from .executor import RustExecutor, SimulatedExecutor
from .experiments import Experiments
from .hooks import CpuWeightHook, HookRegistry, SimulatedCounterHook
from .registry import Estimate, Evidence, PolicyRegistry
from .selector import Selector, SelectorConfig
from .skills import SKILLS
from .tools import build_tools


def jsonl_input(should_stop):
    """Poll raw stdin so SIGTERM can close tools even when the sender is idle."""
    buffer = bytearray()
    # epoll rejects regular-file redirection; select supports stdin as a file,
    # terminal or pipe while keeping idle signal handling responsive.
    with selectors.SelectSelector() as selector:
        selector.register(sys.stdin, selectors.EVENT_READ)
        while not should_stop():
            if not selector.select(0.1):
                continue
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                if buffer:
                    yield buffer.decode("utf-8")
                return
            buffer.extend(chunk)
            if len(buffer) > 1_048_576:
                raise ValueError("tool input exceeds 1 MiB")
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                yield line.decode("utf-8")


def environment_id() -> str:
    identity = {"node": platform.node(), "kernel": platform.release(), "machine": platform.machine(),
                "cpus": os.cpu_count(), "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None}
    try:
        identity["cpuinfo"] = next(s for s in Path("/proc/cpuinfo").read_text().splitlines() if s.startswith("model name"))
    except (OSError, StopIteration):
        pass
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]


def demo_registry() -> PolicyRegistry:
    rows = []
    for policy, throughput, p99 in [("default", 1000, 20), ("scx_bpfland", 1250, 4), ("scx_rusty", 1500, 16)]:
        rows.append(Evidence(f"synthetic-{policy}", policy, "synthetic-demo", "cpu_contended", 5,
                             "SYNTHETIC illustrative data; not a benchmark", now_ms() + 3600000,
                             {"cpu.utilization": (0.7, 1), "cpu.pressure": (0.05, 1)},
                             {"app.throughput": Estimate(throughput, 10, "ops/s"),
                              "app.p99": Estimate(p99, 0.5, "ms")}, 0.1, True))
    return PolicyRegistry("synthetic-demo", rows, synthetic=True)


def demo_observation(throughput=1000, p99=20) -> Observation:
    stamp = now_ms()
    return Observation(identifier(), stamp, 1000, "global", {
        k: Metric(value, unit, "synthetic-demo", stamp) for k, value, unit in [
            ("cpu.utilization", 0.95, "fraction"), ("cpu.pressure", 0.3, "fraction"),
            ("app.throughput", throughput, "ops/s"), ("app.p99", p99, "ms")]})


def make_agent(args, demo=False):
    registry = demo_registry() if demo else PolicyRegistry.load(args.registry)
    objective = Objective("synthetic-mixed", "global", "app.throughput", "ops/s", "maximize", now_ms() + 300000,
                          (Constraint("app.p99", "ms", "<=", 10),)) if demo else Objective.from_dict(json.loads(Path(args.objective).read_text()))
    if not demo and registry.evidence and registry.environment_id != environment_id():
        raise ValueError(f"registry environment differs from current machine; expected {environment_id()}")
    executor = RustExecutor(args.executor, args.executor_config) if args.executor else SimulatedExecutor()
    try:
        actual = executor.request("status")
        if registry.synthetic and actual["backend"] != "simulated":
            raise ValueError("synthetic evidence cannot drive the real executor")
        config = SelectorConfig(**json.loads(Path(args.selector_config).read_text())) if args.selector_config else SelectorConfig()
        output = Path(args.output or f"results/agent/{identifier()}")
        manifest = {"mode": "synthetic-demo" if demo else "agent", "executor_backend": actual["backend"],
                    "synthetic": registry.synthetic, "environment_id": environment_id(),
                    "registry": "built-in synthetic demo" if demo else str(args.registry),
                    "registry_snapshot": {"environment_id": registry.environment_id, "minimum_samples": registry.minimum_samples,
                                          "synthetic": registry.synthetic, "evidence": registry.evidence}, "objective": objective,
                    "selector": config, "executor_config": json.loads(Path(args.executor_config).read_text()) if args.executor else None,
                    "invocation": sys.argv}
        store = RunStore(output, manifest)
        collector = ProcCollector(application_metrics=Path(args.application_metrics) if args.application_metrics else None,
                                  scope=objective.scope, cgroup=Path(args.cgroup) if args.cgroup else None)
        return Agent(collector, Selector(registry, config), executor, store, objective)
    except BaseException:
        executor.close()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ARMA4OS resource management agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities", help="Read-only machine identity and kernel capabilities")
    for name in ["demo", "run", "tools", "skill"]:
        p = sub.add_parser(name)
        p.add_argument("--output", help="New directory for audit artifacts; existing directories are rejected")
        p.add_argument("--executor", help="Rust arma-executor binary; omit for Python simulation")
        p.add_argument("--executor-config", default="configs/executor.simulated.json")
        p.add_argument("--registry", default="configs/registry.json")
        p.add_argument("--objective", default="configs/objective.json")
        p.add_argument("--selector-config")
        p.add_argument("--application-metrics")
        p.add_argument("--cgroup")
        p.add_argument("--hooks-config", help="Optional trusted configuration enabling real hook plugins")
        if name == "run":
            p.add_argument("--interval", type=float, default=1.0)
            p.add_argument("--ticks", type=int, default=0, help="0 means run until interrupted")
            p.add_argument("--monitor-only", action="store_true")
        if name == "tools":
            p.add_argument("--schemas", action="store_true")
        if name == "skill":
            p.add_argument("name", choices=sorted(SKILLS))
            p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        root = Path("/sys/kernel/sched_ext")
        print(dumps({"schema_version": 1, "environment_id": environment_id(), "kernel": platform.release(),
                     "architecture": platform.machine(), "sched_ext_sysfs": root.exists(),
                     "btf": Path("/sys/kernel/btf/vmlinux").exists(), "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").exists(),
                     "effective_uid": os.geteuid(), "warning": "existence is not proof of scheduler load compatibility"}))
        return 0
    if args.command == "run" and (not 0.01 <= args.interval <= 3600 or args.ticks < 0):
        parser.error("interval must be 0.01..3600 seconds and ticks nonnegative")
    agent = None
    cleanup_hooks = []
    stopped = False

    def emit(value):
        line = dumps(value)
        print(line, flush=True)
        if agent:
            with (agent.store.path / "stdout").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        agent = make_agent(args, args.command == "demo")
        hooks = HookRegistry()
        hooks.register("demo.counter", SimulatedCounterHook())
        if args.hooks_config:
            hook_config = json.loads(Path(args.hooks_config).read_text())
            if "cpu_weight" in hook_config:
                hook = CpuWeightHook(hook_config["cpu_weight"]["authorized"])
                hooks.register("cgroup.cpu_weight", hook)
                cleanup_hooks.append(hook)
            if "network_counter" in hook_config:
                from .bpf_hook import BpfCounterHook
                hook = BpfCounterHook(**hook_config["network_counter"])
                hooks.register("ebpf.ingress_counter", hook)
                cleanup_hooks.append(hook)
        configs = {"compression": Path("configs/benchmarks.json")} if Path("configs/benchmarks.json").exists() else {}
        tools = build_tools(agent, hooks, Experiments(configs, agent.store.path / "experiments"))
        if args.command == "demo":
            emit({"mode": "SYNTHETIC DEMO", "performance_evidence": False, "output": str(agent.store.path)})
            for i in range(5):
                if stopped:
                    break
                obs = demo_observation(1250, 4) if i == 3 else demo_observation(900, 30) if i == 4 else demo_observation()
                emit(agent.tick(obs))
        elif args.command == "run":
            index = 0
            while not stopped and (not args.ticks or index < args.ticks):
                emit(agent.tick(act=not args.monitor_only))
                index += 1
                if args.ticks and index >= args.ticks:
                    break
                end = time.monotonic() + args.interval
                while not stopped and time.monotonic() < end:
                    time.sleep(min(0.1, max(0, end - time.monotonic())))
        elif args.command == "tools":
            if args.schemas:
                emit(tools.schemas())
            else:
                for line in jsonl_input(lambda: stopped):
                    if stopped:
                        break
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("tool request must be an object")
                        emit(tools.invoke(request))
                    except (ValueError, TypeError) as exc:
                        emit({"schema_version": 1, "request_id": None, "ok": False,
                              "result": None, "error": {"code": "invalid_json", "message": str(exc)}})
        elif args.command == "skill":
            emit(SKILLS[args.name].run(tools, agent.objective, args.dry_run))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"arma4os: {exc}", file=sys.stderr)
        if agent:
            with (agent.store.path / "stderr").open("a", encoding="utf-8") as stream:
                stream.write(f"arma4os: {exc}\n")
        return 1
    finally:
        try:
            for hook in reversed(cleanup_hooks):
                try:
                    hook.close()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    print(f"hook cleanup failed: {exc}", file=sys.stderr)
        finally:
            try:
                if agent:
                    agent.close()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
