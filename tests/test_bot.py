"""Tests for the Telegram front-end and the retention policy (no network)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from video_clipper.bot import handle_message, parse_request, split_long
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


class TestParseRequest:
    def test_range(self):
        req = parse_request("https://vm.tiktok.com/ZSpFs5jXJ/ 0:10-0:20")
        assert req and req.source.endswith("ZSpFs5jXJ/")
        assert [(s.start, s.end) for s in req.segments] == [(10.0, 20.0)]
        assert req.fast is False

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

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    def send_video(self, chat_id, path, caption=""):
        self.videos.append((chat_id, Path(path), caption))

    def send_chat_action(self, chat_id, action="upload_video"):
        self.actions.append(action)


def _cfg(tmp_path: Path, chats: tuple[int, ...] = ()):
    return load_config(None).with_overrides(
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "out",
        retention_minutes=30,
        telegram_chat_ids=chats,
    )


def make_clipper(tmp_path: Path):
    def _clip(source, segments, fast, cfg, progress=None):
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


class TestHandleMessage:
    def test_sends_video(self, tmp_path):
        bot = FakeBot()
        cfg = _cfg(tmp_path)
        handle_message(bot, cfg, {"chat": {"id": 1}, "text": "https://youtu.be/x 0:01-0:04"}, clipper=make_clipper(tmp_path))
        assert len(bot.videos) == 1
        assert bot.videos[0][0] == 1
        assert any("Принял" in text for _, text in bot.messages)

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

    def test_missing_timecodes(self, tmp_path):
        bot = FakeBot()
        handle_message(bot, _cfg(tmp_path), {"chat": {"id": 1}, "text": "https://youtu.be/x"}, clipper=make_clipper(tmp_path))
        assert bot.videos == []
        assert "таймкод" in bot.messages[0][1].lower()

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
