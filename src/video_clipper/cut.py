"""Cutting fragments with ffmpeg."""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


TELEGRAM_LIMIT_BYTES = 45 * 1024 * 1024


def copy_whole(src: str | Path, out: str | Path, ffmpeg_path: str = "ffmpeg",
               runner=subprocess.run, timeout: float | None = None) -> ClipResult:
    """Take the whole file as-is: remux, no re-encode, no quality loss."""
    src_path, out_path = Path(src), Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src_path), "-c", "copy", "-movflags", "+faststart", str(out_path),
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CutError(f"ffmpeg не успел скопировать {src_path.name}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise CutError(f"ffmpeg завершился с кодом {proc.returncode}: " + " | ".join(tail))
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise CutError(f"ffmpeg не создал файл {out_path}")
    from .probe import probe_media

    info = probe_media(str(out_path))
    seg = Segment(0.0, round(info.duration, 3))
    return ClipResult(
        source=str(src_path), segment=seg, output_path=out_path,
        duration=seg.duration, mode="whole (копия)", size_bytes=out_path.stat().st_size,
    )


def keyframe_times(
    src: str | Path, ffprobe_path: str = "ffprobe", runner=subprocess.run
) -> list[float]:
    """Timestamps of video keyframes: the only legal cut points for copy-splitting.

    Read at packet level: ffprobe 4.4 does not report ``pts_time`` for frames with
    ``-skip_frame nokey``, while packet flags are always there.
    """
    cmd = [
        ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=pts_time,flags", "-of", "json", str(src),
    ]
    proc = runner(cmd, capture_output=True, text=True, check=False)
    try:
        packets = json.loads(proc.stdout or "{}").get("packets", [])
    except ValueError:
        return []
    times: list[float] = []
    for packet in packets:
        if "K" not in str(packet.get("flags") or ""):
            continue
        try:
            times.append(float(packet.get("pts_time")))
        except (TypeError, ValueError):
            continue
    return sorted(set(times))


def split_for_telegram(
    src: str | Path,
    out_dir: str | Path,
    limit_bytes: int = TELEGRAM_LIMIT_BYTES,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    runner=subprocess.run,
    timeout: float | None = None,
    margin: float = 0.9,
) -> list[Path]:
    """Split a clip into equal parts under ``limit_bytes`` without re-encoding.

    Copy-splitting keeps the original bitrate: a 1080p clip that does not fit
    stays 1080p, only cut in two, instead of being crushed to 480p.
    """
    src_path = Path(src)
    out_path_dir = Path(out_dir)
    out_path_dir.mkdir(parents=True, exist_ok=True)
    size = src_path.stat().st_size
    if size <= limit_bytes:
        return [src_path]

    from .probe import probe_media

    duration = probe_media(str(src_path), ffprobe_path).duration
    if duration <= 0:
        raise CutError(f"не удалось определить длительность {src_path.name}")

    budget = limit_bytes * margin
    count = max(2, math.ceil(size / budget))
    frames = keyframe_times(src_path, ffprobe_path, runner)
    if len(frames) < 3:
        # One keyframe per clip: any copy cut would duplicate the whole video.
        raise CutError("в клипе почти нет ключевых кадров — резать без пересжатия нельзя")

    # Cut exactly on keyframes: parts neither overlap nor lose a frame.
    cuts: list[float] = []
    for index in range(1, count):
        target = duration * index / count
        nearest = min(frames, key=lambda t: abs(t - target))
        if cuts and nearest <= cuts[-1]:
            continue
        cuts.append(nearest)
    boundaries = [0.0, *cuts, duration]

    parts: list[Path] = []
    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end - start <= 0.2:
            continue
        out = out_path_dir / f"{src_path.stem}_part{index + 1}{src_path.suffix}"
        cmd = [
            ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(src_path),
            "-t", f"{end - start:.3f}",
            "-map", "0", "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        proc = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise CutError(f"не удалось разрезать {src_path.name} на части: " + " | ".join(tail))
        if out.stat().st_size > limit_bytes:
            raise CutError(f"часть {out.name} вышла больше лимита Telegram")
        parts.append(out)
    if len(parts) < 2:
        raise CutError("не удалось разделить клип на части")
    return parts


def shrink_to_fit(
    src: str | Path,
    out: str | Path,
    limit_bytes: int = TELEGRAM_LIMIT_BYTES,
    ffmpeg_path: str = "ffmpeg",
    has_audio: bool = True,
    runner=subprocess.run,
    timeout: float | None = None,
) -> Path:
    """Re-encode a file so it fits under ``limit_bytes`` (Telegram caps at 50 MB).

    Two passes are enough in practice: 720p at CRF 28, then 480p at CRF 30.
    """
    attempts = [(720, 28), (480, 30)]
    last: Path | None = None
    for height, crf in attempts:
        cmd = [
            ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", str(src),
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p",
        ]
        cmd += ["-c:a", "aac", "-b:a", "96k"] if has_audio else ["-an"]
        cmd += ["-movflags", "+faststart", str(out)]
        proc = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        if proc.returncode != 0 or not Path(out).is_file():
            raise CutError(
                "не удалось сжать под лимит Telegram: "
                + " | ".join((proc.stderr or "").strip().splitlines()[-3:])
            )
        last = Path(out)
        if last.stat().st_size <= limit_bytes:
            return last
    if last is None or last.stat().st_size > limit_bytes:
        raise CutError(
            f"даже после сжатия файл больше лимита Telegram ({limit_bytes // 1048576} МБ) — "
            f"пришли отрезок покороче"
        )
    return last


def _run_with_progress(
    cmd: list[str],
    seg: Segment,
    progress: Callable[[str, float | None, str], None],
    timeout: float | None,
) -> tuple[int, str]:
    """Run ffmpeg and report progress from its ``-progress pipe:1`` stream.

    stderr goes to a temporary file: ffmpeg logs enough to fill a pipe buffer and
    block the process if nobody reads it.
    """
    full = [cmd[0], "-nostats", "-progress", "pipe:1", *cmd[1:]]
    deadline = time.monotonic() + timeout if timeout else None
    with tempfile.TemporaryFile(mode="w+") as err:
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=err, text=True)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    raw = line.split("=", 1)[1]
                    if raw.isdigit():
                        seconds = int(raw) / 1_000_000
                        progress(
                            "encode",
                            min(1.0, seconds / max(seg.duration, 0.001)),
                            seg.label,
                        )
                if deadline and time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise CutError(
                        f"ffmpeg не успел обработать {seg.label} за {timeout} с — отрезок слишком "
                        f"длинный для этого режима, попробуй покороче или --fast"
                    )
        finally:
            proc.stdout.close()
        proc.wait()
        err.seek(0)
        return proc.returncode, err.read()


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
    progress: Callable[[str, float | None, str], None] | None = None,
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

    if progress is None:
        try:
            proc = runner(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CutError(
                f"ffmpeg не успел обработать {seg.label} за {timeout} с — отрезок слишком длинный "
                f"для этого режима, попробуй покороче или --fast"
            ) from exc
        returncode, stderr = proc.returncode, proc.stderr or ""
    else:
        returncode, stderr = _run_with_progress(cmd, seg, progress, timeout)
    if returncode != 0:
        tail = stderr.strip().splitlines()[-5:]
        raise CutError(
            f"ffmpeg завершился с кодом {returncode} для {seg.label}: " + " | ".join(tail)
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
