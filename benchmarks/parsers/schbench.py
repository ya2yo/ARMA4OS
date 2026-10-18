"""Parser for schbench benchmark latency output."""

from __future__ import annotations

import math
import re
from typing import Any

from benchmarks.parsers import ParseError, ParserResult


class SchbenchParser:
    """Parses output from schbench wake latency benchmark.

    Supports standard schbench outputs with explicit units (e.g., usec, ms, ns).
    Strictly verifies p99 wakeup latency exists and is valid. Rejects ambiguous or missing p99.
    Never invents percentiles; only reports percentiles actually observed.
    """

    # Generic percentile pattern supporting:
    # 50.0th: 12
    # *99.0th: 51
    # 99.0000th: 123 (N samples)
    # *99.0000th: 123.45 usec (500 samples)
    PERCENTILE_LINE_PATTERN = re.compile(
        r"^\s*\*?([0-9]+(?:\.[0-9]+)?)th\s*:\s*([0-9]+(?:\.[0-9]+)?)(.*)$",
        re.MULTILINE,
    )

    PERCENTILE_MAP: dict[float, str] = {
        50.0: "p50",
        75.0: "p75",
        90.0: "p90",
        95.0: "p95",
        99.0: "p99",
        99.5: "p99_5",
        99.9: "p99_9",
    }

    HEADER_UNIT_PATTERN = re.compile(
        r"(?:Wakeup\s+)?Latenc(?:y|ies)\s+percentiles\s*\(([a-zA-Z]+)\)",
        re.IGNORECASE,
    )
    LINE_UNIT_PATTERN = re.compile(
        r"\b(usec|us|ms|ns|s)\b",
        re.IGNORECASE,
    )
    MIN_MAX_PATTERN = re.compile(
        r"min\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*max\s*=\s*([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE,
    )
    RPS_PATTERN = re.compile(
        r"Requests:\s*([0-9]+)\s*\(([0-9]+(?:\.[0-9]+)?)/s\)",
        re.IGNORECASE,
    )

    def parse(self, stdout: str, stderr: str, duration_s: float, exit_code: int) -> ParserResult:
        if exit_code != 0:
            raise ParseError(f"Process exited with non-zero exit code: {exit_code}")

        if not stdout or not stdout.strip():
            raise ParseError("schbench stdout is empty")

        # schbench may output multiple blocks during execution; isolate blocks and inspect the final block
        block_markers = list(re.finditer(r"(?:Wakeup\s+)?Latenc(?:y|ies)\s+percentiles", stdout, re.IGNORECASE))
        if block_markers:
            # Use content from the last header to the end
            last_marker = block_markers[-1]
            target_text = stdout[last_marker.start():]
        else:
            target_text = stdout

        # 1. Detect and establish explicit units
        detected_header_unit: str | None = None
        header_unit_match = self.HEADER_UNIT_PATTERN.search(target_text)
        if header_unit_match:
            detected_header_unit = header_unit_match.group(1).lower()

        detected_line_unit: str | None = None
        percentiles: dict[str, float] = {}

        # 2. Extract observed percentiles
        for m in self.PERCENTILE_LINE_PATTERN.finditer(target_text):
            p_val_raw = m.group(1)
            latency_val_raw = m.group(2)
            suffix = m.group(3)

            try:
                p_val = float(p_val_raw)
                lat_val = float(latency_val_raw)
            except ValueError as err:
                raise ParseError(f"Invalid numeric value in schbench output: {err}") from err

            if math.isnan(lat_val) or math.isinf(lat_val) or lat_val < 0:
                raise ParseError(f"Invalid latency value for {p_val_raw}th percentile: {lat_val}")

            # Check suffix for unit (e.g., 'usec' or 'ms')
            # Distinguish from sample count like '(500 samples)'
            unit_match = self.LINE_UNIT_PATTERN.search(suffix)
            if unit_match:
                found_unit = unit_match.group(1).lower()
                if detected_line_unit and detected_line_unit != found_unit:
                    raise ParseError(f"Conflicting line units in schbench output: '{detected_line_unit}' vs '{found_unit}'")
                detected_line_unit = found_unit

            # Map to standard metric name (e.g., 99.0000 -> p99, 99.5 -> p99_5)
            # Only record percentiles actually observed (no invented percentiles)
            p_round = round(p_val, 4)
            if p_round in self.PERCENTILE_MAP:
                metric_name = self.PERCENTILE_MAP[p_round]
            else:
                metric_name = f"p{int(p_val)}" if p_val.is_integer() else f"p{str(p_val).replace('.', '_')}"

            percentiles[metric_name] = lat_val

        # Reconcile units
        unit: str | None = None
        if detected_header_unit and detected_line_unit:
            if detected_header_unit != detected_line_unit:
                raise ParseError(f"Conflicting latency units: header has '{detected_header_unit}', line has '{detected_line_unit}'")
            unit = detected_header_unit
        elif detected_header_unit:
            unit = detected_header_unit
        elif detected_line_unit:
            unit = detected_line_unit
        else:
            raise ParseError("Ambiguous latency unit in schbench output: no explicit unit found in header or percentile lines")

        # 3. Strict requirement: reject missing or malformed p99
        if "p99" not in percentiles:
            raise ParseError("Missing p99 wakeup latency in schbench output")

        p99_val = percentiles["p99"]
        if not isinstance(p99_val, (int, float)) or math.isnan(p99_val) or math.isinf(p99_val) or p99_val < 0:
            raise ParseError(f"Malformed p99 wakeup latency: {p99_val}")

        metrics: dict[str, Any] = {
            "wall_time_s": round(duration_s, 6),
            "latency_unit": unit,
            **percentiles,
        }

        # 4. Extract min and max if present
        min_max_matches = list(self.MIN_MAX_PATTERN.finditer(target_text))
        if min_max_matches:
            last_mm = min_max_matches[-1]
            try:
                metrics["min"] = float(last_mm.group(1))
                metrics["max"] = float(last_mm.group(2))
            except ValueError:
                pass

        # 5. Extract requests/throughput if present
        rps_matches = list(self.RPS_PATTERN.finditer(target_text))
        if rps_matches:
            last_rps = rps_matches[-1]
            try:
                metrics["requests"] = int(last_rps.group(1))
                metrics["rps"] = float(last_rps.group(2))
            except ValueError:
                pass

        return ParserResult(metrics=metrics, raw_summary=f"p99={metrics['p99']} {metrics['latency_unit']}")
