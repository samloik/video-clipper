"""Tests for the Telegram front-end and the retention policy (no network)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from video_clipper.bot import describe_request, handle_message, parse_request, split_long
from video_clipper.config import load_config
from video_clipper.cut import ClipResult
from video_clipper.segments import Segment
from video_clipper.storage import describe, find_cached_source, sweep, video_id_from_url


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

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

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
    return load_config(None).with_overrides(
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
    def _inner(source, segments, fast, cfg, vertical=False, layout="crop"):
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

        def fake(source, segments, fast, cfg, vertical=False, layout="crop"):
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
        assert "fast" in bot.videos[0][2]
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

        def fake(source, segments, fast, cfg, vertical=False, layout="crop"):
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
