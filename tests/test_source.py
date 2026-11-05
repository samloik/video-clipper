"""Tests for source handling: local files, yt-dlp download (mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_clipper.config import load_config
from video_clipper.segments import Segment
from video_clipper.source import Source, SourceError, is_url, prepare_source

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
    reason="нужен ffmpeg",
)


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """A 10-second synthetic mp4 with audio."""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
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


class TestIsUrl:
    @pytest.mark.parametrize("value", ["https://youtu.be/x", "http://a.b/c"])
    def test_urls(self, value):
        assert is_url(value) is True

    @pytest.mark.parametrize("value", ["video.mp4", "/tmp/a.mp4", "ftp://x"])
    def test_not_urls(self, value):
        assert is_url(value) is False


class TestLocalSource:
    def test_reads_media_info(self, sample_video):
        src = prepare_source(str(sample_video), load_config(None))
        assert isinstance(src, Source)
        assert src.is_temporary is False
        assert src.info.has_video and src.info.has_audio
        assert src.info.duration == pytest.approx(10.0, abs=0.2)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            prepare_source(str(tmp_path / "nope.mp4"), load_config(None))


class TestShiftSegments:
    def test_shift_for_section_file(self):
        from video_clipper.source import shift_segments

        shifted = shift_segments([Segment(2415, 2553)], 2415)
        assert (shifted[0].start, shifted[0].end) == (0.0, 138.0)

    def test_no_offset_keeps_segments(self):
        from video_clipper.source import shift_segments

        segs = [Segment(10, 20)]
        assert shift_segments(segs, 0.0) is segs

    def test_offset_is_applied_to_every_segment(self):
        from video_clipper.source import shift_segments

        shifted = shift_segments([Segment(100, 110), Segment(200, 210)], 100)
        assert [(s.start, s.end) for s in shifted] == [(0, 10), (100, 110)]


class TestDownload:
    def test_uses_config_proxy_and_cookies(self, tmp_path, sample_video):
        cfg_file = tmp_path / "config.json"
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        cfg_file.write_text(
            f'{{"proxy": "socks5://127.0.0.1:10808", "cookies": "{cookies}", '
            f'"cache_dir": "{tmp_path / "cache"}", "default_resolution": 480}}',
            encoding="utf-8",
        )
        seen: list[dict] = []

        def fake_downloader(url, opts):
            seen.append(opts)
            if opts.get("skip_download"):
                return {"duration": 600, "title": "Sample"}
            return {
                "title": "Sample",
                "requested_downloads": [{"filepath": str(sample_video)}],
            }

        cfg = load_config(cfg_file).with_overrides(use_sections=True)
        src = prepare_source(
            "https://youtu.be/xyz",
            cfg,
            segments=[(10, 20)],
            downloader=fake_downloader,
        )
        assert src.path == sample_video
        assert src.is_temporary is True
        download_opts = seen[-1]
        # ffmpeg cannot use SOCKS5, so downloads go through the local HTTP bridge
        # that tunnels to it; the SOCKS address itself is no longer passed on.
        assert download_opts["proxy"].startswith("http://127.0.0.1:")
        assert download_opts["cookiefile"] == str(cookies)
        assert "480" in download_opts["format"]
        # 10 s out of 600 s coverage → only sections are fetched
        assert "download_ranges" in download_opts

    def test_sections_used_for_small_coverage(self, sample_video, tmp_path):
        """A 10 s piece of a 600 s video is fetched as a section, not whole."""
        seen: list[dict] = []

        def fake_downloader(url, opts):
            seen.append(opts)
            if opts.get("skip_download"):
                return {"duration": 600}
            return {"requested_downloads": [{"filepath": str(sample_video)}]}

        cfg = load_config(None).with_overrides(cache_dir=tmp_path / "cache")
        prepare_source("https://youtu.be/x", cfg, segments=[(10, 20)], downloader=fake_downloader)
        assert "download_ranges" in seen[-1]

    def test_full_download_when_coverage_is_large(self, sample_video, tmp_path):
        def fake_downloader(url, opts):
            if opts.get("skip_download"):
                return {"duration": 30}
            return {"requested_downloads": [{"filepath": str(sample_video)}]}

        cfg = load_config(None).with_overrides(cache_dir=tmp_path / "cache")
        src = prepare_source("https://youtu.be/x", cfg, segments=[(0, 28)], downloader=fake_downloader)
        assert src.path == sample_video

    def test_download_failure_is_reported(self):
        def broken(url, opts):
            raise RuntimeError("boom")

        with pytest.raises(SourceError, match="Не удалось скачать"):
            prepare_source("https://youtu.be/x", load_config(None), downloader=broken)

    def test_missing_filepath_is_reported(self):
        def no_path(url, opts):
            if opts.get("skip_download"):
                return {}
            return {"title": "x"}

        with pytest.raises(SourceError, match="путь к файлу"):
            prepare_source("https://youtu.be/x", load_config(None), downloader=no_path)

    def test_keep_source_flag(self, sample_video, tmp_path):
        def fake_downloader(url, opts):
            if opts.get("skip_download"):
                return {"duration": 10}
            return {"requested_downloads": [{"filepath": str(sample_video)}]}

        cfg = load_config(None).with_overrides(cache_dir=tmp_path / "cache")
        src = prepare_source("https://youtu.be/x", cfg, keep_source=True, downloader=fake_downloader)
        assert src.is_temporary is False
        src.cleanup()  # must not delete anything
        assert sample_video.exists()
