"""Tests for ffmpeg command building and real cutting on synthetic video."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_clipper.cut import CutError, build_ffmpeg_command, cut_segment, remove_source
from video_clipper.segments import Segment

FFMPEG = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not FFMPEG, reason="нужен ffmpeg")


@pytest.fixture(scope="module")
def ten_seconds(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "ten.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


class TestCommand:
    def test_accurate_default(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(1, 2))
        assert cmd[0] == "ffmpeg"
        assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "1.000"
        assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "1.000"
        assert "libx264" in cmd and "aac" in cmd
        assert "copy" not in cmd

    def test_fast_uses_stream_copy(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(1, 2), fast=True)
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        assert "libx264" not in cmd

    def test_no_audio_stream(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(1, 2), has_audio=False)
        assert "-an" in cmd

    def test_input_seek_before_input_file(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(1, 2))
        assert cmd.index("-ss") < cmd.index("-i")


class TestVertical:
    def test_crop_filter(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(0, 1), vertical=True)
        joined = " ".join(cmd)
        assert "force_original_aspect_ratio=increase" in joined
        assert "crop=1080:1920" in joined
        assert "libx264" in cmd

    def test_blur_filter_uses_two_inputs(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(0, 1), vertical=True, layout="blur")
        joined = " ".join(cmd)
        assert "-filter_complex" in cmd
        assert "gblur=sigma=20" in joined and "overlay=(W-w)/2:(H-h)/2" in joined

    def test_vertical_ignores_fast(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(0, 1), fast=True, vertical=True)
        assert "copy" not in cmd

    def test_custom_size(self):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", Segment(0, 1), vertical=True, vertical_size=(720, 1280))
        assert "crop=720:1280" in " ".join(cmd)

    def test_vertical_name(self):
        from video_clipper.cut import vertical_name

        assert vertical_name("a__1__2.mp4", False) == "a__1__2.mp4"
        assert vertical_name("a__1__2.mp4", True) == "a__1__2_vertical.mp4"
        assert vertical_name("a__1__2.mp4", True, "blur") == "a__1__2_vertical_blur.mp4"

    def test_real_vertical_cut(self, ten_seconds, tmp_path):
        out = tmp_path / "v.mp4"
        res = cut_segment(
            ten_seconds, out, Segment(1, 2), vertical=True, vertical_size=(360, 640)
        )
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        assert probe.stdout.strip() == "360,640"
        assert res.mode == "accurate+vertical"

    def test_real_vertical_blur_cut(self, ten_seconds, tmp_path):
        out = tmp_path / "vb.mp4"
        res = cut_segment(
            ten_seconds, out, Segment(1, 2), vertical=True, layout="blur",
            vertical_size=(360, 640),
        )
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        assert probe.stdout.strip() == "360,640"
        assert res.mode == "accurate+vertical-blur"


class TestCutSegment:
    def test_accurate_duration(self, ten_seconds, tmp_path):
        out = tmp_path / "accurate.mp4"
        res = cut_segment(ten_seconds, out, Segment(2, 4), fast=False)
        assert out.is_file() and out.stat().st_size > 0
        assert res.duration == pytest.approx(2.0, abs=0.15)
        assert res.mode == "accurate"
        assert res.size_bytes == out.stat().st_size

    def test_fast_duration(self, ten_seconds, tmp_path):
        out = tmp_path / "fast.mp4"
        res = cut_segment(ten_seconds, out, Segment(2, 4), fast=True)
        assert out.is_file()
        assert res.mode == "fast"
        assert res.duration == pytest.approx(2.0, abs=0.6)

    def test_audio_is_kept(self, ten_seconds, tmp_path):
        out = tmp_path / "with_audio.mp4"
        cut_segment(ten_seconds, out, Segment(1, 2))
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        assert "audio" in probe.stdout

    def test_ffmpeg_failure_raises(self, tmp_path):
        with pytest.raises(CutError, match="ffmpeg завершился"):
            cut_segment(
                tmp_path / "missing.mp4", tmp_path / "out.mp4", Segment(0, 1),
                ffmpeg_path="ffmpeg",
            )

    def test_remove_source(self, tmp_path):
        f = tmp_path / "tmp" / "file.mp4"
        f.parent.mkdir()
        f.write_bytes(b"x")
        remove_source(f)
        assert not f.exists() and not f.parent.exists()
