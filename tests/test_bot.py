"""Tests for the Telegram front-end and the retention policy (no network)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from video_clipper.bot import (
    ProgressReporter,
    handle_callback,
    build_info,
    clip_caption,
    describe_request,
    estimate_seconds,
    handle_message,
    parse_request,
    split_long,
)
from video_clipper.config import load_config
from video_clipper.cut import ClipResult
from video_clipper.segments import Segment, is_open_end
from video_clipper.source import SourceError
from video_clipper.storage import describe, find_cached_source, sweep, video_id_from_url


@pytest.fixture(autouse=True)
def no_metadata_network(monkeypatch):
    """The bot asks the site for metadata before downloading — never in tests."""
    from video_clipper import source as source_module

    source_module._META_CACHE.clear()
    monkeypatch.setattr("video_clipper.bot.fetch_metadata", lambda *args, **kwargs: {})
    yield
    source_module._META_CACHE.clear()


def _clean_config(**overrides):
    """Конфиг только из кодовых значений: тесты не должны зависеть от config.json."""
    cfg = load_config(Path("/nonexistent/video-clipper-config.json"))
    return cfg.with_overrides(**overrides) if overrides else cfg

class TestSendPolicy:
    def _big_file(self, tmp_path, size=1000) -> Path:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"0" * size)
        return path

    def test_small_file_is_sent_directly(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        monkeypatch.setattr("video_clipper.cut.TELEGRAM_LIMIT_BYTES", 10_000)
        bot = FakeBot()
        botmod.send_video_smart(bot, 1, self._big_file(tmp_path), "подпись")
        assert len(bot.videos) == 1
        assert bot.messages == [], "ничего объяснять не нужно"
        assert bot.videos[0][2] == "подпись"

    def test_big_file_is_split_not_crushed(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        parts = []
        for index in (1, 2, 3):
            part = tmp_path / f"clip_part{index}.mp4"
            part.write_bytes(b"0" * 10)
            parts.append(part)

        monkeypatch.setattr("video_clipper.cut.TELEGRAM_LIMIT_BYTES", 10)
        monkeypatch.setattr("video_clipper.cut.split_for_telegram", lambda *a, **kw: parts)
        called = {"shrink": False}
        monkeypatch.setattr(
            "video_clipper.cut.shrink_to_fit",
            lambda *a, **kw: called.__setitem__("shrink", True) or a[1],
        )
        bot = FakeBot()
        botmod.send_video_smart(bot, 1, self._big_file(tmp_path), "подпись", reply_markup={"x": 1})
        assert called["shrink"] is False, "пересжатие не нужно: клип режется на части"
        assert len(bot.videos) == 3
        assert [v[2] for v in bot.videos] == [
            "подпись · часть 1/3",
            "подпись · часть 2/3",
            "подпись · часть 3/3",
        ]
        assert bot.markups == [None, None, {"x": 1}], "кнопки — только на последней части"
        assert any("без пересжатия" in text for _, text in bot.messages)

    def test_splitting_failure_falls_back_to_compression(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        def broken(*args, **kwargs):
            raise RuntimeError("ffmpeg не смог")

        monkeypatch.setattr("video_clipper.cut.TELEGRAM_LIMIT_BYTES", 10)
        monkeypatch.setattr("video_clipper.cut.split_for_telegram", broken)
        monkeypatch.setattr(
            "video_clipper.cut.shrink_to_fit", lambda src, out, **kw: Path(out)
        )
        bot = FakeBot()
        botmod.send_video_smart(bot, 1, self._big_file(tmp_path), "подпись")
        assert len(bot.videos) == 1
        assert bot.videos[0][2].endswith("сжато под лимит")
        assert any("сжимаю" in text for _, text in bot.messages)

    def test_compress_policy_skips_splitting(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        monkeypatch.setattr("video_clipper.cut.TELEGRAM_LIMIT_BYTES", 10)
        monkeypatch.setattr(
            "video_clipper.cut.split_for_telegram",
            lambda *a, **kw: pytest.fail("при policy=compress резать нельзя"),
        )
        monkeypatch.setattr("video_clipper.cut.shrink_to_fit", lambda src, out, **kw: Path(out))
        bot = FakeBot()
        botmod.send_video_smart(bot, 1, self._big_file(tmp_path), "подпись", policy="compress")
        assert len(bot.videos) == 1


class TestOpenEnd:
    def test_star_after_dash_means_to_the_end(self):
        request = parse_request("https://youtu.be/ID 20-*")
        assert request is not None
        assert request.segments[0].start == 20.0
        assert is_open_end(request.segments[0])

    def test_star_is_understood_among_flags(self):
        request = parse_request("https://youtu.be/ID вертикально 20-*")
        assert request.vertical is True
        assert is_open_end(request.segments[0])

    def test_open_end_is_resolved_from_metadata_before_the_download(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "video_clipper.bot.fetch_metadata",
            lambda source, cfg: {"duration": 42.0, "title": "ролик"},
        )
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen["segments"] = [(s.start, s.end) for s in segments]
            return []

        bot = FakeBot()
        bot.send_message = lambda chat, text, **kw: bot.messages.append((chat, text))
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/ID 20-*"},
            clipper=clipper,
        )
        assert seen["segments"] == [(20.0, 42.0)]
        assert any("0:20" in text or "00:20" in text for _, text in bot.messages)


class TestErrorContext:
    def test_failure_starts_with_the_request(self, tmp_path, monkeypatch):
        from video_clipper.source import SourceError

        monkeypatch.setattr(
            "video_clipper.bot.fetch_metadata", lambda source, cfg: {"duration": 60.0}
        )

        def clipper(*args, **kwargs):
            raise SourceError("Секционная загрузка отклонена сайтом")

        bot = FakeBot()
        bot.send_message = lambda chat, text, **kw: bot.messages.append((chat, text))
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/ID вертикально 11-*"},
            clipper=clipper,
        )
        text = [t for _, t in bot.messages if "Ошибка" in t][-1]
        assert text.startswith("Ссылка: https://youtu.be/ID")
        assert "Отрезок:" in text and "Режим:" in text
        assert "Ошибка: Секционная загрузка отклонена сайтом" in text
        assert "9:16" in text


class TestVerticalProgress:
    def test_vertical_recut_shows_progress(self, tmp_path, monkeypatch):
        from video_clipper import bot as botmod
        from video_clipper.cut import ClipResult

        media = tmp_path / "src.mp4"
        media.write_bytes(b"x")
        key = "abc123"
        botmod.JOBS[key] = {
            "path": str(media),
            "source": "https://youtu.be/x",
            "title": "ролик",
            "segments": [{"start": 0.0, "end": 5.0}],
            "shown": [{"start": 0.0, "end": 5.0}],
            "has_audio": True,
            "height": 720,
        }

        def fake_cut(src, out, seg, **kwargs):
            callback = kwargs.get("progress")
            if callback:
                callback("encode", 0.0, seg.label)
                callback("encode", 0.5, seg.label)
            out = Path(out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return ClipResult(
                source=str(src), segment=seg, output_path=out,
                duration=seg.duration, mode="accurate", size_bytes=1,
            )

        monkeypatch.setattr("video_clipper.cut.cut_segment", fake_cut)
        monkeypatch.setattr(botmod, "send_video_smart", lambda *a, **k: None)

        bot = FakeBot()
        bot.call = lambda method, payload=None: {}
        handle_callback(
            bot, _cfg(tmp_path),
            {"id": "1", "data": f"vert:{key}", "message": {"chat": {"id": 1}}},
        )
        texts = [text for _, text in bot.messages] + [text for _, _, text in bot.edits]
        assert any("Кодирую" in text for text in texts), texts
        assert any("%" in text for text in texts), "процент должен появиться хотя бы раз"
        assert any("Готово" in text for text in texts)


class TestTimeCommand:
    def _cfg(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.config_path = tmp_path / "config.json"
        return cfg

    def test_time_shows_current_value(self, tmp_path):
        cfg = self._cfg(tmp_path)
        bot = FakeBot()
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/time"})
        text = "\n".join(t for _, t in bot.messages)
        assert "30 мин" in text and "/time 1:30" in text

    def test_time_changes_retention_and_persists(self, tmp_path):
        cfg = self._cfg(tmp_path)
        bot = FakeBot()
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/time 1:30"})
        assert cfg.retention_minutes == 90
        assert "1 ч 30 мин" in "\n".join(t for _, t in bot.messages)
        assert cfg.retention_file().read_text().strip() == "90"

    def test_time_rejects_garbage(self, tmp_path):
        cfg = self._cfg(tmp_path)
        bot = FakeBot()
        was = cfg.retention_minutes
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/time полчаса"})
        assert cfg.retention_minutes == was
        assert "Примеры" in "\n".join(t for _, t in bot.messages)

    def test_status_shows_the_deletion_moment(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"x" * 1024)
        bot = FakeBot()
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/status"})
        text = "\n".join(t for _, t in bot.messages)
        assert "удалится" in text and " в " in text
        assert "Хранение: 30 мин" in text


class TestAttachment:
    def _message(self, **extra):
        message = {"chat": {"id": 1}, "caption": ""}
        message.update(extra)
        return message

    def test_video_with_caption_is_cut(self, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen.update(source=source, segments=[(s.start, s.end) for s in segments], vertical=vertical)
            return []

        bot = FakeBot()
        bot.next_file_size = 5 * 1024 * 1024
        handle_message(
            bot,
            _cfg(tmp_path),
            self._message(video={"file_id": "AAA", "file_size": 5 * 1024 * 1024,
                                 "mime_type": "video/mp4"}, caption="0:10-0:20"),
            clipper=clipper,
        )
        assert seen["segments"] == [(10.0, 20.0)]
        assert Path(seen["source"]).is_file(), "файл должен быть скачан в кеш"
        assert "Скачиваю" in " ".join(t for _, t in bot.messages)
        assert bot.files_requested == ["AAA"]

    def test_document_without_caption_is_whole_video(self, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen.update(segments=list(segments))
            return []

        bot = FakeBot()
        bot.next_file_size = 1024 * 1024
        handle_message(
            bot,
            _cfg(tmp_path),
            self._message(document={"file_id": "DOC", "file_size": 1024 * 1024,
                                    "file_name": "lecture.mkv",
                                    "mime_type": "video/x-matroska"}),
            clipper=clipper,
        )
        assert seen["segments"] == [], "без подписи — целиком"
        assert any("лекцию" in t.lower() or "целиком" in t.lower() for _, t in bot.messages)

    def test_too_big_attachment_is_refused_with_a_hint(self, tmp_path):
        bot = FakeBot()
        bot.next_file_size = 60 * 1024 * 1024
        called = []
        handle_message(
            bot,
            _cfg(tmp_path),
            self._message(video={"file_id": "BIG", "file_size": 60 * 1024 * 1024,
                                 "mime_type": "video/mp4"}, caption="0:00-0:10"),
            clipper=lambda *a, **k: called.append(1) or [],
        )
        text = "\n".join(t for _, t in bot.messages)
        assert "больше 20 МБ" in text
        assert "http://" in text, "должна быть ссылка на GUI"
        assert called == [], "нарезку не запускаем"

    def test_vertical_caption_word_works_for_files(self, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen.update(vertical=vertical, layout=layout)
            return []

        bot = FakeBot()
        bot.next_file_size = 2 * 1024 * 1024
        handle_message(
            bot,
            _cfg(tmp_path),
            self._message(video={"file_id": "V", "file_size": 2 * 1024 * 1024,
                                 "mime_type": "video/mp4"}, caption="вертикально фон 0:05=10"),
            clipper=clipper,
        )
        assert seen == {"vertical": True, "layout": "blur"}


class TestBotCheckRetry:
    def test_bot_check_schedules_a_retry_and_says_why(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        botmod.RETRY_AT.clear()
        botmod._RETRY_TIMERS.clear()

        def clipper(source, segments, fast, cfg, progress=None):
            raise SourceError("Не удалось скачать: ERROR: Sign in to confirm you're not a bot")

        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
            clipper=clipper,
        )
        text = "\n".join(t for _, t in bot.messages)
        assert "Это ограничение" in text and "IP" in text
        assert "Повторю тот же запрос сам" in text
        assert 1 in botmod.RETRY_AT, "повтор должен быть запланирован"
        assert botmod.cancel_retry(1) is True

    def test_stop_cancels_the_pending_retry(self, tmp_path):
        import video_clipper.bot as botmod

        botmod.RETRY_AT[1] = time.time() + 600
        timer = threading.Timer(600, lambda: None)
        timer.daemon = True
        botmod._RETRY_TIMERS[1] = timer
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "/stop"})
        assert 1 not in botmod.RETRY_AT
        assert any("Отложенный повтор отменил" in t for _, t in bot.messages)

    def test_ordinary_error_is_not_retried(self, tmp_path):
        import video_clipper.bot as botmod

        botmod.RETRY_AT.clear()

        def clipper(source, segments, fast, cfg, progress=None):
            raise RuntimeError("сеть отвалилась")

        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
            clipper=clipper,
        )
        assert botmod.RETRY_AT == {}
        assert any("Ошибка:" in t for _, t in bot.messages)


class TestProgress:
    def test_first_update_sends_a_message(self):
        bot = FakeBot()
        reporter = ProgressReporter(bot, 1, min_interval=0)
        reporter.update("download", 0.42, "18.4 МБ из 44.0 МБ")
        assert bot.messages[-1][1] == "⏳ Скачиваю · 42% · 18.4 МБ из 44.0 МБ"
        assert bot.edits == []

    def test_updates_are_throttled_but_finish_always_lands(self):
        bot = FakeBot()
        reporter = ProgressReporter(bot, 1, min_interval=60)
        reporter.update("download", 0.1, "")
        reporter.update("download", 0.5, "")
        assert bot.edits == [], "внутри интервала править нельзя"
        reporter.finish("✅ Готово")
        assert bot.edits[-1][2] == "✅ Готово"

    def test_heartbeat_keeps_the_message_alive_when_nothing_reports(self):
        """Section downloads run inside ffmpeg and report no progress at all."""
        bot = FakeBot()
        reporter = ProgressReporter(bot, 1, min_interval=0, heartbeat=0.05)
        reporter.start("download", "готовлю источник")
        assert bot.messages[-1][1] == "⏳ Скачиваю · готовлю источник"
        deadline = time.time() + 3
        while not bot.edits and time.time() < deadline:
            time.sleep(0.02)
        reporter.finish("✅ Готово")
        assert bot.edits, "heartbeat обязан был переписать сообщение"
        assert "уже" in bot.edits[0][2] or bot.edits[-1][2] == "✅ Готово"

    def test_encode_text_names_the_clip(self):
        assert ProgressReporter.render("encode", 0.63, "клип 2/3") == "⏳ Кодирую · 63% · клип 2/3"

    def test_fraction_may_be_unknown(self):
        assert ProgressReporter.render("download", None, "7.2 МБ") == "⏳ Скачиваю · 7.2 МБ"

    def test_bot_passes_progress_to_the_clipper_and_reports_done(self, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None):
            seen["progress"] = progress
            progress("encode", 0.5, "клип 1/1")
            progress("encode", 1.0, "клип 1/1")
            return []

        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
            clipper=clipper,
        )
        assert callable(seen["progress"])
        texts = [text for _, text in bot.messages] + [text for _, _, text in bot.edits]
        assert any("Кодирую" in text for text in texts), texts
        assert bot.edits and bot.edits[-1][2] == "✅ Готово, отправляю..."

    def test_error_is_reported_in_the_status_message(self, tmp_path):
        def clipper(source, segments, fast, cfg, progress=None):
            progress("download", 0.1, "")
            raise RuntimeError("сеть отвалилась")

        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
            clipper=clipper,
        )
        assert bot.edits and bot.edits[-1][2] == "⚠️ Ошибка"


class TestPreDownloadCheck:
    def test_range_past_the_end_is_refused_before_download(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "video_clipper.bot.fetch_metadata",
            lambda *a, **kw: {"duration": 923.0, "title": "Ограбление века"},
        )
        calls = []

        def clipper(*args, **kwargs):
            calls.append(args)
            return []

        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-99:00"},
            clipper=clipper,
        )
        assert calls == [], "скачивание не должно начинаться"
        message = "\n".join(text for _, text in bot.messages)
        assert "выходит за конец ролика" in message
        assert "15 мин 23 с" in message
        assert "Ограбление века" in message

    def test_echo_names_the_video_and_its_length(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "video_clipper.bot.fetch_metadata",
            lambda *a, **kw: {"duration": 923.0, "title": "Ограбление века"},
        )
        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
            clipper=lambda *a, **kw: [],
        )
        echo = next(text for _, text in bot.messages if "Понял так" in text)
        assert "«Ограбление века» длится 15 мин 23 с" in echo

    def test_metadata_failure_does_not_block_the_request(self, tmp_path):
        bot = FakeBot()
        handle_message(
            bot,
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
            clipper=lambda *a, **kw: [],
        )
        assert any("Понял так" in text for _, text in bot.messages)


class TestVideoId:
    def test_youtube_watch(self):
        assert video_id_from_url("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=3s") == "jNQXAC9IVRw"

    def test_youtu_be(self):
        assert video_id_from_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_tiktok_short(self):
        assert video_id_from_url("https://vm.tiktok.com/ZSpFs5jXJ/") == "ZSpFs5jXJ"

    def test_garbage(self):
        assert video_id_from_url("https://example.com/") == ""


class TestCacheReuse:
    def test_finds_fresh_file(self, tmp_path):
        f = tmp_path / "Me at the zoo-jNQXAC9IVRw.mp4"
        f.write_bytes(b"x")
        assert find_cached_source(tmp_path, "https://youtu.be/jNQXAC9IVRw", 30) == f

    def test_ignores_old_file(self, tmp_path):
        f = tmp_path / "old-jNQXAC9IVRw.mp4"
        f.write_bytes(b"x")
        old = time.time() - 7200
        os.utime(f, (old, old))
        assert find_cached_source(tmp_path, "https://youtu.be/jNQXAC9IVRw", 30) is None

    def test_ignores_partial(self, tmp_path):
        (tmp_path / "x-jNQXAC9IVRw.mp4.part").write_bytes(b"x")
        assert find_cached_source(tmp_path, "https://youtu.be/jNQXAC9IVRw", 30) is None


class TestSweep:
    def test_deletes_only_old_files(self, tmp_path):
        fresh = tmp_path / "fresh.mp4"
        stale = tmp_path / "stale.mp4"
        fresh.write_bytes(b"12345")
        stale.write_bytes(b"1234567")
        old = time.time() - 7200
        os.utime(stale, (old, old))
        result = sweep([tmp_path], minutes=30)
        assert fresh.exists() and not stale.exists()
        assert result.count == 1 and result.freed_bytes == 7

    def test_minutes_zero_removes_everything(self, tmp_path):
        (tmp_path / "a.mp4").write_bytes(b"1")
        result = sweep([tmp_path], minutes=0)
        assert result.count == 1

    def test_missing_dir_is_fine(self, tmp_path):
        assert sweep([tmp_path / "nope"], minutes=30).count == 0

    def test_describe(self, tmp_path):
        (tmp_path / "a.mp4").write_bytes(b"1234")
        items = describe(tmp_path)
        assert items[0]["name"] == "a.mp4" and items[0]["size"] == 4


class TestWorker:
    def test_job_runs_in_background_thread(self):
        import threading

        from video_clipper.bot import run_in_worker

        done = threading.Event()
        main_thread = threading.current_thread()

        def job():
            assert threading.current_thread() is not main_thread
            done.set()

        run_in_worker(job)
        assert done.wait(5), "задание должно выполниться в отдельном потоке"

    def test_exception_in_job_does_not_kill_worker(self):
        from video_clipper.bot import run_in_worker

        done = __import__("threading").Event()
        run_in_worker(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        run_in_worker(done.set)
        assert done.wait(5)


class TestParseRequest:
    def test_range(self):
        req = parse_request("https://vm.tiktok.com/ZSpFs5jXJ/ 0:10-0:20")
        assert req and req.source.endswith("ZSpFs5jXJ/")
        assert [(s.start, s.end) for s in req.segments] == [(10.0, 20.0)]
        assert req.fast is False

    def test_range_with_omitted_leading_zero(self):
        """Exactly the message from the chat: link on one line, 0:00-:20 on the next."""
        req = parse_request("https://www.youtube.com/watch?v=AAkdEOqCrWI\n0:00-:20")
        assert [(s.start, s.end) for s in req.segments] == [(0.0, 20.0)]

    def test_short_hand_on_the_left(self):
        req = parse_request("https://youtu.be/x :20-1:00")
        assert [(s.start, s.end) for s in req.segments] == [(20.0, 60.0)]

    def test_equals_duration_from_a_link(self):
        req = parse_request("https://youtu.be/x 1:00=0:30")
        assert [(s.start, s.end) for s in req.segments] == [(60.0, 90.0)]

    def test_semicolon_several_ranges(self):
        req = parse_request("https://youtu.be/x\n0:10-0:20; 2:00=45")
        assert [(s.start, s.end) for s in req.segments] == [(10.0, 20.0), (120.0, 165.0)]

    def test_alternate_separators_from_a_link(self):
        req = parse_request("https://youtu.be/x 1/12-1,30")
        assert [(s.start, s.end) for s in req.segments] == [(72.0, 90.0)]

    def test_info_shows_the_gui_address(self):
        cfg = _cfg(Path("/tmp")).with_overrides(gui_host="0.0.0.0", gui_port=8766)
        memo = build_info(cfg)
        assert "ВЕБ-ИНТЕРФЕЙС" in memo
        assert ":8766" in memo and memo.count("http://") >= 1
        assert "Пароль" in memo

    def test_info_command(self):
        bot = FakeBot()
        handle_message(bot, _cfg(Path("/tmp")), {"chat": {"id": 1}, "text": "/info"})
        memo = "\n".join(text for _, text in bot.messages)
        for needle in ("начало=длина", "миллисекунды", "0:10-0:20", "/stop", ";  разделитель"):
            assert needle in memo, needle

    def test_two_ranges(self):
        req = parse_request("https://youtu.be/x 0:01-0:03 1:00-1:10")
        assert len(req.segments) == 2

    def test_pair_without_dash(self):
        req = parse_request("/cut https://youtu.be/x 0:05 0:12")
        assert [(s.start, s.end) for s in req.segments] == [(5.0, 12.0)]

    def test_multiline(self):
        req = parse_request("https://youtu.be/x\n0:10-0:20\n0:30-0:40")
        assert len(req.segments) == 2

    def test_fast_flag(self):
        assert parse_request("https://youtu.be/x 0:01-0:02 fast").fast is True
        assert parse_request("https://youtu.be/x 0:01-0:02 быстро").fast is True

    def test_url_only(self):
        req = parse_request("https://youtu.be/x")
        assert req is not None and req.segments == []

    def test_no_url(self):
        assert parse_request("просто текст") is None
        assert parse_request("") is None

    def test_trailing_punctuation(self):
        req = parse_request("Смотри https://youtu.be/x, 0:01-0:02")
        assert req.source == "https://youtu.be/x"

    def test_split_long(self):
        text = "\n".join("строка " + str(i) for i in range(1000))
        parts = split_long(text, limit=500)
        assert all(len(p) <= 500 for p in parts)
        assert "".join(parts).replace("\n", "") .startswith("строка")


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []
        self.videos: list[tuple[int, Path, str]] = []
        self.actions: list[str] = []
        self.markups: list[dict | None] = []
        self.answered: list[str] = []
        self.edits: list[tuple[int, int, str]] = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    def get_file(self, file_id):
        self.files_requested = getattr(self, "files_requested", []) + [file_id]
        return {"file_path": f"videos/{file_id}.mp4", "file_size": self.next_file_size}

    def download_file(self, file_path, dest, progress=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"video-bytes")
        if progress:
            progress(1.0, 12)
        return Path(dest)

    def send_video(self, chat_id, path, caption="", reply_markup=None):
        self.videos.append((chat_id, Path(path), caption))
        self.markups.append(reply_markup)
        return {"message_id": 100 + len(self.videos)}

    def send_chat_action(self, chat_id, action="upload_video"):
        self.actions.append(action)

    def call(self, method, payload=None, timeout=None):
        if method == "answerCallbackQuery":
            self.answered.append((payload or {}).get("text", ""))
        return {}


def _cfg(tmp_path: Path, chats: tuple[int, ...] = ()):
    return _clean_config().with_overrides(
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "out",
        retention_minutes=30,
        telegram_chat_ids=chats,
    )


def make_clipper(tmp_path: Path):
    def _clip(source, segments, fast, cfg, progress=None):
        if not segments:  # whole-video request
            segments = [Segment(0.0, 1.0)]
        out = tmp_path / "out" / f"clip_{int(segments[0].start)}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return [
            ClipResult(
                source=source,
                segment=segments[0],
                output_path=out,
                duration=segments[0].duration,
                mode="fast" if fast else "accurate",
                size_bytes=3,
            )
        ]

    return _clip


def fake_clip_for_bot(tmp_path):
    def _inner(source, segments, fast, cfg, vertical=False, layout="crop", progress=None):
        out = tmp_path / "out" / "clip.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        results = [
            ClipResult(
                source=source,
                segment=segments[0],
                output_path=out,
                duration=segments[0].duration,
                mode="fast" if fast else "accurate",
                size_bytes=3,
            )
        ]
        job = {
            "path": str(out),
            "segments": [{"start": segments[0].start, "end": segments[0].end}],
            "shown": [{"start": segments[0].start, "end": segments[0].end}],
            "has_audio": True,
            "source": source,
            "vertical": vertical,
            "layout": layout,
            "created": time.time(),
        }
        return results, job

    return _inner


class TestFastButton:
    def test_keyboard_attached_for_accurate_cut(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        monkeypatch.setattr(botmod, "clip_for_bot", fake_clip_for_bot(tmp_path))
        botmod.JOBS.clear()
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x 0:01-0:04"})
        assert bot.markups and bot.markups[0]
        button = bot.markups[0]["inline_keyboard"][0][0]
        assert button["callback_data"].startswith("recut:")
        key = button["callback_data"].split(":")[1]
        assert key in botmod.JOBS

    def test_keyboard_without_recut_button_for_fast_request(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        monkeypatch.setattr(botmod, "clip_for_bot", fake_clip_for_bot(tmp_path))
        botmod.JOBS.clear()
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x 0:01-0:04 fast"})
        labels = [b["text"] for b in bot.markups[0]["inline_keyboard"][0]]
        assert len(labels) == 2
        assert all("Вертикально" in label for label in labels)
        assert not any("Без перекодирования" in label for label in labels)

    def test_vertical_request_hides_vertical_buttons(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        seen = {}

        def fake(source, segments, fast, cfg, vertical=False, layout="crop", progress=None):
            seen["vertical"] = vertical
            return fake_clip_for_bot(tmp_path)(source, segments, fast, cfg)[0], {
                "path": str(tmp_path / "out" / "clip.mp4"),
                "segments": [{"start": 0.0, "end": 1.0}],
                "shown": [{"start": 0.0, "end": 1.0}],
                "has_audio": True,
                "source": source,
                "vertical": True,
                "created": time.time(),
            }

        monkeypatch.setattr(botmod, "clip_for_bot", fake)
        botmod.JOBS.clear()
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x 0:01-0:04 вертикально"})
        assert seen["vertical"] is True
        labels = [b["text"] for b in bot.markups[0]["inline_keyboard"][0]]
        assert all("Вертикально" not in label for label in labels)

    def _callback(self, text="Режу без перекодирования...", data="recut:known"):
        return {"id": "cb1", "data": data, "message": {"chat": {"id": 1}, "message_id": 5}}

    def test_recut_uses_copy_mode(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod
        from video_clipper.bot import JOBS, handle_callback

        source = tmp_path / "src.mp4"
        source.write_bytes(b"src")
        # a real 1-second clip is needed because cut_segment probes its output
        import subprocess

        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(source)],
            check=True,
        )
        JOBS.clear()
        JOBS["known"] = {
            "path": str(source),
            "segments": [{"start": 0.0, "end": 1.0}],
            "shown": [{"start": 10.0, "end": 11.0}],
            "has_audio": True,
            "source": "https://youtu.be/x",
            "created": time.time(),
        }
        bot = FakeBot()
        handle_callback(bot, _cfg(tmp_path), self._callback())
        assert len(bot.videos) == 1
        assert "<00:00:10–00:00:11>" in bot.videos[0][2]
        assert "без перекодирования" in " ".join(text for _, text in bot.messages)
        assert bot.videos[0][1].name.endswith("-fast.mp4")
        assert any("Готово" in text for _, text in bot.messages)

    def test_recut_without_job(self, tmp_path):
        from video_clipper.bot import JOBS, handle_callback

        JOBS.clear()
        bot = FakeBot()
        handle_callback(bot, _cfg(tmp_path), self._callback())
        assert bot.videos == []
        assert any("устарело" in text for text in bot.answered)

    def test_recut_when_source_deleted(self, tmp_path):
        from video_clipper.bot import JOBS, handle_callback

        JOBS.clear()
        JOBS["known"] = {
            "path": str(tmp_path / "gone.mp4"),
            "segments": [{"start": 0.0, "end": 1.0}],
            "shown": [{"start": 0.0, "end": 1.0}],
            "has_audio": True,
            "source": "https://youtu.be/x",
            "created": time.time(),
        }
        bot = FakeBot()
        handle_callback(bot, _cfg(tmp_path), self._callback())
        assert bot.videos == []
        assert any("удал" in text for text in bot.answered)

    def test_foreign_chat_callback_ignored(self, tmp_path):
        from video_clipper.bot import JOBS, handle_callback

        JOBS.clear()
        JOBS["known"] = {"path": "/tmp/x.mp4", "segments": [], "shown": [], "source": "u"}
        bot = FakeBot()
        cfg = _cfg(tmp_path, chats=(42,))
        handle_callback(bot, cfg, {"id": "c", "data": "recut:known", "message": {"chat": {"id": 7}}})
        assert bot.videos == [] and bot.answered == []


class TestWholeVideo:
    def test_link_without_timecodes_is_whole_video(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x"}, clipper=make_clipper(tmp_path))
        assert len(bot.videos) == 1
        assert any("целиком" in text for _, text in bot.messages)

    def test_whole_video_job_and_buttons(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        seen = {}

        def fake(source, segments, fast, cfg, vertical=False, layout="crop", progress=None):
            seen["segments"] = list(segments)
            out = tmp_path / "out" / "whole.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mp4")
            job = {
                "path": str(out),
                "segments": [{"start": 0.0, "end": 60.0}],
                "shown": [{"start": 0.0, "end": 60.0}],
                "has_audio": True,
                "source": source,
                "vertical": False,
                "layout": "crop",
                "whole": True,
                "created": time.time(),
            }
            return [ClipResult(source=source, segment=Segment(0, 60), output_path=out,
                              duration=60.0, mode="whole (копия)", size_bytes=3)], job

        monkeypatch.setattr(botmod, "clip_for_bot", fake)
        botmod.JOBS.clear()
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x"})
        assert seen["segments"] == []
        assert bot.markups[0] is not None
        assert len(bot.videos) == 1
        assert botmod.JOBS, "задание должно быть сохранено для кнопок"


def _request(text: str):
    request = parse_request(text)
    assert request is not None, text
    return request


class TestEstimate:
    def _estimate(self, text):
        value = estimate_seconds(_request(text))
        assert value is not None, text
        return value

    def test_range_is_sane_for_a_short_clip(self):
        low, high = self._estimate("https://youtu.be/x 0:00-0:12")
        assert 10 <= low < high <= 90

    def test_fast_is_quicker_than_accurate(self):
        fast = self._estimate("https://youtu.be/x 0:00-1:00 fast")
        accurate = self._estimate("https://youtu.be/x 0:00-1:00")
        assert fast[1] < accurate[1]

    def test_vertical_and_blur_add_time(self):
        plain = self._estimate("https://youtu.be/x 0:00-1:00")
        vertical = self._estimate("https://youtu.be/x 0:00-1:00 вертикально")
        blurred = self._estimate("https://youtu.be/x 0:00-1:00 вертикально фон")
        assert plain[0] < vertical[0] < blurred[0]

    def test_grows_with_length(self):
        short = self._estimate("https://youtu.be/x 0:00-0:20")
        long = self._estimate("https://youtu.be/x 0:00-10:00")
        assert long[1] > short[1] * 5

    def test_whole_video_has_no_estimate(self):
        assert estimate_seconds(_request("https://youtu.be/x")) is None

    def test_echo_shows_the_estimate(self):
        text = describe_request(_request("https://youtu.be/x 0:00-0:12"))
        assert "Ожидаю примерно" in text
        assert text.rstrip().endswith("Режу...")


class TestClipCaption:
    def test_format_is_title_link_timecodes_duration(self):
        caption = clip_caption("Ограбление века", "https://youtu.be/ID", Segment(0, 12), 12.08)
        assert caption == "Ограбление века - https://youtu.be/ID - <00:00:00–00:00:12> - 12.08 с"

    def test_long_title_is_trimmed(self):
        caption = clip_caption("я" * 100, "https://youtu.be/ID", Segment(0, 12), 12.0)
        assert caption.startswith("я" * 69 + "…")
        assert "<00:00:00–00:00:12>" in caption

    def test_missing_title_falls_back(self):
        assert clip_caption("", "https://youtu.be/ID", Segment(5, 7), 2.0).startswith("видео - ")

    def test_bot_caption_contains_title_link_and_timecodes(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x 0:00-0:12"},
                       clipper=make_clipper(tmp_path))
        assert bot.videos, "видео должно уйти"
        caption = bot.videos[0][2]
        assert caption.endswith("<00:00:00–00:00:12> - 12.00 с")


class TestDescribeRequest:
    def test_echoes_a_single_range(self):
        text = describe_request(_request("https://youtu.be/x 0:00-:20"))
        assert "0:00–0:20" in text and "20 с" in text and "Режу..." in text

    def test_echoes_duration_as_start_and_end(self):
        text = describe_request(_request("https://youtu.be/x 1:00=0:30"))
        assert "1:00–1:30" in text

    def test_numbers_several_ranges_and_names_the_mode(self):
        text = describe_request(_request("https://youtu.be/x 0:10-0:20; 2:00=45 fast вертикально"))
        assert text.startswith("Понял так — 2 отрезка, режим: без перекодирования, вертикально 9:16")
        assert "1. 0:10–0:20 (10 с)" in text
        assert "2. 2:00–2:45 (45 с)" in text

    def test_whole_video_is_said_plainly(self):
        assert "беру ролик целиком" in describe_request(_request("https://youtu.be/x"))

    def test_long_lists_are_truncated(self):
        ranges = "; ".join(f"{i}:00-{i}:10" for i in range(12))
        text = describe_request(_request(f"https://youtu.be/x {ranges}"))
        assert "Понял так — 12 отрезков" in text
        assert "...и ещё 4" in text

    def test_bot_sends_the_echo(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x 0:10-0:20"},
                       clipper=make_clipper(tmp_path))
        assert any("Понял так" in text for _, text in bot.messages)


class TestControls:
    def test_stop_sets_flag_and_replies(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        calls = []
        monkeypatch.setattr(
            botmod.subprocess,
            "run",
            lambda *a, **kw: calls.append(a) or type("R", (), {"returncode": 0})(),
        )
        botmod.STOP_REQUESTED.clear()
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "/stop"})
        assert botmod.STOP_REQUESTED.is_set()
        assert calls and calls[0][0][0] == "pkill"
        assert any("Останавливаю" in text for _, text in bot.messages)
        botmod.STOP_REQUESTED.clear()

    def test_resume_repeats_last_request(self, tmp_path, monkeypatch):
        import video_clipper.bot as botmod

        seen = []

        def fake_worker(fn, *args):
            seen.append(args[2]["text"] if len(args) > 2 else "")

        monkeypatch.setattr(botmod, "run_in_worker", fake_worker)
        botmod.LAST_REQUESTS.clear()
        botmod.LAST_REQUESTS[1] = botmod.Request(
            source="https://youtu.be/x", segments=[Segment(10, 20)], fast=True
        )
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "/resume"})
        assert seen == ["https://youtu.be/x 10-20 fast"]

    def test_resume_without_request(self, tmp_path):
        from video_clipper.bot import LAST_REQUESTS

        LAST_REQUESTS.clear()
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "/resume"})
        assert any("Нечего повторять" in text for _, text in bot.messages)

    def test_new_clears_state_and_files(self, tmp_path):
        import video_clipper.bot as botmod

        cfg = _cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"1")
        botmod.LAST_REQUESTS[1] = botmod.Request(source="u", segments=[Segment(1, 2)])
        botmod.JOBS["k"] = {"path": "x"}
        bot = FakeBot()
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/new"})
        assert 1 not in botmod.LAST_REQUESTS
        assert botmod.JOBS == {}
        assert not (cfg.cache_dir / "a.mp4").exists()
        assert any("чистого листа" in text for _, text in bot.messages)

    def test_whole_video_request_is_remembered_for_resume(self, tmp_path):
        import video_clipper.bot as botmod

        botmod.LAST_REQUESTS.clear()
        handle_message(
            bot := FakeBot(),
            _cfg(tmp_path),
            {"chat": {"id": 1}, "text": "https://youtu.be/x"},
            clipper=make_clipper(tmp_path),
        )
        assert botmod.LAST_REQUESTS[1].source == "https://youtu.be/x"
        assert botmod.LAST_REQUESTS[1].segments == []


class TestHandleMessage:
    def test_sends_video(self, tmp_path):
        bot = FakeBot()
        cfg = _cfg(tmp_path)
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "https://youtu.be/x 0:01-0:04"}, clipper=make_clipper(tmp_path))
        assert len(bot.videos) == 1
        assert bot.videos[0][0] == 1
        assert any("Понял так" in text for _, text in bot.messages)

    def test_rejects_foreign_chat(self, tmp_path):
        bot = FakeBot()
        cfg = _cfg(tmp_path, chats=(42,))
        handle_message(bot, cfg, {"chat": {"id": 7}, "text": "https://youtu.be/x 0:01-0:04"}, clipper=make_clipper(tmp_path))
        assert bot.messages == [] and bot.videos == []

    def test_allows_listed_chat(self, tmp_path):
        bot = FakeBot()
        cfg = _cfg(tmp_path, chats=(7,))
        handle_message(bot, cfg, {"chat": {"id": 7}, "text": "https://youtu.be/x 0:01-0:04"}, clipper=make_clipper(tmp_path))
        assert len(bot.videos) == 1

    def test_help(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "/help"})
        assert "Пришли ссылку" in bot.messages[0][1]

    def test_id_command(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 99}, "text": "/id"})
        assert "99" in bot.messages[0][1]

    def test_status_lists_files(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"1")
        bot = FakeBot()
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/status"})
        assert "a.mp4" in bot.messages[0][1]

    def test_clean_removes_files(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"1")
        bot = FakeBot()
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "/clean"})
        assert not (cfg.cache_dir / "a.mp4").exists()
        assert "Удалено 1" in bot.messages[0][1]

    def test_parse_vertical_keywords(self):
        for word in ("вертикально", "vertical", "9:16", "шортс", "reels"):
            assert parse_request(f"https://youtu.be/x 0:01-0:02 {word}").vertical is True, word

    def test_parse_vertical_blur(self):
        req = parse_request("https://youtu.be/x 0:01-0:02 вертикально размытый фон")
        assert req.vertical is True and req.layout == "blur"

    def test_parse_plain_request_is_not_vertical(self):
        req = parse_request("https://youtu.be/x 0:01-0:02")
        assert req.vertical is False and req.layout == "crop"

    def test_link_without_timecodes_delivers_whole_video(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x"}, clipper=make_clipper(tmp_path))
        assert len(bot.videos) == 1
        assert "целиком" in bot.messages[0][1].lower()

    def test_no_url(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "привет"})
        assert "ссылк" in bot.messages[0][1].lower()

    def test_clipper_error_is_reported(self, tmp_path):
        def boom(source, segments, fast, cfg, progress=None):
            raise RuntimeError("сайт недоступен")

        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x 0:01-0:02"}, clipper=boom)
        assert any("сайт недоступен" in text for _, text in bot.messages)
