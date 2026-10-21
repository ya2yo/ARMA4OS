"""File, toolchain, and source provenance shared by suites and evidence readers."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(command: list[str]) -> dict[str, Any]:
    """Keep missing tools and failed provenance queries explicit."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        return {"command": command, "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "exit_code": None, "error": str(exc)}


def source_metadata(source: Path) -> dict[str, Any]:
    makefile = source / "Makefile"
    version: dict[str, str] = {}
    for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() in ("VERSION", "PATCHLEVEL", "SUBLEVEL", "EXTRAVERSION"):
            version[name.strip()] = value.strip()
    metadata: dict[str, Any] = {"path": str(source), "makefile_sha256": sha256_file(makefile),
                                "version_fields": version}
    root = probe(["git", "-C", str(source), "rev-parse", "--show-toplevel"])
    metadata["git_root_query"] = root
    if root.get("exit_code") == 0 and Path(root["stdout"].strip()).resolve() == source:
        metadata["git_commit"] = probe(["git", "-C", str(source), "rev-parse", "HEAD"])
        status = probe(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"])
        metadata["git_status"] = status
        metadata["dirty"] = bool(status.get("stdout")) if status.get("exit_code") == 0 else None
        diff = probe(["git", "-C", str(source), "diff", "--binary", "HEAD", "--"])
        metadata["tracked_diff_sha256"] = (
            hashlib.sha256(diff["stdout"].encode()).hexdigest() if diff.get("exit_code") == 0 else None
        )
        metadata["fingerprint_scope"] = "Git HEAD, tracked diff, status, and Makefile; untracked file contents are not hashed."
    else:
        metadata["dirty"] = None
        metadata["provenance_note"] = "Source is not a Git repository root; full source identity is not verified."
    return metadata
