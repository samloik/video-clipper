"""Environment and media probing helpers (ffmpeg / ffprobe)."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


class EnvironmentProblem(RuntimeError):
    """Raised when ffmpeg/ffprobe are missing or unusable."""


def _resolve_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise EnvironmentProblem(
            f"Не найден '{name}'. Установи ffmpeg: sudo apt install ffmpeg "
            f"(или укажи путь в config.json: ffmpeg_path / ffprobe_path)."
        )
    return found


def tool_version(name: str) -> str:
    """Return the first line of ``name -version``."""
    path = _resolve_tool(name)
    try:
        out = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env specific
        raise EnvironmentProblem(f"Не удалось запустить {name}: {exc}") from exc
    return (out.stdout or out.stderr).splitlines()[0] if out.stdout or out.stderr else "unknown"


def check_tools(ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> dict[str, str]:
    """Verify both tools exist and are executable.

    Returns {'ffmpeg': version, 'ffprobe': version}. Raises EnvironmentProblem
    with an actionable message otherwise.
    """
    return {"ffmpeg": tool_version(ffmpeg_path), "ffprobe": tool_version(ffprobe_path)}


@dataclass
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None


def probe_media(path: str, ffprobe_path: str = "ffprobe") -> MediaInfo:
    """Read duration and stream presence with ffprobe."""
    tool = _resolve_tool(ffprobe_path)
    cmd = [
        tool,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise EnvironmentProblem(
            f"ffprobe не смог прочитать '{path}': {(proc.stderr or '').strip()[:400]}"
        )
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration = 0.0
    raw_duration = data.get("format", {}).get("duration")
    if raw_duration is None:
        raw_duration = video.get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    return MediaInfo(
        duration=duration,
        has_video=has_video,
        has_audio=has_audio,
        width=video.get("width"),
        height=video.get("height"),
    )
