"""Bounded raw-output reads shared by execution and offline reconstruction."""

from pathlib import Path

from benchmarks.evidence.parsers.base import ParseError

MAX_OUTPUT_BYTES: int = 16 * 1024 * 1024  # 16 MiB memory safety cap


def read_bounded_file(path: Path, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """Read file content with strict size cap, preventing unbounded memory consumption."""
    if not path.exists():
        return ""
    size = path.stat().st_size
    if size > max_bytes:
        raise ParseError(f"Output file '{path.name}' ({size} bytes) exceeds limit ({max_bytes} bytes)")
    with path.open("r", encoding="utf-8", errors="replace") as f:
        content = f.read(max_bytes + 1)
        if len(content.encode("utf-8", errors="replace")) > max_bytes:
            raise ParseError(f"Output file '{path.name}' exceeds limit ({max_bytes} bytes)")
        return content
