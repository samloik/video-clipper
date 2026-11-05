"""JSON report of a cutting run."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .cut import ClipResult

log = logging.getLogger("video_clipper")


def build_report(source: str, results: list[ClipResult], dry_run: bool = False) -> dict[str, object]:
    total = sum(r.duration for r in results)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "dry_run": dry_run,
        "clips": [r.as_dict() for r in results],
        "total_clips": len(results),
        "total_duration": round(total, 3),
        "total_size_bytes": sum(r.size_bytes for r in results),
    }


def write_json_report(path: str | Path, results: list[ClipResult], source: str = "", dry_run: bool = False) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_report(source, results, dry_run), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Отчёт записан: %s", target)
    return target
