"""Source preparation: a local file or a download through yt-dlp."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Config
from .probe import MediaInfo, probe_media

log = logging.getLogger("video_clipper")

URL_PREFIXES = ("http://", "https://")


class SourceError(RuntimeError):
    """Raised when the source cannot be used (missing file, failed download)."""


@dataclass
class Source:
    path: Path
    title: str
    is_temporary: bool
    info: MediaInfo
    url: str | None = None

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


def prepare_source(
    source: str,
    cfg: Config,
    segments: list[tuple[float, float]] | None = None,
    keep_source: bool = False,
    downloader: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
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

    return _download(source, cfg, segments or [], keep_source, downloader)


SECTION_COVERAGE = 0.4


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
) -> Source:
    try:
        import yt_dlp  # noqa: F401  # imported lazily so CLI --help works without the dependency
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise SourceError("Не установлен yt-dlp: uv pip install yt-dlp") from exc

    cache_dir = cfg.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    height = cfg.default_resolution
    outtmpl = str(cache_dir / "%(title).80s-%(id)s.%(ext)s")

    opts: dict[str, Any] = {
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "outtmpl": outtmpl,
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
        else:
            log.warning("Файл cookies не найден, продолжаю без него: %s", cookies)

    extract = downloader or _ytdlp_extract

    # Metadata first: it tells us whether fetching only the requested sections
    # saves bandwidth (skip_download keeps this cheap).
    meta: dict[str, Any] = {}
    try:
        meta = extract(url, {**opts, "skip_download": True}) or {}
    except Exception as exc:  # metadata failure is not fatal, the download may still work
        log.debug("Не удалось получить метаданные %s: %s", url, exc)

    duration = float(meta.get("duration") or 0.0)
    opts = dict(opts)
    if (
        cfg.use_sections
        and segments
        and duration > 0
        and _coverage(segments, duration) <= SECTION_COVERAGE
    ):
        opts["download_ranges"] = _build_download_ranges(segments)
        opts["force_keyframes_at_cuts"] = True
        log.info(
            "Отрезки покрывают %.0f%% видео — скачиваю только нужные участки",
            _coverage(segments, duration) * 100,
        )

    log.info("Скачивание %s (макс. %sp)", url, height)
    try:
        info = extract(url, opts)
    except Exception as exc:  # yt-dlp raises its own DownloadError hierarchy
        raise SourceError(f"Не удалось скачать {url}: {exc}") from exc

    path = _downloaded_path(info)
    if not path or not path.is_file():
        raise SourceError(f"yt-dlp не вернул путь к файлу для {url}")

    return Source(
        path=path,
        title=str(info.get("title") or path.stem),
        is_temporary=not keep_source,
        info=probe_media(str(path), cfg.ffprobe_path),
        url=url,
    )


def _ytdlp_extract(url: str, opts: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


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
