"""Parser for FFmpeg transcoding workload outputs."""

from __future__ import annotations

import math
import re
from typing import Any

from benchmarks.evidence.parsers.base import ParseError, ParserResult


class FFmpegParser:
    """Extract transcoding metrics (FPS, speed ratio, encoded frames, bitrate) from FFmpeg stderr."""

    PROGRESS_LINE_RE = re.compile(
        r"frame=\s*(\d+)\s+fps=\s*([0-9.]+).*?(?:size|Lsize)=\s*([0-9]+)(kB|KiB|B|MB).*?bitrate=\s*([0-9.]+)\s*kbits/s.*?speed=\s*([0-9.]+)x",
        re.IGNORECASE,
    )
    FRAME_RE = re.compile(r"\bframe=\s*(\d+)", re.IGNORECASE)
    FPS_RE = re.compile(r"\bfps=\s*([0-9.]+)", re.IGNORECASE)
    SPEED_RE = re.compile(r"\bspeed=\s*([0-9.]+)x", re.IGNORECASE)
    BITRATE_RE = re.compile(r"\bbitrate=\s*([0-9.]+)\s*kbits/s", re.IGNORECASE)
    SIZE_RE = re.compile(r"\b(?:size|Lsize)=\s*([0-9]+)\s*(kB|KiB|B|MB)", re.IGNORECASE)

    def parse(self, stdout: str, stderr: str, duration_s: float, exit_code: int) -> ParserResult:
        if exit_code != 0:
            raise ParseError(f"FFmpeg process exited with non-zero exit code: {exit_code}")

        combined = stderr + "\n" + stdout
        if not combined.strip():
            raise ParseError("FFmpeg output is empty")

        # Find all lines with progress indicators
        progress_lines = [
            line.strip()
            for line in combined.splitlines()
            if "fps=" in line.lower() and "speed=" in line.lower()
        ]

        if not progress_lines:
            raise ParseError("No FFmpeg progress reporting lines found in output")

        last_line = progress_lines[-1]

        # Extract frames
        frame_match = list(self.FRAME_RE.finditer(last_line))
        if not frame_match:
            raise ParseError("Missing encoded frame count in FFmpeg output")
        encoded_frames = int(frame_match[-1].group(1))

        # Extract average fps
        fps_match = list(self.FPS_RE.finditer(last_line))
        if not fps_match:
            raise ParseError("Missing fps metric in FFmpeg output")
        avg_fps = float(fps_match[-1].group(1))

        # Extract speed ratio
        speed_match = list(self.SPEED_RE.finditer(last_line))
        if not speed_match:
            raise ParseError("Missing speed ratio in FFmpeg output")
        speed_ratio = float(speed_match[-1].group(1))

        metrics: dict[str, Any] = {
            "transcode_time_s": duration_s,
            "encoded_frames": encoded_frames,
            "fps": avg_fps,
            "speed": speed_ratio,
        }

        # Optional bitrate
        bitrate_match = list(self.BITRATE_RE.finditer(last_line))
        if bitrate_match:
            metrics["bitrate_kbps"] = float(bitrate_match[-1].group(1))

        # Check finite
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if not math.isfinite(v):
                    raise ParseError(f"Non-finite FFmpeg metric: {k}={v}")

        summary = f"fps={avg_fps:.1f}, speed={speed_ratio:.2f}x, frames={encoded_frames}, time={duration_s:.2f}s"
        return ParserResult(metrics=metrics, raw_summary=summary)
