"""Append-only evidence files; no performance claims are inferred from action success."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

from .contracts import dumps, now_ms


class RunStore:
    def __init__(self, directory: str | Path, manifest: dict):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=False)
        self.write("manifest.json", {"schema_version": 1, "created_at_ms": now_ms(),
                   "uname": list(platform.uname()), "python": sys.version, **manifest})
        for name in ["metrics", "decisions", "actions", "tools", "feedback"]:
            (self.path / f"{name}.jsonl").touch()
        (self.path / "stdout").touch()
        (self.path / "stderr").touch()

    def append(self, name: str, value) -> None:
        if name not in {"metrics", "decisions", "actions", "tools", "feedback"}:
            raise ValueError("unknown audit stream")
        with (self.path / f"{name}.jsonl").open("a", encoding="utf-8") as f:
            f.write(dumps(value) + "\n")
            f.flush()

    def write(self, name: str, value) -> None:
        if name not in {"manifest.json", "summary.json"}:
            raise ValueError("unknown artifact")
        p = self.path / name
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(dumps(value) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(p)

    def summarize(self) -> dict:
        decisions = [json.loads(s) for s in (self.path / "decisions.jsonl").read_text().splitlines()]
        actions = [json.loads(s) for s in (self.path / "actions.jsonl").read_text().splitlines()]
        summary = {"schema_version": 1, "observations": len((self.path / "metrics.jsonl").read_text().splitlines()),
                   "decisions": len(decisions), "switch_decisions": sum(d["kind"] == "switch" for d in decisions),
                   "actions": len(actions), "failed_actions": sum(a["status"] == "failed" for a in actions),
                   "performance_conclusion": "requires independent controlled benchmark evidence"}
        self.write("summary.json", summary)
        return summary
