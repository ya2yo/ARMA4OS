"""Adapt the formal CLI command to the shared experiment engine."""
from __future__ import annotations

import sys
from typing import Any
from .config import load_formal_config

def run_formal(args: Any) -> int:
    from benchmarks.orchestration.engine import run_benchmark
    try:
        config = load_formal_config(args.config, args.scenarios, args.warmup)
        return run_benchmark(args.config, args.output, args.seed, args.repeats,
                             args.executor, args.executor_config, config_override=config)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Formal configuration error: {exc}", file=sys.stderr)
        return 1
