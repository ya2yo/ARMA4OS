"""Parser contracts and dispatch; concrete parsers load on demand."""

from benchmarks.evidence.parsers.base import ParseError, Parser, ParserResult
from benchmarks.evidence.parsers.dispatch import get_parser, parse_trial_evidence

__all__ = ["ParseError", "Parser", "ParserResult", "get_parser", "parse_trial_evidence"]
