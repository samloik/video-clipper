"""End-to-end orchestration: source → validation → cutting → results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Config
from .cut import (
    ClipResult,
    copy_whole,
    cut_segment,
    ensure_free_space,
    pick_vertical_size,
    vertical_name,
)
from .segments import (
    Segment,
    build_segments,
    clip_filename,
    validate_segments,
)
from .source import is_url, prepare_source, shift_segments
from .storage import sweep, touch

log = logging.getLogger("video_clipper")


def run_pipeline(args: Any, cfg: Config) -> list[ClipResult]:
    """Run the whole flow for parsed CLI args and return per-clip results."""
    segments: list[Segment] = build_segments(
        ranges=getattr(args, "ranges", None),
        csv_path=getattr(args, "csv", None),
        json_path=getattr(args, "json_segments", None),
        name=getattr(args, "name", None),
        allow_empty=True,
    )
    whole = not segments
    fast = bool(getattr(args, "fast", False))
    vertical = bool(getattr(args, "vertical", False) or getattr(args, "blur", False))
    layout = "blur" if bool(getattr(args, "blur", False)) else "crop"
    dry_run = bool(getattr(args, "dry_run", False))
    keep_source = bool(getattr(args, "keep_source", False)) or cfg.keep_source
    source_value = str(args.source)
    sweep([cfg.cache_dir], cfg.retention_minutes)
    out_dir = Path(cfg.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Dry run on a URL must not download anything: validate only what is local.
    if dry_run and is_url(source_value):
        if whole:
            log.info("dry-run: будет скачан весь ролик целиком → %s", out_dir)
            return []
        validate_segments(segments, duration=None, allow_overlap=args.allow_overlap)
        results = [_planned(None, source_value, seg, out_dir, fast) for seg in segments]
        for res in results:
            log.info("dry-run: %s", res.output_path)
        return results

    src = prepare_source(
        source_value,
        cfg,
        segments=[(s.start, s.end) for s in segments],
        keep_source=keep_source,
    )
    try:
        if whole:
            # No ranges given: the request is the entire video.
            if src.info.duration <= 0:
                raise ValueError("не удалось определить длительность ролика")
            segments = [Segment(0.0, round(src.info.duration, 3))]
            log.info("Ролик целиком: %.1f с", src.info.duration)
            if not vertical and not fast:
                out_path = out_dir / clip_filename(
                    src.title, source_value, segments[0], span="весь ролик"
                )
                result = copy_whole(str(src.path), out_path, ffmpeg_path=cfg.ffmpeg_path)
                return [result]
        # A section download starts at 0, so absolute times are mapped onto it;
        # file names and reports keep the times the user asked for.
        original_segments = segments
        segments = shift_segments(segments, src.offset)
        validate_segments(
            segments,
            duration=src.info.duration if src.info.duration > 0 else None,
            allow_overlap=args.allow_overlap,
        )
        if dry_run:
            results = [
                _planned(src.title, source_value, seg, out_dir, fast) for seg in segments
            ]
            for res in results:
                log.info("dry-run: %s", res.output_path)
            return results

        ensure_free_space(out_dir)
        results = []
        for i, seg in enumerate(segments):
            shown = original_segments[i] if i < len(original_segments) else seg
            name = args.name if (getattr(args, "name", None) and len(segments) == 1) else None
            out_path = out_dir / vertical_name(
                clip_filename(src.title, source_value, shown, name), vertical, layout
            )
            result = cut_segment(
                str(src.path),
                out_path,
                seg,
                fast=fast,
                ffmpeg_path=cfg.ffmpeg_path,
                has_audio=src.info.has_audio,
                vertical=vertical,
                layout=layout,
                vertical_size=pick_vertical_size(
                    src.info.height, cfg.vertical_width, cfg.vertical_height
                ),
            )
            result.segment = shown
            results.append(result)
        return results
    finally:
        if src.kind == "section" or (src.is_temporary and not keep_source):
            src.cleanup()
        else:
            touch(src.path)


def _planned(
    title: str | None, source: str, seg: Segment, out_dir: Path, fast: bool
) -> ClipResult:
    return ClipResult(
        source="dry-run",
        segment=seg,
        output_path=out_dir / clip_filename(title, source, seg),
        duration=seg.duration,
        mode="fast" if fast else "accurate",
        size_bytes=0,
    )
