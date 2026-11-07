"""Кадр-превью и паспорт файла: посмотреть границы до нарезки.

Всё работает по локальному файлу: либо загруженному вручную, либо уже
скачанному в кэш. Ссылку здесь намеренно не качаем — загрузка ради одного
кадра слишком дорога, поэтому GUI честно говорит «сначала нарежь».
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

from .config import Config
from .storage import find_cached_source, video_id_from_url

log = logging.getLogger(__name__)

FRAME_TIMEOUT = 25
STREAM_FRAME_TIMEOUT = 22  # один заход за кадром из потока; всего их два
STREAM_FORMAT = "bv*[height<=480]/b[height<=480]/bv*/b"
# Сколько всего ждём кадр по ссылке (поток + страховочный участок), секунды:
# человек не должен смотреть на вертушку дольше минуты
FRAME_TOTAL_BUDGET = 45.0
STREAM_TTL = 600

# Кадры кэшируем: при быстром перелистывании границы один и тот же кадр
# запрашивается повторно, а вытащить его из потока — секунды и лишняя нагрузка.
FRAME_CACHE_LIMIT = 160
FRAME_FALLBACK_SECONDS = 8.0
_frame_cache: "OrderedDict[tuple[str, float, int], bytes]" = OrderedDict()
_frame_lock = threading.Lock()
# Одновременных извлечений не больше двух: иначе десяток кликов запускает
# десяток ffmpeg и сайт начинает отказывать.
_frame_slots = threading.Semaphore(2)


def cached_frame(target: str, seconds: float, width: int) -> bytes:
    with _frame_lock:
        data = _frame_cache.get((target, round(seconds, 2), width))
        if data:
            _frame_cache.move_to_end((target, round(seconds, 2), width))
        return data or b""


def remember_frame(target: str, seconds: float, width: int, data: bytes) -> None:
    if not data:
        return
    key = (target, round(seconds, 2), width)
    with _frame_lock:
        _frame_cache[key] = data
        _frame_cache.move_to_end(key)
        while len(_frame_cache) > FRAME_CACHE_LIMIT:
            _frame_cache.popitem(last=False)


def closest_frame(target: str, seconds: float, width: int) -> bytes:
    """Ближайший уже добытый кадр — чтобы картинка не пропадала при сбое."""
    best, best_gap = None, None
    with _frame_lock:
        for (stored, when, stored_width), data in _frame_cache.items():
            if stored != target or stored_width != width:
                continue
            gap = abs(when - seconds)
            if gap <= FRAME_FALLBACK_SECONDS and (best_gap is None or gap < best_gap):
                best, best_gap = data, gap
    return best or b""

# Сколько секунд видео качаем, чтобы показать кадры по ссылке. Двадцать секунд —
# компромисс: хватает на правку начала и конца короткого клипа, и качается быстро.
PREVIEW_WINDOW = 20.0
PREVIEW_LEAD = 2.0
# Больше минуты качать ради кадров уже неприлично — даже если отрезок длинный.
MAX_PREVIEW_WINDOW = 60.0


def local_source(source: str, cfg: Config) -> Path | None:
    """Файл на диске для этого источника: загруженный или скачанный в кэш."""
    if not source:
        return None
    candidate = Path(source).expanduser()
    if candidate.is_file():
        return candidate
    if source.startswith(("http://", "https://")):
        return find_cached_source(
            Path(cfg.cache_dir).expanduser(), source, cfg.retention_minutes
        )
    return None


_LINK_ID_RE = {
    "youtu.be": re.compile(r"youtu\.be/([A-Za-z0-9_-]+)"),
    "watch": re.compile(r"[?&]v=([A-Za-z0-9_-]+)"),
    "shorts": re.compile(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)"),
}


UNAVAILABLE_SIGNS = (
    "video is unavailable",
    "video unavailable",
    "this video is private",
    "removed by the uploader",
    "has been terminated",
    "members-only",
    "is not available in your country",
)


def unavailable_video(exc: BaseException | str) -> bool:
    """Сайт говорит, что видео нет/оно закрыто — повторять бессмысленно."""
    text = (exc if isinstance(exc, str) else str(exc)).lower()
    return any(sign in text for sign in UNAVAILABLE_SIGNS)


def link_problem(source: str) -> str:
    """Ссылка, которая заведомо не сработает — чтобы не ходить за кадром впустую.

    YouTube-идентификатор всегда 11 символов: если в ссылке 10, она скопирована
    не целиком, и ни метаданные, ни кадр не придут.
    """
    if not source or "youtu" not in source:
        return ""
    for pattern in _LINK_ID_RE.values():
        match = pattern.search(source)
        if not match:
            continue
        found = match.group(1)
        if len(found) < 11:
            return (
                f"в ссылке {len(found)} символов id вместо 11 — она скопирована не целиком. "
                "Скопируй адрес заново: https://youtu.be/XXXXXXXXXXX"
            )
        if len(found) > 11:
            return (
                f"в ссылке {len(found)} символов id вместо 11 — к адресу приклеился лишний "
                f"символ. Нужны ровно 11: https://youtu.be/{found[:11]}"
            )
        return ""
    return ""


def windows_dir(cfg: Config) -> Path:
    """Кэш коротких участков для предпросмотра — отдельно от полных загрузок."""
    return Path(cfg.cache_dir).expanduser() / "preview"


def find_window(source: str, cfg: Config, seconds: float):
    """Готовый участок для этого источника, накрывающий ``seconds``."""
    folder = windows_dir(cfg)
    if not folder.is_dir():
        return None
    for meta_file in folder.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("source") != source:
            continue
        start, end = float(meta.get("start", 0)), float(meta.get("end", 0))
        if not (start <= seconds <= end):
            continue
        media = folder / str(meta.get("file", ""))
        if media.is_file():
            return media, start, end, float(meta.get("duration") or 0.0)
    return None


def ensure_window(source: str, cfg: Config, seconds: float, until=None, progress=None):
    """Кадры для ссылки: найти участок или скачать короткий кусок вокруг времени.

    ``until`` — конец запрошенного отрезка: если он дальше окна по умолчанию,
    участок качается сразу до него, чтобы кадр конца не требовал второй загрузки.
    """
    found = find_window(source, cfg, seconds)
    if found is not None:
        if until is None or until <= found[2]:
            return found
    # Один участок на источник: две рамки не должны качать его дважды.
    with _window_lock(source):
        found = find_window(source, cfg, seconds)
        if found is not None and (until is None or until <= found[2]):
            return found
        return _download_window(source, cfg, seconds, until, progress)


def _download_window(source: str, cfg: Config, seconds: float, until, progress=None):
    """Скачать участок для кадров: сначала прямой Range, потом yt-dlp."""
    from .source import download_window

    # Длительность берём только из уже добытого — лишний поход в сеть здесь
    # стоит десятки секунд, а окно и без неё качается нормально.
    duration = float(cached_stream(source).get("duration") or 0.0)
    start = max(0.0, float(seconds) - PREVIEW_LEAD)
    span = PREVIEW_WINDOW
    if until is not None:
        try:
            span = max(span, float(until) - start + 1.0)
        except (TypeError, ValueError):
            pass
    span = min(span, MAX_PREVIEW_WINDOW)
    end = start + span
    if duration:
        end = min(end, duration - 0.2)
        if end <= start and duration > 1:
            start, end = max(0.0, duration - PREVIEW_WINDOW), duration - 0.2
    folder = windows_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)

    path = None
    offset = start
    length = 0.0
    try:
        # Прямой Range-запрос: на живом ролике 40 с видео — 5 с вместо минут
        path, offset, _meta = download_window(source, cfg, start, end, folder)
        length = float(probe_media(path).get("duration") or 0.0)
    except Exception as exc:  # noqa: BLE001 - откат на yt-dlp-секции
        log.info("Быстрый участок не вышел (%s) — тяну через yt-dlp", str(exc)[:120])
        from .source import download_section

        path, offset = download_section(source, cfg, start, end, folder, progress)
        length = float(probe_media(path).get("duration") or 0.0)
    if length <= 0:
        # паспорт файла не прочитался — считаем, что участок дошёл до запрошенного конца
        length = max(0.0, float(end) - float(offset))
    suffix = path.suffix or ".mp4"
    target = folder / f"{video_id_from_url(source) or 'clip'}-{int(offset)}{suffix}"
    try:
        if path != target:
            path.replace(target)
    except OSError:
        target = path
    meta = {
        "source": source,
        "file": target.name,
        "start": round(float(offset), 3),
        "end": round(float(offset) + length, 3),
        "duration": round(duration, 3),
    }
    (folder / f"{target.stem}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Предпросмотр: скачал %.0f–%.0f с в %s", start, end, target.name)
    return target, start, end


def resolve_for_time(source: str, cfg: Config, seconds: float):
    """Файл, локальное время кадра и границы участка: полный кэш или короткий кусок."""
    local = local_source(source, cfg)
    if local is not None:
        return local, float(seconds), 0.0, 0.0, False
    window = find_window(source, cfg, seconds)
    if window is not None:
        media, start, end, _ = window
        return media, float(seconds) - start, start, end, True
    return None


_STREAM_CACHE: dict[str, tuple[float, str, dict]] = {}
# Две вкладки-рамки просят кадры одновременно. Если пускать их наперегонки,
# они душат друг друга на прокси и обе не успевают: берём по одному.
_STREAM_LOCKS: dict[str, threading.Lock] = {}
_STREAM_LOCKS_GUARD = threading.Lock()
_REMOTE_FRAME_LOCK = threading.Lock()
_WINDOW_LOCKS: dict[str, threading.Lock] = {}
_WINDOW_LOCKS_GUARD = threading.Lock()


def _window_lock(source: str) -> threading.Lock:
    with _WINDOW_LOCKS_GUARD:
        lock = _WINDOW_LOCKS.get(source)
        if lock is None:
            lock = _WINDOW_LOCKS[source] = threading.Lock()
        return lock


def _source_lock(source: str) -> threading.Lock:
    with _STREAM_LOCKS_GUARD:
        lock = _STREAM_LOCKS.get(source)
        if lock is None:
            lock = _STREAM_LOCKS[source] = threading.Lock()
        return lock


def _stream_cache_file(cfg: Config):
    return Path(cfg.cache_dir).expanduser() / "streams.json"


def _load_disk_stream(source: str, cfg: Config) -> tuple[str, dict] | None:
    """Адрес потока, добытый прошлым запуском: сайт второй раз не дёргаем."""
    path = _stream_cache_file(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(source)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("ts") or 0) >= STREAM_TTL:
        return None
    url = entry.get("url")
    meta = entry.get("meta")
    if not url or not isinstance(meta, dict):
        return None
    return str(url), dict(meta)


def _save_disk_stream(source: str, url: str, meta: dict, cfg: Config) -> None:
    path = _stream_cache_file(cfg)
    data: dict = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items()
            if isinstance(v, dict) and now - float(v.get("ts") or 0) < STREAM_TTL}
    data[source] = {"ts": now, "url": url, "meta": meta}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def stream_info(source: str, cfg: Config) -> tuple[str, dict]:
    """Прямая ссылка на поток: кадр можно взять без скачивания файла.

    Сайт отдаёт временный URL на CDN; он живёт ограниченное время, поэтому
    кэшируем недолго — в памяти и на диске (второй заказ того же ролика
    не должен снова попадать под бот-чек).
    """
    with _source_lock(source):
        try:
            return _stream_info_locked(source, cfg)
        except Exception:
            saved = _load_disk_stream(source, cfg)
            if saved is None:
                raise
            log.info("Сайт не отдал поток — беру адрес из прошлого запроса")
            _STREAM_CACHE[source] = (time.time(), saved[0], saved[1])
            return saved


def _stream_info_locked(source: str, cfg: Config) -> tuple[str, dict]:
    cached = _STREAM_CACHE.get(source)
    if cached and time.time() - cached[0] < STREAM_TTL:
        return cached[1], cached[2]
    from .source import _base_opts, _ytdlp_extract

    base = _base_opts(cfg, min(cfg.default_resolution, 480), None, None)
    base = {
        **base,
        "skip_download": True,
        # сначала видеодорожка, потом что угодно: для кадра звук не нужен
        "format": STREAM_FORMAT,
        "socket_timeout": 10,
    }
    # Один клиент за попытку. Перебор клиентов подряд злит YouTube: каждый лишний
    # запрос повышает шанс бот-чека. mweb — единственный, который на этой сети
    # не получает ни бот-чек, ни «page needs to be reloaded»; android — запасной.
    info: dict = {}
    last_error: Exception | None = None
    for index, client in enumerate(("mweb", "android")):
        # У части клиентов нет форматов ниже 480p — тогда просим «лучший доступный»,
        # иначе yt-dlp отвечает «Requested format is not available» и кадра нет
        for fmt in (STREAM_FORMAT, None):
            opts = dict(base)
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            if fmt:
                opts["format"] = fmt
            else:
                opts.pop("format", None)
            if index and fmt is STREAM_FORMAT:
                time.sleep(2)
            try:
                info = _ytdlp_extract(source, opts) or {}
            except Exception as exc:  # noqa: BLE001 - причина уйдёт наверх
                last_error = exc
                log.info("Поток через %s (%s) не получен: %s", client, fmt or "любой", str(exc)[:110])
                if "Requested format is not available" in str(exc):
                    continue
                if unavailable_video(exc):
                    # приватное/удалённое видео: другие клиенты и форматы не помогут
                    raise
                break
            if info.get("url"):
                break
        if info.get("url"):
            break
    if not info.get("url") and last_error is not None:
        raise last_error
    url = info.get("url")
    if not url:
        raise RuntimeError("сайт не отдал прямую ссылку на поток")
    meta = {
        "duration": float(info.get("duration") or 0.0),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": float(info.get("fps") or 0.0),
        # CDN отдаёт видео только своему клиенту: с чужим User-Agent ffmpeg
        # получает пустой поток — это проверено на живом ролике
        "user_agent": (info.get("http_headers") or {}).get("User-Agent", ""),
    }
    _STREAM_CACHE[source] = (time.time(), url, meta)
    _save_disk_stream(source, url, meta, cfg)
    return url, meta


def cached_stream(source: str) -> dict:
    """Метаданные потока, если их уже добыли (без обращения к сайту)."""
    entry = _STREAM_CACHE.get(source)
    if not entry or time.time() - entry[0] >= STREAM_TTL:
        return {}
    return dict(entry[2])


def grab_stream_frame(source: str, cfg: Config, seconds: float, width: int = 480) -> bytes:
    """Кадр из потока: ffmpeg читает только нужный кусок через HTTP Range.

    CDN иногда отвечает медленно, и один заход не укладывается в отведённое время.
    Тогда берём свежий адрес потока и пробуем ещё раз: адрес на CDN живёт часами,
    но конкретный запрос может «подвиснуть» на прокси.
    """
    from .httpproxy import HttpBridge

    def _attempt() -> bytes:
        url, meta = stream_info(source, cfg)
        bridge = None
        saved = {
            k: os.environ.get(k)
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
        }
        try:
            if cfg.proxy:
                bridge = HttpBridge(cfg.proxy)
                bridge_url = bridge.start()
                for key in saved:
                    os.environ[key] = bridge_url
            with _REMOTE_FRAME_LOCK:
                return _frame_bytes(
                    url,
                    seconds,
                    width,
                    cache_key=source,
                    user_agent=meta.get("user_agent", ""),
                    timeout=STREAM_FRAME_TIMEOUT,
                )
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if bridge is not None:
                bridge.stop()

    frame = _attempt()
    if not frame:
        # запрос мог просто не успеть — обновляем адрес и пробуем второй раз
        _STREAM_CACHE.pop(source, None)
        log.info("Кадр из потока не пришёл с первого раза, повторяю: %s", source[:60])
        frame = _attempt()
    return frame


def probe_media(path: Path) -> dict:
    """Длительность, размеры и частота кадров — из самого файла, а не из метаданных сайта."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,duration:format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=FRAME_TIMEOUT)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.info("Не смог прочитать паспорт %s: %s", path.name, exc)
        return {}
    stream = (data.get("streams") or [{}])[0]
    if not stream.get("width") and not stream.get("height"):
        return {}
    duration = stream.get("duration") or (data.get("format") or {}).get("duration")
    fps = 0.0
    raw_rate = stream.get("avg_frame_rate") or "0/0"
    try:
        fps = float(Fraction(raw_rate))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration": round(float(duration), 3) if duration else 0.0,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 3) if 0 < fps < 240 else 0.0,
    }


def grab_frame(path: Path, seconds: float, width: int = 480) -> bytes:
    """Один кадр в JPEG. ``-ss`` до ``-i`` — перемотка по ключевым кадрам, быстро."""
    return _frame_bytes(str(path), seconds, width)


def _frame_bytes(
    target: str,
    seconds: float,
    width: int = 480,
    cache_key: str | None = None,
    user_agent: str = "",
    timeout: float | None = None,
) -> bytes:
    key = cache_key or target
    seconds = max(0.0, float(seconds))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if user_agent:
        cmd += ["-user_agent", user_agent]
    cmd += [
        "-ss", f"{seconds:.3f}", "-i", target,
        "-frames:v", "1",
        "-vf", f"scale={max(32, int(width))}:-2:flags=fast_bilinear",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", "5",
        "pipe:1",
    ]
    try:
        with _frame_slots:
            out = subprocess.run(
                cmd, capture_output=True, timeout=timeout or FRAME_TIMEOUT
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Кадр не получился (%s, %.2f с): %s", target[:80], seconds, exc)
        return b""
    data = out.stdout if out.stdout[:2] == b"\xff\xd8" else b""
    if data:
        remember_frame(key, seconds, width, data)
    return data
