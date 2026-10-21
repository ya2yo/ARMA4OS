"""Parsers for benchmark trial outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ParseError(Exception):
    """Raised when workload output cannot be parsed or lacks required metrics."""
    pass


@dataclass
class ParserResult:
    """Standard parsed metric result for a trial."""
    metrics: dict[str, Any] = field(default_factory=dict)
    raw_summary: str = ""


class Parser(Protocol):
    """Protocol for workload output parsers."""

    def parse(self, stdout: str, stderr: str, duration_s: float, exit_code: int) -> ParserResult:
        """Parse process output and return metrics dictionary."""
        ...
