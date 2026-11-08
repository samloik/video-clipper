"""Timecode parsing and formatting.

Component separators are ``:`` ``,`` ``/`` and ``\\``, all equivalent: ``45``,
``0:45``, ``34:43``, ``1/12`` and ``1\\12``. One part is seconds, two are
minutes and seconds, three are hours, minutes and seconds. An edge part may be
omitted — ``:12`` is 0:12, ``45:`` is 0:45 — while a hole in the middle
(``1::30``) is a typo and rejected.

A dot is never a separator: it always starts the fraction of a second, from one
to three digits (``1:12.500`` = 72.5 s, ``1:12.5`` = 72.5 s, ``0.30`` = 0.3 s).
Four digits after the dot are a mistake: a frame step used to produce
``0:29.1000``, which was then read as 29 min 1000 s.

Negative values, empty strings and anything that is not a timecode are rejected
with a message that names the offending value.
"""

from __future__ import annotations

import re
import time

# Every character people use instead of ":" in the chat. A dot is deliberately
# absent: it means the fraction of a second, never a part boundary.
_SEPARATOR_RE = re.compile(r"[:\\/,]")
# A trailing ".ddd" after the last part is a fraction of a second (1..3 digits).
_FRACTION_RE = re.compile(r"\.(\d*)$")


def parse_timecode(value: str | int | float) -> float:
    """Parse a timecode into seconds (float)."""
    if isinstance(value, bool):  # bool is an int subclass, but not a timecode
        raise ValueError(f"Некорректный таймкод: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"Таймкод не может быть отрицательным: {value}")
        return float(value)

    raw = str(value).strip()
    if not raw:
        raise ValueError("Пустой таймкод")
    if raw.startswith("-"):
        raise ValueError(f"Таймкод не может быть отрицательным: {raw!r}")

    millis = 0
    fraction_match = _FRACTION_RE.search(raw)
    if fraction_match:
        digits = fraction_match.group(1)
        if len(digits) > 3:
            raise ValueError(
                f"Некорректный таймкод: {value!r} (после точки — до трёх цифр, это доли секунды)"
            )
        millis = int(digits.ljust(3, "0")) if digits else 0
        raw = raw[: fraction_match.start()]

    parts = _SEPARATOR_RE.split(raw)
    if len(parts) > 3:
        raise ValueError(f"Некорректный таймкод: {value!r} (ожидается ЧЧ:ММ:СС[.ммм])")

    if not any(p.strip() for p in parts):
        if millis:
            parts = ["0"]
        else:
            raise ValueError(f"Некорректный таймкод: {value!r}")

    # Shorthand with an omitted edge part; a hole in the middle stays an error.
    normalized = list(parts)
    for index, part in enumerate(parts):
        if part.strip():
            continue
        if index in (0, len(parts) - 1):
            normalized[index] = "0"
        else:
            raise ValueError(f"Некорректный таймкод: {value!r}")
    parts = normalized

    try:
        numbers = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"Некорректный таймкод: {value!r}") from exc

    for p in parts[:-1]:
        if not re.fullmatch(r"\d+", p.strip()):
            raise ValueError(f"Некорректный таймкод: {value!r}")

    if len(parts) == 1:
        seconds = numbers[0]
    elif len(parts) == 2:
        minutes, seconds = numbers
        if minutes < 0 or seconds < 0 or seconds >= 60:
            raise ValueError(f"Некорректный таймкод: {value!r} (секунды должны быть < 60)")
        seconds = minutes * 60 + seconds
    else:
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            raise ValueError(f"Некорректный таймкод: {value!r} (минуты и секунды должны быть < 60)")
        seconds = hours * 3600 + minutes * 60 + seconds

    if seconds < 0:
        raise ValueError(f"Таймкод не может быть отрицательным: {value!r}")
    return round(seconds + millis / 1000, 3)


def format_timecode(seconds: float, decimals: int = 3) -> str:
    """Format seconds as ``HH:MM:SS`` (or ``HH:MM:SS.mmm`` when decimals > 0)."""
    if seconds < 0:
        raise ValueError("Время не может быть отрицательным")
    total = round(float(seconds), decimals)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if decimals:
        width = 3 + decimals  # "SS" + "." + decimals
        text = f"{secs:0{width}.{decimals}f}"
        if text.endswith("0" * decimals):
            text = text[: -(decimals + 1)]  # drop ".000" for whole seconds
        return f"{int(hours):02d}:{int(minutes):02d}:{text}"
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}"


def human_timecode(seconds: float) -> str:
    """Compact form for the chat: ``0:20``, ``3:45``, ``1:02:03``, ``1:12.5``."""
    total = round(float(seconds), 3)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if secs == int(secs):
        sec_text = f"{int(secs):02d}"
    else:
        sec_text = f"{secs:04.1f}"
    if hours:
        return f"{int(hours)}:{int(minutes):02d}:{sec_text}"
    return f"{int(minutes)}:{sec_text}"


def human_duration(seconds: float) -> str:
    """Length in words: ``20 с``, ``3 мин 20 с``, ``1 ч 05 мин``."""
    total = int(round(float(seconds)))
    if total < 60:
        return f"{total} с"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} мин" if secs == 0 else f"{minutes} мин {secs} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


RETENTION_MAX_MINUTES = 7 * 24 * 60  # неделя — дальше это уже не транзитный узел


def parse_retention(text: str) -> int:
    """How long to keep files, in minutes, from a short human spec.

    ``30`` и ``30м`` — тридцать минут, ``1:30`` — час тридцать, ``2ч`` — два часа.
    The bare number means minutes here (not seconds): retention lives on a scale
    of tens of minutes, and ``/time 30`` must mean the default, not half a minute.
    """
    raw = str(text or "").strip().lower().replace("ё", "е")
    if not raw:
        raise ValueError("не указано время хранения")
    hours = minutes = 0
    m = re.fullmatch(r"(\d+)\s*(ч|час[а-я]*|h|hour[s]?)", raw)
    if m:
        hours = int(m.group(1))
    else:
        m = re.fullmatch(r"(\d+)\s*(м|мин[а-я]*|m|min[s]?)", raw)
        if m:
            minutes = int(m.group(1))
    if hours or minutes:
        total = hours * 60 + minutes
    else:
        parts = [p for p in re.split(r"[:\-,./\\]", raw) if p != ""]
        if not parts or len(parts) > 3:
            raise ValueError(f"не понял время хранения: {text!r}")
        try:
            nums = [int(p) for p in parts]
        except ValueError as exc:
            raise ValueError(f"не понял время хранения: {text!r}") from exc
        if len(parts) == 1:
            total = nums[0]
        elif len(parts) == 2:
            total = nums[0] * 60 + nums[1]
        else:
            total = nums[0] * 60 + nums[1] + nums[2] / 60
            total = int(round(total))
    if total <= 0:
        raise ValueError("время хранения должно быть больше нуля")
    if total > RETENTION_MAX_MINUTES:
        raise ValueError(f"слишком долго: максимум {RETENTION_MAX_MINUTES // 60} ч")
    return int(total)


def format_retention(minutes: int) -> str:
    """``30 мин``, ``1 ч 30 мин``, ``2 ч``."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч" if rest == 0 else f"{hours} ч {rest:02d} мин"


def format_moment(timestamp: float) -> str:
    """Local wall clock as ``07.11 в 18:42``."""
    return time.strftime("%d.%m в %H:%M", time.localtime(timestamp))


def filename_timecode(seconds: float) -> str:
    """Compact, filesystem-safe timecode: ``HHMMSS`` or ``HHMMSS-mmm``."""
    total = round(float(seconds), 3)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    base = f"{int(hours):02d}{int(minutes):02d}{whole:02d}"
    return f"{base}-{millis:03d}" if millis else base
