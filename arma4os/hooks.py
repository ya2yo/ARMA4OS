"""Extensible resource-hook lifecycle and scoped cgroup v2 CPU weight control."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Hook(Protocol):
    def attach(self, scope: str, params: dict, dry_run: bool = False) -> dict: ...
    def detach(self, scope: str, dry_run: bool = False) -> dict: ...
    def stats(self, scope: str) -> dict: ...


class HookRegistry:
    def __init__(self):
        self.hooks: dict[str, Hook] = {}

    def register(self, name: str, hook: Hook):
        if name in self.hooks:
            raise ValueError(f"duplicate hook: {name}")
        self.hooks[name] = hook

    def call(self, operation: str, scope: str, params: dict) -> dict:
        hook = self.hooks[params["hook_id"]]
        if operation == "attach":
            return hook.attach(scope, params.get("config", {}), params.get("dry_run", False))
        if operation == "detach":
            return hook.detach(scope, params.get("dry_run", False))
        return hook.stats(scope)


class SimulatedCounterHook:
    """Lifecycle example only; this does not attach eBPF or count real packets."""
    def __init__(self):
        self.scopes: set[str] = set()

    def attach(self, scope, params, dry_run=False):
        if params:
            raise ValueError("simulated counter accepts no parameters")
        if not dry_run:
            self.scopes.add(scope)
        return {**self.stats(scope), "dry_run": dry_run}

    def detach(self, scope, dry_run=False):
        if not dry_run:
            self.scopes.discard(scope)
        return {**self.stats(scope), "dry_run": dry_run}

    def stats(self, scope):
        return {"attached": scope in self.scopes, "backend": "simulated", "packets": None, "bytes": None}


class CpuWeightHook:
    """Only explicitly authorized cgroup directories; restoration uses the saved original."""
    def __init__(self, authorized: dict[str, Path]):
        self.authorized = {scope: Path(path).resolve(strict=True) for scope, path in authorized.items()}
        self.saved: dict[str, tuple[int, int]] = {}

    def file(self, scope):
        if scope not in self.authorized:
            raise ValueError("unauthorized cgroup scope")
        root = self.authorized[scope]
        path = root / "cpu.weight"
        if path.is_symlink() or path.resolve(strict=True).parent != root:
            raise ValueError("cpu.weight must belong to authorized cgroup")
        return path

    def attach(self, scope, params, dry_run=False):
        if set(params) != {"weight"} or type(params["weight"]) is not int or not 1 <= params["weight"] <= 10000:
            raise ValueError("weight must be an integer in 1..10000")
        path, weight = self.file(scope), params["weight"]
        current = int(path.read_text())
        if scope in self.saved and current != self.saved[scope][1]:
            raise ValueError("cgroup modified by external controller")
        if not dry_run:
            original = self.saved.get(scope, (current, weight))[0]
            # Save recovery state before mutation, including when verification fails.
            self.saved[scope] = (original, weight)
            path.write_text(str(weight))
            if int(path.read_text()) != weight:
                raise RuntimeError("cpu.weight verification failed")
        return {"weight": current if dry_run else weight, "dry_run": dry_run, "backend": "cgroupv2"}

    def detach(self, scope, dry_run=False):
        path = self.file(scope)
        if scope not in self.saved:
            return {"restored": True, "changed": False}
        original, expected = self.saved[scope]
        if int(path.read_text()) != expected:
            raise ValueError("external cgroup modification; refusing overwrite")
        if not dry_run:
            path.write_text(str(original))
            if int(path.read_text()) != original:
                raise RuntimeError("cgroup restoration verification failed")
            del self.saved[scope]
        return {"restored": not dry_run, "dry_run": dry_run, "weight": original}

    def stats(self, scope):
        return {"weight": int(self.file(scope).read_text()), "managed": scope in self.saved, "backend": "cgroupv2"}

    def close(self):
        for scope in list(self.saved):
            self.detach(scope)
