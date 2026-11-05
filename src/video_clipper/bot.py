"""Telegram bot front-end: link + timecodes in, video clips back, nothing kept.

Runs as a systemd user unit (`video-clipper-bot`). No third-party dependency: the
Bot API is called over HTTPS with urllib, including the multipart upload.

Requests look like::

    https://vm.tiktok.com/ZSpFs5jXJ/ 0:10-0:20
    /cut https://youtu.be/ID 0:05 0:12 fast
    <link>
    0:10-0:20
    1:00-1:15

Commands: /start, /help, /id, /status, /clean.
Files live only for `retention_minutes` (30 by default): the downloaded source is
kept while the session continues so a second cut of the same link does not
re-download it, and everything is swept once the conversation goes quiet.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, load_config
from .segments import Segment
from .storage import describe, sweep

log = logging.getLogger("video_clipper")

API_BASE = "https://api.telegram.org"
URL_RE = re.compile(r"https?://\S+")
RANGE_RE = re.compile(r"(\d[\d:]*(?:[.,]\d+)?)\s*(?:-|–|—|\.\.)\s*(\d[\d:]*(?:[.,]\d+)?)")
TIME_RE = re.compile(r"^\d[\d:]*(?:[.,]\d+)?$")
HELP = (
    "Пришли ссылку и таймкоды — вырежу и отправлю ролик.\n\n"
    "Примеры:\n"
    "  https://vm.tiktok.com/XXXX/ 0:10-0:20\n"
    "  https://youtu.be/ID 0:05 0:12\n"
    "  <ссылка> и на следующей строке 1:00-1:15\n\n"
    "Несколько отрезков — несколько пар через пробел или запятую.\n"
    "Слово «fast» отключает перекодирование (быстро, но границы по ключевому кадру).\n\n"
    "/status — что лежит на машине, /clean — удалить всё сейчас.\n"
    f"Файлы удаляются автоматически через {{minutes}} мин тишины."
)


@dataclass
class Request:
    source: str
    segments: list[Segment] = field(default_factory=list)
    fast: bool = False


class TelegramError(RuntimeError):
    pass


class Telegram:
    """Minimal Bot API client."""

    def __init__(self, token: str, timeout: int = 60):
        self.token = token
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    def call(self, method: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            self._url(method), data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"{method}: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TelegramError(f"{method}: {exc}") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body.get("result", {})

    def get_updates(self, offset: int | None, long_poll: int = 50) -> list[dict]:
        payload: dict = {"timeout": long_poll, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=long_poll + 20)

    def send_message(self, chat_id: int, text: str) -> dict:
        return self.call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})

    def send_chat_action(self, chat_id: int, action: str = "upload_video") -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)
        except TelegramError:  # cosmetic
            pass

    def send_video(self, chat_id: int, path: Path | str, caption: str = "") -> dict:
        path = Path(path)
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        fields = {"chat_id": str(chat_id), "caption": caption[:1024], "supports_streaming": "true"}
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        req = urllib.request.Request(
            self._url("sendVideo"),
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"sendVideo: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"sendVideo: {payload.get('description')}")
        return payload.get("result", {})


def parse_request(text: str) -> Request | None:
    """Extract the link, its timecode ranges and the fast flag from a message."""
    if not text:
        return None
    raw = text.strip()
    url_match = URL_RE.search(raw)
    if not url_match:
        return None
    source = url_match.group(0).rstrip(".,)")
    rest = raw.replace(url_match.group(0), " ")

    segments: list[Segment] = []
    for left, right in RANGE_RE.findall(rest):
        segments.append(Segment(_num(left), _num(right)))

    if not segments:
        times = [tok for tok in re.split(r"[\s,;]+", rest) if TIME_RE.match(tok)]
        if len(times) >= 2:
            for i in range(0, len(times) - 1, 2):
                segments.append(Segment(_num(times[i]), _num(times[i + 1])))

    fast = bool(re.search(r"\b(fast|быстро|быстрый)\b", rest, re.IGNORECASE))
    if not segments:
        return Request(source=source, segments=[], fast=fast)
    return Request(source=source, segments=segments, fast=fast)


def _num(token: str) -> float:
    from .timecode import parse_timecode

    return parse_timecode(token)


def split_long(text: str, limit: int = 3800) -> list[str]:
    """Telegram caps a message at 4096 characters."""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def handle_message(bot, cfg: Config, message: dict, clipper=None) -> None:
    """One incoming message → reply, clips, cleanup."""
    chat_id = int(message.get("chat", {}).get("id", 0))
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if cfg.telegram_chat_ids and chat_id not in cfg.telegram_chat_ids:
        log.warning("Отклонено сообщение от чужого chat_id: %s", chat_id)
        return

    command = text.split()[0].lower() if text.startswith("/") else ""
    if command in {"/start", "/help"}:
        bot.send_message(chat_id, HELP.format(minutes=cfg.retention_minutes))
        return
    if command == "/id":
        bot.send_message(chat_id, f"chat_id: {chat_id}")
        return
    if command == "/status":
        lines = []
        for label, directory in (("оригиналы", cfg.cache_dir), ("клипы", Path(cfg.output_dir))):
            items = describe(directory)
            total = sum(item["size"] for item in items) / 1048576
            lines.append(f"{label}: {len(items)} файл(ов), {total:.1f} МБ")
            for item in items[:10]:
                lines.append(f"  · {item['name']} ({item['age_minutes']} мин)")
        lines.append(f"Удаление через {cfg.retention_minutes} мин тишины")
        bot.send_message(chat_id, "\n".join(lines))
        return
    if command == "/clean":
        result = sweep([cfg.cache_dir, Path(cfg.output_dir)], minutes=0)
        bot.send_message(
            chat_id, f"Удалено {result.count} файл(ов), {result.freed_bytes / 1048576:.1f} МБ"
        )
        return

    request = parse_request(text)
    if request is None:
        bot.send_message(chat_id, "Не вижу ссылки. Пришли ссылку и таймкоды, например:\nhttps://youtu.be/ID 0:10-0:20")
        return
    if not request.segments:
        bot.send_message(
            chat_id,
            "Ссылку вижу, таймкодов нет. Напиши начало и конец, например `0:10-0:20`.",
        )
        return

    bot.send_message(
        chat_id,
        f"Принял: {len(request.segments)} отрезок(ов), режим {'fast' if request.fast else 'точный'}. Режу...",
    )
    bot.send_chat_action(chat_id, "record_video")

    from .gui import default_clipper

    run = clipper or default_clipper
    try:
        results = run(
            request.source,
            request.segments,
            request.fast,
            cfg,
            progress=None,
        )
    except Exception as exc:  # noqa: BLE001 — the chat is the error channel
        log.exception("Ошибка нарезки")
        bot.send_message(chat_id, f"Ошибка: {exc}")
        return

    for result in results:
        caption = f"{result.segment.label} · {result.mode} · {result.duration:.2f} с"
        try:
            bot.send_video(chat_id, Path(result.output_path), caption)
        except TelegramError as exc:
            bot.send_message(chat_id, f"Не смог отправить видео: {exc}")

    bot.send_message(
        chat_id,
        f"Готово: {len(results)}. Файлы удалю через {cfg.retention_minutes} мин тишины "
        f"(или сразу — /clean).",
    )


def read_token(token_file: Path) -> str | None:
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def run_bot(config_path: str | None = None, token_file: Path | None = None) -> int:
    import os

    cfg = load_config(config_path)
    from .cli import setup_logging

    setup_logging(cfg.log_file)

    token = os.environ.get("VC_TELEGRAM_TOKEN") or read_token(token_file or cfg.telegram_token_file)
    if not token:
        log.error(
            "Нет токена бота. Положи его в %s (chmod 600) или задай VC_TELEGRAM_TOKEN.",
            cfg.telegram_token_file,
        )
        return 2

    bot = Telegram(token)
    me = bot.call("getMe")
    log.info("Бот @%s запущен, хранение %d мин", me.get("username"), cfg.retention_minutes)
    print(f"video-clipper-bot: @{me.get('username')}, retention {cfg.retention_minutes} мин")

    offset: int | None = None
    last_sweep = 0.0
    while True:
        try:
            updates = bot.get_updates(offset)
        except TelegramError as exc:
            log.warning("Ошибка getUpdates: %s — пауза 5 с", exc)
            time.sleep(5)
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            try:
                handle_message(bot, cfg, message)
            except Exception:  # noqa: BLE001 — one bad message must not kill the loop
                log.exception("Сбой обработки сообщения")
        if time.time() - last_sweep > 60:
            sweep([cfg.cache_dir, Path(cfg.output_dir)], cfg.retention_minutes)
            last_sweep = time.time()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="video-clipper-bot",
        description="Telegram-бот video-clipper: ссылка + таймкоды → готовый ролик.",
    )
    parser.add_argument("--config", help="путь к config.json")
    parser.add_argument("--token-file", help="файл с токеном BotFather")
    args = parser.parse_args()
    try:
        return run_bot(config_path=args.config, token_file=Path(args.token_file) if args.token_file else None)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
