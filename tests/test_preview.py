"""Кадр-превью: паспорт файла и один кадр в JPEG."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from video_clipper.config import load_config
from video_clipper.preview import (
    FRAME_CACHE_LIMIT,
    cached_frame,
    cached_stream,
    closest_frame,
    ensure_window,
    find_window,
    grab_frame,
    local_source,
    probe_media,
    remember_frame,
    link_problem,
    resolve_for_time,
    stream_info,
    windows_dir,
)

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


class TestPreviewWindow:
    """Для ссылки кадры берутся из короткого скачанного участка."""

    def _window(self, cfg, source, start, end, name="clip-0.mp4"):
        folder = windows_dir(cfg)
        folder.mkdir(parents=True, exist_ok=True)
        media = folder / name
        media.write_bytes(b"x" * 64)
        (folder / (media.stem + ".json")).write_text(
            json.dumps({"source": source, "file": media.name, "start": start, "end": end}),
            encoding="utf-8",
        )
        return media

    def test_window_is_found_inside_its_range(self, tmp_path):
        cfg = _cfg(tmp_path)
        url = "https://youtu.be/ID"
        media = self._window(cfg, url, 100, 120)
        found = find_window(url, cfg, 110)
        assert found == (media, 100.0, 120.0, 0.0)

    def test_window_is_not_used_far_away(self, tmp_path):
        cfg = _cfg(tmp_path)
        url = "https://youtu.be/ID"
        self._window(cfg, url, 100, 120)
        assert find_window(url, cfg, 300) is None

    def test_window_is_not_shared_between_urls(self, tmp_path):
        cfg = _cfg(tmp_path)
        self._window(cfg, "https://youtu.be/AAA", 0, 20)
        assert find_window("https://youtu.be/BBB", cfg, 5) is None

    def test_time_is_shifted_to_the_window(self, tmp_path):
        cfg = _cfg(tmp_path)
        url = "https://youtu.be/ID"
        media = self._window(cfg, url, 100, 120)
        path, local, start, end, is_window = resolve_for_time(url, cfg, 110)
        assert (path, local, start, end, is_window) == (media, 10.0, 100.0, 120.0, True)

    def test_full_download_has_no_offset(self, tmp_path):
        cfg = _cfg(tmp_path)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        assert resolve_for_time(str(video), cfg, 12) == (video, 12.0, 0.0, 0.0, False)

    def test_nothing_known_gives_none(self, tmp_path):
        assert resolve_for_time("https://youtu.be/NONE", _cfg(tmp_path), 5) is None

    def test_ensure_window_downloads_and_records_metadata(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        url = "https://youtu.be/ID"
        folder = windows_dir(cfg)
        folder.mkdir(parents=True, exist_ok=True)
        downloaded = folder / "sections" / "ролик-ID.mp4"
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"y" * 32)

        calls = {}

        def fake_download(source, cfg_, start, end, out_dir, progress=None):
            calls.update(source=source, start=start, end=end, out_dir=out_dir)
            return downloaded, start

        import video_clipper.source as source_module

        monkeypatch.setattr(source_module, "download_section", fake_download)
        monkeypatch.setattr(
            source_module, "fetch_metadata", lambda url, cfg_: {"duration": 600.0}
        )
        path, start, end = ensure_window(url, cfg, 300)
        assert calls["start"] == 298.0 and calls["end"] == 318.0
        assert path.parent == folder and path.name.endswith("-298.mp4")
        assert find_window(url, cfg, 300) == (path, 298.0, 318.0, 600.0)

    def test_window_does_not_pass_the_end_of_the_video(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        downloaded = windows_dir(cfg) / "x.mp4"
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"y")

        import video_clipper.source as source_module

        monkeypatch.setattr(
            source_module, "download_section",
            lambda source, cfg_, start, end, out_dir, progress=None: (downloaded, start),
        )
        monkeypatch.setattr(source_module, "fetch_metadata", lambda url, cfg_: {"duration": 30.0})
        _, start, end = ensure_window("https://youtu.be/SHORT", cfg, 29)
        assert end <= 29.8 and start < end


class TestLinkProblem:
    def test_short_id_is_named_as_truncated(self):
        from video_clipper.preview import link_problem

        assert "10 символов" in link_problem("https://youtu.be/ABCDEFGHIJ")

    def test_long_id_is_named_as_extra_character(self):
        from video_clipper.preview import link_problem

        problem = link_problem("https://www.youtube.com/shorts/KhPr0wIt81EA")
        assert "12 символов" in problem
        assert "приклеился лишний" in problem
        assert "https://youtu.be/KhPr0wIt81E" in problem

    def test_correct_id_passes(self):
        from video_clipper.preview import link_problem

        assert link_problem("https://www.youtube.com/watch?v=-QQSyL7tnqs") == ""


class TestStream:
    """Кадр по ссылке без скачивания: берём прямую ссылку на поток."""

    def _cfg(self, tmp_path):
        return _cfg(tmp_path)

    def test_stream_info_reads_the_direct_link(self, tmp_path, monkeypatch):
        import video_clipper.source as source_module

        monkeypatch.setattr(
            source_module, "_ytdlp_extract",
            lambda url, opts: {
                "url": "https://cdn.example/x.mp4",
                "duration": 63.0, "width": 360, "height": 450, "fps": 30.0,
                "http_headers": {"User-Agent": "Mozilla/5.0 test"},
            },
        )
        url, meta = stream_info("https://youtu.be/STREAM1", self._cfg(tmp_path))
        assert url == "https://cdn.example/x.mp4"
        assert meta == {
            "duration": 63.0, "width": 360, "height": 450, "fps": 30.0,
            "user_agent": "Mozilla/5.0 test",
        }

    def test_stream_info_is_cached(self, tmp_path, monkeypatch):
        import video_clipper.source as source_module

        calls = []

        def fake(url, opts):
            calls.append(url)
            return {"url": "https://cdn.example/y.mp4", "duration": 10.0}

        monkeypatch.setattr(source_module, "_ytdlp_extract", fake)
        cfg = self._cfg(tmp_path)
        stream_info("https://youtu.be/STREAM2", cfg)
        stream_info("https://youtu.be/STREAM2", cfg)
        assert len(calls) == 1

    def test_cached_stream_metadata_is_reused(self, tmp_path, monkeypatch):
        import video_clipper.source as source_module

        monkeypatch.setattr(
            source_module, "_ytdlp_extract",
            lambda url, opts: {
                "url": "https://cdn.example/z.mp4",
                "duration": 3458.0, "width": 640, "height": 360, "fps": 25.0,
            },
        )
        cfg = self._cfg(tmp_path)
        url = "https://youtu.be/STREAM4"
        assert cached_stream(url) == {}
        stream_info(url, cfg)
        assert cached_stream(url)["duration"] == 3458.0

    def test_slow_cdn_gets_a_second_try_with_a_fresh_url(self, tmp_path, monkeypatch):
        """Медленный CDN — не приговор: второй заход берёт новый адрес потока."""
        import video_clipper.preview as pv

        seen = []

        def fake_info(source, cfg):
            seen.append("info")
            return (f"https://cdn.example/{len(seen)}.mp4", {"user_agent": "ua"})

        attempts = []

        def fake_frame(target, seconds, width=480, cache_key=None, user_agent="", timeout=None):
            attempts.append(target)
            return b"" if len(attempts) == 1 else b"\xff\xd8jpeg"

        monkeypatch.setattr(pv, "stream_info", fake_info)
        monkeypatch.setattr(pv, "_frame_bytes", fake_frame)
        frame = pv.grab_stream_frame("https://youtu.be/SLOW1", self._cfg(tmp_path), 5.0, 240)
        assert frame == b"\xff\xd8jpeg"
        assert len(attempts) == 2, "ровно одна повторная попытка"
        assert attempts[0] != attempts[1], "повтор должен идти по свежему адресу"

    def test_missing_link_is_an_error(self, tmp_path, monkeypatch):
        import video_clipper.source as source_module

        monkeypatch.setattr(source_module, "_ytdlp_extract", lambda url, opts: {"title": "x"})
        with pytest.raises(RuntimeError, match="прямую ссылку"):
            stream_info("https://youtu.be/STREAM3", self._cfg(tmp_path))


class TestLinkProblem:
    """Обрезанная ссылка видна сразу, без похода в сеть."""

    def test_ten_characters_instead_of_eleven(self):
        message = link_problem("https://www.youtube.com/watch?v=-QQ5yL7tnq")
        assert "10 символов" in message and "11" in message

    def test_full_id_is_fine(self):
        assert link_problem("https://www.youtube.com/watch?v=-PqDFgs4C2s") == ""

    def test_shorts_and_short_links_are_checked(self):
        assert link_problem("https://www.youtube.com/shorts/abc") != ""
        assert link_problem("https://youtu.be/abc") != ""
        assert link_problem("https://youtu.be/-PqDFgs4C2s") == ""

    def test_other_sites_are_not_judged(self):
        assert link_problem("https://rutube.ru/video/abc/") == ""


class TestFrameCache:
    """Частые клики не должны оставлять пустую рамку."""

    def test_remembered_frame_is_returned(self):
        remember_frame("ключ-1", 10.0, 360, b"jpeg")
        assert cached_frame("ключ-1", 10.0, 360) == b"jpeg"
        assert cached_frame("ключ-1", 10.0, 320) == b""

    def test_closest_frame_within_tolerance(self):
        remember_frame("ключ-2", 10.0, 360, b"ten")
        remember_frame("ключ-2", 30.0, 360, b"thirty")
        assert closest_frame("ключ-2", 11.5, 360) == b"ten"
        assert closest_frame("ключ-2", 29.0, 360) == b"thirty"
        assert closest_frame("ключ-2", 60.0, 360) == b""

    def test_cache_keeps_the_oldest_out(self):
        for index in range(FRAME_CACHE_LIMIT + 5):
            remember_frame("ключ-3", float(index), 360, b"x")
        assert cached_frame("ключ-3", 0.0, 360) == b""
        assert cached_frame("ключ-3", float(FRAME_CACHE_LIMIT + 4), 360) == b"x"

    def test_other_sources_do_not_mix(self, tmp_path):
        remember_frame("ключ-4", 5.0, 360, b"own")
        assert closest_frame("ключ-5", 5.0, 360) == b""


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
