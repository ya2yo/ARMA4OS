"""Long-lived Rust JSONL client plus an explicitly simulated implementation."""

from __future__ import annotations

import collections
import json
import selectors
import subprocess
import threading
import time
from pathlib import Path

from .contracts import ActionResult, dumps, encode, identifier, now_ms


class ExecutorError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class SimulatedExecutor:
    """Tests and demos only. Never probes or changes kernel scheduling."""
    backend = "simulated"

    def __init__(self, policies=("default", "scx_bpfland", "scx_rusty")):
        self.policies = tuple(policies)
        self.current = self.previous = "default"
        self.fail_policy: str | None = None

    def request(self, operation: str, params: dict | None = None, timeout_ms=5000, request_id=None) -> dict:
        params = params or {}
        if operation == "status":
            return {"policy_id": self.current, "kernel_state": "disabled" if self.current == "default" else "enabled",
                    "kernel_ops": None if self.current == "default" else self.current.removeprefix("scx_"),
                    "managed_pid": None, "healthy": True, "backend": self.backend}
        if operation == "list":
            return {"policies": [{"id": p} for p in self.policies], "backend": self.backend}
        if operation == "capabilities":
            return {"backend": self.backend, "sched_ext": False, "simulated": True}
        if operation == "shutdown":
            self.current = "default"
            return {"shutdown": True, "cleaned_up_pid": None, "status": self.request("status"), "verified": True}
        if operation not in {"apply", "rollback"}:
            raise ExecutorError("unknown_operation", operation)
        if params.get("expires_at_unix_ms", 0) <= now_ms():
            raise ExecutorError("expired", "action plan expired")
        if params.get("expected_policy") != self.current:
            raise ExecutorError("conflict", "scheduler changed since plan")
        target = params.get("policy_id", self.previous) if operation == "rollback" else params.get("policy_id")
        if target not in self.policies:
            raise ExecutorError("unknown_policy", str(target))
        stamp, previous = now_ms(), self.current
        status, error, recovery = "applied", None, None
        if params.get("dry_run"):
            status = "dry_run"
        elif target == self.fail_policy:
            status, error = "failed", "injected simulated load failure"
            recovery = {"actual_policy": previous, "verified": True}
        elif target == previous:
            status = "held"
        else:
            self.current, self.previous = target, previous if operation == "apply" else target
            status = "rolled_back" if operation == "rollback" else "applied"
        return encode(ActionResult(target, previous, self.current, status, True, stamp, now_ms(),
                                   error=error, recovery=recovery, backend=self.backend))

    def close(self):
        self.request("shutdown")


class RustExecutor:
    def __init__(self, binary: str | Path, config: str | Path):
        settings = json.loads(Path(config).read_text())
        # The protocol's action budget does not cancel bounded emergency recovery.
        # Keep the pipe open long enough to receive that recovery's actual result.
        self.recovery_grace_s = (settings.get("stop_timeout_ms", 3000) + settings.get("start_timeout_ms", 5000)) / 1000
        self.process = subprocess.Popen([str(Path(binary).resolve()), "--config", str(Path(config).resolve())],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, bufsize=0)
        self.lock = threading.Lock()
        self.buffer = bytearray()
        self.closed = False

    def request(self, operation: str, params: dict | None = None, timeout_ms=5000, request_id=None) -> dict:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 120000:
            raise ValueError("executor timeout must be 1..120000 milliseconds")
        with self.lock:
            if self.closed:
                raise ExecutorError("closed", "executor is closed")
            request_id = request_id or identifier()
            request = {"schema_version": 1, "request_id": request_id, "operation": operation,
                       "timeout_ms": timeout_ms, "params": params or {}}
            try:
                self.process.stdin.write((dumps(request) + "\n").encode())
                self.process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                raise ExecutorError("disconnected", "Rust executor exited") from exc
            grace = self.recovery_grace_s if operation in {"apply", "rollback"} else 0
            deadline = time.monotonic() + timeout_ms / 1000 + grace + 1.0
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while b"\n" not in self.buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        # A timeout leaves action outcome uncertain. Never reuse a desynchronized channel.
                        self.process.stdin.close()
                        self.closed = True
                        raise ExecutorError("timeout", "executor response timeout; cleanup requested via EOF")
                    chunk = self.process.stdout.read(65536)
                    if not chunk:
                        raise ExecutorError("disconnected", "executor stdout closed")
                    self.buffer.extend(chunk)
                    if len(self.buffer) > 1_048_576:
                        raise ExecutorError("protocol", "executor response exceeds 1 MiB")
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            response = json.loads(line)
            if response.get("request_id") != request_id or response.get("schema_version") != 1:
                raise ExecutorError("protocol", "executor response correlation failed")
            if not response.get("ok"):
                error = response.get("error") or {}
                raise ExecutorError(error.get("code", "executor_error"), error.get("message", "unknown error"))
            return response["result"]

    def close(self):
        try:
            if not self.closed and self.process.poll() is None:
                self.request("shutdown", timeout_ms=10000)
        finally:
            self.closed = True
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
