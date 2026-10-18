"""Transport-neutral structured Tool boundary. CLI JSONL is the first adapter."""

from __future__ import annotations

import hashlib
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass

from .contracts import dumps, encode, now_ms


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    required: tuple[str, ...]
    allowed: tuple[str, ...]
    mutating: bool
    handler: object
    global_only: bool = False

    def schema(self):
        return {"name": self.name, "description": self.description, "schema_version": 1,
                "mutating": self.mutating, "input_schema": {"type": "object", "required": list(self.required),
                "properties": {k: {"type": "boolean" if k == "dry_run" else "object" if k == "config" else "string"}
                               for k in self.allowed}, "additionalProperties": False}}


class ToolRegistry:
    def __init__(self, store):
        self.store = store
        self.tools: dict[str, Tool] = {}
        self.seen: dict[str, tuple[str, dict]] = {}
        self.read_seen: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self.mutation_capacity, self.read_capacity = 10000, 1024

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self.tools[tool.name] = tool

    def schemas(self):
        return [tool.schema() for tool in self.tools.values()]

    def invoke(self, request: dict) -> dict:
        started = time.monotonic()
        request_id = request.get("request_id")
        response = {"schema_version": 1, "request_id": request_id, "ok": False, "result": None, "error": None}
        digest = hashlib.sha256(dumps(request).encode()).hexdigest()
        cache_mutation = False
        try:
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise ValueError("request_id must be a nonempty string of at most 128 characters")
            previous = self.seen.get(request_id) or self.read_seen.get(request_id)
            if previous:
                old_digest, old_response = previous
                if old_digest != digest:
                    raise ValueError("request_id reused for different content")
                return old_response
            if request.get("schema_version") != 1:
                raise ValueError("unsupported schema_version")
            if set(request) != {"schema_version", "request_id", "scope", "timeout_ms", "tool", "params"}:
                raise ValueError("missing or unknown request envelope fields")
            timeout = request["timeout_ms"]
            if type(timeout) is not int or not 1 <= timeout <= 120000:
                raise ValueError("timeout_ms must be integer 1..120000")
            if not isinstance(request.get("scope"), str) or not request["scope"]:
                raise ValueError("scope is required")
            tool = self.tools[request["tool"]]
            cache_mutation = tool.mutating
            if tool.mutating and len(self.seen) >= self.mutation_capacity:
                raise ValueError("idempotency capacity reached; start a new session")
            if tool.global_only and request["scope"] != "global":
                raise ValueError("scheduler mutations require global scope")
            params = request["params"]
            if not isinstance(params, dict) or not set(tool.required) <= set(params) or set(params) - set(tool.allowed):
                raise ValueError("missing or unknown tool parameters")
            for key, value in params.items():
                expected = bool if key == "dry_run" else dict if key == "config" else str
                if type(value) is not expected:
                    raise ValueError(f"invalid parameter type: {key}")
            response.update(ok=True, result=encode(tool.handler(request["scope"], params, timeout)))
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            response.update(ok=False, result=None, error={"code": getattr(exc, "code", "invalid_request"), "message": str(exc)})
        if isinstance(request_id, str) and request_id not in self.seen and request_id not in self.read_seen:
            # Refuse additional new mutations after cache capacity instead of forgetting idempotency keys.
            if cache_mutation and len(self.seen) < self.mutation_capacity:
                self.seen[request_id] = (digest, response)
            elif not cache_mutation:
                self.read_seen[request_id] = (digest, response)
                while len(self.read_seen) > self.read_capacity:
                    self.read_seen.popitem(last=False)
        self.store.append("tools", {"timestamp_ms": now_ms(), "tool": request.get("tool"),
                          "request_id": request_id, "scope": request.get("scope"), "response": response,
                          "duration_ms": (time.monotonic() - started) * 1000})
        return response


def build_tools(agent, hooks, experiments=None) -> ToolRegistry:
    registry = ToolRegistry(agent.store)

    def add(name, description, required, allowed, mutating, handler, global_only=False):
        registry.register(Tool(name, description, tuple(required), tuple(allowed), mutating, handler, global_only))

    add("system.capabilities", "Executor and installed plugin capabilities", (), (), False,
        lambda s, p, t: {"executor": agent.executor.request("capabilities", timeout_ms=t), "hooks": list(hooks.hooks)})
    add("metrics.snapshot", "Collect metrics with units and source timestamps", (), (), False,
        lambda s, p, t: agent.observe())
    add("workload.profile", "Profile measured CPU contention and explicit application objective", (), (), False,
        lambda s, p, t: {"profile": encode(agent.profiler.profile(agent.observe(), now_ms())), "objective": encode(agent.objective)})
    for operation in ["list", "status"]:
        add(f"scheduler.{operation}", f"Read scheduler {operation}", (), (), False,
            lambda s, p, t, op=operation: agent.executor.request(op, timeout_ms=t))
    add("scheduler.plan", "Create a time-limited plan from measured observations", (), (), False,
        lambda s, p, t: agent.plan()[0])
    add("scheduler.apply", "Apply a local validated decision ID", ("decision_id",), ("decision_id", "dry_run"), True,
        lambda s, p, t: agent.apply(p["decision_id"], p.get("dry_run", False), t), True)
    add("scheduler.rollback", "Restore the previous managed scheduler", (), ("dry_run",), True,
        lambda s, p, t: agent.executor.request("rollback", {"expected_policy": agent.executor.request("status")["policy_id"],
                      "expires_at_unix_ms": now_ms() + t, "dry_run": True}, t) if p.get("dry_run") else agent.rollback(timeout_ms=t), True)
    for operation in ["attach", "detach", "stats"]:
        add(f"hook.{operation}", f"Plugin lifecycle: {operation}", ("hook_id",), ("hook_id", "config", "dry_run"), operation != "stats",
            lambda s, p, t, op=operation: hooks.call(op, s, p))
    if experiments:
        add("experiment.run", "Run a locally registered fixed benchmark", ("experiment_id",), ("experiment_id",), True,
            lambda s, p, t: experiments.run(p["experiment_id"], t), True)
        add("experiment.result", "Read registered benchmark results", ("run_id",), ("run_id",), False,
            lambda s, p, t: experiments.result(p["run_id"]))
    return registry
