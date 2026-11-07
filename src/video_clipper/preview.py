"""Кадр-превью и паспорт файла: посмотреть границы до нарезки.

Всё работает по локальному файлу: либо загруженному вручную, либо уже
скачанному в кэш. Ссылку здесь намеренно не качаем — загрузка ради одного
кадра слишком дорога, поэтому GUI честно говорит «сначала нарежь».
"""

from __future__ import annotations

import json
import logging
import subprocess
from fractions import Fraction
from pathlib import Path

from .config import Config
from .storage import find_cached_source

log = logging.getLogger(__name__)

FRAME_TIMEOUT = 25


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
    seconds = max(0.0, float(seconds))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seconds:.3f}", "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={max(32, int(width))}:-2:flags=fast_bilinear",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", "5",
        "pipe:1",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Кадр не получился (%s, %.2f с): %s", path.name, seconds, exc)
        return b""
    return out.stdout if out.stdout[:2] == b"\xff\xd8" else b""
