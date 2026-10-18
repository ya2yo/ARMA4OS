"""procfs/cgroup sampling with explicit units, missing values and KPI timestamps."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .contracts import Metric, Observation, identifier, now_ms


class ProcCollector:
    def __init__(self, proc_root: Path = Path("/proc"), cgroup: Path | None = None,
                 application_metrics: Path | None = None, scope: str = "global"):
        self.proc = proc_root
        self.cgroup = cgroup
        self.app = application_metrics
        self.scope = scope
        self.previous: tuple[float, dict[str, int]] | None = None

    def snapshot(self) -> Observation:
        stamp, monotonic = now_ms(), time.monotonic()
        metrics: dict[str, Metric] = {}

        def add(name: str, value: float | None, unit: str, source: str) -> None:
            metrics[name] = Metric(value, unit, source, stamp, "valid" if value is not None else "missing")

        counters: dict[str, int] = {}
        try:
            lines = (self.proc / "stat").read_text().splitlines()
            cpu = [int(v) for v in lines[0].split()[1:9]]  # guest already included in user/nice
            counters.update(total=sum(cpu), idle=cpu[3] + cpu[4], user=cpu[0] + cpu[1], system=cpu[2] + cpu[5] + cpu[6])
            for line in lines:
                parts = line.split()
                if parts and parts[0] == "ctxt":
                    counters["ctxt"] = int(parts[1])
            old = self.previous[1] if self.previous else {}
            total = counters["total"] - old.get("total", counters["total"])
            for name in ["user", "system", "idle"]:
                delta = counters[name] - old.get(name, counters[name])
                add(f"cpu.{name}", delta / total if total > 0 and 0 <= delta <= total else None,
                    "fraction", "procfs:/proc/stat")
            idle = metrics["cpu.idle"].value
            add("cpu.utilization", 1 - idle if idle is not None else None, "fraction", "procfs:/proc/stat")
            dt = monotonic - self.previous[0] if self.previous else 0
            delta = counters.get("ctxt", 0) - old.get("ctxt", counters.get("ctxt", 0))
            add("cpu.context_switches", delta / dt if dt > 0 and delta >= 0 else None, "1/s", "procfs:/proc/stat")
        except (OSError, ValueError, IndexError):
            for name in ["user", "system", "idle", "utilization"]:
                add(f"cpu.{name}", None, "fraction", "procfs:/proc/stat")
        self.previous = (monotonic, counters)
        try:
            some = next(s for s in (self.proc / "pressure/cpu").read_text().splitlines() if s.startswith("some "))
            values = dict(v.split("=", 1) for v in some.split()[1:])
            pressure = float(values["avg10"]) / 100
            add("cpu.pressure", pressure if 0 <= pressure <= 1 else None, "fraction", "procfs:/proc/pressure/cpu")
        except (OSError, ValueError, KeyError, StopIteration):
            add("cpu.pressure", None, "fraction", "procfs:/proc/pressure/cpu")
        try:
            add("cpu.runnable", float((self.proc / "loadavg").read_text().split()[3].split("/")[0]), "tasks", "procfs:/proc/loadavg")
        except (OSError, ValueError, IndexError):
            add("cpu.runnable", None, "tasks", "procfs:/proc/loadavg")
        if self.cgroup:
            try:
                values = dict(s.split() for s in (self.cgroup / "cpu.stat").read_text().splitlines())
                for key, unit in [("usage_usec", "us"), ("throttled_usec", "us"), ("nr_throttled", "count")]:
                    add(f"cgroup.{key}", float(values[key]), unit, "cgroupv2:cpu.stat")
            except (OSError, KeyError, ValueError):
                add("cgroup.usage_usec", None, "us", "cgroupv2:cpu.stat")
        if self.app:
            try:
                data = json.loads(self.app.read_text())
                if data["scope"] != self.scope:
                    raise ValueError("KPI scope mismatch")
                application = {}
                for key, value in data["metrics"].items():
                    if not key.startswith("app."):
                        raise ValueError("application metrics must use app. prefix")
                    application[key] = Metric(**value)
                metrics.update(application)
            except (OSError, ValueError, TypeError, KeyError):
                add("app.input", None, "status", "application_metrics_file")
        window = int((monotonic - getattr(self, "last_time", monotonic)) * 1000)
        self.last_time = monotonic
        return Observation(identifier(), stamp, window, self.scope, metrics)
