"""Configuration loading.

Defaults live in code, a JSON file (``config.json`` by default) overrides them,
and command-line flags override the file. Nothing secret is stored here: the
cookies file is only referenced by path.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

log = logging.getLogger("video_clipper")

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
    "vertical_width": 1080,
    "vertical_height": 1920,
    "retention_minutes": 30,
    "telegram_token_file": "~/.hermes/video-clipper-bot.token",
    "telegram_chat_ids": [],
    # split — режем клип на части без пересжатия, compress — вписываем в лимит
    "telegram_policy": "split",
    "gui_host": "127.0.0.1",
    # пароль GUI читается из файла, чтобы не держать его в конфиге
    "gui_password_file": None,
    # ключ для cookie-сессий GUI: переживает перезапуск, иначе «запомнить» не работает
    "gui_secret_file": "~/.hermes/video-clipper-gui.secret",
    # диск: порог отказа и порог, при котором чистим старые файлы сами
    "min_free_mb": 500,
    "auto_clean_free_mb": 2000,
    "history_file": None,
    "gui_port": 8766,
    "log_file": "./video-clipper.log",
}

# Keys accepted in config.json (anything else is ignored, so old configs keep working).
_ALIASES = {
    "default_cookies": "cookies",
    "default_proxy": "proxy",
}


@dataclass
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
    vertical_width: int
    vertical_height: int
    retention_minutes: int
    telegram_token_file: Path
    telegram_chat_ids: tuple[int, ...]
    telegram_policy: str
    gui_host: str
    gui_password_file: Path | None
    gui_secret_file: Path
    gui_port: int
    log_file: Path
    # путь к config.json, чтобы рядом хранить сменные настройки (retention.txt)
    config_path: Path | None = None
    min_free_mb: int = 500
    auto_clean_free_mb: int = 2000
    history_file: Path | None = None

    def history_path(self) -> Path | None:
        """Where finished jobs are recorded (JSONL next to config.json)."""
        if self.history_file is not None:
            return self.history_file
        if self.config_path is None:
            return None
        return self.config_path.parent / "history.jsonl"

    def retention_file(self) -> Path | None:
        """Where a runtime override of retention_minutes lives."""
        if self.config_path is None:
            return None
        return self.config_path.parent / "retention.txt"

    def set_retention(self, minutes: int) -> None:
        """Change retention for this process and remember it for the next start."""
        minutes = int(minutes)
        if minutes <= 0:
            raise ValueError("время хранения должно быть больше нуля")
        self.retention_minutes = minutes
        path = self.retention_file()
        if path is not None:
            try:
                path.write_text(f"{minutes}\n", encoding="utf-8")
            except OSError:
                log.warning("не смог записать %s — хранение %d мин только до перезапуска", path, minutes)

    def as_dict(self) -> dict[str, Any]:
        data = {}
        for f in fields(self):
            value = getattr(self, f.name)
            data[f.name] = str(value) if isinstance(value, Path) else value
        return data

    def with_overrides(self, **kwargs: Any) -> "Config":
        """Return a copy with non-None values applied (CLI flags win over file)."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        for key in ("output_dir", "cache_dir"):
            if key in clean and not isinstance(clean[key], Path):
                clean[key] = Path(str(clean[key])).expanduser()
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
        vertical_width=int(merged["vertical_width"]),
        vertical_height=int(merged["vertical_height"]),
        retention_minutes=int(merged["retention_minutes"]),
        telegram_token_file=Path(str(merged["telegram_token_file"])).expanduser(),
        telegram_chat_ids=tuple(int(x) for x in merged["telegram_chat_ids"] or ()),
        telegram_policy=str(merged["telegram_policy"] or "split"),
        gui_host=str(merged["gui_host"]),
        gui_password_file=(
            Path(str(merged["gui_password_file"])).expanduser()
            if merged["gui_password_file"]
            else None
        ),
        gui_secret_file=Path(str(merged["gui_secret_file"])).expanduser(),
        min_free_mb=int(merged["min_free_mb"]),
        auto_clean_free_mb=int(merged["auto_clean_free_mb"]),
        history_file=(
            Path(str(merged["history_file"])).expanduser() if merged["history_file"] else None
        ),
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
        cfg = _from_mapping(data)
        cfg.config_path = candidate
        _apply_retention_override(cfg)
        return cfg
    cfg = _from_mapping({})
    return cfg


def apply_retention_override(cfg: Config) -> None:
    """Re-read retention.txt so /time in the bot is visible in the GUI at once.

    Cheap: one stat and a tiny read per request, and it keeps the two front-ends
    from showing different values.
    """
    path = cfg.retention_file()
    if path is None or not path.is_file():
        return
    try:
        minutes = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if minutes > 0 and minutes != cfg.retention_minutes:
        cfg.retention_minutes = minutes


# старое имя — для совместимости с кодом, который уже импортирует приватную версию
_apply_retention_override = apply_retention_override
