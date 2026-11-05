"""End-to-end orchestration: source → validation → cutting → results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Config
from .cut import ClipResult, cut_segment, ensure_free_space
from .segments import (
    Segment,
    build_segments,
    output_filename,
    source_slug,
    validate_segments,
)
from .source import is_url, prepare_source

log = logging.getLogger("video_clipper")


def run_pipeline(args: Any, cfg: Config) -> list[ClipResult]:
    """Run the whole flow for parsed CLI args and return per-clip results."""
    segments: list[Segment] = build_segments(
        ranges=getattr(args, "ranges", None),
        csv_path=getattr(args, "csv", None),
        json_path=getattr(args, "json_segments", None),
        name=getattr(args, "name", None),
    )
    fast = bool(getattr(args, "fast", False))
    dry_run = bool(getattr(args, "dry_run", False))
    keep_source = bool(getattr(args, "keep_source", False)) or cfg.keep_source
    source_value = str(args.source)
    slug = source_slug(source_value)
    out_dir = Path(cfg.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Dry run on a URL must not download anything: validate only what is local.
    if dry_run and is_url(source_value):
        validate_segments(segments, duration=None, allow_overlap=args.allow_overlap)
        results = [_planned(slug, seg, out_dir, fast, i, len(segments)) for i, seg in enumerate(segments)]
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
        validate_segments(
            segments,
            duration=src.info.duration if src.info.duration > 0 else None,
            allow_overlap=args.allow_overlap,
        )
        if dry_run:
            results = [_planned(slug, seg, out_dir, fast, i, len(segments)) for i, seg in enumerate(segments)]
            for res in results:
                log.info("dry-run: %s", res.output_path)
            return results

        ensure_free_space(out_dir)
        results = []
        for i, seg in enumerate(segments):
            name = args.name if (getattr(args, "name", None) and len(segments) == 1) else None
            out_path = out_dir / output_filename(slug, seg, name)
            results.append(
                cut_segment(
                    str(src.path),
                    out_path,
                    seg,
                    fast=fast,
                    ffmpeg_path=cfg.ffmpeg_path,
                    has_audio=src.info.has_audio,
                )
            )
        return results
    finally:
        if not keep_source:
            src.cleanup()


def _planned(slug: str, seg: Segment, out_dir: Path, fast: bool, index: int, total: int) -> ClipResult:
    return ClipResult(
        source="dry-run",
        segment=seg,
        output_path=out_dir / output_filename(slug, seg),
        duration=seg.duration,
        mode="fast" if fast else "accurate",
        size_bytes=0,
    )
