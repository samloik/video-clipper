"""Configuration loading.

Defaults live in code, a JSON file (``config.json`` by default) overrides them,
and command-line flags override the file. Nothing secret is stored here: the
cookies file is only referenced by path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "config.json"

DEFAULTS: dict[str, Any] = {
    "output_dir": "./output",
    "cache_dir": "./cache",
    "default_resolution": 720,
    "cookies": None,
    "proxy": None,
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "keep_source": False,
    "use_sections": False,
    "gui_host": "127.0.0.1",
    "gui_port": 8765,
    "log_file": "./video-clipper.log",
}

# Keys accepted in config.json (anything else is ignored, so old configs keep working).
_ALIASES = {
    "default_cookies": "cookies",
    "default_proxy": "proxy",
}


@dataclass(frozen=True)
class Config:
    output_dir: Path
    cache_dir: Path
    default_resolution: int
    cookies: str | None
    proxy: str | None
    ffmpeg_path: str
    ffprobe_path: str
    keep_source: bool
    use_sections: bool
    gui_host: str
    gui_port: int
    log_file: Path

    def as_dict(self) -> dict[str, Any]:
        data = {}
        for f in fields(self):
            value = getattr(self, f.name)
            data[f.name] = str(value) if isinstance(value, Path) else value
        return data

    def with_overrides(self, **kwargs: Any) -> "Config":
        """Return a copy with non-None values applied (CLI flags win over file)."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean) if clean else self


def _from_mapping(data: dict[str, Any]) -> Config:
    merged = dict(DEFAULTS)
    for raw_key, value in data.items():
        key = _ALIASES.get(raw_key, raw_key)
        if key in merged and value is not None:
            merged[key] = value
    return Config(
        output_dir=Path(str(merged["output_dir"])).expanduser(),
        cache_dir=Path(str(merged["cache_dir"])).expanduser(),
        default_resolution=int(merged["default_resolution"]),
        cookies=str(merged["cookies"]) if merged["cookies"] else None,
        proxy=str(merged["proxy"]) if merged["proxy"] else None,
        ffmpeg_path=str(merged["ffmpeg_path"]),
        ffprobe_path=str(merged["ffprobe_path"]),
        keep_source=bool(merged["keep_source"]),
        use_sections=bool(merged["use_sections"]),
        gui_host=str(merged["gui_host"]),
        gui_port=int(merged["gui_port"]),
        log_file=Path(str(merged["log_file"])).expanduser(),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from ``path`` (or ``$VC_CONFIG`` / ``config.json`` if present)."""
    candidate = Path(path or os.environ.get("VC_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    if candidate.is_file():
        with candidate.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"{candidate}: config must be a JSON object")
        return _from_mapping(data)
    return _from_mapping({})
