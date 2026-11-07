"""Timecode parsing and formatting.

Accepted input uses any of ``:`` ``,`` ``.`` ``/`` ``\\`` as the component
separator, all of them equivalent: ``45``, ``0:45``, ``34:43``, ``1.12``,
``1/12`` and ``1\\12``. One part is seconds, two are minutes and seconds, three
are hours, minutes and seconds. An edge part may be omitted — ``:12`` is 0:12,
``45:`` is 0:45 — while a hole in the middle (``1::30``) is a typo and rejected.
A final group of exactly three digits after a dot or a comma is milliseconds:
``1:12.500`` is 72.5 s, ``0.500`` is 0.5 s, but ``0.30`` is 0:30 because two
digits are read as seconds.

Negative values, empty strings and anything that is not a timecode are rejected
with a message that names the offending value.
"""

from __future__ import annotations

import re

# Every character people use instead of ":" in the chat.
_SEPARATOR_RE = re.compile(r"[:\\/,.]")
# A trailing group after a dot or a comma is a fraction of a second when it has
# exactly three digits ("1:12.500") or when reading it as a component would make
# four parts ("00:00:10.5"). "0.30" stays 0:30 — two digits are seconds.
_FRACTION_RE = re.compile(r"([.,])(\d{1,3})$")


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
        digits = fraction_match.group(2)
        if len(digits) == 3 or len(_SEPARATOR_RE.split(raw)) == 4:
            millis = int(digits.ljust(3, "0"))
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
