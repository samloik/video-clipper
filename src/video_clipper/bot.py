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

import http.client
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
    "Слово «fast» отключает перекодирование (быстро, но границы по ключевому кадру).\n"
    "У готового ролика есть кнопка «⚡ Без перекодирования» — перережет тот же отрезок\n"
    "за секунды из уже скачанного файла.\n\n"
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


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials through a SOCKS proxy.

    The proxy is applied per connection instead of replacing ``socket.socket``
    globally: a global patch silently breaks yt-dlp's own networking (it showed
    up as "Unable to download API page: 2 bytes missing").
    """

    def __init__(self, *args, proxy: tuple[str, int] | None = None, **kwargs):
        self._proxy = proxy
        super().__init__(*args, **kwargs)

    def connect(self) -> None:  # noqa: D102
        import socks

        if self._proxy is None:
            return super().connect()
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, self._proxy[0], self._proxy[1], rdns=True)
        sock.settimeout(self.timeout)
        sock.connect((self.host, self.port))
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: tuple[str, int]):
        super().__init__()
        self._proxy = proxy

    def https_open(self, req):  # noqa: D102
        return self.do_open(
            lambda *a, **kw: _SocksHTTPSConnection(*a, proxy=self._proxy, **kw), req
        )


def build_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    """Opener that talks to the Bot API directly or through SOCKS5."""
    if not proxy_url:
        return urllib.request.build_opener()
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname:
        log.warning("Прокси %s не SOCKS5 — обращаюсь к Telegram напрямую", proxy_url)
        return urllib.request.build_opener()
    return urllib.request.build_opener(_SocksHTTPSHandler((parsed.hostname, parsed.port or 1080)))


class Telegram:
    """Minimal Bot API client."""

    def __init__(self, token: str, timeout: int = 60, proxy_url: str | None = None):
        self.token = token
        self.timeout = timeout
        self.proxy_url = proxy_url
        self._opener = build_opener(proxy_url)

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    def call(self, method: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            self._url(method), data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"{method}: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TelegramError(f"{method}: {exc}") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body.get("result", {})

    def get_updates(self, offset: int | None, long_poll: int = 50) -> list[dict]:
        payload: dict = {"timeout": long_poll, "allowed_updates": ["message", "callback_query"]}
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

    def send_video(
        self,
        chat_id: int,
        path: Path | str,
        caption: str = "",
        reply_markup: dict | None = None,
    ) -> dict:
        path = Path(path)
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        fields = {"chat_id": str(chat_id), "caption": caption[:1024], "supports_streaming": "true"}
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
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
            with self._opener.open(req, timeout=300) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"sendVideo: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"sendVideo: {payload.get('description')}")
        return payload.get("result", {})


# job key -> what is needed to re-cut without touching the network again
JOBS: dict[str, dict] = {}


def fast_keyboard(job_key: str) -> dict:
    """Inline button that re-cuts the same fragment with -c copy."""
    return {
        "inline_keyboard": [
            [{"text": "⚡ Без перекодирования (быстро)", "callback_data": f"recut:{job_key}"}]
        ]
    }


def clip_for_bot(
    source: str,
    segments: list[Segment],
    fast: bool,
    cfg: Config,
) -> tuple[list, dict]:
    """Cut for the bot and keep the source file so a re-cut is instant.

    The CLI removes the download after cutting; the bot must not, otherwise the
    "без перекодирования" button would have to download everything again.
    """
    from .cut import cut_segment
    from .segments import output_filename, source_slug, validate_segments
    from .source import prepare_source, shift_segments

    src = prepare_source(source, cfg, segments=[(s.start, s.end) for s in segments], keep_source=True)
    shifted = shift_segments(segments, src.offset)
    validate_segments(shifted, duration=src.info.duration or None)

    out_dir = Path(cfg.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = source_slug(source)
    results = []
    for i, seg in enumerate(shifted):
        shown = segments[i] if i < len(segments) else seg
        result = cut_segment(
            str(src.path),
            out_dir / output_filename(slug, shown),
            seg,
            fast=fast,
            ffmpeg_path=cfg.ffmpeg_path,
            has_audio=src.info.has_audio,
        )
        result.segment = shown
        results.append(result)
    job = {
        "path": str(src.path),
        "segments": [{"start": s.start, "end": s.end} for s in shifted],
        "shown": [{"start": s.start, "end": s.end} for s in segments],
        "has_audio": src.info.has_audio,
        "source": source,
    }
    return results, job


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
        JOBS.clear()
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

    job: dict | None = None
    try:
        if clipper is not None:
            results = clipper(request.source, request.segments, request.fast, cfg, progress=None)
        else:
            results, job = clip_for_bot(request.source, request.segments, request.fast, cfg)
    except Exception as exc:  # noqa: BLE001 — the chat is the error channel
        log.exception("Ошибка нарезки")
        bot.send_message(chat_id, f"Ошибка: {exc}")
        return

    job_key = ""
    if job is not None and not request.fast:
        job_key = uuid.uuid4().hex[:8]
        job["created"] = time.time()
        JOBS[job_key] = job

    for index, result in enumerate(results):
        caption = f"{result.segment.label} · {result.mode} · {result.duration:.2f} с"
        is_last = index == len(results) - 1
        markup = fast_keyboard(job_key) if job_key and is_last else None
        try:
            bot.send_video(chat_id, Path(result.output_path), caption, reply_markup=markup)
        except TelegramError as exc:
            bot.send_message(chat_id, f"Не смог отправить видео: {exc}")

    bot.send_message(
        chat_id,
        f"Готово: {len(results)}. Файлы удалю через {cfg.retention_minutes} мин тишины "
        f"(или сразу — /clean).",
    )


def handle_callback(bot, cfg: Config, callback: dict) -> None:
    """Re-cut the same fragment with -c copy (the ⚡ button)."""
    from .cut import cut_segment

    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    callback_id = callback.get("id")

    def answer(text: str) -> None:
        try:
            bot.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
        except TelegramError:
            pass

    if not chat_id or (cfg.telegram_chat_ids and chat_id not in cfg.telegram_chat_ids):
        return
    if not data.startswith("recut:"):
        answer("Не понимаю команду")
        return

    job = JOBS.get(data.split(":", 1)[1]) if ":" in data else None
    if not job:
        answer("Это задание уже устарело — пришли ссылку заново")
        return
    source_path = Path(job["path"])
    if not source_path.is_file():
        answer("Исходник уже удалён — пришли ссылку заново")
        return

    answer("Режу без перекодирования...")
    bot.send_chat_action(chat_id, "record_video")

    from .segments import Segment, output_filename, source_slug

    out_dir = Path(cfg.output_dir).expanduser()
    slug = source_slug(job["source"])
    sent = 0
    for i, raw in enumerate(job["segments"]):
        seg = Segment(raw["start"], raw["end"])
        shown_raw = job["shown"][i] if i < len(job["shown"]) else raw
        shown = Segment(shown_raw["start"], shown_raw["end"])
        try:
            result = cut_segment(
                str(source_path),
                out_dir / output_filename(slug, shown, name=f"{slug}_{shown.start:.0f}_{shown.end:.0f}_fast"),
                seg,
                fast=True,
                ffmpeg_path=cfg.ffmpeg_path,
                has_audio=bool(job.get("has_audio", True)),
            )
        except Exception as exc:  # noqa: BLE001
            bot.send_message(chat_id, f"Не смог перерезать: {exc}")
            continue
        try:
            bot.send_video(
                chat_id,
                Path(result.output_path),
                f"{shown.label} · fast (без перекодирования) · {result.duration:.2f} с",
            )
            sent += 1
        except TelegramError as exc:
            bot.send_message(chat_id, f"Не смог отправить видео: {exc}")
    if sent:
        bot.send_message(
            chat_id,
            "Готово без перекодирования. Границы могли сместиться к ключевому кадру; "
            "точный вариант остался в чате выше.",
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

    bot = Telegram(token, proxy_url=cfg.proxy)
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
            try:
                if update.get("callback_query"):
                    handle_callback(bot, cfg, update["callback_query"])
                else:
                    handle_message(bot, cfg, update.get("message") or {})
            except Exception:  # noqa: BLE001 — one bad update must not kill the loop
                log.exception("Сбой обработки обновления")
        if time.time() - last_sweep > 60:
            sweep([cfg.cache_dir, Path(cfg.output_dir)], cfg.retention_minutes)
            cutoff = time.time() - cfg.retention_minutes * 60
            for key in [k for k, v in JOBS.items() if v.get("created", 0) < cutoff]:
                JOBS.pop(key, None)
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
