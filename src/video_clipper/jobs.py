"""Журнал заданий на диске: очередь переживает перезапуск сервиса.

В памяти (``gui.JOBS``) живёт всё, что нужно для работы прямо сейчас: прогресс,
поток управления, объекты ffmpeg. Здесь — только то, что имеет смысл показать
после падения или обновления: что просили, чем кончилось, какие файлы готовы.

Запись атомарная: сначала временный файл, потом ``replace``. Иначе оборванный
процесс оставит половину JSON, и история заданий потеряется целиком.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Больше сорока записей в файл не пишем: это журнал последних заданий, а не база.
RECORD_LIMIT = 40
# Состояния, которые после старта сервиса не могут быть настоящими.
LIVE_STATES = ("running", "queued")


class JobStore:
    """Плоский журнал заданий в одном JSON-файле."""

    def __init__(self, path: Path | None, limit: int = RECORD_LIMIT) -> None:
        self.path = Path(path).expanduser() if path else None
        self.limit = limit
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # --- чтение -----------------------------------------------------------
    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Журнал заданий не прочитался (%s) — начну с пустого", exc)
            return
        items = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return
        with self._lock:
            self._records = {
                str(item["id"]): dict(item)
                for item in items
                if isinstance(item, dict) and item.get("id")
            }

    def mark_interrupted(self, reason: str) -> int:
        """Задания, которые «выполнялись» при прошлом старте, уже не выполняются."""
        changed = 0
        with self._lock:
            for record in self._records.values():
                if record.get("state") in LIVE_STATES:
                    record["state"] = "interrupted"
                    record["error"] = reason
                    record["updated"] = time.time()
                    changed += 1
        if changed:
            self._write()
            log.info("Прерванных заданий в журнале: %d", changed)
        return changed

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id)
            return dict(record) if record else None

    def snapshot(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(
                self._records.values(), key=lambda item: item.get("created") or 0, reverse=True
            )
        return [dict(item) for item in items[:limit]]

    # --- запись -----------------------------------------------------------
    def upsert(self, job_id: str, **fields: Any) -> None:
        if not job_id:
            return
        with self._lock:
            record = self._records.setdefault(job_id, {"id": job_id, "created": time.time()})
            record.update(fields)
            record["updated"] = time.time()
            if len(self._records) > self.limit:
                keep = sorted(
                    self._records.values(),
                    key=lambda item: item.get("created") or 0,
                    reverse=True,
                )[: self.limit]
                self._records = {str(item["id"]): item for item in keep}
        self._write()

    def _write(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = {"jobs": list(self._records.values())}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # pragma: no cover - диск мог кончиться
            log.warning("Не смог записать журнал заданий: %s", exc)
