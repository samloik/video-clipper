"""Smoke tests for the local web GUI: page, API validation, background job."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from video_clipper.config import load_config
from video_clipper.cut import ClipResult
from video_clipper.gui import create_server, default_clipper
from video_clipper.segments import Segment


@pytest.fixture()
def cfg(tmp_path):
    conf = _clean_config().with_overrides(
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        log_file=tmp_path / "log.txt",
        gui_secret_file=tmp_path / "gui.secret",
    )
    conf.config_path = tmp_path / "config.json"
    return conf


def _clean_config(**overrides):
    """Конфиг только из кодовых значений: тесты не должны зависеть от config.json."""
    cfg = load_config(Path("/nonexistent/video-clipper-config.json"))
    return cfg.with_overrides(**overrides) if overrides else cfg

@pytest.fixture()
def server_factory(cfg, tmp_path):
    servers = []

    def start(clipper):
        httpd = create_server(cfg, clipper, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append((httpd, thread))
        host, port = httpd.server_address[:2]
        return f"http://{host}:{port}"

    yield start
    for httpd, thread in servers:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def auth_server(cfg, tmp_path):
    """GUI on a non-loopback host with a password, as it runs in the LAN."""
    servers = []

    def start(password: str = "s3cret"):
        httpd = create_server(cfg, fake_clipper(tmp_path / "clip.mp4"), host="0.0.0.0",
                              port=0, password=password)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append((httpd, thread))
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd, thread in servers:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects by default, which hides the 303 from the test."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _get(url, cookie=None, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method, data=data)
    if cookie:
        req.add_header("Cookie", cookie)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with _opener.open(req, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


class TestAuth:
    def test_lan_bind_without_password_is_refused(self, cfg, tmp_path):
        with pytest.raises(RuntimeError, match="без пароля запрещён"):
            create_server(cfg, fake_clipper(tmp_path / "c.mp4"), host="0.0.0.0", port=0, password="")

    def test_loopback_without_password_stays_open(self, cfg, tmp_path):
        httpd = create_server(cfg, fake_clipper(tmp_path / "c.mp4"), host="127.0.0.1", port=0)
        assert httpd.server_address[0] == "127.0.0.1"
        httpd.server_close()

    def test_page_requires_login(self, auth_server):
        base = auth_server()
        status, _, headers = _get(base + "/")
        assert status == 303
        assert headers["Location"] == "/login"

    def test_login_page_is_served(self, auth_server):
        status, body, _ = _get(auth_server() + "/login")
        assert status == 200
        assert b"password" in body.lower()

    def test_api_is_closed_without_cookie(self, auth_server):
        status, body, _ = _get(auth_server() + "/api/jobs/abc")
        assert status == 401
        assert "вход" in json.loads(body)["error"]

    def test_health_stays_open(self, auth_server):
        status, body, _ = _get(auth_server() + "/api/health")
        assert status == 200 and json.loads(body)["status"] == "ok"

    def test_wrong_password_is_rejected(self, auth_server):
        base = auth_server()
        status, body, _ = _get(base + "/login", method="POST",
                               data=b"password=nope")
        assert status == 401
        assert "неверный пароль".encode() in body

    def test_correct_password_opens_the_gui(self, auth_server):
        base = auth_server()
        status, _, headers = _get(base + "/login", method="POST", data=b"password=s3cret")
        assert status == 303
        cookie = headers["Set-Cookie"].split(";")[0]
        assert cookie.startswith("vc_session=")
        assert "HttpOnly" in headers["Set-Cookie"]

        status, body, _ = _get(base + "/", cookie=cookie)
        assert status == 200 and b"video-clipper" in body.lower()

        status, body, _ = _get(base + "/api/health", cookie=cookie)
        assert status == 200

    def test_cookie_survives_a_restart_with_the_same_secret(self, auth_server):
        base = auth_server()
        _, _, headers = _get(base + "/login", method="POST", data=b"password=s3cret")
        cookie = headers["Set-Cookie"].split(";")[0]
        other = auth_server()
        status, body, _ = _get(other + "/", cookie=cookie)
        assert status == 200, "ключ сессий лежит на диске — «запомнить» переживает рестарт"

    def test_cookie_signed_by_another_secret_is_rejected(self, cfg, tmp_path):
        first = create_server(cfg, fake_clipper(tmp_path / "c.mp4"), host="0.0.0.0",
                              port=0, password="s3cret")
        second_cfg = cfg.with_overrides(gui_secret_file=tmp_path / "other.secret")
        second = create_server(second_cfg, fake_clipper(tmp_path / "c.mp4"), host="0.0.0.0",
                               port=0, password="s3cret")
        servers = []
        for httpd in (first, second):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            servers.append(httpd)
        try:
            base1 = f"http://127.0.0.1:{first.server_address[1]}"
            base2 = f"http://127.0.0.1:{second.server_address[1]}"
            _, _, headers = _get(base1 + "/login", method="POST", data=b"password=s3cret")
            cookie = headers["Set-Cookie"].split(";")[0]
            status, _, _ = _get(base2 + "/", cookie=cookie)
            assert status == 303, "чужая подпись не проходит"
        finally:
            for httpd in servers:
                httpd.shutdown()
                httpd.server_close()

    def test_remember_me_sets_a_month_cookie(self, auth_server):
        base = auth_server()
        status, _, headers = _get(base + "/login", method="POST",
                                  data=b"password=s3cret&remember=1")
        assert status == 303
        cookie = headers["Set-Cookie"]
        assert "Max-Age=2592000" in cookie, "месяц = 30*24*3600"

    def test_login_without_remember_is_a_session_cookie(self, auth_server):
        base = auth_server()
        _, _, headers = _get(base + "/login", method="POST", data=b"password=s3cret")
        assert "Max-Age" not in headers["Set-Cookie"], "без галочки cookie живёт до закрытия"

    def test_brute_force_is_slowed_down(self, auth_server):
        base = auth_server()
        codes = [
            _get(base + "/login", method="POST", data=b"password=bad")[0] for _ in range(7)
        ]
        assert codes[:6] == [401] * 6
        assert codes[-1] == 429

    def test_logout_clears_the_cookie(self, auth_server):
        base = auth_server()
        _, _, headers = _get(base + "/login", method="POST", data=b"password=s3cret")
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, headers = _get(base + "/logout", cookie=cookie)
        assert status == 303
        assert "Max-Age=0" in headers["Set-Cookie"]


def fake_clipper(out_path: Path, fail: bool = False):
    def _clip(source, segments, fast, cfg, progress=None):
        if fail:
            raise RuntimeError("ffmpeg сломался")
        if progress:
            progress("нарезка")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-mp4")
        return [
            ClipResult(
                source=source,
                segment=segments[0],
                output_path=out_path,
                duration=segments[0].duration,
                mode="fast" if fast else "accurate",
                size_bytes=out_path.stat().st_size,
            )
        ]

    return _clip


def get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_json(base: str, path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class TestPageAndHealth:
    def test_page_is_served(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        status, body = get(base, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "Выбери файл или перетащи" in text
        assert "Нарезать" in text

    def test_health(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        status, body = get(base, "/api/health")
        assert status == 200 and json.loads(body)["status"] == "ok"

    def test_binds_localhost_by_default(self, cfg, tmp_path):
        httpd = create_server(cfg, fake_clipper(tmp_path / "x.mp4"), port=0)
        try:
            assert httpd.server_address[0] == "127.0.0.1"
        finally:
            httpd.server_close()


    def test_confirm_dialog_is_styled_not_a_browser_popup(self):
        from video_clipper.gui import PAGE

        assert 'id="confirm"' in PAGE and 'modal-box' in PAGE
        assert 'window.confirm' not in PAGE and 'confirm(' not in PAGE.replace('askConfirm(', '')
        assert 'askConfirm(' in PAGE, "очистка должна спрашивать свой диалог"


class TestDownload:
    def test_attachment_header_survives_a_dash(self):
        from video_clipper.gui import attachment_header

        name = "clip - [00:00:00\u201300:00:20].mp4"
        header = attachment_header(name)
        header.encode("latin-1")  # заголовки уходят только в latin-1
        assert "filename*=UTF-8''" in header
        assert "%E2%80%93" in header, "en dash должен быть процентно закодирован"
        assert "\u2013" not in header

    def test_download_with_unicode_name_returns_bytes(self, server_factory, tmp_path):
        name = "clip - [00:00:00\u201300:00:20].mp4"

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            out = tmp_path / "out" / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"clip-bytes" * 100)
            return [
                ClipResult(source=source, segment=segments[0], output_path=out,
                           duration=2.0, mode="accurate", size_bytes=1000)
            ]

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(
            base, "/api/clip", {"source": str(media), "start": "0:00", "end": "0:02"}
        )
        assert status == 202
        job_id = body["job_id"]
        for _ in range(50):
            job = json.loads(get(base, f"/api/jobs/{job_id}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        status, payload, headers = _get(base + f"/api/download/{job_id}/0")
        assert status == 200, payload
        assert payload == b"clip-bytes" * 100
        assert headers.get("Content-Length") == str(len(payload))


@pytest.fixture()
def ten_seconds(tmp_path) -> Path:
    """Настоящий файл на 10 секунд: «до конца» проверяется на реальной длительности."""
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        pytest.skip("нужен ffmpeg")
    path = tmp_path / "ten.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


class TestOpenEnd:
    def test_api_accepts_star_as_the_end(self, server_factory, tmp_path, ten_seconds):
        base = server_factory(default_clipper)
        status, body = post_json(
            base, "/api/clip",
            {"source": str(ten_seconds), "start": "0:06", "end": "*"},
        )
        assert status == 202
        job_id = body["job_id"]
        for _ in range(120):
            job = json.loads(get(base, f"/api/jobs/{job_id}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.1)
        assert job["state"] == "done", job.get("error")
        result = job["results"][0]
        assert result["duration"] == pytest.approx(4.0, abs=0.4)
        assert "00:10" in result["filename"], "конец должен стать длительностью файла"

    def test_api_accepts_the_word_конец(self, server_factory, tmp_path, ten_seconds):
        base = server_factory(default_clipper)
        status, body = post_json(
            base, "/api/clip",
            {"source": str(ten_seconds), "start": "0:08", "end": "конец"},
        )
        assert status == 202

    def test_bad_end_still_fails(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(
            base, "/api/clip", {"source": str(media), "start": "0:00", "end": "abc"}
        )
        assert status == 400

    def test_open_end_for_a_url_without_metadata_is_reported_in_the_job(
        self, server_factory, cfg, tmp_path, monkeypatch
    ):
        from video_clipper.source import SourceError

        def boom(*args, **kwargs):
            raise SourceError("нет сети")

        monkeypatch.setattr("video_clipper.source.fetch_metadata", boom)
        base = server_factory(default_clipper)
        status, body = post_json(
            base, "/api/clip",
            {"source": "https://youtu.be/ID", "start": "0:05", "end": "*"},
        )
        assert status == 202
        for _ in range(50):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.1)
        assert job["state"] == "error"
        assert "конец ролика" in job["error"]


class TestRetention:
    def test_get_returns_current_value(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/retention")
        assert status == 200
        data = json.loads(body)
        assert data["minutes"] == 30 and data["human"] == "30 мин"

    def test_post_changes_and_persists(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body, _ = _get(base + "/api/retention", method="POST",
                               data=b'{"value": "1:30"}',
                               headers={"Content-Type": "application/json"})
        assert status == 200
        assert json.loads(body)["minutes"] == 90
        assert cfg.retention_minutes == 90
        assert cfg.retention_file().read_text().strip() == "90"

    def test_bad_value_is_rejected(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body, _ = _get(base + "/api/retention", method="POST",
                               data=json.dumps({"value": "полчаса"}).encode(),
                               headers={"Content-Type": "application/json"})
        assert status == 400
        assert cfg.retention_minutes == 30

    def test_info_picks_up_retention_set_by_the_bot(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        cfg.retention_file().write_text("120\n", encoding="utf-8")
        status, body = get(base, "/api/info")
        assert status == 200
        assert "2 ч" in json.loads(body)["text"], "GUI должен видеть /time из бота"
        assert cfg.retention_minutes == 120

    def test_status_shows_when_files_die(self, server_factory, cfg, tmp_path):
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"x" * 1024)
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/status")
        text = json.loads(body)["text"]
        assert "удалится" in text and " в " in text
        assert "Хранение" in text or "хранение" in text


class TestServicePanel:
    def test_hidden_panel_really_hides(self):
        from video_clipper.gui import PAGE

        assert ".panel[hidden]{display:none}" in PAGE, (
            "у .panel задан display:flex — без этого правила атрибут hidden не работает"
        )

    def test_panel_lives_in_the_result_column(self):
        from video_clipper.gui import PAGE

        assert PAGE.index('class="col-out"') < PAGE.index('id="panel"')
        assert 'panel-title' in PAGE, "у панели должен быть заголовок"
        # панель не должна висеть под шапкой на всю ширину
        assert '</header>\n\n<div class="panel"' not in PAGE


class TestServiceEndpoints:
    def test_status_reports_files(self, server_factory, cfg, tmp_path):
        (cfg.cache_dir).mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"x" * 1024)
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/status")
        assert status == 200
        text = json.loads(body)["text"]
        assert "оригиналы" in text and "клипы" in text
        assert "слушаю" in text

    def test_info_is_written_for_the_gui_not_the_chat(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/info")
        assert status == 200
        text = json.loads(body)["text"]
        assert "Нарезать" in text, "подсказка должна говорить про кнопку GUI"
        assert "4 ГБ" in text and "Таймкоды" in text
        assert "/info" not in text and "Пришли" not in text, "это памятка бота, а не GUI"
        assert "Очистить" in text

    def test_job_gets_a_time_estimate_before_any_work(self, server_factory, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            return []

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(
            base, "/api/clip",
            {"source": str(media), "start": "0:00", "end": "0:20", "fast": True},
        )
        assert status == 202
        for _ in range(50):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job.get("estimate"):
                break
            time.sleep(0.05)
        low, high = job["estimate"]
        assert 0 < low < high
        assert high >= 20 * 0.26, "20 с клипа не могут обещать меньше секунды на секунду работы"
        assert high <= 20 * 3, "и не могут обещать часы"

    def test_clean_removes_files(self, server_factory, cfg, tmp_path):
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "a.mp4").write_bytes(b"x" * 2048)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        (cfg.output_dir / "b.mp4").write_bytes(b"x" * 2048)
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body, _ = _get(base + "/api/clean", method="POST")
        assert status == 200
        assert "Удалено" in json.loads(body)["text"]
        assert not (cfg.cache_dir / "a.mp4").exists()
        assert not (cfg.output_dir / "b.mp4").exists()

    def test_job_reports_progress_fraction(self, server_factory, tmp_path):
        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            progress("encode", 0.5, "клип 1/1")
            out = tmp_path / "out" / "p.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mp4")
            return [
                ClipResult(source=source, segment=segments[0], output_path=out,
                           duration=1.0, mode="accurate", size_bytes=3)
            ]

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(
            base, "/api/clip", {"source": str(media), "start": "0:00", "end": "0:02"}
        )
        assert status == 202
        for _ in range(50):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "done"
        assert job["stage"] == "encode" and job["fraction"] == 0.5


class TestClipApi:
    def test_blur_layout_reaches_the_clipper(self, server_factory, tmp_path):
        seen: dict = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen.update(vertical=vertical, layout=layout)
            out = tmp_path / "out" / "blur.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mp4")
            return [
                ClipResult(
                    source=source,
                    segment=segments[0],
                    output_path=out,
                    duration=1.0,
                    mode="accurate+vertical-blur",
                    size_bytes=3,
                )
            ]

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(
            base,
            "/api/clip",
            {"source": str(media), "start": "0:00", "end": "0:05", "vertical": True, "layout": "blur"},
        )
        assert status == 202
        job_id = body["job_id"]
        for _ in range(50):
            job = json.loads(get(base, f"/api/jobs/{job_id}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.1)
        assert job["state"] == "done", job
        assert seen == {"vertical": True, "layout": "blur"}

    def test_unknown_layout_falls_back_to_crop(self, server_factory, tmp_path):
        seen: dict = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen["layout"] = layout
            out = tmp_path / "out" / "c.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mp4")
            return [
                ClipResult(
                    source=source, segment=segments[0], output_path=out,
                    duration=1.0, mode="accurate", size_bytes=3,
                )
            ]

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(
            base, "/api/clip",
            {"source": str(media), "start": "0:00", "end": "0:05", "vertical": True, "layout": "hack"},
        )
        assert status == 202
        for _ in range(50):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.1)
        assert seen["layout"] == "crop"

    def test_rejects_bad_timecode(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {"source": str(media), "start": "abc", "end": "1:00"})
        assert status == 400 and "таймкод" in body["error"].lower()

    def test_rejects_inverted_segment(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {"source": str(media), "start": "10", "end": "5"})
        assert status == 400

    def test_rejects_missing_file(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        status, body = post_json(base, "/api/clip", {"source": str(tmp_path / "nope.mp4"), "start": "1", "end": "2"})
        assert status == 400 and "не найден" in body["error"]

    def test_job_completes_and_serves_file(self, server_factory, tmp_path):
        out = tmp_path / "out" / "clip.mp4"
        base = server_factory(fake_clipper(out))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {"source": str(media), "start": "0:02", "end": "0:05", "fast": True})
        assert status == 202 and body["job_id"]

        job = {}
        for _ in range(50):
            _, raw = get(base, f"/api/jobs/{body['job_id']}")
            job = json.loads(raw)
            if job["state"] != "running":
                break
            time.sleep(0.05)

        assert job["state"] == "done"
        assert job["results"][0]["filename"] == "clip.mp4"
        assert job["results"][0]["mode"] == "fast"

        status, data = get(base, f"/api/download/{body['job_id']}/0")
        assert status == 200 and data == b"fake-mp4"

    def test_clipper_failure_is_reported(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4", fail=True))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {"source": str(media), "start": "1", "end": "2"})
        job = {}
        for _ in range(50):
            _, raw = get(base, f"/api/jobs/{body['job_id']}")
            job = json.loads(raw)
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "error" and "сломался" in job["error"]

    def test_unknown_job(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        status, _ = get(base, "/api/jobs/deadbeef")
        assert status == 404


class TestUpload:
    def test_upload_saves_file_in_cache(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "out" / "clip.mp4"))
        req = urllib.request.Request(
            base + "/api/upload?name=my%20video.mp4", data=b"123456", method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
        saved = Path(payload["path"])
        assert saved.is_file() and saved.read_bytes() == b"123456"
        assert saved.parent == cfg.cache_dir
        assert saved.name.endswith("my video.mp4")


class TestSegmentValidation:
    def test_overlap_not_reachable_single_segment(self):
        # GUI sends one segment per job; this guards the invariant used there.
        with pytest.raises(ValueError):
            from video_clipper.segments import validate_segments

            validate_segments([Segment(5, 4)])
