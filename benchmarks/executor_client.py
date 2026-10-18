"""Client communicating with arma-executor using primary RustExecutor transport."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from arma4os.executor import ExecutorError as BaseExecutorError, RustExecutor, SimulatedExecutor
from arma4os.contracts import identifier


class ExecutorError(BaseExecutorError):
    """Raised when executor protocol interaction fails or returns an error response."""

    def __init__(self, code_or_msg: str, message: str | None = None):
        if message is None:
            super().__init__("executor_error", code_or_msg)
        else:
            super().__init__(code_or_msg, message)


class ExecutorClient:
    """Thin domain wrapper over primary arma4os.executor.RustExecutor.

    Reuses RustExecutor for JSONL transport (eliminates duplicate transport, undrained stderr,
    and missing response ID checks).
    Preserves audit logging to actions.jsonl, healthy status verification, and exact policy checks.
    """

    def __init__(
        self,
        executor_path: str | Path | None = None,
        config_path: str | Path | None = None,
        actions_log_path: str | Path | None = None,
        executor: Any = None,
    ) -> None:
        self.executor_path = str(executor_path) if executor_path else None
        self.config_path = str(config_path) if config_path else None
        self.actions_log_path = Path(actions_log_path) if actions_log_path else None
        self.executor = executor
        self.current_policy: str = "default"

        if self.actions_log_path:
            self.actions_log_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.actions_log_path.exists():
                self.actions_log_path.touch()

    def start(self) -> None:
        """Start or attach to the primary executor."""
        if self.executor is not None:
            return

        if not self.executor_path or not self.config_path:
            raise ExecutorError(
                "config_missing",
                "Cannot start executor: executor_path and config_path must both be provided",
            )

        try:
            self.executor = RustExecutor(binary=self.executor_path, config=self.config_path)
        except Exception as exc:
            raise ExecutorError("start_failed", f"Failed to start RustExecutor: {exc}") from exc

    def _log_action(self, entry: dict[str, Any]) -> None:
        """Append an audit action entry to actions.jsonl."""
        if self.actions_log_path:
            with self.actions_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()

    def request(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """Send a request via primary executor and record audit entry."""
        if self.executor is None:
            self.start()

        t0 = time.perf_counter()
        t0_ms = int(time.time() * 1000)
        request_id = identifier()

        try:
            result = self.executor.request(operation, params=params or {}, timeout_ms=timeout_ms, request_id=request_id)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            self._log_action({
                "timestamp_unix_ms": t0_ms,
                "request_id": request_id,
                "operation": operation,
                "duration_ms": duration_ms,
                "params": params or {},
                "result": result,
                "status": result.get("status") if isinstance(result, dict) else "ok",
            })
            return result
        except Exception as exc:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            self._log_action({
                "timestamp_unix_ms": t0_ms,
                "request_id": request_id,
                "operation": operation,
                "duration_ms": duration_ms,
                "params": params or {},
                "error": str(exc),
                "status": "failed",
            })
            if isinstance(exc, BaseExecutorError):
                raise
            raise ExecutorError("request_failed", str(exc)) from exc

    def verify_status(self) -> dict[str, Any]:
        """Verify executor status, health, and absence of external schedulers."""
        result = self.request("status", {})
        if not isinstance(result, dict):
            raise ExecutorError("invalid_status", f"Expected dictionary status result, got {type(result)}")

        if not result.get("healthy", False):
            raise ExecutorError("unhealthy", f"Executor reported unhealthy status: {result}")

        current_pol = result.get("policy_id", "default")
        if current_pol == "external":
            raise ExecutorError("external_scheduler", f"External scheduler active; executor cannot safely manage: {result}")

        self.current_policy = current_pol
        return result

    def verify_policy(self, expected_policy: str) -> dict[str, Any]:
        """Verify executor is healthy and reported policy matches expected_policy exactly."""
        status = self.verify_status()
        actual = status.get("policy_id")
        if actual != expected_policy:
            raise ExecutorError(
                "policy_mismatch",
                f"Executor policy mismatch: expected '{expected_policy}', but actual status reported '{actual}'",
            )
        return status

    def apply_policy(self, policy_id: str, timeout_sec: float = 30.0) -> dict[str, Any]:
        """Apply a scheduling policy through executor and verify transition."""
        if self.current_policy == policy_id:
            # Held policy: verify healthy status and exact policy
            self.verify_policy(policy_id)
            return {
                "status": "held",
                "actual_policy": policy_id,
                "verified": True,
            }

        params = {
            "policy_id": policy_id,
            "expected_policy": self.current_policy,
            "expires_at_unix_ms": int((time.time() + timeout_sec) * 1000),
            "dry_run": False,
        }

        result = self.request("apply", params, timeout_ms=int(timeout_sec * 1000))
        if not isinstance(result, dict):
            raise ExecutorError("apply_failed", f"Invalid response from apply: {result}")

        status = result.get("status")
        actual = result.get("actual_policy", policy_id)
        if status not in ("applied", "held") or actual != policy_id:
            raise ExecutorError(
                "apply_failed",
                f"Failed to apply policy '{policy_id}': status={status}, result={result}",
            )

        self.current_policy = actual
        self.verify_policy(policy_id)
        return result

    def rollback(self, timeout_sec: float = 30.0) -> dict[str, Any]:
        """Roll back to previous scheduling policy."""
        params = {
            "expected_policy": self.current_policy,
            "expires_at_unix_ms": int((time.time() + timeout_sec) * 1000),
        }
        result = self.request("rollback", params, timeout_ms=int(timeout_sec * 1000))
        if isinstance(result, dict):
            self.current_policy = result.get("actual_policy", "default")
        return result

    def restore_default(self) -> None:
        """Ensure policy is restored to 'default'."""
        if self.current_policy != "default":
            try:
                self.apply_policy("default")
            except Exception:
                try:
                    self.rollback()
                except Exception:
                    pass

    def shutdown(self) -> None:
        """Restore default policy and close executor connection."""
        try:
            self.restore_default()
        except Exception:
            pass

        if self.executor is not None:
            try:
                self.executor.close()
            finally:
                self.executor = None

    def __enter__(self) -> ExecutorClient:
        try:
            self.start()
            self.verify_status()
            return self
        except BaseException:
            self.shutdown()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.shutdown()
