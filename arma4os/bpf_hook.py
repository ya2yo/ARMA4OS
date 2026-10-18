"""Optional real, allow-all cgroup_skb ingress accounting hook using bpftool.

Object path, bpftool and cgroups are trusted deployment configuration. The caller
can select only a configured scope. Nothing is compiled or attached automatically.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path


class BpfCounterHook:
    def __init__(self, authorized: dict[str, Path], object_file: Path, pin_root: Path,
                 bpftool: Path = Path("/usr/sbin/bpftool")):
        self.authorized = {key: Path(p).resolve(strict=True) for key, p in authorized.items()}
        self.object = Path(object_file).resolve(strict=True)
        self.bpftool = Path(bpftool).resolve(strict=True)
        self.pin_root = Path(pin_root)
        if not self.pin_root.is_absolute():
            raise ValueError("pin_root must be absolute")
        self.managed: dict[str, tuple[Path, int]] = {}

    def command(self, *args, json_output=False):
        result = subprocess.run([str(self.bpftool), *(["-j"] if json_output else []), *map(str, args)],
                                capture_output=True, text=True, timeout=5, check=True)
        return json.loads(result.stdout) if json_output else result.stdout

    def attached(self, scope):
        if scope not in self.authorized:
            raise ValueError("unauthorized cgroup scope")
        return self.command("cgroup", "show", self.authorized[scope], json_output=True)

    @staticmethod
    def ingress(entries):
        return [e for e in entries if e.get("attach_type") in {"ingress", "cgroup_inet_ingress"}]

    def attach(self, scope, params, dry_run=False):
        if params:
            raise ValueError("network counter accepts no mutable parameters")
        entries = self.ingress(self.attached(scope))
        if scope in self.managed:
            return self.stats(scope)
        if entries:
            raise ValueError("external ingress program present; refusing conflict")
        if dry_run:
            return {"dry_run": True, "backend": "bpftool", "authorized": True}
        self.pin_root.mkdir(parents=True, exist_ok=True)
        directory = self.pin_root / str(uuid.uuid4())
        directory.mkdir(exist_ok=False)
        maps = directory / "maps"
        maps.mkdir()
        programs = directory / "programs"
        attached = False
        try:
            self.command("prog", "loadall", self.object, programs, "type", "cgroup_skb", "pinmaps", maps)
            program = programs / "count_ingress"
            info = self.command("prog", "show", "pinned", program, json_output=True)
            if isinstance(info, list):
                info = info[0]
            program_id = info["id"]
            self.managed[scope] = (directory, program_id)
            self.command("cgroup", "attach", self.authorized[scope], "ingress", "pinned", program)
            attached = True
            if not any(e.get("id") == program_id for e in self.ingress(self.attached(scope))):
                raise RuntimeError("kernel did not report expected attached program")
            return self.stats(scope)
        except BaseException:
            if attached:
                self.detach(scope)
            else:
                self.managed.pop(scope, None)
                self.remove_pins(directory)
            raise

    @staticmethod
    def remove_pins(directory):
        for sub in [directory / "programs", directory / "maps"]:
            if sub.exists():
                for p in sub.iterdir():
                    p.unlink()
                sub.rmdir()
        directory.rmdir()

    def detach(self, scope, dry_run=False):
        self.attached(scope)  # enforce authorization even for an unmanaged scope
        if scope not in self.managed:
            return {"attached": False, "backend": "bpftool", "changed": False}
        directory, program_id = self.managed[scope]
        entries = self.ingress(self.attached(scope))
        ours = any(e.get("id") == program_id for e in entries)
        if dry_run:
            return {"dry_run": True, "attached": ours, "backend": "bpftool"}
        if ours:
            self.command("cgroup", "detach", self.authorized[scope], "ingress", "pinned", directory / "programs/count_ingress")
            if any(e.get("id") == program_id for e in self.ingress(self.attached(scope))):
                raise RuntimeError("program detach verification failed")
        self.remove_pins(directory)
        del self.managed[scope]
        return {"attached": False, "backend": "bpftool", "verified": True}

    def stats(self, scope):
        entries = self.ingress(self.attached(scope))
        if scope not in self.managed:
            return {"attached": False, "backend": "bpftool", "packets": None, "bytes": None}
        directory, program_id = self.managed[scope]
        return {"attached": any(e.get("id") == program_id for e in entries), "program_id": program_id,
                "backend": "bpftool", "raw_map": self.command("map", "dump", "pinned", directory / "maps/counters", json_output=True),
                "source": "per-CPU counters; aggregate values across CPUs before reporting totals"}

    def close(self):
        for scope in list(self.managed):
            self.detach(scope)
