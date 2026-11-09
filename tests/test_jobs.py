"""Журнал заданий: очередь должна переживать перезапуск сервиса."""

from __future__ import annotations

import json

from video_clipper.jobs import JobStore


def _store(tmp_path, limit=40):
    return JobStore(tmp_path / "jobs.json", limit=limit)


class TestStore:
    def test_roundtrip(self, tmp_path):
        store = _store(tmp_path)
        store.upsert("abc", state="running", label="ролик · 0:10–0:20")
        again = _store(tmp_path)
        again.load()
        record = again.get("abc")
        assert record and record["state"] == "running"
        assert record["label"] == "ролик · 0:10–0:20"

    def test_interrupted_after_restart(self, tmp_path):
        store = _store(tmp_path)
        store.upsert("live", state="running")
        store.upsert("done", state="done")
        fresh = _store(tmp_path)
        fresh.load()
        changed = fresh.mark_interrupted("прервано")
        assert changed == 1
        assert fresh.get("live")["state"] == "interrupted"
        assert fresh.get("live")["error"] == "прервано"
        assert fresh.get("done")["state"] == "done", "готовые задания не трогаем"

    def test_state_survives_a_second_restart(self, tmp_path):
        store = _store(tmp_path)
        store.upsert("live", state="running")
        first = _store(tmp_path)
        first.load()
        first.mark_interrupted("прервано")
        second = _store(tmp_path)
        second.load()
        assert second.get("live")["state"] == "interrupted"
        assert second.mark_interrupted("прервано") == 0, "повторно уже нечего менять"

    def test_broken_file_does_not_kill_the_journal(self, tmp_path):
        path = tmp_path / "jobs.json"
        path.write_text("{не json", encoding="utf-8")
        store = _store(tmp_path)
        store.load()
        assert store.snapshot() == []
        store.upsert("new", state="done")
        assert json.loads(path.read_text(encoding="utf-8"))["jobs"][0]["id"] == "new"

    def test_limit_keeps_newest(self, tmp_path):
        store = JobStore(tmp_path / "jobs.json", limit=3)
        for index in range(5):
            store.upsert(f"job{index}", state="done", created=100 + index)
        store.load()
        ids = [item["id"] for item in store.snapshot(10)]
        assert ids == ["job4", "job3", "job2"]

    def test_snapshot_is_a_copy(self, tmp_path):
        store = _store(tmp_path)
        store.upsert("x", state="done")
        item = store.snapshot()[0]
        item["state"] = "взлом"
        assert store.get("x")["state"] == "done"

    def test_no_path_means_memory_only(self, tmp_path):
        store = JobStore(None)
        store.upsert("x", state="done")
        assert store.get("x")["state"] == "done"
