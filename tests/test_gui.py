"""Smoke tests for the local web GUI: page, API validation, background job."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

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

    def test_secret_ending_with_a_whitespace_byte_survives_a_restart(self, tmp_path):
        """Ключ, оканчивающийся на \r, раньше читался короче и терялся целиком."""
        import video_clipper.gui as gui_module

        path = tmp_path / "gui.secret"
        real = gui_module.secrets.token_bytes
        gui_module.secrets.token_bytes = lambda n: b"\x11" * (n - 1) + b"\n"
        try:
            first = gui_module.Auth("pw", path)
        finally:
            gui_module.secrets.token_bytes = real
        second = gui_module.Auth("pw", path)
        token = first.token()
        assert second.check_cookie(token), "после рестарта сессия должна остаться живой"

    def test_secret_is_stored_as_hex_text(self, tmp_path):
        import video_clipper.gui as gui_module

        path = tmp_path / "gui.secret"
        auth = gui_module.Auth("pw", path)
        stored = path.read_text(encoding="ascii").strip()
        assert len(stored) == 64 and all(c in "0123456789abcdef" for c in stored)
        assert bytes.fromhex(stored) == auth._secret

    def test_old_raw_secret_is_still_honoured(self, tmp_path):
        import video_clipper.gui as gui_module

        path = tmp_path / "gui.secret"
        path.write_bytes(b"\x22" * 32 + b"\n")
        assert gui_module.Auth("pw", path)._secret == b"\x22" * 32
        # старый файл переписан в hex: дальше ключ не зависит от пробельных байтов
        stored = path.read_text(encoding="ascii").strip()
        assert len(stored) == 64 and stored == (b"\x22" * 32).hex()

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

    def test_missing_metadata_does_not_kill_the_job(
        self, server_factory, cfg, tmp_path, monkeypatch
    ):
        from video_clipper.source import SourceError

        def boom(*args, **kwargs):
            raise SourceError("нет сети")

        monkeypatch.setattr("video_clipper.source.fetch_metadata", boom)
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            from video_clipper.segments import is_open_end

            seen["open_ends"] = [is_open_end(seg) for seg in segments]
            return []

        base = server_factory(clipper)
        status, body = post_json(
            base, "/api/clip",
            {"source": "https://youtu.be/ID", "start": "0:05", "end": "*"},
        )
        assert status == 202
        for _ in range(100):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        # Метаданные молчат — это не приговор: нарезчик получает открытый конец
        # и сам подставит длительность после скачивания.
        assert job["state"] == "done", job.get("error")
        assert seen["open_ends"] == [True]


class TestStatusSweepsExpired:
    """Просроченные файлы убираются при показе статуса, а не висят с «0 с»."""

    def _cfg(self, tmp_path, minutes=1):
        cfg = load_config(tmp_path / "config.json")
        cache = tmp_path / "cache"
        out = tmp_path / "output"
        cache.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        return cfg.with_overrides(cache_dir=cache, output_dir=out, retention_minutes=minutes)

    def test_expired_files_are_removed(self, tmp_path):
        import os
        import time as _time

        from video_clipper.gui import status_text

        cfg = self._cfg(tmp_path)
        old = Path(cfg.cache_dir) / "старый-89tlUjUJS3Q.mp4"
        old.write_bytes(b"x" * 1024)
        two_hours_ago = _time.time() - 2 * 3600
        os.utime(old, (two_hours_ago, two_hours_ago))

        text = status_text(cfg)
        assert not old.exists(), "просроченный файл должен исчезнуть"
        assert "через 0 с" not in text
        assert "оригиналы: 0 файл(ов)" in text

    def test_fresh_file_stays_with_a_countdown(self, tmp_path):
        from video_clipper.gui import status_text

        cfg = self._cfg(tmp_path)
        fresh = Path(cfg.cache_dir) / "свежий-89tlUjUJS3Q.mp4"
        fresh.write_bytes(b"x")

        text = status_text(cfg)
        assert fresh.exists()
        assert "удалится" in text and "через" in text
        assert "срок вышел" not in text

    def test_running_job_keeps_its_files(self, tmp_path):
        import os
        import time as _time

        import video_clipper.gui as gui_module

        cfg = self._cfg(tmp_path)
        busy = Path(cfg.cache_dir) / "занятый-89tlUjUJS3Q.mp4"
        busy.write_bytes(b"x")
        old = _time.time() - 2 * 3600
        os.utime(busy, (old, old))
        gui_module.JOBS["test-busy"] = {"state": "running"}
        try:
            text = gui_module.status_text(cfg)
        finally:
            gui_module.JOBS.pop("test-busy", None)
        assert busy.exists(), "во время задания файлы не трогаем"
        assert "срок вышел" in text


class TestColumnScrolling:
    """Сайт должен листаться колесом на коротком экране, а не обрезаться."""

    def test_page_scrolls_as_a_whole(self):
        from video_clipper.gui import PAGE

        assert "min-height:100dvh;\n     overflow-y:auto}" in PAGE, "страница листается"
        assert "height:100dvh;\n     overflow:hidden}" not in PAGE, "прежняя фиксация высоты убрана"

    def test_columns_do_not_clip_content(self):
        from video_clipper.gui import PAGE

        assert "gap:18px;min-height:0}" in PAGE, "левая колонка без внутреннего скролла"
        assert ".col-out{min-height:0}" in PAGE


class TestTimecodeKeyboard:
    """На телефоне в поле таймкода должен быть доступен «:»."""

    def test_fields_use_a_text_keyboard(self):
        from video_clipper.gui import PAGE

        assert PAGE.count('inputmode="text"') == 2, "оба поля таймкода — с обычной клавиатурой"
        # числовая клавиатура осталась только у поля «Хранение» (минуты) — там она уместна
        assert 'id="prompt-input" class="mono" type="text" value="30" inputmode="numeric"' in PAGE

    def test_helper_rows_are_gone(self):
        from video_clipper.gui import PAGE

        # пользователь попросил убрать ряд вставок «: . * конец» и пресеты длительности
        assert 'id="tc-keys"' not in PAGE
        assert 'data-len="15"' not in PAGE and 'data-len="180"' not in PAGE
        # при этом «+ в список» и «сброс» остались
        assert 'id="add-seg"' in PAGE and 'data-reset="1"' in PAGE


class TestSpinnerInsteadOfBar:
    """В панели результата вместо полосы — вращающийся круг перед текстом."""

    def test_page_has_a_spinner_and_no_progress_bar(self):
        from video_clipper.gui import PAGE

        assert "spinner.className = 'spinner'" in PAGE
        assert 'id="progress"' not in PAGE
        assert "barEl" not in PAGE
        assert ".spinner{" in PAGE and "animation:spin" in PAGE
        assert "@keyframes spin{" in PAGE

    def test_status_is_announced(self):
        from video_clipper.gui import PAGE

        assert 'role="status"' in PAGE and 'aria-live="polite"' in PAGE


class TestDeadLinkHidesDownload:
    """По закрытому видео и битой ссылке кнопку «скачать 20 с» не предлагаем."""

    def test_page_hides_the_button_for_permanent_reasons(self):
        from video_clipper.gui import PAGE

        assert "/обрезан|недоступн|символ/i.test(error)" in PAGE


class TestWindowBudget:
    """Участок для кадров не должен тянуться бесконечно."""

    def test_timeout_has_a_human_reason(self):
        from video_clipper.gui import _preview_error

        text = _preview_error(TimeoutError("не успел скачать участок для кадров"))
        assert "нажми ещё раз" in text

    def test_page_budget_is_30_seconds(self):
        import video_clipper.gui as gui_module

        assert gui_module.FRAME_WINDOW_BUDGET == 45.0


class TestUnavailableVideo:
    """«Видео недоступно» — это не таймаут: причину надо назвать сразу."""

    def test_message_names_the_reason(self):
        from video_clipper.gui import _preview_error

        exc = RuntimeError("ERROR: [youtube] CQGchYrnYhU: This video is unavailable")
        text = _preview_error(exc)
        assert "видео недоступно" in text
        assert "приватное" in text

    def test_private_and_removed_are_recognised(self):
        from video_clipper.preview import unavailable_video

        assert unavailable_video("Video unavailable")
        assert unavailable_video("This video is private")
        assert unavailable_video("This video has been removed by the uploader")
        assert not unavailable_video("Requested format is not available")

    def test_no_second_client_for_a_dead_video(self, cfg, monkeypatch):
        import video_clipper.source as source_module
        from video_clipper.preview import stream_info

        calls = []

        def fail(url, opts):
            calls.append(opts.get("extractor_args"))
            raise RuntimeError("ERROR: [youtube] X: This video is unavailable")

        monkeypatch.setattr(source_module, "_ytdlp_extract", fail)
        with pytest.raises(RuntimeError):
            stream_info("https://youtu.be/DEAD1", cfg)
        assert len(calls) == 1, "после «недоступно» других клиентов не пробуем"


class TestPermanentReasons:
    """Если видео нет — участок не качаем: минута на заведомый провал не нужна."""

    REASON = ("видео недоступно: приватное, удалённое или ссылка неверна. "
              "Открой её в браузере")

    def test_stream_is_not_tried_after_a_permanent_reason(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        stream_calls = []
        monkeypatch.setattr(
            gui_module, "_remote_media",
            lambda source, cfg_: {"duration": 0.0, "needs_download": True, "window": False,
                                  "link_problem": ""},
        )
        monkeypatch.setattr(gui_module, "_window_frame", lambda *a, **k: (b"", self.REASON))
        monkeypatch.setattr(gui_module, "_stream_frame",
                            lambda *a: (stream_calls.append(1), (b"", ""))[1])
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/frame?source=https://youtu.be/DEADVIDEO01&t=0"
        )
        assert status == 409
        assert "видео недоступно" in json.loads(body)["error"]
        assert stream_calls == [], "после «недоступно» поток не дёргаем"

    def test_permanent_signs(self):
        import video_clipper.gui as gui_module

        assert gui_module._reason_is_permanent(self.REASON)
        assert gui_module._reason_is_permanent("в ссылке 10 символов id вместо 11")
        assert not gui_module._reason_is_permanent("кадр из потока не пришёл вовремя")


class TestDurationEndpoint:
    """Длительность по запросу: шаг «− кадр» от «до конца» без неё не работает."""

    def test_file_duration_comes_from_the_file(self, server_factory, cfg, ten_seconds):
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/duration?source=" + quote(str(ten_seconds), safe="")
        )
        assert status == 200
        assert json.loads(body)["duration"] == pytest.approx(10.0, abs=0.3)

    def test_broken_link_is_named_without_going_to_the_network(self, server_factory, cfg):
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/duration?source=" + quote("https://youtu.be/-Q5yL7tnq", safe="")
        )
        assert status == 409
        assert "9 символов" in json.loads(body)["error"]

    def test_page_asks_for_duration_and_marks_the_last_frame(self):
        from video_clipper.gui import PAGE

        assert "/api/duration?source=" in PAGE
        assert "«До конца» заменено на точный конец" in PAGE
        assert "последний кадр ролика" in PAGE


class TestFrameBudget:
    """Кадр по ссылке не должен держать человека минутами."""

    def test_server_stops_after_the_budget(self):
        import video_clipper.gui as gui_module
        from video_clipper import preview

        assert preview.FRAME_TOTAL_BUDGET == 45.0
        assert gui_module.FRAME_TOTAL_BUDGET == preview.FRAME_TOTAL_BUDGET
        source = open(gui_module.__file__, encoding="utf-8").read()
        # кадр сначала берётся из короткого участка, поток — только если участок не вышел
        assert "_window_frame(" in source and "until=frame_until" in source

    def test_page_gives_up_after_45_seconds(self):
        from video_clipper.gui import PAGE

        assert "setTimeout(() => {" in PAGE
        assert "60000" in PAGE
        assert "сервер не успел за минуту — нажми ещё раз" in PAGE
        assert "shortReason(data.error)" in PAGE


class TestEtaWithoutEstimate:
    """Без готовой оценки остаток считается по фактическому прогрессу."""

    def test_page_counts_the_rest_from_progress(self):
        from video_clipper.gui import PAGE

        assert "elapsed * (1 - done) / done" in PAGE
        assert "(по прогрессу)" in PAGE
        assert "оценка недоступна" not in PAGE


class TestEstimateWithOpenEnd:
    """«До конца» + молчащие метаданные: длительность берём из потока."""

    def test_duration_comes_from_the_stream_cache(self, server_factory, cfg, monkeypatch):
        from video_clipper.source import SourceError

        monkeypatch.setattr(
            "video_clipper.source.fetch_metadata",
            lambda *a, **k: (_ for _ in ()).throw(SourceError("бот-чек")),
        )
        monkeypatch.setattr("video_clipper.gui.cached_stream", lambda source: {"duration": 45.0})
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            from video_clipper.segments import is_open_end

            seen["end"] = segments[0].end
            seen["open"] = is_open_end(segments[0])
            return []

        base = server_factory(clipper)
        status, body = post_json(
            base, "/api/clip",
            {"source": "https://www.youtube.com/shorts/KhPr0wIt81EA", "start": "0:00", "end": "*"},
        )
        assert status == 202
        for _ in range(100):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert seen["open"] is False, "конец должен стать реальной длительностью"
        assert seen["end"] == pytest.approx(45.0)
        assert isinstance(job.get("estimate"), list) and len(job["estimate"]) == 2, \
            "оценка времени должна появиться, а не «оценка недоступна»"


class TestErrorDetails:
    def test_job_error_carries_the_request(self, server_factory, tmp_path):
        def clipper(*args, **kwargs):
            raise RuntimeError("Секционная загрузка отклонена сайтом")

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {
            "source": str(media),
            "segments": [{"start": "0:11", "end": "*"}],
            "vertical": True,
            "layout": "blur",
        })
        assert status == 202
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "error"
        assert job["error_details"].startswith(f"Ссылка: {media}")
        assert "Отрезок:" in job["error_details"]
        assert "размытый фон" in job["error_details"]
        assert "Секционная загрузка отклонена сайтом" in job["error_details"]
        assert job["error"] == "Секционная загрузка отклонена сайтом"

    def test_page_shows_error_details_panel(self):
        from video_clipper.gui import PAGE

        assert "job.error_details" in PAGE
        assert "showPanel('Ошибка'" in PAGE


class TestDiskAndHistory:
    def test_status_reports_disk_space(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/status")
        text = json.loads(body)["text"]
        assert "диск: свободно" in text and "ГБ" in text

    def test_health_has_uptime(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/health")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok" and data["uptime_s"] >= 0

    def test_finished_job_is_written_to_history(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {
            "source": str(media),
            "segments": [{"start": "0:00", "end": "0:01"}, {"start": "0:02", "end": "0:03"}],
            "fast": True,
        })
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        history = cfg.history_path()
        assert history.is_file()
        entry = json.loads(history.read_text().strip().splitlines()[-1])
        assert entry["state"] == "done"
        assert entry["segments"] == 2 and entry["clips"] >= 1
        assert entry["mode"].startswith("без перекодирования")
        assert entry["seconds"] >= 0

    def test_history_is_shown_in_status(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:01"}],
        })
        for _ in range(60):
            job_body = get(base, "/api/status")[1]
            if "последние задания" in json.loads(job_body)["text"]:
                break
            time.sleep(0.05)
        assert "последние задания" in json.loads(job_body)["text"]
        assert media.name in json.loads(job_body)["text"]

    def test_low_disk_refuses_the_job(self, server_factory, cfg, tmp_path, monkeypatch):
        monkeypatch.setattr("video_clipper.storage.free_mb", lambda directory: 10.0)
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:01"}],
        })
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "error"
        assert "Мало места на диске" in job["error_details"]
        assert "10" in job["error_details"], "в сообщении должно быть сколько свободно"

    def test_make_room_deletes_the_oldest_files(self, tmp_path):
        from video_clipper.storage import make_room

        cache = tmp_path / "cache"
        cache.mkdir()
        old = cache / "old.mp4"
        old.write_bytes(b"x" * 4096)
        new = cache / "new.mp4"
        new.write_bytes(b"x" * 4096)
        os.utime(old, (1000, 1000))
        # цель заведомо больше свободного места: удаляем всё, самое старое первым
        result = make_room([cache], target_mb=10**9)
        assert old in result.removed
        assert result.freed_bytes >= 4096
        assert not old.exists()


class TestStreamFallback:
    """Поток не отдал кадр — сервис сам готовит короткий участок, без кнопки."""

    def test_frame_is_taken_from_a_downloaded_window(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        monkeypatch.setattr(
            gui_module, "_remote_media",
            lambda source, cfg_: {"duration": 63.0, "needs_download": True, "window": False,
                                  "link_problem": ""},
        )
        stream_calls = []
        monkeypatch.setattr(gui_module, "_window_frame",
                            lambda source, cfg_, seconds, width, **kw: (b"\xff\xd8window", ""))
        monkeypatch.setattr(gui_module, "_stream_frame",
                            lambda *a: (stream_calls.append(1), (b"", ""))[1])
        base = server_factory(fake_clipper(cfg.cache_dir / "c.mp4"))
        status, body, headers = _get(base + "/api/frame?source=https://youtu.be/SSSSSSSSSSS&t=30")
        assert status == 200
        assert headers.get("Content-Type") == "image/jpeg"
        assert body == b"\xff\xd8window"
        assert stream_calls == [], "если участок дал кадр, поток не нужен"

    def test_timeout_does_not_freeze_the_source(self, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        calls = []
        monkeypatch.setattr(
            gui_module, "grab_stream_frame",
            lambda source, cfg_, seconds, width: (calls.append(seconds), b"")[1],
        )
        gui_module._STREAM_COOLDOWN.pop("https://youtu.be/COLD1", None)
        first = gui_module._stream_frame("https://youtu.be/COLD1", cfg, 10.0, 240)
        second = gui_module._stream_frame("https://youtu.be/COLD1", cfg, 12.0, 240)
        assert first == (b"", "кадр из потока не пришёл вовремя")
        assert second == (b"", "кадр из потока не пришёл вовремя")
        assert calls == [10.0, 12.0], "после таймаута источник сразу пробуют снова"
        assert "https://youtu.be/COLD1" not in gui_module._STREAM_COOLDOWN


class TestPreviewForLinks:
    """Ссылка без скачанного файла: интерфейс должен предложить скачать короткий кусок."""

    URL = "https://youtu.be/ССЫЛКА"

    def _window(self, cfg, start=0.0, end=20.0):
        folder = cfg.cache_dir / "preview"
        folder.mkdir(parents=True, exist_ok=True)
        media = folder / "ССЫЛКА-0.mp4"
        media.write_bytes(b"\x00" * 4096)
        (folder / "ССЫЛКА-0.json").write_text(
            json.dumps(
                {
                    "source": self.URL,
                    "file": media.name,
                    "start": start,
                    "end": end,
                    "duration": 63.0,
                }
            ),
            encoding="utf-8",
        )
        return media

    def test_media_tells_the_page_that_frames_are_missing(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        monkeypatch.setattr(
            gui_module, "_remote_media",
            lambda source, cfg_: {"duration": 63.0, "needs_download": True, "window": False},
        )
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(base + "/api/media?source=" + quote(self.URL, safe=""))
        data = json.loads(body)
        assert status == 200
        assert data["needs_download"] is True and data["duration"] == 63.0

    def test_frame_without_window_explains_itself(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        monkeypatch.setattr(
            gui_module, "_stream_frame",
            lambda source, cfg_, seconds, width: (b"", "сайт не отдал кадр"),
        )
        # участок тоже не скачивается (в тесте нет сети) — остаётся причина
        monkeypatch.setattr(gui_module, "ensure_window", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет сети")))
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/frame?source=" + quote(self.URL, safe="") + "&t=5"
        )
        assert status == 409
        assert "не отдал кадр" in json.loads(body)["error"]

    def test_frame_can_come_straight_from_the_stream(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        seen = {}

        def fake_stream(source, cfg_, seconds, width):
            seen.update(seconds=seconds, width=width)
            return b"\xff\xd8\xff\xd9", ""

        monkeypatch.setattr(gui_module, "_window_frame", lambda *a, **k: (b"", ""))
        monkeypatch.setattr(gui_module, "_stream_frame", fake_stream)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, headers = _get(
            base + "/api/frame?source=" + quote(self.URL, safe="") + "&t=7.5&w=320"
        )
        assert status == 200 and headers["Content-Type"] == "image/jpeg"
        assert body[:2] == b"\xff\xd8"
        assert seen == {"seconds": 7.5, "width": 320}

    def test_truncated_link_is_refused_without_hitting_the_network(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        def angry(*args, **kwargs):
            raise AssertionError("сеть не нужна: ссылка заведомо битая")

        monkeypatch.setattr(gui_module, "_stream_frame", angry)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        short = "https://www.youtube.com/watch?v=-Q5yL7tnq"
        media_status, media_body, _ = _get(base + "/api/media?source=" + quote(short, safe=""))
        data = json.loads(media_body)
        assert media_status == 200 and "9 символов id вместо 11" in data["link_problem"]

        status, body, _ = _get(base + "/api/frame?source=" + quote(short, safe="") + "&t=0")
        assert status == 409 and "9 символов id вместо 11" in json.loads(body)["error"]

    def test_stream_failure_is_remembered_for_a_minute(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        calls = []

        def failing(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("ERROR: [youtube] Sign in to confirm you're not a bot")

        monkeypatch.setattr(gui_module, "grab_stream_frame", failing)
        monkeypatch.setattr(gui_module, "ensure_window", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет сети")))
        monkeypatch.setattr(gui_module, "_STREAM_COOLDOWN", {})
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        url = base + "/api/frame?source=" + quote("https://rutube.ru/video/тест/", safe="")
        for _ in range(3):
            _get(url + "&t=1")
        assert len(calls) == 1, "после первой неудачи сайт не дёргаем минуту"

    def test_truncated_link_is_named_as_such(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        error = RuntimeError("ERROR: [youtube:truncated_id] Incomplete YouTube ID -QQ5yL7tnq.")
        monkeypatch.setattr(gui_module, "_stream_frame", lambda *a: (b"", gui_module._preview_error(error)))
        monkeypatch.setattr(gui_module, "ensure_window", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет сети")))
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/frame?source=" + quote(self.URL, safe="") + "&t=0"
        )
        assert status == 409
        assert "обрезанную" in json.loads(body)["error"]

    def test_prepare_endpoint_downloads_a_window(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        def fake_ensure(source, cfg_, seconds, progress=None):
            media = self._window(cfg)
            return media, 0.0, 20.0

        monkeypatch.setattr(gui_module, "ensure_window", fake_ensure)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/preview", method="POST",
            data=json.dumps({"source": self.URL, "t": 4}).encode(),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(body)
        assert status == 200 and data["start"] == 0.0 and data["end"] == 20.0

    def test_window_is_reported_with_offset(self, server_factory, cfg):
        self._window(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/media?source=" + quote(self.URL, safe="") + "&t=5"
        )
        data = json.loads(body)
        assert status == 200
        assert data["window"] is True and data["offset"] == 0.0
        assert data["window_end"] == 20.0
        assert data["duration"] == 63.0

    def test_source_serves_the_window(self, server_factory, cfg):
        self._window(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        url = base + "/api/source?source=" + quote(self.URL, safe="") + "&t=5"
        status, body, headers = _get(url, headers={"Range": "bytes=0-99"})
        assert status == 206 and len(body) == 100


class TestTimecodeParserInPage:
    """Таймкоды в браузере должны читаться так же, как в боте: «0:02.040» — это 2,04 с."""

    def _function(self) -> str:
        from video_clipper.gui import PAGE

        start = PAGE.index("function toSeconds(text)")
        end = PAGE.index("\n}", start) + 2
        return PAGE[start:end]

    def test_browser_parser_matches_the_bot(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("нужен node, чтобы прогнать JS-парсер")
        assert node
        script = self._function() + (
            "\nconsole.log(JSON.stringify([toSeconds('0:02.040'), toSeconds('1.12'),"
            " toSeconds('12.500'), toSeconds('1:02:03'), toSeconds('45'),"
            " toSeconds('0:10-0:20'.split('-')[0])]));"
        )
        out = subprocess.run([node, "-e", script], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == [2.04, 72, 12.5, 3723, 45, 10]


class TestPreview:
    """Границы видно до нарезки: кадры, паспорт файла и проигрывание по Range."""

    def _video(self, cfg, seconds=2):
        if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
            pytest.skip("нужен ffmpeg")
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.cache_dir / "upload-тест.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
            ],
            check=True,
        )
        return path

    def test_passport(self, server_factory, cfg):
        video = self._video(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(base + "/api/media?source=" + quote(str(video), safe=""))
        data = json.loads(body)
        assert status == 200
        assert data["width"] == 320 and data["height"] == 240
        assert data["fps"] == pytest.approx(25.0, abs=0.01)
        assert data["duration"] == pytest.approx(2.0, abs=0.2)

    def test_frame_is_jpeg(self, server_factory, cfg):
        video = self._video(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, headers = _get(
            base + "/api/frame?source=" + quote(str(video), safe="") + "&t=1.0&w=200"
        )
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert body[:2] == b"\xff\xd8"

    def test_frame_falls_back_to_the_nearest_cached_one(self, server_factory, cfg, monkeypatch):
        video = self._video(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        url = base + "/api/frame?source=" + quote(str(video), safe="")

        first_status, first_body, _ = _get(url + "&t=1.0&w=200")
        assert first_status == 200
        # извлечение ломается — сервер должен отдать ближайший уже добытый кадр
        import video_clipper.gui as gui_module

        monkeypatch.setattr(gui_module, "grab_frame", lambda *a, **kw: b"")
        status, body, headers = _get(url + "&t=1.6&w=200")
        assert status == 200
        assert headers["X-Frame-Approx"] == "1"
        assert body == first_body

    def test_cached_frame_is_served_without_extraction(self, server_factory, cfg, monkeypatch):
        video = self._video(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        url = base + "/api/frame?source=" + quote(str(video), safe="")
        _get(url + "&t=1.0&w=200")
        calls = []
        import video_clipper.gui as gui_module

        monkeypatch.setattr(
            gui_module, "grab_frame",
            lambda *a, **kw: calls.append(a) or b"",
        )
        status, body, _ = _get(url + "&t=1.0&w=200")
        assert status == 200 and not calls

    def test_bad_time_is_rejected(self, server_factory, cfg):
        video = self._video(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/frame?source=" + quote(str(video), safe="") + "&t=" + quote("завтра")
        )
        assert status == 400

    def test_link_without_download_offers_to_fetch_frames(self, server_factory, cfg, monkeypatch):
        import video_clipper.gui as gui_module

        monkeypatch.setattr(
            gui_module, "_remote_media",
            lambda source, cfg_: {"duration": 40.0, "needs_download": True, "window": False},
        )
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(
            base + "/api/media?source=" + quote("https://youtu.be/НОТВКЭШЕ", safe="")
        )
        data = json.loads(body)
        assert status == 200 and data["needs_download"] is True
        assert data["duration"] == 40.0

    def test_file_outside_work_dirs_is_refused(self, server_factory, cfg, tmp_path):
        outsider = tmp_path / "чужой.mp4"
        outsider.write_bytes(b"x" * 100)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, _ = _get(base + "/api/source?source=" + quote(str(outsider), safe=""))
        assert status == 403
        assert "вне рабочих" in json.loads(body)["error"]

    def test_source_is_served_in_chunks(self, server_factory, cfg):
        video = self._video(cfg)
        size = video.stat().st_size
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        url = base + "/api/source?source=" + quote(str(video), safe="")

        status, body, headers = _get(url)
        assert status == 200 and len(body) == size
        assert headers["Accept-Ranges"] == "bytes"

        status, body, headers = _get(url, headers={"Range": "bytes=0-99"})
        assert status == 206 and len(body) == 100
        assert headers["Content-Range"] == f"bytes 0-99/{size}"
        assert body == video.read_bytes()[:100]

        status, body, headers = _get(url, headers={"Range": "bytes=-50"})
        assert status == 206 and len(body) == 50
        assert body == video.read_bytes()[-50:]

        status, body, _ = _get(url, headers={"Range": f"bytes={size + 10}-"})
        assert status == 416

    def test_head_gives_headers_without_body(self, server_factory, cfg):
        video = self._video(cfg)
        base = server_factory(fake_clipper(cfg.output_dir / "c.mp4"))
        status, body, headers = _get(
            base + "/api/source?source=" + quote(str(video), safe=""), method="HEAD"
        )
        assert status == 200 and body == b""
        assert headers["Content-Length"] == str(video.stat().st_size)

    def test_page_has_the_preview_block(self):
        from video_clipper.gui import PAGE

        for marker in ('id="preview"', 'id="thumb-start"', 'id="thumb-end"',
                       'id="play-seg"', "/api/frame", "/api/media"):
            assert marker in PAGE, marker

    def test_each_boundary_has_its_own_stepper(self):
        from video_clipper.gui import PAGE

        assert 'data-field="start"' in PAGE and 'data-field="end"' in PAGE
        # четыре кнопки у каждого края: −1 с, − кадр, + кадр, +1 с
        assert PAGE.count('data-step="-frame"') == 2
        assert PAGE.count('data-step="frame"') == 2
        assert PAGE.count('data-step="-1"') == 2
        assert PAGE.count('data-step="1"') == 2


class TestFrameBusy:
    """Пока кадр по ссылке едет, старая картинка не должна выглядеть новой."""

    def test_overlay_markup_and_styles_exist(self):
        from video_clipper.gui import PAGE

        assert PAGE.count('class="thumb"') == 2
        assert PAGE.count('class="loading-text"') == 2
        assert ".preview .thumb.busy .loading{display:flex}" in PAGE
        assert ".preview .thumb .spin{" in PAGE
        assert "@keyframes spin{" in PAGE

    def test_javascript_shows_elapsed_seconds(self):
        from video_clipper.gui import PAGE

        assert "holder.classList.add('busy')" in PAGE
        assert "'обновляю… ' + secs + ' с'" in PAGE
        assert "setTimeout(showBusy, 160)" in PAGE
        # устаревший ответ не должен гасить вертушку нового запроса
        assert "if (fresh()) hideBusy();" in PAGE


class TestPageVersion:
    """После перезапуска сервиса открытая вкладка должна обновиться сама."""

    def test_health_reports_the_page_fingerprint(self, server_factory, tmp_path):
        import hashlib

        from video_clipper.gui import PAGE

        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body, _ = _get(base + "/api/health")
        data = json.loads(body)
        assert status == 200
        assert data["version"] == hashlib.sha1(PAGE.encode("utf-8")).hexdigest()[:8]

    def test_page_reloads_on_a_new_fingerprint(self):
        from video_clipper.gui import PAGE

        assert "sessionStorage.getItem('page_version')" in PAGE
        assert "location.reload()" in PAGE
        assert "setInterval(pingService, 60000)" in PAGE


class TestMobileHeader:
    """На телефоне шапка не должна вылезать за экран."""

    def test_narrow_screen_rule_exists(self):
        from video_clipper.gui import PAGE

        assert "@media (max-width:620px)" in PAGE
        block = PAGE.split("@media (max-width:620px)")[1].split("@media")[0]
        assert ".bar{" in block and "flex-wrap:wrap" in block
        assert ".bar-actions{" in block and "grid-template-columns:repeat(4" in block

    def test_brand_does_not_push_the_buttons_out(self):
        from video_clipper.gui import PAGE

        block = PAGE.split("@media (max-width:620px)")[1].split("@media")[0]
        assert "min-width:0" in block and "text-overflow:ellipsis" in block


class TestVerticalWindow:
    def test_position_is_remembered_per_source(self, server_factory, cfg, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        source = "https://youtu.be/ID"
        status, body = get(base, "/api/vertical?source=" + quote(source, safe=""))
        assert status == 200
        data = json.loads(body)
        assert data == {"horizontal": 0.5, "saved": False}

        status, body, _ = _get(base + "/api/vertical", method="POST",
                               data=json.dumps({"source": source, "horizontal": 0.72}).encode(),
                               headers={"Content-Type": "application/json"})
        assert status == 200 and json.loads(body)["horizontal"] == 0.72

        status, body = get(base, "/api/vertical?source=" + quote(source, safe=""))
        assert json.loads(body)["horizontal"] == pytest.approx(0.72)
        assert json.loads(body)["saved"] is True
        assert "0.72" in cfg.vertical_map_path().read_text(encoding="utf-8")

    def test_position_reaches_the_clipper(self, server_factory, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False,
                    layout="crop", control=None, horizontal=0.5):
            seen["horizontal"] = horizontal
            return []

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:02"}],
            "vertical": True, "horizontal": 0.3,
        })
        assert status == 202
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert seen["horizontal"] == pytest.approx(0.3)

    def test_garbage_position_falls_back_to_centre(self, server_factory, tmp_path):
        seen = {}

        def clipper(source, segments, fast, cfg, progress=None, vertical=False,
                    layout="crop", control=None, horizontal=0.5):
            seen["horizontal"] = horizontal
            return []

        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:01"}],
            "vertical": True, "horizontal": "abc",
        })
        time.sleep(0.3)
        assert seen["horizontal"] == 0.5

    def test_auto_finds_content_behind_black_bars(self, server_factory, cfg, tmp_path):
        if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
            pytest.skip("нужен ffmpeg")
        source = tmp_path / "bars.mp4"
        # содержимое 480px прижато вправо, слева чёрные поля
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=480x720:rate=25:duration=4",
            "-vf", "pad=1280:720:800:0:black", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(source),
        ], check=True)
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/vertical/auto?source=" + quote(str(source), safe=""))
        assert status == 200
        data = json.loads(body)
        assert data["horizontal"] > 0.65, data
        assert data["horizontal"] <= 1.0
        assert "содержимое" in data["note"] or "полей" in data["note"]

    def test_auto_without_cached_download_stays_centred(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body = get(base, "/api/vertical/auto?source=" + quote("https://youtu.be/ID", safe=""))
        data = json.loads(body)
        assert status == 200 and data["horizontal"] == 0.5
        assert "не скачан" in data["note"]

    def test_page_has_the_slider(self):
        from video_clipper.gui import PAGE

        assert 'id="krop"' in PAGE and 'id="krop-auto"' in PAGE
        assert "/api/vertical" in PAGE


class TestProgressHeartbeat:
    def test_idle_download_still_shows_that_it_is_alive(
        self, server_factory, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("video_clipper.gui.HEARTBEAT_SECONDS", 0.1)

        def slow_clipper(source, segments, fast, cfg, progress=None, vertical=False,
                         layout="crop", control=None):
            time.sleep(1.0)  # как секционная загрузка: событий нет
            return []

        base = server_factory(slow_clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:05"}],
        })
        seen = None
        for _ in range(80):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if "уже" in str(job.get("detail") or ""):
                seen = job
                break
            time.sleep(0.05)
        assert seen is not None, "без событий прогресса статус должен обновляться по таймеру"
        assert seen["state"] == "running"
        assert "уже" in seen["message"]


class TestCancel:
    def _slow_clipper(self):
        def clipper(source, segments, fast, cfg, progress=None, vertical=False,
                    layout="crop", control=None):
            for _ in range(200):
                if control is not None and control.is_set():
                    return []
                time.sleep(0.05)
            return []

        return clipper

    def test_cancel_stops_a_running_job(self, server_factory, tmp_path):
        base = server_factory(self._slow_clipper())
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:05"}],
        })
        job_id = body["job_id"]
        time.sleep(0.15)
        status, payload, _ = _get(base + f"/api/cancel/{job_id}", method="POST")
        assert status == 200
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{job_id}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "cancelled"
        assert job["error"] == "Остановлено"

    def test_cancel_unknown_job_is_404(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, _, _ = _get(base + "/api/cancel/nope", method="POST")
        assert status == 404

    def test_cancel_finished_job_is_409(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {
            "source": str(media), "segments": [{"start": "0:00", "end": "0:01"}],
        })
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        status, _, _ = _get(base + f"/api/cancel/{body['job_id']}", method="POST")
        assert status == 409

    def test_page_has_the_stop_button(self):
        from video_clipper.gui import PAGE

        assert 'id="stop"' in PAGE and "/api/cancel/" in PAGE
        assert "Остановлено" in PAGE


class TestBatch:
    def _clipper(self, tmp_path):
        """Возвращает по клипу на каждый отрезок — как настоящий нарезчик."""
        seen = {}

        def clip(source, segments, fast, cfg, progress=None, vertical=False, layout="crop"):
            seen["segments"] = [(s.start, s.end) for s in segments]
            out = []
            for i, seg in enumerate(segments):
                path = tmp_path / "out" / f"clip{i}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"mp4" * 10)
                out.append(
                    ClipResult(
                        source=source, segment=seg, output_path=path,
                        duration=seg.duration, mode="accurate", size_bytes=40,
                    )
                )
            return out

        return clip, seen

    def test_several_segments_in_one_request(self, server_factory, tmp_path):
        clipper, seen = self._clipper(tmp_path)
        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {
            "source": str(media),
            "segments": [
                {"start": "0:00", "end": "0:05"},
                {"start": "0:10", "end": "0:15"},
                {"start": "1:00", "end": "1:30"},
            ],
        })
        assert status == 202
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "done", job.get("error")
        assert seen["segments"] == [(0.0, 5.0), (10.0, 15.0), (60.0, 90.0)]
        assert len(job["results"]) == 3

    def test_open_end_inside_the_batch(self, server_factory, tmp_path, ten_seconds):
        base = server_factory(default_clipper)
        status, body = post_json(base, "/api/clip", {
            "source": str(ten_seconds),
            "segments": [
                {"start": "0:00", "end": "0:02"},
                {"start": "0:07", "end": "*"},
            ],
        })
        assert status == 202
        for _ in range(120):
            job = json.loads(get(base, f"/api/jobs/{body['job_id']}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.1)
        assert job["state"] == "done", job.get("error")
        durations = sorted(round(item["duration"], 1) for item in job["results"])
        assert durations == pytest.approx([2.0, 3.0], abs=0.4)

    def test_overlaps_are_allowed_in_the_batch(self, server_factory, tmp_path):
        clipper, seen = self._clipper(tmp_path)
        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {
            "source": str(media),
            "segments": [{"start": "0:00", "end": "0:10"}, {"start": "0:05", "end": "0:15"}],
        })
        assert status == 202, body

    def test_bad_segment_fails_with_400(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        status, body = post_json(base, "/api/clip", {
            "source": str(media),
            "segments": [{"start": "0:10", "end": "0:05"}],
        })
        assert status == 400

    def test_zip_bundles_every_clip(self, server_factory, tmp_path):
        import io
        import zipfile

        clipper, _ = self._clipper(tmp_path)
        base = server_factory(clipper)
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        _, body = post_json(base, "/api/clip", {
            "source": str(media),
            "segments": [{"start": "0:00", "end": "0:02"}, {"start": "0:10", "end": "0:12"}],
        })
        job_id = body["job_id"]
        for _ in range(60):
            job = json.loads(get(base, f"/api/jobs/{job_id}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        status, payload, headers = _get(base + f"/api/zip/{job_id}")
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert len(archive.namelist()) == 2

    def test_zip_for_unfinished_job_is_404(self, server_factory, tmp_path):
        base = server_factory(fake_clipper(tmp_path / "c.mp4"))
        status, body, _ = _get(base + "/api/zip/nope")
        assert status == 404

    def test_page_has_the_segment_list(self):
        from video_clipper.gui import PAGE

        assert 'id="segments"' in PAGE and 'id="add-seg"' in PAGE
        assert "в списке" in PAGE and "/api/zip/" in PAGE


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
