"""Timecode parsing and formatting.

Accepted input: ``SS``, ``SS.mmm``, ``MM:SS``, ``MM:SS.mmm``, ``HH:MM:SS``,
``HH:MM:SS.mmm``. A comma is accepted as the decimal separator. Negative values,
empty strings and anything that is not a timecode are rejected with a message
that names the offending value.
"""

from __future__ import annotations

import re

_SEPARATORS = (":",)


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
    if "," in raw:
        raw = raw.replace(",", ".")

    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(f"Некорректный таймкод: {value!r} (ожидается ЧЧ:ММ:СС[.ммм])")

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
    return round(seconds, 3)


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
