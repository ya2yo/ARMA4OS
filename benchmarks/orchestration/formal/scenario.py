"""A/B/C child orchestration; parent runner owns process group and deadline."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from benchmarks.execution.workloads.kernel_build.build import prepare_build, start_build, collect_build_result
from benchmarks.evidence.provenance import sha256_file, source_metadata


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run(scenario: str, spec: dict[str, Any], root: Path) -> None:
    root.mkdir(exist_ok=False)
    write_json(root / "spec.json", {"scenario": scenario, "spec": spec})
    for key, value in spec["build_environment"].items():
        if os.environ.get(key) != value:
            raise ValueError(f"Build environment changed: {key}")
    prepared = None
    if scenario in ("kernel_build", "mixed"):
        kernel = spec["kernel"]
        if sha256_file(Path(kernel["config"])) != kernel["input_config_sha256"]:
            raise ValueError("Kernel input configuration changed after freeze")
        if sha256_file(Path(kernel["make"])) != kernel["make_sha256"]:
            raise ValueError("make binary changed after freeze")
        if source_metadata(Path(kernel["source"])) != kernel["source_metadata"]:
            raise ValueError("Kernel source changed after freeze")
        prepared = prepare_build(Path(kernel["source"]), Path(kernel["config"]),
                                 root / "kernel", kernel["jobs"], kernel["make"])
    children: dict[str, tuple[Any, Any, Any, dict[str, Any]]] = {}
    try:
        if prepared:
            process, stdout, stderr = start_build(prepared)
            timing = {"start_time_unix": prepared["_build_start_time_unix"],
                      "start_monotonic": prepared["_build_start_monotonic"], "command": prepared["command"]}
            children["kernel"] = (process, stdout, stderr, timing)
            write_json(root / "kernel.started.json", timing)
        if scenario in ("schbench", "mixed"):
            sch = spec["schbench"]
            if sha256_file(Path(sch["binary"])) != sch["sha256"]:
                raise ValueError("schbench binary changed after freeze")
            command = [sch["binary"], *sch["args"], "-r", str(sch["runtime_sec"])]
            stdout = (root / "schbench.stdout.log").open("xb")
            stderr = (root / "schbench.stderr.log").open("xb")
            timing = {"start_time_unix": time.time(), "start_monotonic": time.monotonic(), "command": command}
            try:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            except BaseException:
                stdout.close()
                stderr.close()
                raise
            children["schbench"] = (process, stdout, stderr, timing)
            write_json(root / "schbench.started.json", timing)
        failed = False
        pending = set(children)
        last_alive = {name: child[3]["start_monotonic"] for name, child in children.items()}
        while pending:
            for name in list(pending):
                process, stdout, stderr, timing = children[name]
                observed_at = time.monotonic()
                code = process.poll()
                if code is None:
                    last_alive[name] = observed_at
                    continue
                end = time.monotonic()
                timing.update(end_lower_bound_monotonic=last_alive[name], end_monotonic=end, end_time_unix=time.time(), exit_code=code,
                              elapsed_s=end - timing["start_monotonic"])
                stdout.close()
                stderr.close()
                if name == "kernel":
                    assert prepared is not None
                    result = collect_build_result(prepared, timing["start_time_unix"], timing["start_monotonic"], code)
                    failed = failed or not result["success"]
                failed = failed or code != 0
                write_json(root / f"{name}.execution.json", timing)
                pending.remove(name)
            if pending:
                time.sleep(0.005)
        if failed:
            raise RuntimeError("Component failed; retained component execution records and logs")
        # Parse through exactly the same raw-evidence path used by summarize.
        from benchmarks.orchestration.formal.parsing import parse_evidence
        parse_evidence(root, {"scenario": scenario, "formal_spec": spec})
        print("Formal task completed; component raw evidence retained", flush=True)
    finally:
        # On ordinary errors terminate and reap children; outer runner kills the
        # entire inherited process group, including compiler descendants.
        for process, stdout, stderr, _ in children.values():
            if process.poll() is None:
                process.kill()
            process.wait()
            stdout.close()
            stderr.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=["kernel_build", "schbench", "mixed"],
        required=True,
    )
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    try:
        run(args.scenario, json.loads(args.spec), Path(os.environ["ARMA_BENCHMARK_TRIAL_DIR"]) / "components")
        return 0
    except Exception as exc:
        print(f"Formal task failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
