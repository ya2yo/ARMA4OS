"""Source snapshots and validated archive extraction for guest deployments."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

from benchmarks.deployment._support import require

SOURCE_ROOTS = {"arma4os", "benchmarks", "crates", "configs", "schemas", "hooks", "tests", "scripts", "docs", ".github"}

SOURCE_FILES = {"Makefile", "Cargo.toml", "Cargo.lock", "pyproject.toml", "README.md", "AGENTS.md", "LICENSE", ".gitignore"}

SOURCE_SUFFIXES = {".py", ".rs", ".toml", ".lock", ".json", ".c", ".h", ".md", ".yaml", ".yml", ".sh"}

EXCLUDED_PARTS = {"target", "build", "results", "__pycache__", ".git", ".venv", "node_modules"}

def source_files(root: Path) -> list[Path]:
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
    selected = []
    for raw in names.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts or EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if relative.as_posix() not in SOURCE_FILES and not (
                relative.parts[0] in SOURCE_ROOTS and relative.suffix in SOURCE_SUFFIXES):
            continue
        path = root / relative
        require(not any(p.is_symlink() for p in [path, *path.parents] if p != root.parent),
                f"Source symlink is not supported: {relative}")
        if path.is_file():
            selected.append(relative)
    require(Path("benchmarks/deployment/vm.py") in selected, "Remote harness missing from source snapshot")
    return sorted(set(selected))

def snapshot(root: Path, archive: Path) -> dict:
    # Hash and archive the same bytes even if an editor saves during packing.
    metadata = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                "working_tree": subprocess.check_output(["git", "status", "--short"], cwd=root, text=True),
                "files": {}}
    with tarfile.open(archive, "w:gz") as tar:
        for name in source_files(root):
            data = (root / name).read_bytes()
            metadata["files"][name.as_posix()] = hashlib.sha256(data).hexdigest()
            entry = tarfile.TarInfo(name.as_posix())
            entry.size = len(data)
            entry.mode = (root / name).stat().st_mode & 0o777
            tar.addfile(entry, io.BytesIO(data))
        data = (json.dumps(metadata, indent=2) + "\n").encode()
        entry = tarfile.TarInfo("deployment-source.json")
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))
    return metadata

def extract_files(archive: Path, destination: Path) -> None:
    """Only regular files and directories; reject links and path traversal."""
    with tarfile.open(archive) as tar:
        seen = set()
        for entry in tar.getmembers():
            path = PurePosixPath(entry.name)
            require(not path.is_absolute() and ".." not in path.parts and entry.name not in seen and
                    (entry.isfile() or entry.isdir()), f"Unsafe archive entry: {entry.name}")
            seen.add(entry.name)
        for entry in tar.getmembers():
            target = destination / entry.name
            require(not any(p.is_symlink() for p in [target, *target.parents]), "Archive destination contains a symlink")
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)

BOOTSTRAP = r'''
import hashlib,json,pathlib,tarfile
archive=pathlib.Path(ARCHIVE)
if hashlib.sha256(archive.read_bytes()).hexdigest()!=DIGEST:raise RuntimeError("Source archive hash mismatch")
root=pathlib.Path(DESTINATION);root.mkdir(parents=True,exist_ok=False)
with tarfile.open(archive) as t:
 for e in t.getmembers():
  p=pathlib.PurePosixPath(e.name)
  if p.is_absolute() or ".." in p.parts or not e.isfile():raise RuntimeError("Unsafe source archive")
  dest=root/e.name;dest.parent.mkdir(parents=True,exist_ok=True)
  with t.extractfile(e) as src,dest.open("xb") as out:out.write(src.read())
meta=json.loads((root/"deployment-source.json").read_text())
for name,digest in meta["files"].items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise RuntimeError("Source file mismatch: "+name)
print("Verified source files:",len(meta["files"]))
'''
