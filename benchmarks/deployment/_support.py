"""Small shared helpers for deployment evidence and validation."""
from __future__ import annotations

import json
from pathlib import Path

def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)

def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
