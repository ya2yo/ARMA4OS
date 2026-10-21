"""Parser for elapsed wall-clock time and batch workload metrics."""

from __future__ import annotations

import json
from typing import Any

from benchmarks.evidence.parsers.base import ParseError, ParserResult


class ElapsedParser:
    """Parses wall-clock execution time and optional structured JSON metrics from stdout."""

    def parse(self, stdout: str, stderr: str, duration_s: float, exit_code: int) -> ParserResult:
        if exit_code != 0:
            raise ParseError(f"Process exited with non-zero exit code: {exit_code}")

        if duration_s < 0:
            raise ParseError(f"Negative duration measured: {duration_s}")

        metrics: dict[str, Any] = {
            "wall_time_s": round(duration_s, 6),
            "exit_code": exit_code,
        }

        # Attempt to discover and parse any JSON metrics emitted on stdout lines
        if stdout:
            for line in stdout.strip().splitlines():
                line = line.strip()
                if not line or not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        for k, v in payload.items():
                            if isinstance(v, (int, float, bool, str)) and k not in metrics:
                                metrics[k] = v
                except json.JSONDecodeError:
                    continue

        return ParserResult(metrics=metrics, raw_summary=f"wall_time_s={metrics['wall_time_s']}")
