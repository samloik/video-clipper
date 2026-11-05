"""Cutting fragments with ffmpeg."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .segments import Segment

log = logging.getLogger("video_clipper")

ACCURATE_VIDEO_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
]
ACCURATE_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k"]
FAST_ARGS = ["-c", "copy"]


class CutError(RuntimeError):
    """Raised when ffmpeg fails or the result looks wrong."""


@dataclass
class ClipResult:
    source: str
    segment: Segment
    output_path: Path
    duration: float
    mode: str
    size_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "start": self.segment.start,
            "end": self.segment.end,
            "output": str(self.output_path),
            "duration": self.duration,
            "mode": self.mode,
            "size_bytes": self.size_bytes,
        }


def build_ffmpeg_command(
    src: str | Path,
    out: str | Path,
    seg: Segment,
    fast: bool = False,
    ffmpeg_path: str = "ffmpeg",
    has_audio: bool = True,
) -> list[str]:
    """Build the ffmpeg argv for one segment.

    ``-ss`` before ``-i`` keeps seeking fast; with re-encoding it is still
    frame accurate because everything from the seek point is decoded.
    """
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-y",
        "-ss", f"{seg.start:.3f}",
        "-i", str(src),
        "-t", f"{seg.duration:.3f}",
    ]
    if fast:
        cmd += FAST_ARGS
    else:
        cmd += ACCURATE_VIDEO_ARGS
        cmd += ACCURATE_AUDIO_ARGS if has_audio else ["-an"]
        # Re-encoded output may start at a negative PTS offset; zero it.
        # With -c copy this option inflates the duration by the seek offset,
        # so it is applied only for re-encoding.
        cmd += ["-avoid_negative_ts", "make_zero"]
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def cut_segment(
    src: str | Path,
    out: str | Path,
    seg: Segment,
    fast: bool = False,
    ffmpeg_path: str = "ffmpeg",
    has_audio: bool = True,
    runner=subprocess.run,
) -> ClipResult:
    """Run ffmpeg for one segment and return the result."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_command(src, out_path, seg, fast=fast, ffmpeg_path=ffmpeg_path, has_audio=has_audio)
    mode = "fast" if fast else "accurate"
    log.info("Нарезка %s: %s → %s", mode, seg.label, out_path.name)
    log.debug("ffmpeg: %s", " ".join(cmd))

    proc = runner(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise CutError(
            f"ffmpeg завершился с кодом {proc.returncode} для {seg.label}: " + " | ".join(tail)
        )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise CutError(f"ffmpeg не создал файл {out_path}")

    from .probe import probe_media

    info = probe_media(str(out_path))
    return ClipResult(
        source=str(src),
        segment=seg,
        output_path=out_path,
        duration=round(info.duration, 3),
        mode=mode,
        size_bytes=out_path.stat().st_size,
    )


def remove_source(path: str | Path) -> None:
    """Delete a downloaded source file after cutting."""
    p = Path(path)
    try:
        p.unlink(missing_ok=True)
        if p.parent.is_dir() and not any(p.parent.iterdir()):
            p.parent.rmdir()
    except OSError:  # pragma: no cover - best effort
        log.warning("Не удалось удалить исходник %s", p)


def ensure_free_space(target_dir: Path, needed_bytes: int = 200 * 1024 * 1024) -> None:
    """Fail early when the output disk is nearly full."""
    target_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    if free < needed_bytes:
        raise CutError(
            f"Мало места на диске: свободно {free // (1024 * 1024)} МБ, "
            f"нужно хотя бы {needed_bytes // (1024 * 1024)} МБ"
        )
