"""Tests for FFmpeg workload, FFmpegParser, and media preparation script."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from benchmarks.evidence.parsers import ParseError, get_parser
from benchmarks.evidence.parsers.ffmpeg import FFmpegParser
from benchmarks.execution.workloads.ffmpeg import transcode as ffmpeg_transcode
from scripts.prepare_media_bench import (
    build_guest_media_script,
    generate_ffmpeg_config,
    main as media_main,
)


class FFmpegParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = FFmpegParser()
        self.sample_stderr = (
            "ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers\n"
            "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'input.mp4':\n"
            "  Duration: 00:00:30.00, start: 0.000000, bitrate: 1250 kb/s\n"
            "Stream mapping:\n"
            "  Stream #0:0 -> #0:0 (h264 (native) -> h264 (libx264))\n"
            "frame=  150 fps= 42.1 q=28.0 size=     850kB time=00:00:05.00 bitrate=1392.6kbits/s speed=1.40x    \n"
            "frame=  450 fps= 45.8 q=28.0 size=    2600kB time=00:00:15.00 bitrate=1420.1kbits/s speed=1.53x    \n"
            "frame=  900 fps= 48.5 q=-1.0 Lsize=    5200kB time=00:00:29.96 bitrate=1422.0kbits/s speed=1.62x    \n"
            "video:5100kB audio:0kB subtitle:0kB other streams:0kB global headers:0kB muxing overhead: 0.52%\n"
        )

    def test_parser_registered(self) -> None:
        parser = get_parser("ffmpeg")
        self.assertIsInstance(parser, FFmpegParser)

    def test_parse_successful_output(self) -> None:
        result = self.parser.parse("", self.sample_stderr, duration_s=18.5, exit_code=0)
        metrics = result.metrics
        self.assertEqual(metrics["encoded_frames"], 900)
        self.assertAlmostEqual(metrics["fps"], 48.5)
        self.assertAlmostEqual(metrics["speed"], 1.62)
        self.assertAlmostEqual(metrics["transcode_time_s"], 18.5)
        self.assertAlmostEqual(metrics["bitrate_kbps"], 1422.0)
        self.assertIn("fps=48.5", result.raw_summary)

    def test_parse_nonzero_exit_code_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse("", self.sample_stderr, duration_s=5.0, exit_code=1)
        self.assertIn("non-zero exit code", str(ctx.exception))

    def test_parse_empty_output_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse("", "   \n\t", duration_s=5.0, exit_code=0)
        self.assertIn("empty", str(ctx.exception))

    def test_parse_missing_progress_lines_raises(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse("", "just some header without progress", duration_s=5.0, exit_code=0)
        self.assertIn("No FFmpeg progress reporting lines", str(ctx.exception))


class FFmpegWorkloadTests(unittest.TestCase):
    def test_input_not_found(self) -> None:
        ret = ffmpeg_transcode.run_transcode(
            input_path=Path("/nonexistent/video.mp4"),
            output_path=Path("/tmp/out.mp4"),
        )
        self.assertNotEqual(ret, 0)

    @patch("benchmarks.execution.workloads.ffmpeg.transcode.shutil.which", return_value=None)
    def test_ffmpeg_not_found(self, mock_which: MagicMock) -> None:
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(b"data")
            tmp.flush()
            ret = ffmpeg_transcode.run_transcode(
                input_path=Path(tmp.name),
                output_path=Path("/tmp/out.mp4"),
            )
            self.assertNotEqual(ret, 0)


class MediaScriptTests(unittest.TestCase):
    def test_build_guest_media_script_synthetic(self) -> None:
        script = build_guest_media_script(
            dest_dir="/srv/bench/media",
            video_name="bbb_sample.mp4",
            download_url=None,
            duration_s=25,
        )
        self.assertIn("mkdir -p '/srv/bench/media'", script)
        self.assertIn("lavfi -i testsrc=duration=25", script)
        self.assertIn("/srv/bench/media/bbb_sample.mp4", script)

    def test_build_guest_media_script_download(self) -> None:
        script = build_guest_media_script(
            dest_dir="/srv/bench/media",
            video_name="custom.mp4",
            download_url="https://example.com/test.mp4",
        )
        self.assertIn("curl -L -o '/srv/bench/media/custom.mp4'", script)

    def test_generate_ffmpeg_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_cfg = Path(tmpdir) / "ffmpeg.json"
            generate_ffmpeg_config(
                output_path=out_cfg,
                input_video_path="/srv/bench/media/sample.mp4",
                preset="fast",
            )
            self.assertTrue(out_cfg.is_file())
            data = json.loads(out_cfg.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            workload = data["workloads"][0]
            self.assertEqual(workload["parser"], "ffmpeg")
            self.assertIn("--preset", workload["command"])
            self.assertIn("fast", workload["command"])


if __name__ == "__main__":
    unittest.main()
