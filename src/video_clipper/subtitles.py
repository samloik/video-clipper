"""Субтитры: разбор srt/vtt, отбор реплик отрезка и запись готового .srt.

Задача модуля — превратить файл субтитров исходного ролика в файл, который
описывает ровно вырезанный клип: время начинается с нуля, реплики за границами
отрезка отброшены, а те, что попали на границу, обрезаны по ней.

Никакого ffmpeg здесь нет: это чистая логика, её проверяют тестами без роликов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Cue",
    "SubtitleError",
    "detect_format",
    "parse_subtitles",
    "parse_srt",
    "parse_vtt",
    "shift_cues",
    "cues_for_segment",
    "render_srt",
    "write_segment_srt",
    "escape_for_ffmpeg",
]


class SubtitleError(ValueError):
    """Файл субтитров не разобрался."""


@dataclass(frozen=True)
class Cue:
    """Одна реплика: секунды от начала и текст (может быть многострочным)."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


_TIME_RE = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_ASS_RE = re.compile(r"\{\\[^}]*\}")
_CUE_ID_RE = re.compile(r"^\d+$")


def _seconds(stamp: str) -> float:
    match = _TIME_RE.search(stamp.strip())
    if not match:
        raise SubtitleError(f"не понял время субтитров: {stamp!r}")
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int(match.group("ms").ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _clean(text: str) -> str:
    """Убираем разметку и лишние пробелы, сохраняя перенос строк."""
    lines = []
    for raw in text.splitlines():
        line = _ASS_RE.sub("", _TAG_RE.sub("", raw)).strip()
        if line:
            lines.append(" ".join(line.split()))
    return "\n".join(lines)


def detect_format(text: str) -> str:
    """``srt`` или ``vtt`` — по заголовку, затем по виду таймкодов."""
    head = text.lstrip("\ufeff \n\r\t")[:200].upper()
    if head.startswith("WEBVTT"):
        return "vtt"
    if "-->" in text and re.search(r"\d{2}:\d{2}:\d{2}\.\d{3}", text):
        return "vtt"
    return "srt"


def _blocks(text: str) -> list[list[str]]:
    """Абзацы файла: пустая строка разделяет реплики."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _parse(text: str, *, skip_range: bool) -> list[Cue]:
    cues: list[Cue] = []
    for block in _blocks(text):
        lines = [line for line in block if line.strip()]
        if not lines:
            continue
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if _CUE_ID_RE.match(lines[0].strip()):
            lines = lines[1:]
        # в vtt у реплики может быть своё имя вместо номера: «cue-1»
        if len(lines) > 1 and "-->" not in lines[0] and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        # в vtt после таймкодов бывают настройки: «... align:middle line:90%»
        left, _, right = lines[0].partition("-->")
        right = right.strip().split(" ")[0] if right.strip() else ""
        if skip_range and "-" in right and not _TIME_RE.search(right):
            continue
        try:
            start = _seconds(left)
            end = _seconds(right)
        except SubtitleError:
            continue
        text_body = _clean("\n".join(lines[1:]))
        if not text_body:
            continue
        if end <= start:
            end = start + 0.5
        cues.append(Cue(start=start, end=end, text=text_body))
    return sorted(cues, key=lambda cue: (cue.start, cue.end))


def parse_srt(text: str) -> list[Cue]:
    return _parse(text, skip_range=False)


def parse_vtt(text: str) -> list[Cue]:
    return _parse(text, skip_range=True)


def _read(path: Path) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SubtitleError(f"не смог прочитать субтитры как текст: {path}")


def parse_subtitles(path: str | Path) -> list[Cue]:
    """Все реплики файла. Формат определяется по содержимому, не по расширению."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise SubtitleError(f"файл субтитров не найден: {target}")
    text = _read(target)
    cues = parse_vtt(text) if detect_format(text) == "vtt" else parse_srt(text)
    if not cues:
        raise SubtitleError(f"в субтитрах нет ни одной реплики: {target.name}")
    return cues


def shift_cues(cues: list[Cue], offset: float) -> list[Cue]:
    """Сдвинуть реплики на ``offset`` секунд (может быть отрицательным)."""
    if not offset:
        return list(cues)
    return [Cue(start=cue.start + offset, end=cue.end + offset, text=cue.text) for cue in cues]


def cues_for_segment(cues: list[Cue], start: float, end: float | None = None) -> list[Cue]:
    """Реплики клипа: обрезаны по границам отрезка и сдвинуты к нулю."""
    left = float(start)
    right = float("inf") if end is None else float(end)
    result: list[Cue] = []
    for cue in cues:
        if cue.end <= left or cue.start >= right:
            continue
        cue_start = max(cue.start, left)
        cue_end = min(cue.end, right)
        if cue_end - cue_start < 0.05:
            continue
        result.append(
            Cue(start=cue_start - left, end=cue_end - left, text=cue.text)
        )
    return result


def _stamp(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_srt(cues: list[Cue]) -> str:
    """Текст .srt; пустой список даёт пустую строку."""
    parts = []
    for index, cue in enumerate(cues, start=1):
        parts.append(f"{index}\n{_stamp(cue.start)} --> {_stamp(cue.end)}\n{cue.text}\n")
    return "\n".join(parts)


def write_segment_srt(
    source: str | Path, target: str | Path, start: float, end: float | None = None
) -> Path:
    """Записать рядом с клипом .srt, описывающий только этот отрезок."""
    cues = cues_for_segment(parse_subtitles(source), start, end)
    if not cues:
        raise SubtitleError("в этом отрезке нет реплик — субтитры не нужны")
    out = Path(target).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_srt(cues), encoding="utf-8")
    return out


def escape_for_ffmpeg(path: str | Path) -> str:
    """Путь для фильтра ``subtitles=``: экранируются : ' \\ и запятые."""
    text = str(Path(path).expanduser().resolve())
    for char in ("\\", ":", "'", ",", "[", "]", ";"):
        text = text.replace(char, "\\" + char)
    return text
