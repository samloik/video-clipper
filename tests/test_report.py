"""Tests for the JSON report."""

from __future__ import annotations

import json
from pathlib import Path

from video_clipper.cut import ClipResult
from video_clipper.report import build_report, write_json_report
from video_clipper.segments import Segment


def _result(tmp_path: Path) -> ClipResult:
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x" * 10)
    return ClipResult(
        source="/tmp/source.mp4",
        segment=Segment(1.0, 4.0),
        output_path=out,
        duration=3.0,
        mode="accurate",
        size_bytes=out.stat().st_size,
    )


class TestBuildReport:
    def test_structure(self, tmp_path):
        report = build_report("/tmp/source.mp4", [_result(tmp_path)])
        assert report["source"] == "/tmp/source.mp4"
        assert report["total_clips"] == 1
        assert report["total_duration"] == 3.0
        assert report["total_size_bytes"] == 10
        clip = report["clips"][0]
        assert clip["start"] == 1.0 and clip["end"] == 4.0
        assert clip["mode"] == "accurate"
        assert report["dry_run"] is False

    def test_dry_run_flag(self, tmp_path):
        assert build_report("x", [], dry_run=True)["dry_run"] is True

    def test_empty(self):
        report = build_report("x", [])
        assert report["total_clips"] == 0 and report["clips"] == []


class TestWriteJsonReport:
    def test_writes_file(self, tmp_path):
        target = tmp_path / "reports" / "run.json"
        write_json_report(target, [_result(tmp_path)], source="/tmp/source.mp4")
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["total_clips"] == 1
        assert data["clips"][0]["output"].endswith("clip.mp4")
