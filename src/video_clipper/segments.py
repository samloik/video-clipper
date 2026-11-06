"""Segment list: sources (CLI ranges, CSV, JSON), validation and file names."""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .timecode import filename_timecode, format_timecode, parse_timecode

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    name: str | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    @property
    def label(self) -> str:
        return f"{format_timecode(self.start)}–{format_timecode(self.end)}"


def slugify(text: str, fallback: str = "video") -> str:
    """ASCII slug for file names; Cyrillic is transliterated."""
    lowered = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return slug[:60] or fallback


def source_slug(source: str) -> str:
    """File stem for a local path, a useful id for a URL.

    Prefers ``?v=`` (YouTube watch links), then the last path part, which covers
    ``youtu.be/<id>``, ``/shorts/<id>`` and ``tiktok.com/@user/video/<id>``.
    """
    if "://" in source:
        parsed = urlparse(source)
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if video_id:
            return slugify(video_id, "clip")
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            return slugify(parts[-1], "clip")
        return slugify(parsed.netloc, "clip")
    return slugify(Path(source).stem, "clip")


_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_KEEP_CATEGORIES = frozenset("LNPZ")


def safe_part(text: str, limit: int = 80) -> str:
    """Human-readable name fragment: letters, digits and punctuation; no emoji or control marks."""
    cleaned = _FORBIDDEN.sub("", str(text))
    cleaned = "".join(
        ch for ch in cleaned if unicodedata.category(ch)[0] in _KEEP_CATEGORIES
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    return cleaned[:limit].strip(" ._-")


def source_ref(source: str) -> str:
    """Filesystem-safe reference to the source: ``youtube.com-<id>`` or the file name."""
    if "://" in source:
        parsed = urlparse(source)
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        parts = [p for p in parsed.path.split("/") if p]
        tail = video_id or (parts[-1] if parts else "")
        host = parsed.netloc.lower().removeprefix("www.")
        return safe_part(f"{host}-{tail}" if tail else host, 60)
    return safe_part(Path(source).name, 60)


def span_timecode(seconds: float) -> str:
    """``MM:SS`` under an hour, ``H:MM:SS`` above — the way users type timecodes."""
    total = round(float(seconds), 3)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    secs_text = f"{whole:02d}" if not millis else f"{whole:02d}.{millis:03d}".rstrip("0")
    if hours >= 1:
        return f"{int(hours)}:{int(minutes):02d}:{secs_text}"
    return f"{int(minutes):02d}:{secs_text}"


def clip_filename(
    title: str | None,
    source: str,
    seg: Segment,
    name: str | None = None,
    span: str | None = None,
    suffix: str = "",
) -> str:
    """``Название - ссылка - [29:00-31:59].mp4``.

    ``title`` is what a human recognises (video title), ``source`` adds the origin so a
    clip can be traced back. ``span`` replaces the timecode brackets (``весь ролик``),
    ``suffix`` marks a variant (``-fast``), and ``name`` still wins over everything.
    """
    if name or seg.name:
        return output_filename(slugify(title or source), seg, name)

    base = safe_part(title or "", 80)
    ref = source_ref(source)
    if not base:
        base, ref = ref, ""
    elif "://" not in source and ref and Path(source).stem == title:
        ref = ""  # a local file named like its own title adds nothing
    if ref and ref.lower() in base.lower():
        ref = ""

    bracket = f"[{span}]" if span else f"[{span_timecode(seg.start)}-{span_timecode(seg.end)}]"
    parts = [base, ref, bracket]
    stem = " - ".join(p for p in parts if p)
    if suffix:
        stem = f"{stem}{suffix}"
    return f"{stem}.mp4"


def parse_range_spec(spec: str) -> Segment:
    """Parse ``START-END`` (e.g. ``1:12-1:45``)."""
    raw = spec.strip()
    if "-" not in raw:
        raise ValueError(f"Отрезок должен быть в формате НАЧАЛО-КОНЕЦ, получено: {spec!r}")
    left, right = raw.split("-", 1)
    return Segment(parse_timecode(left), parse_timecode(right))


def _segments_from_rows(rows: list[dict[str, object]]) -> list[Segment]:
    out: list[Segment] = []
    for i, row in enumerate(rows, start=1):
        if "start" not in row or "end" not in row:
            raise ValueError(f"Строка {i}: нужны поля start и end")
        name = row.get("name")
        out.append(
            Segment(
                parse_timecode(row["start"]),  # type: ignore[arg-type]
                parse_timecode(row["end"]),  # type: ignore[arg-type]
                str(name) if name not in (None, "") else None,
            )
        )
    return out


def load_csv(path: str | Path) -> list[Segment]:
    """CSV with ``start,end[,name]``; a header row and ``#`` comments are allowed."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Файл отрезков не найден: {p}")
    rows: list[dict[str, object]] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            cells = [c.strip() for c in raw]
            if not cells or not any(cells) or cells[0].startswith("#"):
                continue
            if cells[0].lower() in {"start", "начало", "from"}:
                continue
            if len(cells) < 2:
                raise ValueError(f"{p}: строка без конца отрезка: {raw}")
            row: dict[str, object] = {"start": cells[0], "end": cells[1]}
            if len(cells) > 2 and cells[2]:
                row["name"] = cells[2]
            rows.append(row)
    if not rows:
        raise ValueError(f"{p}: не найдено ни одного отрезка")
    return _segments_from_rows(rows)


def load_json(path: str | Path) -> list[Segment]:
    """JSON list of ``{"start", "end", "name"?}``, ``[start, end]`` or ``"start-end"``."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Файл отрезков не найден: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{p}: ожидается непустой JSON-массив отрезков")
    rows: list[dict[str, object]] = []
    for item in data:
        if isinstance(item, dict):
            rows.append(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            row: dict[str, object] = {"start": item[0], "end": item[1]}
            if len(item) > 2 and item[2]:
                row["name"] = item[2]
            rows.append(row)
        elif isinstance(item, str):
            seg = parse_range_spec(item)
            rows.append({"start": seg.start, "end": seg.end})
        else:
            raise ValueError(f"{p}: неподдерживаемый элемент {item!r}")
    return _segments_from_rows(rows)


def build_segments(
    ranges: list[str] | None = None,
    csv_path: str | None = None,
    json_path: str | None = None,
    name: str | None = None,
    allow_empty: bool = False,
) -> list[Segment]:
    """Collect segments from every source; ``name`` is only valid for a single one.

    ``allow_empty`` lets the caller ask for the whole video: no segments at all
    means "take everything" instead of an error.
    """
    segments: list[Segment] = [parse_range_spec(r) for r in (ranges or [])]
    if csv_path:
        segments.extend(load_csv(csv_path))
    if json_path:
        segments.extend(load_json(json_path))
    if not segments and not allow_empty:
        raise ValueError("Не задан ни один отрезок: используй --range, --csv или --json")
    if name and len(segments) > 1:
        raise ValueError("--name допустим только для одного отрезка")
    return segments


def validate_segments(
    segments: list[Segment], duration: float | None = None, allow_overlap: bool = False
) -> None:
    """Reject empty, inverted, out-of-range and (by default) overlapping segments."""
    if not segments:
        raise ValueError("Список отрезков пуст")
    for seg in segments:
        if seg.end <= seg.start:
            raise ValueError(
                f"Отрезок {seg.label}: конец ({format_timecode(seg.end)}) должен быть больше начала "
                f"({format_timecode(seg.start)})"
            )
        if duration and seg.end > duration + 1e-6:
            raise ValueError(
                f"Отрезок {seg.label} выходит за длительность видео ({format_timecode(duration)})"
            )

    if not allow_overlap and len(segments) > 1:
        ordered = sorted(segments, key=lambda s: s.start)
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.start < prev.end:
                raise ValueError(
                    f"Отрезки пересекаются: {prev.label} и {cur.label} "
                    f"(разрешить можно флагом --allow-overlap)"
                )


def output_filename(slug: str, seg: Segment, name: str | None = None) -> str:
    """``<slug>__<start>__<end>.mp4`` or ``<name>.mp4`` when a name is given."""
    if name:
        safe = re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("._") or "clip"
        return safe if safe.lower().endswith(".mp4") else f"{safe}.mp4"
    if seg.name:
        safe = re.sub(r"[^\w.-]+", "_", seg.name, flags=re.UNICODE).strip("._") or "clip"
        return safe if safe.lower().endswith(".mp4") else f"{safe}.mp4"
    return f"{slug}__{filename_timecode(seg.start)}__{filename_timecode(seg.end)}.mp4"
