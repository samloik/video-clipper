"""Кадр-превью: паспорт файла и один кадр в JPEG."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from video_clipper.config import load_config
from pathlib import Path

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

        def fake_window(source, cfg_, start, end, out_dir):
            calls.update(source=source, start=start, end=end, out_dir=out_dir)
            return downloaded, start, {"title": "ролик"}

        import video_clipper.preview as preview_module
        import video_clipper.source as source_module

        monkeypatch.setattr(source_module, "download_window", fake_window)
        monkeypatch.setattr(preview_module, "cached_stream", lambda source: {"duration": 600.0})
        path, start, end = ensure_window(url, cfg, 300)
        assert calls["start"] == 298.0 and calls["end"] == 318.0
        assert path.parent == folder and path.name.endswith("-298.mp4")
        assert find_window(url, cfg, 300) == (path, 298.0, 318.0, 600.0)

    def test_window_does_not_pass_the_end_of_the_video(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        downloaded = windows_dir(cfg) / "x.mp4"
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"y")

        import video_clipper.preview as preview_module
        import video_clipper.source as source_module

        monkeypatch.setattr(
            source_module, "download_window",
            lambda source, cfg_, start, end, out_dir: (downloaded, start, {}),
        )
        monkeypatch.setattr(preview_module, "cached_stream", lambda source: {"duration": 30.0})
        _, start, end = ensure_window("https://youtu.be/SHORT", cfg, 29)
        assert end <= 29.8 and start < end


class TestPlaybackCopy:
    """Участок для плеера должен начинаться с нуля, иначе шкала уезжает."""

    def _offset_file(self, tmp_path):
        import subprocess

        source = tmp_path / "window.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                "-output_ts_offset", "917.4", str(source),
            ],
            check=True,
        )
        return source

    def _start_time(self, path):
        import subprocess

        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=start_time,duration",
             "-of", "default=nw=1", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        values = dict(line.split("=") for line in out.strip().splitlines())
        return float(values["start_time"]), float(values["duration"])

    def test_copy_starts_at_zero(self, tmp_path):
        import shutil

        if not shutil.which("ffmpeg"):
            pytest.skip("нужен ffmpeg")
        from video_clipper.config import load_config
        from video_clipper.preview import playback_copy

        cfg = load_config(tmp_path / "config.json")
        cfg.with_overrides(cache_dir=tmp_path / "cache")
        window = self._offset_file(tmp_path)
        start, _container = self._start_time(window)
        assert start == 917.4  # исходный участок живёт в шкале ролика

        copy = playback_copy(window, 917.4, cfg)
        assert copy is not None and copy.is_file()
        start, copied_duration = self._start_time(copy)
        assert start == 0.0, "плеер должен видеть шкалу с нуля"
        assert 2.5 < copied_duration < 3.5, "длительность — это сам участок, без сдвига"

    def test_zero_offset_is_served_as_is(self, tmp_path):
        from video_clipper.config import load_config
        from video_clipper.preview import playback_copy

        cfg = load_config(tmp_path / "config.json")
        window = tmp_path / "plain.mp4"
        window.write_bytes(b"x")
        assert playback_copy(window, 0.0, cfg) == window


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
            "title": "", "user_agent": "Mozilla/5.0 test",
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

    def test_stream_addresses_survive_a_restart_and_a_bot_check(self, tmp_path, monkeypatch):
        """Адрес потока кэшируется на диске: второй заказ не упирается в бот-чек."""
        import video_clipper.source as source_module

        cfg = self._cfg(tmp_path)

        def ok(url, opts):
            return {"url": "https://cdn.example/disk.mp4", "duration": 100.0,
                    "width": 640, "height": 360, "fps": 25.0}

        monkeypatch.setattr(source_module, "_ytdlp_extract", ok)
        url = "https://youtu.be/DISK1"
        stream_info(url, cfg)
        cache_file = Path(cfg.cache_dir) / "streams.json"
        assert cache_file.is_file(), "адрес должен лежать на диске"

        # «перезапуск»: память пуста, а сайт отвечает бот-чеком
        import video_clipper.preview as pv

        pv._STREAM_CACHE.clear()

        def bot_check(*args, **kwargs):
            raise RuntimeError("ERROR: [youtube] X: Sign in to confirm you're not a bot")

        monkeypatch.setattr(source_module, "_ytdlp_extract", bot_check)
        got_url, meta = stream_info(url, cfg)
        assert got_url == "https://cdn.example/disk.mp4"
        assert meta["duration"] == 100.0

    def test_two_requests_for_one_source_extract_once(self, tmp_path, monkeypatch):
        """Две рамки просят поток одновременно — запрос к сайту должен быть один."""
        import threading
        import time

        import video_clipper.source as source_module

        calls = []

        def slow(url, opts):
            calls.append(1)
            time.sleep(0.2)
            return {"url": "https://cdn.example/one.mp4", "duration": 10.0,
                    "width": 320, "height": 180, "fps": 25.0}

        monkeypatch.setattr(source_module, "_ytdlp_extract", slow)
        cfg = self._cfg(tmp_path)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(stream_info("https://youtu.be/ONE1", cfg)))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(calls) == 1, "второй поток берёт уже полученный адрес"
        assert len(results) == 2

    def test_stream_metadata_carries_the_title(self, tmp_path, monkeypatch):
        """Название ролика нужно для имени клипа, а не «window-38-90-7844»."""
        import video_clipper.source as source_module

        monkeypatch.setattr(
            source_module, "_ytdlp_extract",
            lambda url, opts: {"url": "https://cdn.example/t.mp4", "duration": 100.0,
                               "width": 640, "height": 360, "fps": 25.0,
                               "title": "Дефолты в России"},
        )
        _, meta = stream_info("https://youtu.be/TITLE1", self._cfg(tmp_path))
        assert meta["title"] == "Дефолты в России"

    def test_missing_small_format_falls_back_to_any(self, tmp_path, monkeypatch):
        """«Requested format is not available» — не приговор: просим любой формат."""
        import video_clipper.source as source_module

        seen = []

        def fake(url, opts):
            seen.append(opts.get("format"))
            if opts.get("format"):
                raise RuntimeError("ERROR: [youtube] X: Requested format is not available")
            return {"url": "https://cdn.example/any.mp4", "duration": 45.0,
                    "width": 1080, "height": 1920, "fps": 30.0}

        monkeypatch.setattr(source_module, "_ytdlp_extract", fake)
        url, meta = stream_info("https://youtu.be/FMT1", self._cfg(tmp_path))
        assert url == "https://cdn.example/any.mp4"
        assert seen[0] is not None and seen[1] is None, "второй заход — без ограничения формата"

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

    def test_id_longer_than_eleven_is_named_as_extra_character(self):
        # 12 символов: yt-dlp молча обрежет до 11 и ответит «video is unavailable»
        problem = link_problem("https://www.youtube.com/shorts/KhPr0wIt81EA")
        assert "12 символов" in problem
        assert "приклеился лишний" in problem
        assert "https://youtu.be/KhPr0wIt81E" in problem


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
