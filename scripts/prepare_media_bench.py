#!/usr/bin/env python3
"""Prepare media assets and FFmpeg transcoding benchmarks for local host or guest VM."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Sequence

# Ensure project root is in sys.path when executed directly
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.vm_smoke import Connection

DEFAULT_HOST = "root@192.168.66.128"
DEFAULT_DEST_ROOT = "/srv/bench/media"
DEFAULT_VIDEO_NAME = "bbb_sample.mp4"
DEFAULT_LOCAL_CONFIG = "configs/benchmarks.ffmpeg.local.json"
DEFAULT_ONLINE_URL = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"


def run_local_cmd(cmd: list[str]) -> tuple[int, str, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode, res.stdout, res.stderr
    except Exception as exc:
        return -1, "", str(exc)


def build_guest_media_script(
    dest_dir: str,
    video_name: str,
    download_url: str | None = None,
    duration_s: int = 30,
) -> str:
    target_video = f"{dest_dir}/{video_name}"
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"echo '=== [1/3] Preparing media directory {dest_dir} ==='",
        f"mkdir -p '{dest_dir}'",
        "",
        "echo '=== [2/3] Checking FFmpeg binary ==='",
        "if ! command -v ffmpeg >/dev/null 2>&1; then",
        "    echo 'Installing ffmpeg via package manager...'",
        "    if command -v dnf >/dev/null 2>&1; then",
        "        dnf install -y ffmpeg || true",
        "    elif command -v apt-get >/dev/null 2>&1; then",
        "        apt-get update -y && apt-get install -y ffmpeg || true",
        "    fi",
        "fi",
        "if ! command -v ffmpeg >/dev/null 2>&1; then",
        "    echo '[ERROR] FFmpeg not found and could not be installed automatically.' >&2",
        "    exit 1",
        "fi",
        "",
        "echo '=== [3/3] Preparing benchmark input video ==='",
        f"if [ -f '{target_video}' ] && [ -s '{target_video}' ]; then",
        f"    echo 'Test video already exists at {target_video}.'",
        "else",
    ]

    if download_url:
        lines.extend([
            f"    echo 'Downloading video from {download_url}...'",
            f"    if command -v curl >/dev/null 2>&1; then",
            f"        curl -L -o '{target_video}' '{download_url}'",
            f"    elif command -v wget >/dev/null 2>&1; then",
            f"        wget -O '{target_video}' '{download_url}'",
            "    else",
            "        echo 'Neither curl nor wget available for download' >&2",
            "        exit 1",
            "    fi",
        ])
    else:
        # Generate a standard 1080p 30fps deterministic synthetic video via lavfi
        lines.extend([
            f"    echo 'Synthesizing standard 1080p test video ({duration_s}s, 30fps) with FFmpeg lavfi...'",
            f"    ffmpeg -y -f lavfi -i testsrc=duration={duration_s}:size=1920x1080:rate=30 \\",
            f"        -c:v libx264 -preset ultrafast -pix_fmt yuv420p '{target_video}'",
        ])

    lines.extend([
        "fi",
        f"echo 'Input video ready: {target_video} (size: ' $(stat -c %s '{target_video}') 'bytes)'",
        "echo '=== Media Preparation Completed Successfully ==='",
    ])
    return "\n".join(lines)


def generate_ffmpeg_config(
    output_path: Path,
    input_video_path: str,
    preset: str = "medium",
    codec: str = "libx264",
) -> None:
    cfg = {
        "schema_version": 1,
        "policies": ["default"],
        "timeout_sec": 300,
        "warmup": 1,
        "description": "FFmpeg 1080p video transcoding workload for CPU scheduling evaluation",
        "workloads": [
            {
                "name": "ffmpeg_transcode_1080p",
                "command": [
                    sys.executable,
                    "-m",
                    "benchmarks.workloads.ffmpeg_transcode",
                    "--input",
                    input_video_path,
                    "--codec",
                    codec,
                    "--preset",
                    preset,
                ],
                "parser": "ffmpeg",
                "timeout_sec": 300.0,
                "warmup": 1,
            }
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"\n[OK] Local FFmpeg benchmark configuration created: {output_path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare video assets and generate FFmpeg benchmark config.",
    )
    parser.add_argument("--local", action="store_true", help="Run setup locally on current host")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"SSH host for remote VM (default: {DEFAULT_HOST})")
    parser.add_argument(
        "--transport",
        choices=["auto", "windows", "native"],
        default="auto",
        help="SSH transport mechanism",
    )
    parser.add_argument(
        "--dest-dir",
        default=DEFAULT_DEST_ROOT,
        help=f"Media destination directory (default: {DEFAULT_DEST_ROOT})",
    )
    parser.add_argument(
        "--video-name",
        default=DEFAULT_VIDEO_NAME,
        help=f"Target filename for input video (default: {DEFAULT_VIDEO_NAME})",
    )
    parser.add_argument(
        "--download-url",
        help="Optional URL to download real video (defaults to generating standard testsrc video)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Synthetic video duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        help="FFmpeg encoding preset for workload (default: medium)",
    )
    parser.add_argument(
        "--output-config",
        default=DEFAULT_LOCAL_CONFIG,
        help=f"Path to local configuration JSON to generate (default: {DEFAULT_LOCAL_CONFIG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated script without executing it",
    )

    args = parser.parse_args(argv)

    script = build_guest_media_script(
        dest_dir=args.dest_dir,
        video_name=args.video_name,
        download_url=args.download_url,
        duration_s=args.duration,
    )

    target_video_path = f"{args.dest_dir}/{args.video_name}"

    if args.dry_run:
        print("[DRY-RUN] Generated media preparation script:")
        print("-" * 60)
        print(script)
        print("-" * 60)
        return 0

    if args.local:
        print("Running media setup locally...")
        res = subprocess.run(["bash", "-c", script], check=False)
        if res.returncode != 0:
            print(f"[ERROR] Local media preparation failed with code {res.returncode}", file=sys.stderr)
            return res.returncode
    else:
        print(f"Connecting to test guest: {args.host} (transport: {args.transport})...")
        conn = Connection(args.host, args.transport)
        try:
            from scripts.prepare_kernel_bench import (
                build_remote_bash_command,
                probe_guest_environment,
                run_remote_streaming,
            )
            probe_guest_environment(conn)
        except RuntimeError as exc:
            print(f"\n[ERROR] Connectivity check failed:\n{exc}", file=sys.stderr)
            return 1

        print(f"Executing media preparation on {args.host}...")
        remote_cmd = build_remote_bash_command(script)
        code = run_remote_streaming(conn, remote_cmd)
        if code != 0:
            print(f"\n[ERROR] Remote media preparation failed with code {code}", file=sys.stderr)
            return code

    generate_ffmpeg_config(
        output_path=Path(args.output_config).resolve(),
        input_video_path=target_video_path,
        preset=args.preset,
    )

    print("\n" + "=" * 80)
    print("Media setup completed! You can run the FFmpeg benchmark with:")
    print(f"  python3 -m benchmarks run --config {args.output_config} \\")
    print("    --output results/ffmpeg-benchmark-001 --repeats 5 --seed 42")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
