"""Optional slow analysis seam inspired by SchedCP. Never executes generated commands.

A provider receives a structured context and returns a proposal; it can be an LLM,
an offline report generator, or a test double. The online selector stays independent.
No provider is enabled automatically and proposals never enter the executable allowlist.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError


class Advisor:
    def __init__(self, provider, allowed_policies):
        self.provider = provider
        self.allowed = set(allowed_policies)

    def analyze(self, context: dict, timeout_s: float = 10) -> dict:
        # Providers must implement their own cancellable I/O deadline; threads cannot stop I/O.
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.provider, context, timeout_s)
        try:
            proposal = future.result(timeout=timeout_s)
            if not isinstance(proposal, dict) or set(proposal) != {"policy_id", "rationale", "evidence_ids"}:
                raise ValueError("advisor returned an invalid proposal schema")
            if proposal["policy_id"] not in self.allowed or not isinstance(proposal["rationale"], str):
                raise ValueError("advisor proposed an unknown policy")
            if not isinstance(proposal["evidence_ids"], list) or not all(isinstance(x, str) for x in proposal["evidence_ids"]):
                raise ValueError("advisor evidence_ids must be strings")
            return {"status": "proposal_only", "proposal": proposal, "requires_calibration": True}
        except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
            return {"status": "unavailable", "reason": type(exc).__name__, "online_loop_affected": False}
        finally:
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
