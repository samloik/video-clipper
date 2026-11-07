"""Кадр-превью: паспорт файла и один кадр в JPEG."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from video_clipper.config import load_config
from video_clipper.preview import grab_frame, local_source, probe_media

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="нужны ffmpeg и ffprobe",
)


def make_video(path, seconds=2, size="320x240", fps=25):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )
    return path


def _cfg(tmp_path):
    return load_config(tmp_path / "нет-такого.json").with_overrides(
        cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"
    )


@needs_ffmpeg
class TestProbe:
    def test_passport_of_a_real_file(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=3)
        info = probe_media(video)
        assert info["width"] == 320 and info["height"] == 240
        assert info["fps"] == pytest.approx(25.0, abs=0.01)
        assert info["duration"] == pytest.approx(3.0, abs=0.2)

    def test_garbage_file_gives_empty_passport(self, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a video at all")
        assert probe_media(broken) == {}


@needs_ffmpeg
class TestFrame:
    def test_frame_is_jpeg(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=2)
        frame = grab_frame(video, 1.0, width=320)
        assert frame[:2] == b"\xff\xd8" and frame[-2:] == b"\xff\xd9"
        assert len(frame) > 1000

    def test_negative_time_is_clamped(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=2)
        assert grab_frame(video, -5, width=160)[:2] == b"\xff\xd8"

    def test_time_beyond_the_end_gives_nothing(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=1)
        assert grab_frame(video, 999, width=160) == b""


class TestLocalSource:
    def test_local_file_is_used_as_is(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        assert local_source(str(video), _cfg(tmp_path)) == video

    def test_cached_download_is_found_by_id(self, tmp_path):
        cfg = _cfg(tmp_path)
        cache = cfg.cache_dir
        cache.mkdir(parents=True, exist_ok=True)
        cached = cache / "ролик-abcdefgh.mp4"
        cached.write_bytes(b"x")
        found = local_source("https://youtu.be/abcdefgh", cfg)
        assert found == cached

    def test_link_without_download_is_none(self, tmp_path):
        assert local_source("https://youtu.be/zzzzzzzz", _cfg(tmp_path)) is None

    def test_empty_source_is_none(self, tmp_path):
        assert local_source("", _cfg(tmp_path)) is None

    def test_upload_metadata(self, tmp_path):
        # паспорт не должен падать на пустом файле — просто без данных
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        assert isinstance(probe_media(empty), dict)
