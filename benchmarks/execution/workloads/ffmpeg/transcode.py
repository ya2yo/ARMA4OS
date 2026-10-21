"""Deterministic FFmpeg video transcoding workload for ARMA4OS benchmarks.

Transcodes an input video with specified codec, preset, and thread parallelism.
Outputs FFmpeg stderr progress for FFmpegParser to extract performance metrics.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Sequence


def run_transcode(
    input_path: Path,
    output_path: Path,
    codec: str = "libx264",
    preset: str = "medium",
    threads: int = 0,
    ffmpeg_bin: str = "ffmpeg",
    keep_output: bool = False,
) -> int:
    input_path = input_path.resolve()
    if not input_path.is_file() or input_path.stat().st_size == 0:
        print(f"[ERROR] Input video not found or empty: {input_path}", file=sys.stderr)
        return 1

    ffmpeg_exec = shutil.which(ffmpeg_bin)
    if not ffmpeg_exec:
        print(f"[ERROR] FFmpeg executable not found: {ffmpeg_bin}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    cmd = [
        ffmpeg_exec,
        "-y",
        "-i",
        str(input_path),
        "-c:v",
        codec,
        "-preset",
        preset,
        "-threads",
        str(threads),
        "-an",  # Strip audio for pure CPU video transcoding test
        "-f",
        "mp4",
        str(output_path),
    ]

    start_monotonic = time.monotonic()
    start_unix = time.time()

    # FFmpeg writes progress to stderr
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stderr_chunks: list[str] = []
    # Read stderr line by line and stream to current stderr
    if proc.stderr:
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            stderr_chunks.append(line)

    code = proc.wait()
    end_monotonic = time.monotonic()
    elapsed = end_monotonic - start_monotonic

    if code != 0:
        print(f"\n[ERROR] FFmpeg transcoding failed with exit code {code}", file=sys.stderr)
        return code

    if not output_path.is_file() or output_path.stat().st_size == 0:
        print(f"\n[ERROR] Output video missing or zero bytes: {output_path}", file=sys.stderr)
        return 1

    out_bytes = output_path.stat().st_size
    print(
        f"\nTranscode completed in {elapsed:.2f}s | Output: {out_bytes} bytes",
        file=sys.stdout,
        flush=True,
    )

    if not keep_output:
        # Avoid filling disk across multiple benchmark repetitions
        output_path.unlink()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FFmpeg transcoding workload for CPU benchmarks")
    parser.add_argument("--input", required=True, help="Path to input video file")
    parser.add_argument("--output", help="Path to output video file (optional)")
    parser.add_argument("--codec", default="libx264", help="Video codec (default: libx264)")
    parser.add_argument("--preset", default="medium", help="Encoding preset (default: medium)")
    parser.add_argument("--threads", type=int, default=0, help="Threads (0 for auto)")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="Path to ffmpeg binary")
    parser.add_argument("--keep-output", action="store_true", help="Keep output file after transcoding")

    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if args.output:
        out_path = Path(args.output)
    else:
        trial_dir_env = os.environ.get("ARMA_BENCHMARK_TRIAL_DIR")
        if trial_dir_env:
            out_path = Path(trial_dir_env) / "transcode_out.mp4"
        else:
            temp_dir = tempfile.gettempdir()
            out_path = Path(temp_dir) / f"arma_ffmpeg_out_{os.getpid()}.mp4"

    return run_transcode(
        input_path=input_path,
        output_path=out_path,
        codec=args.codec,
        preset=args.preset,
        threads=args.threads,
        ffmpeg_bin=args.ffmpeg_bin,
        keep_output=args.keep_output,
    )


if __name__ == "__main__":
    sys.exit(main())
