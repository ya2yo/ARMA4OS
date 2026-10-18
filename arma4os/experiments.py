"""Tool adapter for registered experiment configs; callers cannot supply commands."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .contracts import identifier


class Experiments:
    def __init__(self, configs: dict[str, Path], output: Path):
        self.configs = {k: str(v.resolve(strict=True)) for k, v in configs.items()}
        self.output = output.resolve()
        self.runs: dict[str, dict] = {}

    def run(self, experiment_id: str, timeout_ms: int):
        if experiment_id not in self.configs:
            raise ValueError("unregistered experiment")
        run_id = identifier()
        directory = self.output / run_id
        directory.mkdir(parents=True, exist_ok=False)
        command = [sys.executable, "-m", "benchmarks", "run", "--config", self.configs[experiment_id],
                   "--output", str(directory / "matrix"), "--repeats", "1", "--seed", "42"]
        with (directory / "stdout").open("wb") as stdout, (directory / "stderr").open("wb") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=timeout_ms / 1000)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        result = {"run_id": run_id, "path": str(directory), "exit_code": process.returncode,
                  "timed_out": timed_out, "valid": not timed_out and process.returncode == 0}
        self.runs[run_id] = result
        (directory / "result.json").write_text(json.dumps(result) + "\n")
        return result

    def result(self, run_id: str):
        return self.runs[run_id]
