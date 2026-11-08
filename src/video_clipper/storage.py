"""Session cache and retention.

The exchange is deliberately short-lived: a downloaded source stays only while
the session is active (repeated cuts of the same link must not re-download it),
and clips stay until the same idle window expires. `sweep` is the only place
that deletes anything, so `video-clipper-bot` calls it before every request and
the CLI calls it on start.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
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


def _probe_path(directory: Path) -> Path:
    """Nearest existing ancestor: the output dir may not exist until the first clip."""
    probe = Path(directory)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def free_mb(directory: Path) -> float:
    """Free space on the volume holding ``directory``, in megabytes."""
    try:
        return round(shutil.disk_usage(_probe_path(directory)).free / 1048576, 1)
    except OSError:
        return 0.0


def total_mb(directory: Path) -> float:
    try:
        return round(shutil.disk_usage(_probe_path(directory)).total / 1048576, 1)
    except OSError:
        return 0.0


def make_room(dirs: list[Path], target_mb: float) -> "SweepResult":
    """Delete the oldest files until ``target_mb`` is free again.

    Used before a job when the disk is close to full: better to lose a cached
    download than to fail halfway through writing a clip.
    """
    removed: list[Path] = []
    freed = 0
    probe = dirs[0] if dirs else Path(".")
    if free_mb(probe) >= target_mb:
        return SweepResult(removed=[], freed_bytes=0)
    files: list[Path] = []
    for directory in dirs:
        if directory and directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        if free_mb(probe) >= target_mb:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            removed.append(path)
            freed += size
        except OSError:
            continue
    if removed:
        log.info("Мало места: удалил %d старых файлов, освободил %.1f МБ", len(removed), freed / 1048576)
    return SweepResult(removed=removed, freed_bytes=freed)


def load_positions(path: Path | None) -> dict[str, float]:
    """Remembered horizontal positions of the 9:16 window, keyed by source."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            continue
    return out


def save_position(path: Path | None, source: str, horizontal: float) -> None:
    if path is None or not source:
        return
    data = load_positions(path)
    data[source] = min(1.0, max(0.0, float(horizontal)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("Не смог сохранить позицию кадра в %s", path)


def record_history(path: Path | None, entry: dict) -> None:
    """Append one finished job to the JSONL history (best effort)."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("Не смог записать историю задания в %s", path)


def read_history(path: Path | None, limit: int = 10) -> list[dict]:
    """Last ``limit`` finished jobs, newest first."""
    if path is None or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def describe(directory: Path, minutes: int | None = None) -> list[dict[str, object]]:
    """Files currently kept in a directory, with a deletion deadline when asked."""
    if not directory.is_dir():
        return []
    items = []
    now = time.time()
    for path in sorted(directory.rglob("*")):
        # служебные файлы (кэш адресов потоков) — не «файлы на машине»
        if path.name == "streams.json":
            continue
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
