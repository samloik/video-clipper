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

VERTICAL_SIZE = (1080, 1920)

# "cover": scale so the frame fills 1080x1920, then crop the overflow — the usual
# centre crop for Shorts/Reels. "blur": fit the whole frame, back it with a
# blurred, cropped copy. Both keep the SAR at 1 so phones do not letterbox.
VERTICAL_CROP_FILTER = (
    "scale={w}:{h}:force_original_aspect_ratio=increase,"
    "crop={w}:{h},setsar=1"
)
# The backdrop is blurred anyway, so it is built at 1/4 size and scaled up:
# gblur at 1080x1920 costs minutes per clip on a small VM, at 270x480 — seconds,
# with no visible difference after the upscale.
BLUR_BG_SCALE = 4

VERTICAL_BLUR_FILTER = (
    "[0:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
    "gblur=sigma=8,scale={w}:{h}[bg];"
    "[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
)


def pick_vertical_size(source_height: int | None, width: int = 1080, height: int = 1920) -> tuple[int, int]:
    """Do not upscale a small source into 1080x1920 for no reason.

    A 720p source gives a 405x720 crop; blowing it up to 1080x1920 costs ~2x the
    encode time and adds no detail, so 720x1280 is used instead.
    """
    if source_height and source_height <= 720:
        return (min(width, 720), min(height, 1280))
    return (width, height)


def vertical_name(filename: str, vertical: bool, layout: str = "crop") -> str:
    """Mark the variant in the file name so both versions can coexist."""
    if not vertical:
        return filename
    suffix = "_vertical_blur" if layout == "blur" else "_vertical"
    stem = filename[:-4] if filename.lower().endswith(".mp4") else filename
    return f"{stem}{suffix}.mp4"


def vertical_args(layout: str = "crop", size: tuple[int, int] = VERTICAL_SIZE) -> list[str]:
    """ffmpeg video-filter arguments for a 9:16 frame."""
    width, height = size
    if layout == "blur":
        return [
            "-filter_complex",
            VERTICAL_BLUR_FILTER.format(
                w=width,
                h=height,
                bw=max(2, width // BLUR_BG_SCALE) // 2 * 2,
                bh=max(2, height // BLUR_BG_SCALE) // 2 * 2,
            ),
        ]
    return ["-vf", VERTICAL_CROP_FILTER.format(w=width, h=height)]


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
    vertical: bool = False,
    layout: str = "crop",
    vertical_size: tuple[int, int] = VERTICAL_SIZE,
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
    if fast and not vertical:
        cmd += FAST_ARGS
    else:
        # Vertical mode always re-encodes: cropping cannot be stream-copied.
        cmd += ACCURATE_VIDEO_ARGS
        if vertical:
            cmd += vertical_args(layout, vertical_size)
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
    vertical: bool = False,
    layout: str = "crop",
    vertical_size: tuple[int, int] = VERTICAL_SIZE,
    timeout: float | None = None,
) -> ClipResult:
    """Run ffmpeg for one segment and return the result."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_command(
        src,
        out_path,
        seg,
        fast=fast,
        ffmpeg_path=ffmpeg_path,
        has_audio=has_audio,
        vertical=vertical,
        layout=layout,
        vertical_size=vertical_size,
    )
    mode = "fast" if fast and not vertical else "accurate"
    if vertical:
        mode += "+vertical" if layout != "blur" else "+vertical-blur"
    log.info("Нарезка %s: %s → %s", mode, seg.label, out_path.name)
    log.debug("ffmpeg: %s", " ".join(cmd))

    try:
        proc = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CutError(
            f"ffmpeg не успел обработать {seg.label} за {timeout} с — отрезок слишком длинный "
            f"для этого режима, попробуй покороче или --fast"
        ) from exc
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
