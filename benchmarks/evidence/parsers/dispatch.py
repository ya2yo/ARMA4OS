"""Dispatch raw trial evidence through the same parser for execution and reconstruction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks.evidence.parsers.base import Parser, ParserResult


def get_parser(name: str) -> Parser:
    """Return a text-output parser; formal suites require trial evidence context."""
    from benchmarks.evidence.parsers.elapsed import ElapsedParser
    from benchmarks.evidence.parsers.ffmpeg import FFmpegParser
    from benchmarks.evidence.parsers.schbench import SchbenchParser

    registry: dict[str, type[Parser]] = {
        "elapsed": ElapsedParser,
        "schbench": SchbenchParser,
        "ffmpeg": FFmpegParser,
    }
    if name not in registry:
        raise ValueError(f"Unknown parser: {name}. Available: {list(registry.keys())}")
    return registry[name]()


def parse_trial_evidence(
    parser_name: str,
    trial_dir: Path,
    stdout: str,
    stderr: str,
    duration_s: float,
    exit_code: int,
    metadata: dict[str, Any] | None = None,
) -> ParserResult:
    """Parse text outputs or suite-owned component evidence without running workloads."""
    if parser_name == "formal":
        from benchmarks.orchestration.formal.parsing import parse_evidence

        return parse_evidence(trial_dir / "components", metadata or {})
    return get_parser(parser_name).parse(stdout, stderr, duration_s, exit_code)
