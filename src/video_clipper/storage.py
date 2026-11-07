"""Session cache and retention.

The exchange is deliberately short-lived: a downloaded source stays only while
the session is active (repeated cuts of the same link must not re-download it),
and clips stay until the same idle window expires. `sweep` is the only place
that deletes anything, so `video-clipper-bot` calls it before every request and
the CLI calls it on start.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("video_clipper")

DEFAULT_RETENTION_MINUTES = 30


def video_id_from_url(url: str) -> str:
    """Raw site id for a URL: YouTube ``v``, youtu.be path, TikTok last segment."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    candidate = parse_qs(parsed.query).get("v", [""])[0]
    if candidate:
        return re.sub(r"[^A-Za-z0-9_-]", "", candidate)
    parts = [p for p in parsed.path.split("/") if p]
    for part in reversed(parts):
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", part)
        if len(cleaned) >= 6:
            return cleaned
    return ""


def find_cached_source(cache_dir: Path, url: str, minutes: int = DEFAULT_RETENTION_MINUTES) -> Path | None:
    """Return a fresh cached download for ``url`` (same session), else None."""
    if not cache_dir.is_dir():
        return None
    cutoff = time.time() - minutes * 60
    video_id = video_id_from_url(url)
    patterns = [f"*-{video_id}.*", f"{video_id}.*"] if video_id else []
    for pattern in patterns:
        for candidate in cache_dir.glob(pattern):
            if candidate.suffix in {".part", ".ytdl", ".tmp"} or not candidate.is_file():
                continue
            if candidate.stat().st_mtime >= cutoff:
                log.info("Использую скачанное ранее: %s", candidate.name)
                return candidate
    return None


@dataclass
class SweepResult:
    removed: list[Path]
    freed_bytes: int

    @property
    def count(self) -> int:
        return len(self.removed)


def sweep(dirs: list[Path], minutes: int = DEFAULT_RETENTION_MINUTES) -> SweepResult:
    """Delete files older than ``minutes`` from each directory, then empty dirs."""
    cutoff = time.time() - minutes * 60
    removed: list[Path] = []
    freed = 0
    for directory in dirs:
        if not directory or not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*"), reverse=True):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                freed += path.stat().st_size
                path.unlink()
                removed.append(path)
            except OSError:  # pragma: no cover - best effort
                log.warning("Не удалось удалить %s", path)
    for directory in dirs:
        if directory and directory.is_dir():
            for sub in sorted(directory.rglob("*"), reverse=True):
                if sub.is_dir() and not any(sub.iterdir()):
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
    if removed:
        log.info("Удалено файлов: %d, освобождено %.1f МБ", len(removed), freed / 1048576)
    return SweepResult(removed=removed, freed_bytes=freed)


def touch(path: Path) -> None:
    """Mark a file as used now, so the retention clock restarts."""
    try:
        path.touch()
    except OSError:  # pragma: no cover
        pass


def describe(directory: Path, minutes: int | None = None) -> list[dict[str, object]]:
    """Files currently kept in a directory, with a deletion deadline when asked."""
    if not directory.is_dir():
        return []
    items = []
    now = time.time()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            stat = path.stat()
            item: dict[str, object] = {
                "name": path.name,
                "size": stat.st_size,
                "age_minutes": round((now - stat.st_mtime) / 60, 1),
                "mtime": stat.st_mtime,
            }
            if minutes:
                item["delete_at"] = stat.st_mtime + minutes * 60
                item["left_minutes"] = max(0.0, round((item["delete_at"] - now) / 60, 1))
            items.append(item)
    return items
