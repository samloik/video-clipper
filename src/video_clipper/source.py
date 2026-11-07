"""Source preparation: a local file or a download through yt-dlp."""

from __future__ import annotations

import logging
import math
import os
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Config
from .probe import MediaInfo, probe_media
from .segments import Segment
from .timecode import format_timecode
from .storage import find_cached_source

log = logging.getLogger("video_clipper")

URL_PREFIXES = ("http://", "https://")


class SourceError(RuntimeError):
    """Raised when the source cannot be used (missing file, failed download)."""


class ChallengeCooldown(SourceError):
    """The site is blocking this IP right now; the request was not even attempted."""

    def __init__(self, until: float):
        self.until = until
        when = time.strftime("%H:%M", time.localtime(until))
        super().__init__(
            f"YouTube требует подтверждение для этого IP, скачивание не начинал. "
            f"Следующая попытка — после {when}."
        )


@dataclass
class Source:
    path: Path
    title: str
    is_temporary: bool
    info: MediaInfo
    url: str | None = None
    # Section downloads start at 0, so segment times must be shifted by this.
    offset: float = 0.0
    kind: str = "full"

    @property
    def absolute_duration(self) -> float:
        """Length of the whole video, even when only a section was downloaded.

        ``info.duration`` of a section download is the *section* length; «до конца»
        must resolve to the end of the original video, not of the fragment.
        """
        return round(self.offset + self.info.duration, 3)

    def cleanup(self) -> None:
        """Remove the downloaded file and an empty cache directory."""
        if not self.is_temporary:
            return
        try:
            self.path.unlink(missing_ok=True)
            parent = self.path.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:  # pragma: no cover - best effort
            log.warning("Не удалось удалить временный файл %s", self.path)


def is_url(value: str) -> bool:
    return value.strip().lower().startswith(URL_PREFIXES)


def _coverage(segments: Iterable[tuple[float, float]], duration: float) -> float:
    """Fraction of the video covered by the requested segments."""
    if duration <= 0:
        return 1.0
    total = sum(max(0.0, end - start) for start, end in segments)
    return min(1.0, total / duration)


# Metadata is fetched twice per request in the worst case — once to validate the
# timecodes before downloading, once inside the download. A short cache makes the
# second one free.
_META_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
META_TTL = 300.0


def _base_opts(
    cfg: Config,
    height: int | None = None,
    cancel_event: Any = None,
    progress: Callable[[str, float | None, str], None] | None = None,
) -> dict[str, Any]:
    height = height or cfg.default_resolution
    opts: dict[str, Any] = {
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "restrictfilenames": False,
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if cfg.proxy:
        opts["proxy"] = cfg.proxy
    if cfg.cookies:
        cookies = Path(cfg.cookies).expanduser()
        if cookies.is_file():
            opts["cookiefile"] = str(cookies)
    hooks = []
    if cancel_event is not None:
        # yt-dlp calls the hook while downloading: raising aborts the transfer.
        def _cancel_hook(_progress: dict[str, Any]) -> None:
            if cancel_event.is_set():
                raise RuntimeError("остановлено пользователем")

        hooks.append(_cancel_hook)
    if progress is not None:
        def _progress_hook(status: dict[str, Any]) -> None:
            state = status.get("status")
            if state == "downloading":
                done = float(status.get("downloaded_bytes") or 0)
                total = float(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
                fraction = done / total if total else None
                detail = f"{done / 1048576:.1f} МБ"
                if total:
                    detail += f" из {total / 1048576:.1f} МБ"
                progress("download", fraction, detail)
            elif state == "finished":
                progress("download", 1.0, "")

        hooks.append(_progress_hook)
    if hooks:
        opts["progress_hooks"] = hooks
    return opts


def fetch_metadata(
    source: str,
    cfg: Config,
    downloader: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ttl: float = META_TTL,
) -> dict[str, Any]:
    """Title and duration without downloading media. Returns {} on failure.

    Used to check the timecodes before a download starts: on a two-hour video a
    wrong end time used to cost minutes of transfer before the error surfaced.
    """
    if not is_url(source):
        return {}
    if time.time() < challenge_until():
        return {}
    cached = _META_CACHE.get(source)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    try:
        extract = downloader or _ytdlp_extract
        meta = extract(source, {**_base_opts(cfg), "skip_download": True}) or {}
    except Exception as exc:  # metadata is best-effort, never fatal here
        log.info("Метаданные недоступны (%s): %s", source, exc)
        return {}
    if meta.get("duration"):
        _META_CACHE[source] = (time.time(), meta)
    return meta


def prepare_source(
    source: str,
    cfg: Config,
    segments: list[tuple[float, float]] | None = None,
    keep_source: bool = False,
    downloader: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    cancel_event: Any = None,
    progress: Callable[[str, float | None, str], None] | None = None,
) -> Source:
    """Return a usable local file for ``source``.

    Local paths are validated with ffprobe. URLs are downloaded with yt-dlp using
    the config's resolution, proxy and cookies file; when the requested fragments
    cover less than ``SECTION_COVERAGE`` of the video, only those sections are
    fetched (``--download-sections`` equivalent).
    """
    if not is_url(source):
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")
        return Source(path=path, title=path.stem, is_temporary=False,
                      info=probe_media(str(path), cfg.ffprobe_path))

    # Same link again inside the retention window: reuse the download instead of
    # fetching it twice (the bot session relies on this).
    cached = find_cached_source(cfg.cache_dir, source, cfg.retention_minutes)
    if cached is not None:
        return Source(
            path=cached,
            title=cached.stem,
            is_temporary=False,
            info=probe_media(str(cached), cfg.ffprobe_path),
            url=source,
        )

    return _download(source, cfg, segments or [], keep_source, downloader, cancel_event, progress)


SECTION_COVERAGE = 0.4


def resolve_open_ends_after_download(
    segments: list[Segment], src: "Source"
) -> list[Segment]:
    """Fill ``START-*`` ends from the downloaded source.

    Uses the *absolute* duration: a section download only knows the length of the
    fragment, and resolving to it silently cut clips short.
    """
    from .segments import has_open_ends, resolve_open_ends

    if not has_open_ends(segments):
        return segments
    b = src.absolute_duration
    return resolve_open_ends(segments, b or src.info.duration)


def resolve_open_ends_for_source(
    segments: list[Segment], source: str, cfg: Config
) -> list[Segment]:
    """Turn ``20-*`` into ``20-<duration>`` using metadata (URL) or ffprobe (file)."""
    from .segments import has_open_ends, resolve_open_ends

    if not has_open_ends(segments):
        return segments
    if is_url(source):
        meta = fetch_metadata(source, cfg)
        duration = float(meta.get("duration") or 0)
    else:
        duration = probe_media(str(Path(source).expanduser()), cfg.ffprobe_path).duration
    return resolve_open_ends(segments, duration)


def shift_segments(segments: list[Segment], offset: float) -> list[Segment]:
    """Map requested times onto a section file that starts at 0."""
    if not offset:
        return segments
    return [Segment(s.start - offset, s.end - offset, s.name) for s in segments]


def _build_download_ranges(segments: list[tuple[float, float]]):
    def ranges(info: dict[str, Any], ydl: Any) -> list[dict[str, float]]:  # noqa: ARG001
        return [{"start_time": s, "end_time": e} for s, e in segments]

    return ranges


def _download(
    url: str,
    cfg: Config,
    segments: list[tuple[float, float]],
    keep_source: bool,
    downloader: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
    cancel_event: Any = None,
    progress: Callable[[str, float | None, str], None] | None = None,
) -> Source:
    try:
        import yt_dlp  # noqa: F401  # imported lazily so CLI --help works without the dependency
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise SourceError("Не установлен yt-dlp: uv pip install yt-dlp") from exc

    if time.time() < _challenge_until:
        raise ChallengeCooldown(_challenge_until)

    cache_dir = cfg.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    height = cfg.default_resolution
    # Sections are derived artefacts: kept out of the reuse cache and deleted
    # right after cutting, so a later request for another part of the same video
    # never mistakes a section for the whole file.
    sections_dir = cache_dir / "sections"
    outtmpl = str(cache_dir / "%(title).80s-%(id)s.%(ext)s")

    opts: dict[str, Any] = _base_opts(cfg, height, cancel_event, progress)
    opts["outtmpl"] = outtmpl
    if cfg.cookies and not Path(cfg.cookies).expanduser().is_file():
        log.warning("Файл cookies не найден, продолжаю без него: %s", cfg.cookies)

    extract = downloader or _ytdlp_extract

    # Metadata first: it tells us whether fetching only the requested sections
    # saves bandwidth (skip_download keeps this cheap). Site throttling makes a
    # single attempt flaky, and losing the duration means an expensive full
    # download — so it is retried.
    cached_meta = _META_CACHE.get(url)
    meta: dict[str, Any] = {}
    if cached_meta and time.time() - cached_meta[0] < META_TTL:
        meta = cached_meta[1]
    for attempt in range(3):
        if meta.get("duration"):
            break
        try:
            meta = extract(url, {**opts, "skip_download": True}) or {}
            if meta.get("duration"):
                _META_CACHE[url] = (time.time(), meta)
                break
        except Exception as exc:
            log.warning("Метаданные не получены (попытка %d/3): %s", attempt + 1, exc)
            if is_bot_check(exc):
                break  # тот же IP, тот же клиент: повтор ничего не изменит
            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    duration = float(meta.get("duration") or 0.0)
    if segments and duration > 0:
        for start, end in segments:
            if math.isinf(end):
                continue  # «до конца» — конец и есть длительность
            if end > duration + 0.5:
                raise SourceError(
                    f"Отрезок {format_timecode(start)}–{format_timecode(end)} выходит за конец "
                    f"ролика ({format_timecode(duration)}). Скачивание не начинал — поправь таймкод."
                )
    opts = dict(opts)
    # Unknown duration: sections are still the cheaper bet for a short request,
    # and a failure falls back to the full download below.
    sections = bool(segments) and (
        duration == 0 or _coverage(segments, duration) <= SECTION_COVERAGE
    )
    if sections and duration == 0:
        log.info("Длительность видео неизвестна — пробую скачать только участки")

    # Section downloads are cut by ffmpeg, which cannot use SOCKS5; a local
    # HTTP→SOCKS bridge makes ffmpeg work. Everything falls back to a full
    # download if the bridged attempt fails.
    bridge = None
    saved_env: dict[str, str | None] = {}
    if sections:
        from .httpproxy import HttpBridge

        bridge = HttpBridge(cfg.proxy)
        bridge_url = bridge.start()
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            saved_env[key] = os.environ.get(key)
            os.environ[key] = bridge_url
        sections_dir.mkdir(parents=True, exist_ok=True)
        for stale in sections_dir.iterdir():
            if stale.is_file():
                stale.unlink(missing_ok=True)
        opts["outtmpl"] = str(sections_dir / "%(title).60s-%(id)s.%(ext)s")
        opts["proxy"] = bridge_url
        opts["download_ranges"] = _build_download_ranges(segments)
        opts["force_keyframes_at_cuts"] = True
        log.info(
            "Отрезки покрывают %.0f%% видео — качаю только нужные участки (через мост %s)",
            _coverage(segments, duration) * 100,
            bridge_url,
        )

    cookie_warning(cfg)
    log.info("Скачивание %s (макс. %sp)", url, height)
    try:
        try:
            info, opts = _extract_with_clients(url, opts, extract)
        except Exception as exc:
            if not sections:
                raise
            if _is_auth_error(exc):
                # A bot-check will fail the full download too, and a long video
                # means hundreds of megabytes before the error — stop right here.
                raise SourceError(
                    f"Секционная загрузка отклонена сайтом. {explain_error(exc, cfg)}\n\n"
                    f"Полную загрузку не начинаю — с этой блокировкой она тоже не пройдёт."
                ) from exc
            if duration > FULL_FALLBACK_MAX_SECONDS:
                raise SourceError(
                    f"Не удалось взять только участки ({explain_error(exc, cfg)}).\n\n"
                    f"Полную загрузку {duration / 60:.0f}-минутного ролика не начинаю: это "
                    f"десятки-сотни мегабайт, а сбой участка обычно означает, что и она "
                    f"не пройдёт. Повтори запрос через 10–15 минут."
                ) from exc
            log.warning("Секционная загрузка не удалась (%s) — качаю видео целиком", exc)
            _clear_partials(cfg.cache_dir)
            opts.pop("download_ranges", None)
            opts.pop("force_keyframes_at_cuts", None)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if bridge is not None:
                bridge.stop()
                bridge = None
            opts["proxy"] = cfg.proxy
            info, opts = _extract_with_clients(url, opts, extract)
    except SourceError:
        raise
    except Exception as exc:  # yt-dlp raises its own DownloadError hierarchy
        if is_bot_check(exc):
            note_challenge()
            log.warning(
                "YouTube отбил запрос — пауза до %s",
                time.strftime("%H:%M", time.localtime(_challenge_until)),
            )
        raise SourceError(f"Не удалось скачать {url}: {_with_cookie_hint(exc, cfg)}") from exc
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if bridge is not None:
            bridge.stop()

    path = _downloaded_path(info)
    if not path or not path.is_file():
        raise SourceError(f"yt-dlp не вернул путь к файлу для {url}")

    offset = min((s for s, _ in segments), default=0.0) if sections else 0.0
    return Source(
        path=path,
        title=str(info.get("title") or path.stem),
        is_temporary=True if sections else not keep_source,
        info=probe_media(str(path), cfg.ffprobe_path),
        url=url,
        offset=offset,
        kind="section" if sections else "full",
    )


# A bot-check is IP- and volume-driven: hammering it with more requests makes it
# last longer. After one full failure we wait before touching the site again.
CHALLENGE_COOLDOWN_SECONDS = 12 * 60
_challenge_until = 0.0


def challenge_until() -> float:
    """Timestamp until which the site is known to be blocking this IP."""
    return _challenge_until


def note_challenge(seconds: float = CHALLENGE_COOLDOWN_SECONDS) -> float:
    """Remember that the site challenged us, so the next attempt waits."""
    global _challenge_until
    _challenge_until = max(_challenge_until, time.time() + seconds)
    return _challenge_until


def clear_challenge() -> None:
    global _challenge_until
    _challenge_until = 0.0


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _extract_with_clients(
    url: str, opts: dict[str, Any], extract: Callable[[str, dict[str, Any]], dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the request, then retry with other player clients when challenged.

    A bot-check depends on the client YouTube thinks we are, so the same request
    done as ``android`` or ``tv_embedded`` often goes through where ``web`` does
    not. Only authentication errors are retried — anything else is a real error.
    """
    # Measured on this network: the default client and tv_embedded get "page
    # needs to be reloaded" on section downloads, while android goes through.
    attempts: list[dict[str, Any]] = []
    if _is_youtube(url):
        for client in YT_CLIENTS:
            variant = dict(opts)
            args = dict(variant.get("extractor_args") or {})
            args["youtube"] = {"player_client": [client]}
            variant["extractor_args"] = args
            attempts.append(variant)
    attempts.append(opts)

    last: Exception | None = None
    for variant in attempts:
        try:
            return extract(url, variant), variant
        except Exception as exc:
            last = exc
            if not _is_auth_error(exc):
                raise
            client = (variant.get("extractor_args") or {}).get("youtube", {}).get("player_client")
            log.info("Бот-чек (клиент %s) — пробую другой клиент", client or "по умолчанию")
    assert last is not None
    raise last


def _ytdlp_extract(url: str, opts: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


# YouTube's challenge, not a cookie problem: it is IP-based and comes and goes.
BOT_CHECK_SIGNS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you\u2019re not a bot",
    "not a bot",
    "this helps protect our community",
    "page needs to be reloaded",
)
# Genuine authentication problems where refreshing cookies is the fix.
AUTH_SIGNS = BOT_CHECK_SIGNS + ("login required", "private video", "members-only")
COOKIE_SIGNS = AUTH_SIGNS  # kept for callers that still use the old name


def is_bot_check(exc: BaseException | str) -> bool:
    """True when the site demanded a bot-check rather than rejecting credentials."""
    message = (exc if isinstance(exc, str) else str(exc)).lower()
    return any(sign in message for sign in BOT_CHECK_SIGNS)


# Clients that answered on this network; order matters — first one that works wins.
YT_CLIENTS = ("android", "tv_embedded", "web")


def cookies_path(cfg: Config) -> Path | None:
    return Path(cfg.cookies).expanduser() if cfg.cookies else None


def cookie_warning(cfg: Config) -> None:
    """Warn (once per run) when the cookies file looks stale."""
    path = cookies_path(cfg)
    if not path or not path.is_file():
        return
    age_days = (time.time() - path.stat().st_mtime) / 86400
    if age_days > 30:
        log.warning(
            "Файл cookies не менялся %.0f дн. — если YouTube просит авторизацию, обнови его: %s",
            age_days,
            path,
        )


COOKIE_STALE_DAYS = 30


def explain_error(exc: Exception, cfg: Config) -> str:
    """Name the real cause. A bot-check is not the same thing as stale cookies."""
    message = str(exc)
    path = cookies_path(cfg)
    age_days = None
    if path and path.is_file():
        age_days = (time.time() - path.stat().st_mtime) / 86400

    if not is_bot_check(exc):
        if any(sign in message.lower() for sign in ("login required", "members-only", "private video")):
            return f"{message}\n\nРолик требует входа в аккаунт: обнови cookies в {path}."
        return message

    if age_days is None:
        return (
            f"{message}\n\nYouTube просит подтвердить, что клиент не бот, а файла cookies нет "
            f"({path}). Экспортируй их из браузера расширением «Get cookies.txt LOCALLY»."
        )
    if age_days > COOKIE_STALE_DAYS:
        return (
            f"{message}\n\nФайл cookies не менялся {age_days:.0f} дн. — обнови его: {path} "
            f"(«Get cookies.txt LOCALLY»)."
        )
    return (
        f"{message}\n\nДело не в cookies: файл свежий (обновлён {age_days:.1f} дн. назад). "
        f"YouTube выборочно требует подтверждение для этого IP — повтори запрос через "
        f"10–15 минут, обычно проходит."
    )


def _with_cookie_hint(exc: Exception, cfg: Config) -> str:
    """Backwards-compatible name for :func:`explain_error`."""
    return explain_error(exc, cfg)


LONG_VIDEO_SECONDS = 90 * 60
# Above this length a failed section attempt is not worth a full download.
FULL_FALLBACK_MAX_SECONDS = 30 * 60


def _is_auth_error(exc: Exception) -> bool:
    """Site asked for authentication / flagged the client as a bot."""
    message = str(exc).lower()
    return any(sign in message for sign in COOKIE_SIGNS)


def _clear_partials(cache_dir: Path) -> None:
    """Drop .part/.ytdl leftovers so the fallback download starts clean."""
    for pattern in ("*.part", "*.ytdl", "*.temp"):
        for leftover in cache_dir.glob(pattern):
            try:
                leftover.unlink()
            except OSError:  # pragma: no cover
                pass


def _downloaded_path(info: dict[str, Any]) -> Path | None:
    requested = info.get("requested_downloads") or []
    if requested:
        candidate = requested[0].get("filepath") or requested[0].get("_filename")
        if candidate:
            return Path(candidate)
    for key in ("filepath", "_filename"):
        if info.get(key):
            return Path(info[key])
    return None


def copy_source(src: Path, target_dir: Path) -> Path:
    """Copy a source file (used when a local file must survive cleanup)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / src.name
    shutil.copy2(src, target)
    return target
