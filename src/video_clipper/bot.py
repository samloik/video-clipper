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
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config, load_config
from .segments import Segment, parse_range_spec
from .source import ChallengeCooldown, challenge_until, fetch_metadata, is_bot_check, is_url
from .storage import describe, sweep

log = logging.getLogger("video_clipper")

API_BASE = "https://api.telegram.org"
URL_RE = re.compile(r"https?://\S+")
# Characters a timecode may consist of: digits and any of the component
# separators the parser understands (":12" is a valid 0:12).
_TC_CHARS_RE = re.compile(r"^[\d:\\/.,]+$")
INFO = """Как присылать запрос

Ссылка и таймкоды в одном сообщении — в строку или с новой строки:
https://youtu.be/ID 0:10-0:20

ЧАСТИ ТАЙМКОДА разделяются любым из символов  :  ,  .  \  /
  45          -> 0:00:45      одна часть — секунды
  :12         -> 0:00:12      ведущий ноль можно опустить (но ":45" — это 45 с, а не 3:45)
  34:43       -> 0:34:43      две части — минуты:секунды
  1:12  1.12  1/12  1\12  1,12   -> 0:01:12
  1:02:03     -> 1:02:03      три части — часы:минуты:секунды
  1:12.500    -> 72.5 с       три цифры после точки/запятой — миллисекунды

ОТРЕЗКИ
  начало-конец   0:10-0:20          с 10-й по 20-ю секунду
  начало=длина   1:00=0:30          с 1:00 длиной 30 с, то есть 1:00–1:30
  несколько      ;  разделитель:  0:10-0:20; 2:00=45; 3:30-3:45
  можно и парой   0:10 0:20         два таймкода подряд — начало и конец

РЕЖИМЫ — словом в том же сообщении
  fast / быстро     без перекодирования, границы по ключевым кадрам (в разы быстрее)
  вертикально       вертикальный кадр 9:16 (обрезает бока)
  фон / размыт      вертикальный кадр с размытой подложкой

КОМАНДЫ
  /info     эта памятка
  /status   что лежит на машине и сколько ему жить
  /stop     прервать текущую нарезку
  /resume   повторить последний запрос
  /new      забыть запрос и удалить файлы
  /clean    только удалить файлы
  /id       chat_id для конфига

ОГРАНИЧЕНИЯ
  Telegram принимает файлы до 50 МБ — больший клип бот не отправит.
  Режется один ролик за раз, следующие ждут в очереди.
  Файлы живут retention_minutes (обычно 30 минут) после последнего использования.
  Если YouTube требует подтверждение (бот-чек), бот делает паузу и повторяет
  запрос сам через 15 минут — отменить повтор можно командой /stop."""


HELP = (
    "Пришли ссылку и таймкоды — вырежу и отправлю ролик.\n\n"
    "Примеры:\n"
    "  https://vm.tiktok.com/XXXX/ 0:10-0:20\n"
    "  https://youtu.be/ID 0:05 0:12\n"
    "  <ссылка> и на следующей строке 1:00-1:15\n\n"
    "Начало=длительность: 1:00=0:30 — с 1:00 тридцать секунд. Несколько отрезков — через ;\n"
    "Вместо двоеточия подойдёт , . \\ / — «1/12» это 1 мин 12 с. Подробности — /info\n"
    "Слово «fast» отключает перекодирование (быстро, но границы по ключевому кадру).\n"
    "У готового ролика есть кнопки: «⚡ Без перекодирования» — тот же отрезок за секунды\n"
    "из уже скачанного файла, и «📱 Вертикально 9:16» — кадр под смартфон (можно с размытым фоном).\n"
    "Словом тоже можно: «вертикально», «9:16», «шортс», а «размытый фон» — фон вместо обрезки.\n"
    "Пришли только ссылку без таймкодов — отдам ролик целиком (большие файлы сожму до 45 МБ).\n\n"
    "/stop — остановить текущую нарезку, /resume — повторить последний запрос.\n"
    "/new — начать с чистого листа, /clean — только удалить файлы, /status — что на машине.\n"
    f"Файлы удаляются автоматически через {{minutes}} мин тишины."
)


@dataclass
class Request:
    source: str
    segments: list[Segment] = field(default_factory=list)
    fast: bool = False
    vertical: bool = False
    layout: str = "crop"


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

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Rewrite the status message; cosmetic failures must not break the job."""
        try:
            self.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
        except TelegramError:
            pass

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

# chat_id -> last request, so /resume can repeat it
LAST_REQUESTS: dict[int, "Request"] = {}

# /stop raises this; ffmpeg children of the bot are killed, downloads abort via
# yt-dlp progress hooks
STOP_REQUESTED = threading.Event()


class JobCancelled(RuntimeError):
    """Raised when the user asked to stop the running job."""

# Telegram polling must never wait for ffmpeg: one heavy job at a time runs in a
# background thread, so buttons and messages keep being accepted meanwhile.
WORK_SLOT = threading.Semaphore(1)
FFMPEG_TIMEOUT = 1800


def send_video_smart(bot, chat_id: int, path: Path, caption: str, has_audio: bool = True,
                     reply_markup: dict | None = None) -> None:
    """Send a video, shrinking it first when Telegram would refuse it."""
    from .cut import TELEGRAM_LIMIT_BYTES, shrink_to_fit

    path = Path(path)
    if path.stat().st_size > TELEGRAM_LIMIT_BYTES:
        bot.send_message(
            chat_id,
            f"Файл {path.stat().st_size / 1048576:.0f} МБ — больше лимита Telegram, "
            f"сжимаю до 45 МБ...",
        )
        shrunk = path.with_name(path.stem + "_small.mp4")
        path = shrink_to_fit(
            path,
            shrunk,
            limit_bytes=TELEGRAM_LIMIT_BYTES,
            has_audio=has_audio,
            timeout=FFMPEG_TIMEOUT,
        )
        caption += " · сжато под лимит"
    bot.send_video(chat_id, path, caption, reply_markup=reply_markup)


def run_in_worker(fn, *args) -> None:
    """Start ``fn`` in a thread, serialised by WORK_SLOT."""

    def _wrap() -> None:
        with WORK_SLOT:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 — a failed job must not kill the thread
                log.exception("Сбой фонового задания")

    threading.Thread(target=_wrap, daemon=True).start()


def recut_keyboard(
    job_key: str,
    vertical: bool = False,
    layout: str = "crop",
    allow_recut: bool = True,
) -> dict:
    """Buttons under the clip: other useful variants of the same fragment."""
    row: list[dict] = []
    if vertical:
        # Already vertical: offer the other framing, nothing else.
        if layout == "blur":
            row.append({"text": "📱 9:16 без размытия", "callback_data": f"vert:{job_key}"})
        else:
            row.append({"text": "📱 9:16 + размытый фон", "callback_data": f"vertblur:{job_key}"})
    else:
        if allow_recut:
            row.append(
                {"text": "⚡ Без перекодирования (быстро)", "callback_data": f"recut:{job_key}"}
            )
        row.append({"text": "📱 Вертикально 9:16", "callback_data": f"vert:{job_key}"})
        row.append(
            {"text": "📱 Вертикально + размытый фон", "callback_data": f"vertblur:{job_key}"}
        )
    return {"inline_keyboard": [row]}


def clip_for_bot(
    source: str,
    segments: list[Segment],
    fast: bool,
    cfg: Config,
    vertical: bool = False,
    layout: str = "crop",
    progress: "Callable[[str, float | None, str], None] | None" = None,
) -> tuple[list, dict]:
    """Cut for the bot and keep the source file so a re-cut is instant.

    The CLI removes the download after cutting; the bot must not, otherwise the
    "без перекодирования" button would have to download everything again.
    """
    from .cut import cut_segment, pick_vertical_size, vertical_name
    from .segments import clip_filename, validate_segments
    from .source import prepare_source, shift_segments

    src = prepare_source(
        source,
        cfg,
        segments=[(s.start, s.end) for s in segments],
        keep_source=True,
        cancel_event=STOP_REQUESTED,
        progress=progress,
    )
    whole = not segments
    if whole:
        if src.info.duration <= 0:
            raise ValueError("не удалось определить длительность ролика")
        segments = [Segment(0.0, round(src.info.duration, 3))]
    shifted = shift_segments(segments, src.offset)
    validate_segments(shifted, duration=src.info.duration or None)

    out_dir = Path(cfg.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if whole and not vertical:
        # Whole video, no re-encoding needed: remux, seconds even for long files.
        from .cut import copy_whole

        result = copy_whole(
            str(src.path),
            out_dir
            / clip_filename(src.title, source, segments[0], span="весь ролик"),
            ffmpeg_path=cfg.ffmpeg_path,
            timeout=FFMPEG_TIMEOUT,
        )
        result.segment = segments[0]
        return [result], {
            "height": src.info.height,
            "vertical": False,
            "layout": "crop",
            "path": str(src.path),
            "segments": [{"start": 0.0, "end": segments[0].end}],
            "shown": [{"start": 0.0, "end": segments[0].end}],
            "has_audio": src.info.has_audio,
            "source": source,
            "title": src.title,
            "whole": True,
        }

    results = []
    total = len(shifted)
    for i, seg in enumerate(shifted):
        if STOP_REQUESTED.is_set():
            raise JobCancelled("остановлено пользователем")
        shown = segments[i] if i < len(segments) else seg

        def clip_progress(
            stage: str, fraction: float | None, detail: str, _i: int = i, _total: int = total
        ) -> None:
            if progress is not None:
                progress(stage, fraction, f"клип {_i + 1}/{_total}")

        result = cut_segment(
            str(src.path),
            out_dir / vertical_name(clip_filename(src.title, source, shown), vertical, layout),
            seg,
            fast=fast,
            ffmpeg_path=cfg.ffmpeg_path,
            has_audio=src.info.has_audio,
            vertical=vertical,
            layout=layout,
            vertical_size=pick_vertical_size(
                src.info.height, cfg.vertical_width, cfg.vertical_height
            ),
            timeout=FFMPEG_TIMEOUT,
            progress=clip_progress,
        )
        result.segment = shown
        results.append(result)
    job = {
        "whole": whole,
        "height": src.info.height,
        "vertical": vertical,
        "layout": layout,
        "path": str(src.path),
        "segments": [{"start": s.start, "end": s.end} for s in shifted],
        "shown": [{"start": s.start, "end": s.end} for s in segments],
        "has_audio": src.info.has_audio,
        "source": source,
        "title": src.title,
    }
    return results, job


def parse_request(text: str) -> Request | None:
    """Extract the link, its timecode ranges and the mode flags from a message.

    Ranges are written as ``НАЧАЛО-КОНЕЦ`` or ``НАЧАЛО=ДЛИТЕЛЬНОСТЬ`` and chained
    with ``;``. Inside a timecode any of ``: , . \ /`` may separate the parts, and
    a bare pair of timecodes (``0:10 0:20``) still means start and end.
    """
    if not text:
        return None
    raw = text.strip()
    url_match = URL_RE.search(raw)
    if not url_match:
        return None
    source = url_match.group(0).rstrip(".,)")
    rest = raw.replace(url_match.group(0), " ")

    fast = bool(re.search(r"\b(fast|быстро|быстрый)\b", rest, re.IGNORECASE))
    vertical = bool(
        re.search(r"\b(vertical|вертикаль\w*|9\s*[:xх]\s*16|шортс\w*|reels?)\b", rest, re.IGNORECASE)
    )
    layout = "blur" if re.search(r"\b(blur|размыт\w*|фон)\b", rest, re.IGNORECASE) else "crop"

    # Drop the flag words so "1:12 fast" cannot turn into a timecode pair.
    body = re.sub(
        r"\b(fast|быстро|быстрый|vertical|вертикаль\w*|blur|размыт\w*|фон|шортс\w*|reels?)\b|9\s*[:xх]\s*16",
        " ",
        rest,
        flags=re.IGNORECASE,
    )
    segments = segments_from_text(body)
    return Request(source=source, segments=segments, fast=fast, vertical=vertical, layout=layout)


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def clip_caption(title: str, source: str, segment: Segment, duration: float) -> str:
    """«Название ролика - ссылка - <00:00:00–00:00:12> - 12.08 с»."""
    name = (title or "").strip() or "видео"
    if len(name) > 70:
        name = name[:69].rstrip() + "…"
    return f"{name} - {source} - <{segment.label}> - {duration:.2f} с"


# Rough speed model, calibrated on the real runs of this bot (720p, SOCKS proxy):
# an accurate cut costs ~0.65 s of CPU per second of video plus ~0.45 s of
# download, a copy cut ~0.26 s. The answer is a range, never a promise.
ENCODE_RATE = {"accurate": 0.65, "fast": 0.26}
DOWNLOAD_RATE = 0.45
ESTIMATE_OVERHEAD = 15.0


def estimate_seconds(request: "Request") -> tuple[float, float] | None:
    """Low and high estimate for the whole job, or None when it is unknowable."""
    total = sum(seg.duration for seg in request.segments)
    if total <= 0:
        return None
    per_second = ENCODE_RATE["fast" if request.fast else "accurate"] + DOWNLOAD_RATE
    if request.vertical:
        per_second *= 1.25
    if request.layout == "blur":
        per_second *= 1.15
    work = ESTIMATE_OVERHEAD + total * per_second
    return work * 0.65, work * 1.8


def describe_request(
    request: "Request", limit: int = 8, duration: float = 0.0, title: str = ""
) -> str:
    """Repeat the parsed request back, so a misread timecode is visible at once."""
    from .timecode import human_duration, human_timecode

    modes = [
        name
        for name, enabled in (
            ("без перекодирования", request.fast),
            ("вертикально 9:16", request.vertical),
            ("размытый фон", request.vertical and request.layout == "blur"),
        )
        if enabled
    ]
    mode_text = f", режим: {', '.join(modes)}" if modes else ", режим: точный"
    if not request.segments:
        return (
            f"Таймкодов нет — беру ролик целиком{mode_text}.\n"
            f"Качаю, это может занять пару минут..."
        )
    from .timecode import human_duration

    count = len(request.segments)
    lines = [f"Понял так — {count} {_plural(count, 'отрезок', 'отрезка', 'отрезков')}{mode_text}:"]
    if duration > 0:
        name = f"«{title.strip()}»" if title.strip() else "ролик"
        lines.append(f"{name} длится {human_duration(duration)}")
    for index, seg in enumerate(request.segments[:limit], 1):
        number = f"{index}. " if count > 1 else ""
        lines.append(
            f"{number}{human_timecode(seg.start)}–{human_timecode(seg.end)} "
            f"({seg.duration:g} с)"
        )
    if count > limit:
        lines.append(f"...и ещё {count - limit}")
    estimate = estimate_seconds(request)
    if estimate is not None:
        low, high = estimate
        lines.append(
            f"Ожидаю примерно {human_duration(low)} – {human_duration(high)} "
            f"(оценка по прошлым запускам)"
        )
    lines.append("Режу...")
    return "\n".join(lines)


RETRY_DELAY_MINUTES = 15.0
RETRY_AT: dict[int, float] = {}
_RETRY_TIMERS: dict[int, threading.Timer] = {}


def schedule_retry(
    bot, cfg: Config, chat_id: int, text: str, delay_minutes: float = RETRY_DELAY_MINUTES
) -> float:
    """Repeat the same request later: a YouTube bot-check usually clears by itself."""
    return schedule_retry_at(bot, cfg, chat_id, text, time.time() + delay_minutes * 60)


def schedule_retry_at(bot, cfg: Config, chat_id: int, text: str, due: float) -> float:
    """Same as :func:`schedule_retry`, but at an explicit timestamp."""
    cancel_retry(chat_id)
    RETRY_AT[chat_id] = due

    def _fire() -> None:
        RETRY_AT.pop(chat_id, None)
        _RETRY_TIMERS.pop(chat_id, None)
        run_in_worker(handle_message, bot, cfg, {"chat": {"id": chat_id}, "text": text})

    timer = threading.Timer(max(1.0, due - time.time()), _fire)
    timer.daemon = True
    _RETRY_TIMERS[chat_id] = timer
    timer.start()
    return due


def cancel_retry(chat_id: int) -> bool:
    """Drop a pending retry (used by /stop and /new)."""
    RETRY_AT.pop(chat_id, None)
    timer = _RETRY_TIMERS.pop(chat_id, None)
    if timer is not None:
        timer.cancel()
        return True
    return False


class ProgressReporter:
    """One chat message that is edited as the job advances.

    Two sources feed it: real progress from yt-dlp/ffmpeg, and a heartbeat
    thread. The heartbeat is not a luxury: a section download is executed by
    yt-dlp through ffmpeg, which reports nothing, so without it the chat stays
    silent for minutes while the job is happily working.
    """

    def __init__(self, bot, chat_id: int, min_interval: float = 8.0, heartbeat: float = 10.0):
        self.bot = bot
        self.chat_id = chat_id
        self.min_interval = min_interval
        self.heartbeat = heartbeat
        self.message_id: int | None = None
        self._last_at = 0.0
        self._last_text = ""
        self._stage = ""
        self._fraction: float | None = None
        self._detail = ""
        self._started_at = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def render(stage: str, fraction: float | None, detail: str) -> str:
        head = {"download": "Скачиваю", "encode": "Кодирую"}.get(stage, stage or "Работаю")
        parts = [head]
        if fraction is not None:
            parts.append(f"{fraction * 100:.0f}%")
        if detail:
            parts.append(detail)
        return "⏳ " + " · ".join(parts)

    def _text(self) -> str:
        text = self.render(self._stage, self._fraction, self._detail)
        elapsed = time.monotonic() - self._started_at
        if elapsed >= self.heartbeat:
            from .timecode import human_duration

            text += f" · уже {human_duration(elapsed)}"
        return text

    def _send(self, text: str) -> bool:
        try:
            result = self.bot.send_message(self.chat_id, text)
        except Exception:  # noqa: BLE001 — status must never break the job
            return False
        self.message_id = result.get("message_id") if isinstance(result, dict) else None
        self._last_at, self._last_text = time.monotonic(), text
        return True

    def start(self, stage: str = "", detail: str = "") -> None:
        """Post the first status message and keep it ticking."""
        self._stage, self._detail = stage, detail
        self._send(self._text())
        self._thread = threading.Thread(target=self._beat, daemon=True, name="progress")
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat):
            self._edit(force=True)

    def _edit(self, force: bool) -> None:
        if self.message_id is None or self._stop.is_set():
            return
        text = self._text()
        if text == self._last_text:
            return
        if not force and time.monotonic() - self._last_at < self.min_interval:
            return
        self._last_at, self._last_text = time.monotonic(), text
        try:
            self.bot.edit_message_text(self.chat_id, self.message_id, text)
        except Exception:  # noqa: BLE001
            pass

    def update(self, stage: str, fraction: float | None = None, detail: str = "") -> None:
        self._stage, self._fraction, self._detail = stage, fraction, detail
        if self._stop.is_set():
            return
        if self.message_id is None:
            self._send(self._text())
            return
        self._edit(force=fraction is not None and fraction >= 0.999)

    def finish(self, text: str = "✅ Готово") -> None:
        self._stop.set()
        if self.message_id is None:
            self._send(text)
        elif text != self._last_text:
            self._last_text = text
            try:
                self.bot.edit_message_text(self.chat_id, self.message_id, text)
            except Exception:  # noqa: BLE001
                pass


def segments_from_text(body: str) -> list[Segment]:
    """Pull ranges out of free text without ever guessing at a number."""

    def _timecode(token: str) -> float | None:
        if not token or not _TC_CHARS_RE.match(token) or not any(c.isdigit() for c in token):
            return None
        try:
            return _num(token)
        except ValueError:
            return None

    segments: list[Segment] = []
    pending: list[float] = []
    for token in re.split(r"[\s;]+", body):
        token = token.strip().strip("()[]{}")
        if not token:
            continue
        if "=" in token or "-" in token:
            try:
                segments.append(parse_range_spec(token))
            except ValueError:
                continue
            continue
        value = _timecode(token)
        if value is None:
            continue
        pending.append(value)
        if len(pending) == 2:
            segments.append(Segment(pending[0], pending[1]))
            pending = []
    return segments


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


def stop_current_job(bot, chat_id: int) -> None:
    """Kill the running encoder / abort the download and report it."""
    STOP_REQUESTED.set()
    killed = 0
    try:
        # ffmpeg is a child of this process; yt-dlp aborts itself via the
        # progress hook that watches STOP_REQUESTED.
        result = subprocess.run(
            ["pkill", "-P", str(os.getpid()), "-f", "ffmpeg"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        killed = 0 if result.returncode != 0 else 1
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env specific
        pass
    log.info("Остановка задания по /stop (ffmpeg: %s)", "убит" if killed else "не запущен")
    bot.send_message(
        chat_id,
        "Останавливаю: кодирование прервано, загрузка оборвётся на ближайшем шаге. "
        "Повторить потом — /resume.",
    )


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
    if command in ("/info", "/форматы"):
        for chunk in split_long(INFO):
            bot.send_message(chat_id, chunk)
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
        from .source import cookies_path

        cookies = cookies_path(cfg)
        if cookies and cookies.is_file():
            age = (time.time() - cookies.stat().st_mtime) / 86400
            lines.append(f"cookies: обновлены {age:.1f} дн. назад ({cookies})")
        else:
            lines.append("cookies: файла нет — YouTube может требовать авторизацию")
        if chat_id in RETRY_AT:
            when = time.strftime("%H:%M", time.localtime(RETRY_AT[chat_id]))
            lines.append(f"Отложен повтор запроса в {when} (держит бот-чек YouTube)")
        lines.append(f"Удаление через {cfg.retention_minutes} мин тишины")
        bot.send_message(chat_id, "\n".join(lines))
        return
    if command == "/stop":
        stop_current_job(bot, chat_id)
        if cancel_retry(chat_id):
            bot.send_message(chat_id, "Отложенный повтор отменил.")
        return
    if command == "/resume":
        request = LAST_REQUESTS.get(chat_id)
        if request is None:
            bot.send_message(chat_id, "Нечего повторять — пришли ссылку и таймкоды.")
            return
        bot.send_message(chat_id, "Повторяю последний запрос...")
        STOP_REQUESTED.clear()
        tail = " ".join(f"{s.start:.0f}-{s.end:.0f}" for s in request.segments)
        flags = " ".join(
            flag
            for flag, enabled in (
                ("fast", request.fast),
                ("вертикально", request.vertical),
                ("размытый фон", request.layout == "blur"),
            )
            if enabled
        )
        text = " ".join(part for part in (request.source, tail, flags) if part)
        run_in_worker(handle_message, bot, cfg, {"chat": {"id": chat_id}, "text": text})
        return
    if command == "/new":
        LAST_REQUESTS.pop(chat_id, None)
        JOBS.clear()
        STOP_REQUESTED.clear()
        cancel_retry(chat_id)
        result = sweep([cfg.cache_dir, Path(cfg.output_dir)], minutes=0)
        bot.send_message(
            chat_id,
            f"Начали с чистого листа: забыл последний запрос, удалил {result.count} файл(ов) "
            f"({result.freed_bytes / 1048576:.1f} МБ).",
        )
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
        bot.send_message(chat_id, "Таймкодов нет — пришлю ролик целиком.")

    # Metadata before the download: a wrong end time on a long video used to cost
    # minutes of transfer before the error appeared.
    meta: dict = {}
    if request.segments and is_url(request.source):
        meta = fetch_metadata(request.source, cfg)
    duration = float(meta.get("duration") or 0.0)
    title = str(meta.get("title") or "")
    if duration and request.segments:
        over = [seg for seg in request.segments if seg.end > duration + 0.5]
        if over:
            seg = over[0]
            from .timecode import human_duration, human_timecode

            name = f"«{title.strip()}»" if title.strip() else "ролик"
            bot.send_message(
                chat_id,
                f"Не режу: отрезок {human_timecode(seg.start)}–{human_timecode(seg.end)} "
                f"выходит за конец ролика.\n{name} длится {human_duration(duration)}. "
                f"Поправь таймкод и пришли снова.",
            )
            return

    LAST_REQUESTS[chat_id] = request
    STOP_REQUESTED.clear()
    bot.send_message(chat_id, describe_request(request, duration=duration, title=title))
    bot.send_chat_action(chat_id, "record_video")
    reporter = ProgressReporter(bot, chat_id)
    reporter.start("download", "готовлю источник")

    job: dict | None = None
    try:
        if clipper is not None:
            results = clipper(
                request.source, request.segments, request.fast, cfg, progress=reporter.update
            )
        else:
            results, job = clip_for_bot(
                request.source,
                request.segments,
                request.fast,
                cfg,
                vertical=request.vertical,
                layout=request.layout,
                progress=reporter.update,
            )
    except JobCancelled:
        reporter.finish("⛔️ Остановлено")
        bot.send_message(chat_id, "Остановлено. Повторить — /resume.")
        return
    except Exception as exc:  # noqa: BLE001 — the chat is the error channel
        if STOP_REQUESTED.is_set():
            reporter.finish("⛔️ Остановлено")
            bot.send_message(chat_id, "Остановлено. Повторить — /resume.")
            return
        log.exception("Ошибка нарезки")
        if isinstance(exc, ChallengeCooldown):
            reporter.finish("⏸ Пауза из-за бот-чека")
            due = schedule_retry_at(bot, cfg, chat_id, text, exc.until)
            bot.send_message(
                chat_id,
                f"{exc}\nПовторю сам в {time.strftime('%H:%M', time.localtime(due))}. "
                f"Отменить — /stop.",
            )
            return
        if is_bot_check(exc):
            due = schedule_retry_at(
                bot, cfg, chat_id, text, max(challenge_until(), time.time() + RETRY_DELAY_MINUTES * 60)
            )
            reporter.finish("⏳ YouTube просит подтверждение")
            bot.send_message(
                chat_id,
                f"YouTube потребовал подтверждения, что клиент не бот. Это ограничение "
                f"этого IP, а не cookies.\n"
                f"Повторю тот же запрос сам в {time.strftime('%H:%M', time.localtime(due))} — "
                f"обычно к этому времени проходит. Отменить — /stop.",
            )
            return
        reporter.finish("⚠️ Ошибка")
        bot.send_message(chat_id, f"Ошибка: {exc}")
        return
    reporter.finish("✅ Готово, отправляю...")

    job_key = ""
    if job is not None:
        job_key = uuid.uuid4().hex[:8]
        job["created"] = time.time()
        JOBS[job_key] = job

    for index, result in enumerate(results):
        caption = clip_caption(
            (job or {}).get("title", ""),
            (job or {}).get("source", request.source),
            result.segment,
            result.duration,
        )
        is_last = index == len(results) - 1
        markup = (
            recut_keyboard(
                job_key,
                vertical=request.vertical,
                layout=request.layout,
                allow_recut=not request.fast and not (job or {}).get("whole"),
            )
            if job_key and is_last
            else None
        )
        try:
            send_video_smart(
                bot,
                chat_id,
                Path(result.output_path),
                caption,
                has_audio=bool(job.get("has_audio", True)) if job else True,
                reply_markup=markup,
            )
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

    kind, _, key = data.partition(":")
    if kind not in {"recut", "vert", "vertblur"}:
        answer("Не понимаю команду")
        return
    fast = kind == "recut"
    vertical = kind in {"vert", "vertblur"}
    layout = "blur" if kind == "vertblur" else "crop"

    job = JOBS.get(key)
    if not job:
        answer("Это задание уже устарело — пришли ссылку заново")
        return
    source_path = Path(job["path"])
    if not source_path.is_file():
        answer("Исходник уже удалён — пришли ссылку заново")
        return

    total = sum(item["end"] - item["start"] for item in job["segments"])
    if vertical:
        answer("Режу вертикально...")
        bot.send_message(
            chat_id,
            f"Режу вертикально: {total:.0f} с видео, обычно 1–3 минуты. "
            f"Пришлю сюда, как будет готово.",
        )
    else:
        answer("Режу без перекодирования...")
        bot.send_message(chat_id, f"Режу без перекодирования: {total:.0f} с, это быстро.")
    bot.send_chat_action(chat_id, "record_video")

    from .cut import pick_vertical_size, vertical_name
    from .segments import Segment, clip_filename

    out_dir = Path(cfg.output_dir).expanduser()
    sent = 0
    for i, raw in enumerate(job["segments"]):
        seg = Segment(raw["start"], raw["end"])
        shown_raw = job["shown"][i] if i < len(job["shown"]) else raw
        shown = Segment(shown_raw["start"], shown_raw["end"])
        try:
            result = cut_segment(
                str(source_path),
                out_dir
                / vertical_name(
                    clip_filename(
                        job.get("title"), job["source"], shown, suffix="" if vertical else "-fast"
                    ),
                    vertical,
                    layout,
                ),
                seg,
                fast=fast,
                ffmpeg_path=cfg.ffmpeg_path,
                has_audio=bool(job.get("has_audio", True)),
                vertical=vertical,
                layout=layout,
                vertical_size=pick_vertical_size(
                    job.get("height"), cfg.vertical_width, cfg.vertical_height
                ),
                timeout=FFMPEG_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            bot.send_message(chat_id, f"Не смог перерезать: {exc}")
            continue
        try:
            send_video_smart(
                bot,
                chat_id,
                Path(result.output_path),
                clip_caption(job.get("title", ""), job.get("source", ""), shown, result.duration),
                has_audio=bool(job.get("has_audio", True)),
            )
            sent += 1
        except TelegramError as exc:
            bot.send_message(chat_id, f"Не смог отправить видео: {exc}")
    if sent:
        note = (
            "Готово без перекодирования. Границы могли сместиться к ключевому кадру; "
            "точный вариант остался в чате выше."
            if fast
            else "Готово: вертикальный кадр 1080x1920."
        )
        bot.send_message(chat_id, note)


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
                    run_in_worker(handle_callback, bot, cfg, update["callback_query"])
                else:
                    run_in_worker(handle_message, bot, cfg, update.get("message") or {})
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
