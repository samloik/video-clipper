"""Tests for source handling: local files, yt-dlp download (mocked)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from video_clipper.config import load_config
from video_clipper.segments import Segment
from video_clipper.source import (
    YT_CLIENTS,
    ChallengeCooldown,
    Source,
    SourceError,
    _extract_with_clients,
    explain_error,
    is_url,
    prepare_source,
)

def _clean_config(**overrides):
    """Конфиг только из кодовых значений: тесты не должны зависеть от config.json."""
    cfg = load_config(Path("/nonexistent/video-clipper-config.json"))
    return cfg.with_overrides(**overrides) if overrides else cfg


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
        src = prepare_source(str(sample_video), _clean_config())
        assert isinstance(src, Source)
        assert src.is_temporary is False
        assert src.info.has_video and src.info.has_audio
        assert src.info.duration == pytest.approx(10.0, abs=0.2)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            prepare_source(str(tmp_path / "nope.mp4"), _clean_config())


class TestCookieHints:
    def test_hint_added_for_signin_error(self, tmp_path):
        from video_clipper.source import _with_cookie_hint

        cfg = _clean_config().with_overrides(cookies=str(tmp_path / "c.txt"))
        msg = _with_cookie_hint(
            RuntimeError("ERROR: [youtube] x: Sign in to confirm you're not a bot"), cfg
        )
        assert "Экспортируй их из браузера" in msg
        assert str(tmp_path / "c.txt") in msg

    def test_plain_error_untouched(self):
        from video_clipper.source import _with_cookie_hint

        msg = _with_cookie_hint(RuntimeError("network unreachable"), _clean_config())
        assert msg == "network unreachable"

    def test_stale_cookie_warning(self, tmp_path, caplog):
        import os
        import time as _time

        from video_clipper.source import cookie_warning

        path = tmp_path / "cookies.txt"
        path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        old = _time.time() - 40 * 86400
        os.utime(path, (old, old))
        cfg = _clean_config().with_overrides(cookies=str(path))
        with caplog.at_level("WARNING"):
            cookie_warning(cfg)
        assert "не менялся" in caplog.text

    def test_fresh_cookies_no_warning(self, tmp_path, caplog):
        from video_clipper.source import cookie_warning

        path = tmp_path / "cookies.txt"
        path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        cfg = _clean_config().with_overrides(cookies=str(path))
        with caplog.at_level("WARNING"):
            cookie_warning(cfg)
        assert caplog.text == ""


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


class TestChallengeCooldown:
    def test_second_attempt_is_refused_without_touching_the_network(self, tmp_path):
        from video_clipper import source as source_module

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        until = source_module.note_challenge(600)
        calls = []

        def downloader(url, opts):
            calls.append(1)
            return {}

        with pytest.raises(ChallengeCooldown) as excinfo:
            prepare_source("https://youtu.be/x", cfg, segments=[(0, 10)], downloader=downloader)
        assert calls == [], "во время паузы запросов к сайту быть не должно"
        assert excinfo.value.until == until
        assert "Следующая попытка" in str(excinfo.value)
        source_module.clear_challenge()

    def test_cooldown_expires(self, tmp_path):
        from video_clipper import source as source_module

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        source_module.note_challenge(0.01)
        time.sleep(0.05)
        assert source_module.challenge_until() < time.time()
        source_module.clear_challenge()


class TestClientFallback:
    def test_android_is_tried_first(self):
        calls = []

        def extractor(url, opts):
            client = (opts.get("extractor_args") or {}).get("youtube", {}).get("player_client")
            calls.append(client)
            return {"title": "ок", "duration": 10}

        info, used = _extract_with_clients(
            "https://www.youtube.com/watch?v=x", {"quiet": True}, extractor
        )
        assert info["title"] == "ок"
        assert calls == [["android"]], calls
        assert used["extractor_args"]["youtube"]["player_client"] == ["android"]

    def test_challenge_moves_to_the_next_client(self):
        calls = []

        def extractor(url, opts):
            client = (opts.get("extractor_args") or {}).get("youtube", {}).get("player_client")
            calls.append(client)
            if client in (["android"], ["tv_embedded"]):
                raise RuntimeError("The page needs to be reloaded.")
            return {"title": "ок"}

        info, used = _extract_with_clients("https://youtu.be/x", {}, extractor)
        assert info["title"] == "ок"
        assert calls == [["android"], ["tv_embedded"], ["web"]]

    def test_other_errors_are_not_retried(self):
        calls = []

        def extractor(url, opts):
            calls.append(1)
            raise RuntimeError("HTTP Error 404: Not Found")

        with pytest.raises(RuntimeError):
            _extract_with_clients("https://www.youtube.com/watch?v=x", {}, extractor)
        assert len(calls) == 1

    def test_non_youtube_is_never_retried(self):
        calls = []

        def extractor(url, opts):
            calls.append(1)
            raise RuntimeError("Sign in to confirm you're not a bot")

        with pytest.raises(RuntimeError):
            _extract_with_clients("https://vimeo.com/1", {}, extractor)
        assert len(calls) == 1


class TestExplainError:
    def _cfg(self, tmp_path, age_days=None):
        path = tmp_path / "c.txt"
        path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        if age_days is not None:
            stamp = time.time() - age_days * 86400
            os.utime(path, (stamp, stamp))
        return _clean_config().with_overrides(cookies=str(path))

    def test_bot_check_with_fresh_cookies_does_not_blame_cookies(self, tmp_path):
        text = explain_error(RuntimeError("Sign in to confirm you're not a bot"), self._cfg(tmp_path, 0.2))
        assert "Дело не в cookies" in text
        assert "повтори запрос" in text.lower()

    def test_bot_check_with_stale_cookies_asks_for_refresh(self, tmp_path):
        text = explain_error(RuntimeError("Sign in to confirm you're not a bot"), self._cfg(tmp_path, 60))
        assert "не менялся 60 дн." in text

    def test_bot_check_without_file_says_so(self, tmp_path):
        cfg = _clean_config().with_overrides(cookies=str(tmp_path / "нет.txt"))
        assert "файла cookies нет" in explain_error(RuntimeError("not a bot"), cfg)

    def test_ordinary_error_stays_ordinary(self, tmp_path):
        assert explain_error(RuntimeError("boom"), self._cfg(tmp_path, 1)) == "boom"


@pytest.fixture(autouse=True)
def clear_meta_cache():
    """Metadata caching must not leak between tests (they reuse the same fake URL)."""
    from video_clipper import source as source_module

    source_module._META_CACHE.clear()
    source_module.clear_challenge()
    yield
    source_module._META_CACHE.clear()
    source_module.clear_challenge()


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

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        prepare_source("https://youtu.be/x", cfg, segments=[(10, 20)], downloader=fake_downloader)
        assert "download_ranges" in seen[-1]

    def test_full_download_when_coverage_is_large(self, sample_video, tmp_path):
        def fake_downloader(url, opts):
            if opts.get("skip_download"):
                return {"duration": 30}
            return {"requested_downloads": [{"filepath": str(sample_video)}]}

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        src = prepare_source("https://youtu.be/x", cfg, segments=[(0, 28)], downloader=fake_downloader)
        assert src.path == sample_video

    def test_section_auth_error_does_not_fall_back_to_full_download(self, sample_video, tmp_path):
        """A bot-check on the section must not start a multi-hundred-MB download."""
        attempts = []

        def downloader(url, opts):
            attempts.append(opts)
            if opts.get("skip_download"):
                return {"duration": 152 * 60}
            raise RuntimeError("Sign in to confirm you're not a bot")

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        with pytest.raises(SourceError, match="Полную загрузку не начинаю"):
            prepare_source("https://youtu.be/x", cfg, segments=[(0, 20)], downloader=downloader)
        downloads = [opts for opts in attempts if not opts.get("skip_download")]
        assert downloads, "секционная попытка должна была состояться"
        assert all("download_ranges" in opts for opts in downloads), "полной загрузки быть не должно"
        assert len(downloads) == len(YT_CLIENTS) + 1, "каждый клиент пробуется ровно раз"

    def test_download_reports_progress(self, sample_video, tmp_path):
        events: list[tuple[str, float | None, str]] = []

        def downloader(url, opts):
            if opts.get("skip_download"):
                return {"duration": 60.0, "title": "ролик"}
            for hook in opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 5_000_000,
                      "total_bytes": 10_000_000})
                hook({"status": "finished"})
            return {"requested_downloads": [{"filepath": str(sample_video)}]}

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        prepare_source(
            "https://youtu.be/x",
            cfg,
            segments=[(0, 10)],
            downloader=downloader,
            progress=lambda stage, fraction, detail: events.append((stage, fraction, detail)),
        )
        assert events[0][0] == "download"
        assert events[0][1] == 0.5
        assert "МБ" in events[0][2]
        assert events[-1][1] == 1.0

    def test_range_past_the_end_never_starts_a_download(self, tmp_path):
        calls = []

        def downloader(url, opts):
            calls.append(bool(opts.get("skip_download")))
            if opts.get("skip_download"):
                return {"duration": 10.0, "title": "короткий"}
            raise AssertionError("скачивание не должно было начаться")

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        with pytest.raises(SourceError, match="выходит за конец ролика"):
            prepare_source("https://youtu.be/x", cfg, segments=[(0, 20)], downloader=downloader)
        assert calls == [True], "был только запрос метаданных"

    def test_download_failure_is_reported(self):
        def broken(url, opts):
            raise RuntimeError("boom")

        with pytest.raises(SourceError, match="Не удалось скачать"):
            prepare_source("https://youtu.be/x", _clean_config(), downloader=broken)

    def test_missing_filepath_is_reported(self):
        def no_path(url, opts):
            if opts.get("skip_download"):
                return {}
            return {"title": "x"}

        with pytest.raises(SourceError, match="путь к файлу"):
            prepare_source("https://youtu.be/x", _clean_config(), downloader=no_path)

    def test_keep_source_flag(self, sample_video, tmp_path):
        def fake_downloader(url, opts):
            if opts.get("skip_download"):
                return {"duration": 10}
            return {"requested_downloads": [{"filepath": str(sample_video)}]}

        cfg = _clean_config().with_overrides(cache_dir=tmp_path / "cache")
        src = prepare_source("https://youtu.be/x", cfg, keep_source=True, downloader=fake_downloader)
        assert src.is_temporary is False
        src.cleanup()  # must not delete anything
        assert sample_video.exists()
