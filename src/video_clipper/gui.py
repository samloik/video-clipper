"""Minimal local web GUI: drag & drop a file or paste a URL, set two timecodes.

Binds to 127.0.0.1 only, no authentication, no CDN, no build step. The page is a
single HTML string served from memory; the API runs cutting in a background
thread and reports progress through /api/jobs/<id>.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .config import Config, load_config
from .cut import ClipResult
from .probe import EnvironmentProblem, check_tools
from .segments import Segment, clip_filename, validate_segments
from .source import is_url

log = logging.getLogger("video_clipper")

MAX_UPLOAD = 4 * 1024 * 1024 * 1024  # 4 GB
SESSION_COOKIE = "vc_session"
SESSION_MAX_AGE = 12 * 3600
LOGIN_WINDOW = 600.0
LOGIN_ATTEMPTS = 6

LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-clipper — вход</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;background:#111418;color:#e7e9ee;margin:0;
      display:flex;min-height:100vh;align-items:center;justify-content:center}
 form{background:#1a1e24;padding:28px;border-radius:14px;min-width:280px;
      box-shadow:0 8px 30px #0006}
 h1{font-size:18px;margin:0 0 16px}
 input{width:100%;padding:11px;border-radius:9px;border:1px solid #2c333d;
       background:#111418;color:#e7e9ee;font-size:16px;box-sizing:border-box}
 button{margin-top:14px;width:100%;padding:11px;border:0;border-radius:9px;
        background:#3b82f6;color:#fff;font-size:16px;cursor:pointer}
 p.err{color:#f87171;margin:0 0 12px;font-size:14px}
</style></head><body>
<form method="post" action="/login">
  <h1>video-clipper</h1>
  {error}
  <input type="password" name="password" placeholder="пароль" autofocus autocomplete="current-password">
  <button type="submit">Войти</button>
</form></body></html>"""


class Auth:
    """Password gate for the GUI, with a lightweight brute-force brake.

    The session cookie is an HMAC of a per-process random secret, so restarting
    the server invalidates every session — which is the behaviour we want.
    """

    def __init__(self, password: str | None):
        self.password = (password or "").strip()
        self.enabled = bool(self.password)
        self._secret = secrets.token_bytes(32)
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def token(self) -> str:
        return hmac.new(self._secret, b"session", hashlib.sha256).hexdigest()

    def check_cookie(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, self.token())

    def check_password(self, candidate: str) -> bool:
        return self.enabled and hmac.compare_digest(candidate, self.password)

    def blocked(self, client: str) -> bool:
        now = time.time()
        with self._lock:
            attempts = [t for t in self._attempts.get(client, []) if now - t < LOGIN_WINDOW]
            self._attempts[client] = attempts
            return len(attempts) >= LOGIN_ATTEMPTS

    def note_failure(self, client: str) -> None:
        with self._lock:
            self._attempts.setdefault(client, []).append(time.time())

    def clear(self, client: str) -> None:
        with self._lock:
            self._attempts.pop(client, None)

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>video-clipper</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:15px/1.5 system-ui, sans-serif; background:#14161a; color:#e7e9ee;
         display:flex; justify-content:center; padding:32px 16px; }
  main { width:100%; max-width:720px; }
  h1 { font-size:20px; margin:0 0 4px; }
  p.hint { color:#98a2b3; margin:0 0 20px; font-size:13px; }
  #drop { border:2px dashed #3a4250; border-radius:12px; padding:28px; text-align:center;
          color:#98a2b3; transition:.15s; cursor:pointer; }
  #drop.over { border-color:#5b8def; background:#1b2130; color:#cfe0ff; }
  .row { display:flex; gap:12px; margin-top:16px; flex-wrap:wrap; align-items:end; }
  label { display:flex; flex-direction:column; gap:6px; font-size:13px; color:#98a2b3; }
  input[type=text] { background:#1b1f27; border:1px solid #2c333f; color:#e7e9ee;
                     border-radius:8px; padding:9px 11px; font:inherit; min-width:190px; }
  input[type=text]:focus { outline:none; border-color:#5b8def; }
  button { background:#5b8def; color:#fff; border:0; border-radius:8px; padding:10px 18px;
           font:inherit; font-weight:600; cursor:pointer; }
  button:disabled { background:#39414f; cursor:default; }
  #status { margin-top:18px; font-size:13px; color:#98a2b3; white-space:pre-wrap; }
  ul#results { list-style:none; padding:0; margin:14px 0 0; }
  ul#results li { background:#1b1f27; border:1px solid #2c333f; border-radius:10px;
                  padding:10px 12px; margin-bottom:8px; display:flex; justify-content:space-between;
                  gap:12px; align-items:center; }
  a { color:#7aa2ff; }
  .err { color:#ff8f8f; }
  .src { font-size:12px; color:#98a2b3; word-break:break-all; }
</style>
</head>
<body>
<main>
  <h1>video-clipper</h1>
  <p class="hint">Перетащи видеофайл в поле ниже или вставь ссылку. Таймкоды: ЧЧ:ММ:СС, ММ:СС или секунды.</p>

  <div id="drop">Перетащи файл сюда или нажми, чтобы выбрать</div>
  <input id="file" type="file" accept="video/*" hidden>

  <div class="row">
    <label>Ссылка (YouTube, TikTok, ...)<input id="url" type="text" placeholder="https://..."></label>
  </div>
  <div class="row">
    <label>Начало<input id="start" type="text" value="0:00" size="10"></label>
    <label>Конец<input id="end" type="text" value="0:30" size="10"></label>
    <label style="flex-direction:row;align-items:center;gap:8px;color:#e7e9ee">
      <input id="fast" type="checkbox"> быстро, без перекодирования
    </label>
    <label style="flex-direction:row;align-items:center;gap:8px;color:#e7e9ee">
      <input id="vertical" type="checkbox"> вертикально 9:16
    </label>
    <button id="go">Нарезать</button>
  </div>

  <div id="status"></div>
  <ul id="results"></ul>
</main>
<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const statusEl = document.getElementById('status');
const resultsEl = document.getElementById('results');
let uploadedPath = null;

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { if (fileInput.files[0]) upload(fileInput.files[0]); });
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.add('over');
}));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', ev => {
  const f = ev.dataTransfer.files[0];
  if (f) upload(f);
});

async function upload(file) {
  drop.textContent = file.name;
  statusEl.textContent = 'Загружаю ' + file.name + ' (' + Math.round(file.size/1048576) + ' МБ)...';
  const res = await fetch('/api/upload?name=' + encodeURIComponent(file.name), { method:'POST', body:file });
  const data = await res.json();
  if (!res.ok) { statusEl.innerHTML = '<span class="err">' + (data.error || 'ошибка загрузки') + '</span>'; return; }
  uploadedPath = data.path; statusEl.textContent = 'Файл готов: ' + file.name;
}

document.getElementById('go').addEventListener('click', async () => {
  resultsEl.innerHTML = '';
  const url = document.getElementById('url').value.trim();
  const source = url || uploadedPath;
  if (!source) { statusEl.innerHTML = '<span class="err">Нужен файл или ссылка</span>'; return; }
  const body = {
    source,
    start: document.getElementById('start').value.trim(),
    end: document.getElementById('end').value.trim(),
    fast: document.getElementById('fast').checked,
    vertical: document.getElementById('vertical').checked
  };
  const btn = document.getElementById('go'); btn.disabled = true;
  statusEl.textContent = 'Запускаю...';
  try {
    const res = await fetch('/api/clip', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) { statusEl.innerHTML = '<span class="err">' + (data.error || 'ошибка') + '</span>'; return; }
    await poll(data.job_id);
  } finally { btn.disabled = false; }
});

async function poll(id) {
  for (;;) {
    const res = await fetch('/api/jobs/' + id);
    const job = await res.json();
    statusEl.textContent = job.state === 'running' ? (job.message || 'Нарезка...') : '';
    if (job.state === 'error') { statusEl.innerHTML = '<span class="err">' + job.error + '</span>'; return; }
    if (job.state === 'done') {
      statusEl.textContent = 'Готово: ' + job.results.length + ' файл(ов)';
      job.results.forEach((r, i) => {
        const li = document.createElement('li');
        li.innerHTML = '<div><a href="/api/download/' + id + '/' + i + '">' + r.filename +
          '</a><div class="src">' + r.duration.toFixed(2) + ' с, ' + r.mode + '</div></div>' +
          '<span class="src">' + Math.round(r.size_bytes/1024) + ' КБ</span>';
        resultsEl.appendChild(li);
      });
      return;
    }
    await new Promise(r => setTimeout(r, 700));
  }
}
</script>
</body>
</html>
"""

# job_id -> {"state": running|done|error, "results": [...], "error": str, "message": str, "files": [paths]}
JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[job_id] = {"state": "running", "results": [], "error": "", "message": "Подготовка...", "files": []}
    return job_id


def _job_update(job_id: str, **kwargs: Any) -> None:
    with _JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kwargs)


def _job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _run_job(
    job_id: str,
    source: str,
    segments: list[Segment],
    fast: bool,
    cfg: Config,
    clipper: Callable[..., list[ClipResult]],
    vertical: bool = False,
) -> None:
    try:
        _job_update(job_id, message="Готовлю источник...")
        try:
            results = clipper(
                source, segments, fast, cfg, lambda msg: _job_update(job_id, message=msg),
                vertical=vertical,
            )
        except TypeError:  # a custom clipper without the vertical parameter (tests)
            results = clipper(source, segments, fast, cfg, lambda msg: _job_update(job_id, message=msg))
        payload = []
        files = []
        for res in results:
            payload.append(
                {
                    "filename": res.output_path.name,
                    "duration": res.duration,
                    "mode": res.mode,
                    "size_bytes": res.size_bytes,
                }
            )
            files.append(str(res.output_path))
        _job_update(job_id, state="done", results=payload, files=files, message="")
    except Exception as exc:  # noqa: BLE001 - reported to the page
        log.exception("Ошибка задания %s", job_id)
        _job_update(job_id, state="error", error=str(exc), message="")


def default_clipper(
    source: str,
    segments: list[Segment],
    fast: bool,
    cfg: Config,
    progress=None,
    vertical: bool = False,
    layout: str = "crop",
) -> list[ClipResult]:
    """Download (if needed), validate and cut — the same path the CLI uses."""
    from .cut import cut_segment, pick_vertical_size, vertical_name
    from .source import prepare_source, shift_segments

    if progress:
        progress("Подготовка источника...")
    src = prepare_source(source, cfg, segments=[(s.start, s.end) for s in segments])
    try:
        original_segments = segments
        segments = shift_segments(segments, src.offset)
        validate_segments(segments, duration=src.info.duration or None)
        out_dir = Path(cfg.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for i, seg in enumerate(segments):
            shown = original_segments[i] if i < len(original_segments) else seg
            if progress:
                progress(f"Нарезка {shown.label}")
            result = cut_segment(
                str(src.path),
                out_dir
                / vertical_name(clip_filename(src.title, source, shown), vertical, layout),
                seg,
                fast=fast,
                ffmpeg_path=cfg.ffmpeg_path,
                has_audio=src.info.has_audio,
                vertical=vertical,
                layout=layout,
                vertical_size=pick_vertical_size(
                    src.info.height, cfg.vertical_width, cfg.vertical_height
                ),
            )
            result.segment = shown
            results.append(result)
        return results
    finally:
        src.cleanup()


def _dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(
    cfg: Config,
    clipper: Callable[..., list[ClipResult]] = default_clipper,
    auth: "Auth | None" = None,
):
    auth_gate: Auth = auth if auth is not None else Auth(None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "video-clipper"

        def _client(self) -> str:
            return self.client_address[0] if self.client_address else "?"

        def _cookie(self) -> str | None:
            raw = self.headers.get("Cookie") or ""
            for chunk in raw.split(";"):
                name, _, value = chunk.strip().partition("=")
                if name == SESSION_COOKIE:
                    return value
            return None

        def _authorized(self) -> bool:
            return (not auth_gate.enabled) or auth_gate.check_cookie(self._cookie())

        def _redirect(self, location: str, code: int = 303) -> None:
            self.send_response(code)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _login_page(self, error: str = "", code: int = 200) -> None:
            body = LOGIN_PAGE.replace("{error}", f'<p class="err">{error}</p>' if error else "")
            self._send(code, body.encode("utf-8"), "text/html; charset=utf-8")

        def _handle_login(self, body: bytes) -> None:
            from urllib.parse import parse_qs

            client = self._client()
            if auth_gate.blocked(client):
                self._login_page("слишком много попыток — подождите несколько минут", 429)
                return
            try:
                password = (parse_qs(body.decode("utf-8", "ignore")).get("password") or [""])[0]
            except Exception:  # noqa: BLE001
                password = ""
            if not auth_gate.check_password(password):
                auth_gate.note_failure(client)
                log.warning("GUI: неверный пароль с %s", client)
                self._login_page("неверный пароль", 401)
                return
            auth_gate.clear(client)
            self.send_response(303)
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={auth_gate.token()}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={SESSION_MAX_AGE}",
            )
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle_logout(self) -> None:
            self.send_response(303)
            self.send_header(
                "Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            )
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
            log.debug("gui: " + fmt, *args)

        def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: int, message: str) -> None:
            self._send(code, _dumps({"error": message}))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/health":
                self._send(200, _dumps({"status": "ok"}))
                return
            if path == "/login":
                if not auth_gate.enabled:
                    self._redirect("/")
                else:
                    self._login_page()
                return
            if path == "/logout":
                self._handle_logout()
                return
            if not self._authorized():
                if path.startswith("/api/"):
                    self._error(401, "нужен вход: откройте /login")
                else:
                    self._redirect("/login")
                return
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                job = _job_snapshot(job_id)
                if job is None:
                    self._error(404, "задание не найдено")
                    return
                self._send(200, _dumps(job))
                return
            if path.startswith("/api/download/"):
                parts = path.split("/")
                if len(parts) < 5:
                    self._error(400, "некорректная ссылка")
                    return
                job_id, idx = parts[3], parts[4]
                job = _job_snapshot(job_id)
                if not job or job["state"] != "done":
                    self._error(404, "файл не готов")
                    return
                try:
                    file_path = Path(job["files"][int(idx)])
                except (IndexError, ValueError):
                    self._error(404, "файл не найден")
                    return
                if not file_path.is_file():
                    self._error(404, "файл удалён")
                    return
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._error(404, "не найдено")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if parsed.path == "/login" and length <= 4096:
                self._handle_login(self.rfile.read(length) if length else b"")
                return
            if not self._authorized():
                self.rfile.read(length) if length else None  # drain, keep the socket sane
                if parsed.path.startswith("/api/"):
                    self._error(401, "нужен вход: откройте /login")
                else:
                    self._redirect("/login")
                return
            if length > MAX_UPLOAD:
                self._error(413, "файл слишком большой")
                return
            body = self.rfile.read(length) if length else b""

            if parsed.path == "/api/upload":
                raw_name = unquote(parsed.query.replace("name=", "", 1) or "upload.mp4")
                name = Path(raw_name).name or "upload.mp4"
                cfg.cache_dir.mkdir(parents=True, exist_ok=True)
                target = cfg.cache_dir / f"upload-{uuid.uuid4().hex[:8]}-{name}"
                target.write_bytes(body)
                self._send(200, _dumps({"path": str(target), "name": name}))
                return

            if parsed.path == "/api/clip":
                try:
                    data = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    self._error(400, "ожидается JSON")
                    return
                try:
                    from .timecode import parse_timecode

                    source = str(data.get("source") or "").strip()
                    if not source:
                        raise ValueError("не указан файл или ссылка")
                    start = parse_timecode(data.get("start"))
                    end = parse_timecode(data.get("end"))
                    segments = [Segment(start, end)]
                    validate_segments(segments, duration=None)
                    vertical = bool(data.get("vertical", False))
                except ValueError as exc:
                    self._error(400, str(exc))
                    return
                if not is_url(source) and not Path(source).expanduser().is_file():
                    self._error(400, f"файл не найден: {source}")
                    return
                job_id = _new_job()
                threading.Thread(
                    target=_run_job,
                    args=(
                        job_id,
                        source,
                        segments,
                        bool(data.get("fast", False)),
                        cfg,
                        clipper,
                        vertical,
                    ),
                    daemon=True,
                ).start()
                self._send(202, _dumps({"job_id": job_id}))
                return

            self._error(404, "не найдено")

    return Handler


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def read_gui_password(cfg: Config) -> str | None:
    """Password from the file named in the config, or from the environment."""
    env = (os.environ.get("VIDEO_CLIPPER_GUI_PASSWORD") or "").strip()
    if env:
        return env
    path = cfg.gui_password_file
    if path and path.is_file():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def create_server(
    cfg: Config,
    clipper: Callable[..., list[ClipResult]] = default_clipper,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
) -> ThreadingHTTPServer:
    bind_host = host or cfg.gui_host or "127.0.0.1"
    bind_port = cfg.gui_port if port is None else port
    password = password if password is not None else read_gui_password(cfg)
    if bind_host not in LOOPBACK_HOSTS and not (password or "").strip():
        # Fail closed: an unprotected GUI handing out files over the LAN is worse
        # than a GUI that refuses to start.
        raise RuntimeError(
            f"GUI на {bind_host} без пароля запрещён. Создай файл с паролем и укажи его "
            f"в конфиге: gui_password_file, либо задай VIDEO_CLIPPER_GUI_PASSWORD."
        )
    httpd = ThreadingHTTPServer((bind_host, bind_port), make_handler(cfg, clipper, Auth(password)))
    httpd.daemon_threads = True
    return httpd


def run_gui(config_path: str | None = None, open_browser: bool = True,
            host: str | None = None, port: int | None = None) -> int:
    from .cli import setup_logging

    cfg = load_config(config_path)
    setup_logging(cfg.log_file)
    try:
        check_tools(cfg.ffmpeg_path, cfg.ffprobe_path)
    except EnvironmentProblem as exc:
        print(f"Ошибка окружения: {exc}")
        return 2

    try:
        httpd = create_server(cfg, host=host, port=port)
    except OSError as exc:
        hint = "порт занят другим процессом" if exc.errno == 98 else str(exc)
        print(
            f"Не удалось занять {cfg.gui_host}:{cfg.gui_port if port is None else port} — {hint}. "
            f"Поменяй gui_port в конфиге или останови занявший его процесс."
        )
        return 2
    except RuntimeError as exc:
        print(f"Ошибка запуска GUI: {exc}")
        return 2
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}/"
    gate = "вход по паролю" if read_gui_password(cfg) else "без пароля (только localhost)"
    print(f"GUI: {url}  ({gate}; Ctrl+C для остановки)")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="video-clipper-gui",
        description="Локальный веб-интерфейс video-clipper (только 127.0.0.1).",
    )
    parser.add_argument("--config", help="путь к config.json")
    parser.add_argument("--host", help="адрес привязки (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, help="порт (по умолчанию из конфига)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args()
    return run_gui(
        config_path=args.config,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
