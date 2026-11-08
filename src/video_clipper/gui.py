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
import mimetypes
import os
import re
import secrets
import shutil
import threading
import tempfile
import time
import uuid
import zipfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import Config, apply_retention_override, load_config
from .cut import ClipResult, CutCancelled, clamp_horizontal
from .probe import EnvironmentProblem, check_tools
from .storage import SweepResult
from .preview import (
    cached_frame,
    cached_stream,
    unavailable_video,
    closest_frame,
    ensure_window,
    find_window,
    grab_frame,
    FRAME_TOTAL_BUDGET,
    grab_stream_frame,
    link_problem,
    local_source,
    probe_media,
    resolve_for_time,
    stream_info,
    windows_dir,
)
from .segments import (
    Segment,
    clip_filename,
    has_open_ends,
    is_open_end,
    params_summary,
    parse_end_token,
    resolve_open_ends,
    validate_segments,
)
from .source import is_bot_check, is_url

log = logging.getLogger("video_clipper")
STARTED_AT = time.time()


def page_version() -> str:
    """Короткий отпечаток страницы: по нему открытые вкладки замечают обновление."""
    return hashlib.sha1(PAGE.encode("utf-8")).hexdigest()[:8]

MAX_UPLOAD = 4 * 1024 * 1024 * 1024  # 4 GB
# Эстимейт для GUI: локальный файл не надо качать, поэтому накладные меньше.
ESTIMATE_LOCAL_OVERHEAD = 4.0
ESTIMATE_URL_OVERHEAD = 15.0
# Секционная загрузка идёт через ffmpeg и не даёт событий прогресса — раз в N секунд
# обновляем строку статуса, чтобы «готовлю источник» не висело мёртвым.
HEARTBEAT_SECONDS = 4.0
SESSION_COOKIE = "vc_session"
SESSION_MAX_AGE = 12 * 3600
SESSION_REMEMBER_AGE = 30 * 24 * 3600  # «запомнить меня на месяц»
LOGIN_WINDOW = 600.0
LOGIN_ATTEMPTS = 6

LOGIN_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-clipper — вход</title>
<style>
:root{--bg:#0e1013;--panel:#15181d;--line:#252b34;--ink:#eef1f5;--ink-3:#6d7683;
      --accent:#e0a458;--accent-ink:#1a1206;--err:#ef7c6a;
      --mono:"JetBrains Mono","DejaVu Sans Mono",ui-monospace,monospace;
      --sans:"IBM Plex Sans","Noto Sans","Liberation Sans",system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
     font:15px/1.5 var(--sans);display:grid;place-items:center;padding:24px}
.card{width:100%;max-width:340px}
.brand{font-weight:600;margin-bottom:4px}
.brand span{color:var(--ink-3);font-weight:400}
.sub{color:var(--ink-3);font-size:12.5px;margin:0 0 22px}
label{display:block;font-size:11px;letter-spacing:.09em;text-transform:uppercase;
      color:var(--ink-3);margin-bottom:7px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--line);
      background:var(--panel);color:var(--ink);font-size:15px;outline:none}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #e0a45822}
button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:9px;
       background:var(--accent);color:var(--accent-ink);font:600 15px var(--sans);
       cursor:pointer;min-height:44px}
button:hover{filter:brightness(1.06)}
.err{color:var(--err);font-size:13px;margin:0 0 14px}
.foot{margin-top:18px;color:var(--ink-3);font-size:12px;font-family:var(--mono)}
.check{display:flex;align-items:center;gap:9px;margin:16px 0 0;font-size:13px;
  letter-spacing:0;text-transform:none;color:var(--ink-2);cursor:pointer;min-height:32px}
.check input{width:18px;height:18px;accent-color:var(--accent);padding:0;flex:none}
</style>
</head>
<body>
<form class="card" method="post" action="/login">
  <div class="brand">video-clipper <span>· вход</span></div>
  <p class="sub">Локальный сервер нарезки. Пароль — в файле gui_password_file.</p>
  {error}
  <label for="pw">пароль</label>
  <input id="pw" type="password" name="password" autofocus autocomplete="current-password">
  <label class="check"><input type="checkbox" name="remember" value="1"> запомнить меня на месяц</label>
  <button type="submit">Войти</button>
  <div class="foot">без галочки — 12 часов, с галочкой — 30 дней</div>
</form>
</body></html>"""


class Auth:
    """Password gate for the GUI, with a lightweight brute-force brake.

    A cookie carries its own expiry, signed with a secret kept on disk, so a
    «запомнить на месяц» session survives a restart of the service. Without the
    checkbox the cookie is a browser-session one and dies with the token.
    """

    def __init__(self, password: str | None, secret_file: Path | None = None):
        self.password = (password or "").strip()
        self.enabled = bool(self.password)
        self._secret = self._load_secret(secret_file)
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _load_secret(path: Path | None) -> bytes:
        raw = b""
        if path is not None:
            try:
                raw = path.read_bytes()
            except OSError:
                raw = b""
        # Новый формат — 64 символа hex. Так ключ нельзя испортить обрезкой
        # пробельных байтов: раньше ключ, случайно оканчивавшийся на \r, \n или
        # пробел, после strip() становился короче, считался битым и генерировался
        # заново — сессии пропадали после перезапуска (примерно в 2% случаев).
        text = raw.strip()
        if len(text) == 64:
            try:
                return bytes.fromhex(text.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                pass
        # Старый формат — 32 сырых байта плюс перевод строки: берём ровно 32
        # и сразу переводим файл в hex, чтобы больше не зависеть от пробельных байтов.
        if len(raw) in (32, 33):
            legacy = raw[:32]
            if path is not None:
                try:
                    path.write_text(legacy.hex() + "\n", encoding="ascii")
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            return legacy
        secret = secrets.token_bytes(32)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secret.hex() + "\n", encoding="ascii")
                os.chmod(path, 0o600)
            except OSError:
                log.warning("не смог сохранить ключ сессий в %s", path)
        return secret

    def token(self, ttl: float = SESSION_MAX_AGE) -> str:
        expiry = int(time.time() + ttl)
        return f"{expiry}.{self._sign(expiry)}"

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._secret, f"session:{expiry}".encode(), hashlib.sha256).hexdigest()

    def check_cookie(self, value: str | None) -> bool:
        if not value:
            return False
        expiry_raw, _, signature = value.partition(".")
        try:
            expiry = int(expiry_raw)
        except ValueError:
            return False
        if expiry < time.time():
            return False
        return hmac.compare_digest(signature, self._sign(expiry))

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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-clipper</title>
<style>
:root{
  --bg:#0e1013; --panel:#161a1f; --panel-2:#1b2027; --line:#2b323c; --line-2:#3a424e;
  --ink:#eef1f5; --ink-2:#bcc5d1; --ink-3:#9aa3b0;
  --accent:#e0a458; --accent-ink:#1a1206; --ok:#7fd1a3; --err:#ef7c6a;
  --r:10px;
  --mono:"JetBrains Mono","DejaVu Sans Mono",ui-monospace,SFMono-Regular,monospace;
  --sans:"IBM Plex Sans","Noto Sans","Liberation Sans",system-ui,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);
     -webkit-font-smoothing:antialiased;
     /* Страница прокручивается как обычный сайт: раньше высота была зафиксирована
        и на коротких экранах колесо не листало содержимое колонок. */
     display:flex;flex-direction:column;min-height:100vh;min-height:100dvh;
     overflow-y:auto}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}

.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;
     padding:13px 22px;border-bottom:1px solid var(--line);background:#101317;
     position:sticky;top:0;z-index:5}
.brand{font-weight:600;letter-spacing:-.01em}
.brand span{color:var(--ink-3);font-weight:400}
.chip{font-size:12px;color:var(--ink-2);border:1px solid var(--line);
      padding:4px 10px;border-radius:999px;display:inline-flex;align-items:center;gap:7px}
.chip i{width:6px;height:6px;border-radius:50%;background:var(--line-2);display:block}
.chip.ok i{background:var(--ok)}
.bar-actions{display:flex;align-items:center;gap:8px}
.ghost{background:transparent;border:1px solid var(--line);color:var(--ink-2);
  border-radius:8px;padding:7px 11px;font-size:12.5px;cursor:pointer;min-height:34px}
.ghost:hover{border-color:var(--line-2);color:var(--ink)}
.ghost:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.panel{display:flex;flex-direction:column;gap:12px;flex:1;min-height:0;
  border:1px solid var(--line);border-radius:var(--r);background:var(--panel);
  padding:14px 16px}
.panel[hidden]{display:none}
.panel .sweep{margin:10px 16px 14px;width:calc(100% - 32px)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.panel pre{margin:0;flex:1;min-height:0;overflow:auto;white-space:pre-wrap;
  font:12.5px/1.65 var(--mono);color:var(--ink-2)}
.spinner{width:16px;height:16px;border-radius:50%;flex:none;
  border:2px solid var(--line);border-top-color:var(--accent);
  animation:spin .9s linear infinite}
@media (prefers-reduced-motion:reduce){.spinner{animation:none;border-top-color:var(--line-2)}}
.modal{position:fixed;inset:0;background:#05070acc;backdrop-filter:blur(2px);
  display:flex;align-items:center;justify-content:center;padding:20px;z-index:20}
.modal[hidden]{display:none}
.modal-box{background:var(--panel);border:1px solid var(--line);border-radius:calc(var(--r) + 2px);
  width:min(400px,100%);padding:20px 22px 18px;box-shadow:0 24px 60px #000a}
.modal-box h3{margin:0 0 8px;font-size:15.5px;font-weight:600;letter-spacing:-.01em}
.modal-box p{margin:0 0 18px;color:var(--ink-2);font-size:13.5px;line-height:1.55}
.modal-actions{display:flex;justify-content:flex-end;gap:9px}
.modal-box input{margin-bottom:8px}
.modal-box .hint{margin:0 0 16px}
.modal-actions .primary{width:auto;padding:9px 15px;font-size:13px;min-height:38px}
.danger{background:#e0574a;color:#180b08;border:1px solid transparent;border-radius:8px;
  padding:9px 14px;font-size:13px;font-weight:600;cursor:pointer;min-height:38px}
.danger:hover{filter:brightness(1.07)}
.danger:focus-visible,.modal .ghost:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.grid{display:grid;grid-template-columns:minmax(320px,420px) minmax(0,1fr);
      align-items:start;min-height:0}
.col-out{min-height:0}
html{scrollbar-width:thin;scrollbar-color:#3a424e transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#3a424e;border-radius:6px}
::-webkit-scrollbar-track{background:transparent}
.col-form{padding:22px;border-right:1px solid var(--line);display:flex;
          flex-direction:column;gap:18px;min-height:0}
.col-out{padding:22px;display:flex;flex-direction:column;gap:14px;min-width:0}

.label{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
       color:var(--ink-3);margin-bottom:8px;display:block;font-weight:500}

.drop{border:1.5px dashed var(--line-2);border-radius:var(--r);padding:20px 16px;
      display:flex;flex-direction:column;gap:4px;cursor:pointer;
      background:var(--panel);transition:border-color .15s,background .15s}
.drop:hover{border-color:var(--accent);background:var(--panel-2)}
.drop.over{border-color:var(--accent);background:#211c14}
.drop strong{font-weight:500;font-size:14.5px}
.drop small{color:var(--ink-3);font-size:12.5px}
.drop.has-file{border-style:solid;border-color:var(--line-2)}
.drop.has-file strong{color:var(--accent);font-family:var(--mono);font-size:13px;
                      word-break:break-all}

input[type=text]{width:100%;background:var(--panel);border:1px solid var(--line);
  color:var(--ink);border-radius:9px;padding:11px 12px;font-size:15px;outline:none;
  line-height:1.25}
input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px #e0a45822}
input::placeholder{color:#78818e}

.or{display:flex;align-items:center;gap:10px;color:var(--ink-3);font-size:12px;
    margin:2px 0}
.or::before,.or::after{content:"";height:1px;background:var(--line);flex:1}

.range{display:grid;grid-template-columns:1fr auto 1fr;align-items:end;gap:10px}
.range label{display:flex;flex-direction:column;min-width:0}
.range label span{font-size:11.5px;color:var(--ink-3);margin-bottom:6px}
.range .dash{color:var(--ink-3);padding-bottom:12px;font-family:var(--mono)}
.range input{font-size:15px}
.range label:first-child input,.range label:last-child input{text-align:center}

.presets{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:10px}
.presets button{background:transparent;border:1px solid var(--line);color:var(--ink-2);
  border-radius:7px;padding:6px 10px;font-size:12px;font-family:var(--mono);
  cursor:pointer;min-height:32px}
.presets button:hover{border-color:var(--line-2);color:var(--ink)}
.presets button.on{border-color:var(--accent);color:var(--accent);background:#e0a45814}
.presets .to-end,.presets .addseg{border-color:#4a3a20;color:var(--accent)}
.presets .addseg{border-style:dashed}
.segments{list-style:none;margin:10px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.segments[hidden]{display:none}
.segments li{display:flex;align-items:center;justify-content:space-between;gap:10px;
  border:1px solid var(--line);border-radius:8px;padding:7px 10px;background:var(--panel)}
.segments li span{font-family:var(--mono);font-size:13px;color:var(--ink);cursor:pointer}
.segments li span:hover{color:var(--accent)}
.segments li button{background:transparent;border:0;color:var(--ink-3);cursor:pointer;
  font-size:14px;line-height:1;padding:4px 6px;min-height:26px;border-radius:6px}
.segments li button:hover{color:var(--err);background:#1e1513}
.seg-count{font-size:11.5px;color:var(--ink-3);margin-top:8px;min-height:14px}
.preview{margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:10px;
  background:var(--panel-2,#15181d);display:flex;flex-direction:column;gap:9px}
.preview[hidden]{display:none}
.preview .thumbs{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.preview figure{margin:0;display:flex;flex-direction:column;gap:5px}
.preview img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:7px;
  background:#0b0d10;border:1px solid var(--line);display:block}
/* кадр по ссылке идёт несколько секунд: показываем, что он ещё едет */
.preview .thumb{position:relative;border-radius:7px;overflow:hidden}
.preview .thumb img{color:transparent}  /* пока кадра нет, alt не должен лезть в рамку */
.preview .thumb .loading{position:absolute;inset:0;display:none;align-items:center;
  justify-content:center;gap:8px;background:#0b0d10c4;color:var(--ink-2);
  font-size:11.5px;font-family:var(--mono);border:1px solid var(--line);border-radius:7px}
.preview .thumb.busy .loading{display:flex}
.preview .thumb.busy img{opacity:.35;filter:saturate(.5)}
.preview .thumb .spin{width:13px;height:13px;border-radius:50%;flex:none;
  border:2px solid #ffffff2e;border-top-color:var(--accent);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.preview figcaption{font-size:11px;color:var(--ink-3)}
.preview figcaption b{color:var(--ink-2);font-weight:500}
.preview .frame-note{color:var(--err);font-size:10.5px}
.preview .stepper{display:flex;gap:4px;margin-top:2px}
.preview .stepper button{flex:1;min-height:24px;padding:1px 4px;font-size:10.5px;
  border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--ink-2);
  cursor:pointer;font-family:var(--sans);white-space:nowrap}
.preview .stepper button:hover{border-color:var(--accent);color:var(--accent)}
.preview-bar{display:flex;flex-wrap:wrap;gap:6px}
.preview-bar .ghost{min-height:26px;padding:2px 8px;font-size:11px;border-radius:6px}
.preview-bar .play{margin-left:auto;min-height:26px;padding:2px 10px;font-size:11.5px;
  border-radius:6px;border:1px solid var(--accent);background:transparent;color:var(--accent);
  cursor:pointer}
.preview-bar .play:hover{background:var(--accent);color:#101214}
.preview video{width:100%;border-radius:8px;background:#000;display:block}
.preview video[hidden]{display:none}
.preview-note{font-size:11px;color:var(--ink-3);font-family:var(--mono)}
.presets .reset{margin-left:auto;border-color:transparent;color:var(--ink-3);
  text-decoration:underline;text-underline-offset:3px;font-family:var(--sans)}
.presets .reset:hover{color:var(--ink-2)}

.modes{display:flex;flex-direction:column;border-top:1px solid var(--line);padding-top:10px}
.switch{display:flex;align-items:flex-start;gap:11px;padding:9px 0;cursor:pointer;min-height:44px}
.switch input{appearance:none;width:36px;height:20px;border-radius:999px;
  background:#3a424e;position:relative;cursor:pointer;flex:none;margin-top:2px;
  transition:background .15s}
.switch input::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;
  border-radius:50%;background:#aab3c0;transition:transform .15s,background .15s}
.switch input:checked{background:#4a3a20}
.switch input:checked::after{transform:translateX(16px);background:var(--accent)}
.switch strong{display:block;font-size:14px;font-weight:400;color:var(--ink-2)}
.switch input:checked ~ strong{color:var(--ink)}
.switch em{display:block;font-style:normal;font-size:12px;color:var(--ink-3);margin-top:1px}
.layout-choice{display:flex;flex-wrap:wrap;align-items:center;gap:7px;padding:0 0 8px 47px}
.layout-choice[hidden]{display:none}
.layout-choice span{font-size:12px;color:var(--ink-3);margin-right:2px}
.crop-pos{display:flex;align-items:center;gap:9px;padding:0 0 10px 47px}
.crop-pos[hidden]{display:none}
.crop-pos span{font-size:12px;color:var(--ink-3)}
.crop-pos input[type=range]{flex:1;min-width:60px;accent-color:var(--accent);height:26px;
  background:transparent;border:0}
.crop-pos .ghost{min-height:28px;padding:3px 9px;font-size:11.5px;border-radius:7px}
.crop-pos .mono{font-size:12px;color:var(--ink-2);min-width:38px;text-align:right}
.seg{border:1px solid var(--line);background:transparent;color:var(--ink-2);
  border-radius:7px;padding:7px 11px;font-size:12.5px;cursor:pointer;min-height:34px}
.seg:hover{border-color:var(--line-2);color:var(--ink)}
.seg.on{border-color:var(--accent);color:var(--accent);background:#e0a45814}

.primary{width:100%;padding:13px 16px;border:0;border-radius:9px;
  background:var(--accent);color:var(--accent-ink);font:600 15px var(--sans);
  cursor:pointer;transition:filter .15s,transform .06s}
.primary:hover:not(:disabled){filter:brightness(1.06)}
.primary:active:not(:disabled){transform:translateY(1px)}
.primary:disabled{background:#252b34;color:#98a1ae;cursor:not-allowed}
.primary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.hint{margin:0;color:var(--ink-3);font-size:12.5px;line-height:1.5}
.hint b{color:var(--ink-2);font-weight:500;font-family:var(--mono)}

.out-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.out-head .label{margin:0}
.out-actions{display:flex;align-items:center;gap:10px}
.timer{font-size:12.5px;color:var(--ink-3)}
.ghost.stop{border-color:#5f2f28;color:var(--err)}
.ghost.stop:hover{background:#1e1513;border-color:var(--err);color:var(--err)}
.ghost.stop:disabled{opacity:.55;cursor:default}

.status{color:var(--ink-3);font-size:13.5px;min-height:22px;display:flex;
  align-items:center;gap:9px;padding:0}
.eta{font-size:12px;color:var(--ink-3);margin:-2px 0 10px;min-height:15px;
  font-variant-numeric:tabular-nums}
.status.busy,.status.err{border:1px solid var(--line);background:var(--panel);
  border-radius:var(--r);padding:12px 14px;min-height:46px}
.status.busy{color:var(--ink)}
.status.err{border-color:#5f2f28;background:#1e1513;color:var(--err)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}

.results{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;
  border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.results:empty{display:none}
.results li{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:14px;
  align-items:center;padding:13px 14px;border-bottom:1px solid var(--line);
  background:var(--panel)}
.results li:last-child{border-bottom:0}
.results a.file{color:var(--ink);text-decoration:none;font-size:13.5px;display:block;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.results a.file:hover{color:var(--accent);text-decoration:underline}
.results .meta{color:var(--ink-3);font-size:12px;margin-top:3px}
.results .size{color:var(--ink-3);font-size:12px;font-family:var(--mono)}
.results .get{color:var(--accent);text-decoration:none;font-size:12.5px;
  border:1px solid var(--line);border-radius:7px;padding:7px 11px;min-height:34px;
  display:inline-flex;align-items:center}
.results .get:hover{border-color:var(--accent)}
.results li.zip{grid-template-columns:minmax(0,1fr) auto;background:var(--panel-2)}
.results li.zip .meta{margin-top:0;color:var(--ink-2)}

.empty{border:1px solid var(--line);border-radius:var(--r);padding:18px 16px;
  color:var(--ink-3);font-size:13px;line-height:1.6;display:flex;flex-direction:column;gap:10px}
.empty b{color:var(--ink-2);font-weight:500;font-size:13.5px}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px}
.steps li{display:flex;gap:10px;align-items:baseline}
.steps i{font-style:normal;font-family:var(--mono);font-size:11.5px;color:var(--accent);
  width:14px;flex:none}

@media (max-width:840px){
  body{height:auto;min-height:100dvh;overflow:visible;display:block}
  .grid{grid-template-columns:1fr;height:auto;overflow:visible}
  .col-out{overflow:visible}
  .col-form{border-right:0;border-bottom:1px solid var(--line)}
  .results li{grid-template-columns:minmax(0,1fr) auto}
  .results .size{display:none}
  .empty{min-height:120px}
  .presets button{min-height:40px;padding:9px 13px}
  .presets .reset{display:inline-flex;align-items:center;padding:9px 8px}
  .brand span{display:none}
  .switch{padding:11px 0}
}
@media (max-width:620px){
  .bar{flex-wrap:wrap;gap:8px;padding:10px 14px}
  .brand{flex:1 1 100%;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar-actions{flex:1 1 100%;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
    gap:6px;align-items:center}
  .bar-actions .chip{grid-column:1/-1;justify-self:start;font-size:11.5px;padding:3px 9px}
  .bar-actions .ghost{width:100%;padding:6px 4px;font-size:11.5px;min-height:32px}
}
@media (max-width:520px){
  .bar{padding:10px 12px}
  .col-form,.col-out{padding:16px}
  .range{grid-template-columns:1fr auto 1fr;gap:8px}
}
</style>
</head>
<body>
<header class="bar">
  <div class="brand">video-clipper <span>· нарезка по таймкодам</span></div>
  <div class="bar-actions">
    <span class="chip mono" id="svc"><i></i><span id="svc-text">проверяю связь</span></span>
    <button class="ghost" id="btn-status" type="button">Статус</button>
    <button class="ghost" id="btn-info" type="button">Справка</button>
    <button class="ghost" id="btn-keep" type="button">Хранение</button>
    <button class="ghost" id="btn-clean" type="button">Очистить</button>
  </div>
</header>

<div class="modal" id="confirm" hidden>
  <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
    <h3 id="confirm-title">Очистить всё?</h3>
    <p id="confirm-text">Все скачанные оригиналы и готовые клипы будут удалены. Отменить это нельзя.</p>
    <div class="modal-actions">
      <button class="ghost" id="confirm-no" type="button">Отмена</button>
      <button class="danger" id="confirm-yes" type="button">Удалить</button>
    </div>
  </div>
</div>

<div class="modal" id="prompt" hidden>
  <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="prompt-title">
    <h3 id="prompt-title">Хранение файлов</h3>
    <p>Сколько держать скачанные оригиналы и готовые клипы после последнего использования?</p>
    <input id="prompt-input" class="mono" type="text" value="30" inputmode="numeric"
           aria-label="Время хранения" autocomplete="off">
    <p class="hint" id="prompt-hint">30 — минут, 1:30 — час тридцать, 2ч — два часа.</p>
    <div class="modal-actions">
      <button class="ghost" id="prompt-no" type="button">Отмена</button>
      <button class="primary prompt-ok" id="prompt-yes" type="button">Сохранить</button>
    </div>
  </div>
</div>

<main class="grid">
  <section class="col-form">
    <div>
      <span class="label">Источник</span>
      <div id="drop" class="drop" tabindex="0" role="button" aria-label="Выбрать видеофайл">
        <strong id="drop-title">Выбери файл или перетащи</strong>
        <small id="drop-sub">mp4, mov, mkv, webm · до 4 ГБ</small>
      </div>
      <input id="file" type="file" accept="video/*" hidden>
      <div class="or">или ссылка</div>
      <input id="url" class="mono" type="text" placeholder="https://youtu.be/…" autocomplete="off" spellcheck="false" aria-label="Ссылка на видео">
    </div>

    <div>
      <span class="label">Диапазон</span>
      <div class="range">
        <label><span>начало</span><input id="start" class="mono" type="text" value="0:00" inputmode="text"
          autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="Начало"></label>
        <span class="dash">→</span>
        <label><span>конец</span><input id="end" class="mono" type="text" value="0:30" inputmode="text"
          autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="Конец"></label>
      </div>
      <div class="presets">
        <button type="button" class="addseg" id="add-seg">+ в список</button>
        <button type="button" class="reset" data-reset="1">сброс</button>
      </div>
      <ul class="segments" id="segments" hidden></ul>
      <div class="seg-count mono" id="seg-count"></div>
      <div class="preview" id="preview" hidden>
        <div class="thumbs">
          <figure>
            <div class="thumb">
              <img id="thumb-start" alt="кадр в начале отрезка">
              <span class="loading"><i class="spin"></i><span class="loading-text">обновляю…</span></span>
            </div>
            <figcaption>начало <b id="cap-start" class="mono">0:00</b>
              <span class="frame-note" id="note-start"></span></figcaption>
            <div class="stepper" data-field="start">
              <button type="button" data-step="-1" title="начало на секунду раньше">−1 с</button>
              <button type="button" data-step="-frame" title="начало на кадр раньше">− кадр</button>
              <button type="button" data-step="frame" title="начало на кадр позже">+ кадр</button>
              <button type="button" data-step="1" title="начало на секунду позже">+1 с</button>
            </div>
          </figure>
          <figure>
            <div class="thumb">
              <img id="thumb-end" alt="кадр в конце отрезка">
              <span class="loading"><i class="spin"></i><span class="loading-text">обновляю…</span></span>
            </div>
            <figcaption>конец <b id="cap-end" class="mono">0:30</b>
              <span class="frame-note" id="note-end"></span></figcaption>
            <div class="stepper" data-field="end">
              <button type="button" data-step="-1" title="конец на секунду раньше">−1 с</button>
              <button type="button" data-step="-frame" title="конец на кадр раньше">− кадр</button>
              <button type="button" data-step="frame" title="конец на кадр позже">+ кадр</button>
              <button type="button" data-step="1" title="конец на секунду позже">+1 с</button>
            </div>
          </figure>
        </div>
        <div class="preview-bar">
          <button type="button" class="ghost" id="prep-frames" hidden>скачать 20 с</button>
          <button type="button" class="play" id="play-seg">▶ посмотреть отрезок</button>
        </div>
        <video id="player" playsinline controls hidden></video>
        <div class="preview-note" id="preview-note"></div>
      </div>
      <p class="hint" style="margin-top:9px">Таймкоды: <b>45</b>, <b>1:12</b>, <b>0:00:10</b>.<br>
        <b>*</b> во втором поле — от начала до конца ролика.</p>
    </div>

    <div class="modes">
      <label class="switch">
        <input id="fast" type="checkbox">
        <span><strong>без перекодирования</strong><em>быстрее, но границы по ключевому кадру</em></span>
      </label>
      <label class="switch">
        <input id="vertical" type="checkbox">
        <span><strong>вертикально 9:16</strong><em>для смартфона</em></span>
      </label>
      <div class="layout-choice" id="layout-choice" hidden>
        <span>фон:</span>
        <button type="button" class="seg on" data-layout="crop">обрезать бока</button>
        <button type="button" class="seg" data-layout="blur">размытый фон</button>
      </div>
      <div class="crop-pos" id="crop-pos" hidden>
        <span class="crop-label">окно:</span>
        <input id="krop" type="range" min="0" max="100" step="1" value="50"
               aria-label="Положение окна 9:16 по горизонтали">
        <span class="mono" id="krop-val">50%</span>
        <button type="button" class="ghost" id="krop-auto">авто</button>
      </div>
    </div>

    <button id="go" class="primary" disabled>Нарезать</button>
  </section>

  <section class="col-out">
    <div class="out-head">
      <span class="label">Результат</span>
      <div class="out-actions">
        <span class="timer mono" id="timer"></span>
        <button class="ghost stop" id="stop" type="button" hidden>Стоп</button>
      </div>
    </div>
    <div class="status" id="status" role="status" aria-live="polite">Ждёт задание</div>
    <div class="eta" id="eta"></div>
    <ul class="results" id="results"></ul>
    <div class="empty" id="empty">
      <b>Здесь появятся клипы</b>
      <ul class="steps">
        <li><i>1</i><span>файл или ссылка — слева</span></li>
        <li><i>2</i><span>начало и конец отрезка</span></li>
        <li><i>3</i><span>клип можно скачать сразу</span></li>
      </ul>
    </div>
    <div class="panel" id="panel" hidden>
      <div class="panel-head">
        <span class="label" id="panel-title">Статус</span>
        <button class="ghost" id="panel-close" type="button">закрыть</button>
      </div>
      <pre id="panel-text"></pre>
    </div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const drop = $('drop'), fileInput = $('file'), statusEl = $('status'),
      resultsEl = $('results'), timerEl = $('timer'), emptyEl = $('empty'),
      goBtn = $('go'), urlEl = $('url');
let uploadedPath = null, startedAt = 0, tick = null, layout = 'crop';

function checkPageVersion(version) {
  // После перезапуска сервиса в открытой вкладке остаётся старый JS.
  // Замечаем новый отпечаток и перезагружаемся один раз, запомнив его.
  if (!version) return false;
  const seen = sessionStorage.getItem('page_version');
  if (seen && seen !== version) {
    sessionStorage.setItem('page_version', version);
    location.reload();
    return true;
  }
  sessionStorage.setItem('page_version', version);
  return false;
}

function pingService() {
  fetch('/api/health').then(r => r.json()).then(d => {
    if (checkPageVersion(d.version)) return;
    $('svc-text').textContent = d.status === 'ok' ? 'сервис работает' : 'нет связи';
    if (d.status === 'ok') $('svc').classList.add('ok');
  }).catch(() => { $('svc-text').textContent = 'нет связи'; });
}

pingService();
setInterval(pingService, 60000);

function setStatus(text, kind) {
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
  statusEl.textContent = '';
  if (kind === 'busy') {
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    spinner.setAttribute('aria-hidden', 'true');
    statusEl.append(spinner);
  }
  statusEl.append(document.createTextNode(text));
}

const panelEl = $('panel'), panelText = $('panel-text');
const etaEl = $('eta');
let durationPending = false;

function shortReason(text, limit) {
  const cut = limit || 52;
  const line = String(text || '').replace(/\\s+/g, ' ').trim();
  if (!line) return '';
  if (line.length <= cut) return line;
  const clipped = line.slice(0, cut);
  const space = clipped.lastIndexOf(' ');
  return (space > cut * 0.6 ? clipped.slice(0, space) : clipped) + '…';
}

function fmtSeconds(value) {
  const s = Math.round(value);
  if (s < 60) return s + ' с';
  const m = Math.floor(s / 60), rest = s % 60;
  return rest ? m + ' мин ' + rest + ' с' : m + ' мин';
}

function renderEta(job) {
  if (job.state === 'running') {
    if (Array.isArray(job.estimate) && job.estimate.length === 2) {
      const [totalLow, totalHigh] = job.estimate;
      const done = typeof job.fraction === 'number' ? Math.min(Math.max(job.fraction, 0), 1) : 0;
      const rest = 1 - done;
      if (done > 0.02) {
        etaEl.textContent = 'осталось примерно ' + fmtSeconds(totalLow * rest) +
          ' – ' + fmtSeconds(totalHigh * rest);
      } else {
        etaEl.textContent = 'весь клип примерно ' + fmtSeconds(totalLow) +
          ' – ' + fmtSeconds(totalHigh);
      }
    } else {
      // Оценки нет (например, конец «до конца» узнали поздно) — считаем остаток
      // по фактическому прогрессу: за 24 с сделано 75% → осталось ~8 с
      const done = typeof job.fraction === 'number' ? job.fraction : 0;
      const elapsed = (Date.now() - startedAt) / 1000;
      if (done > 0.05 && elapsed > 3) {
        etaEl.textContent = 'осталось примерно ' +
          fmtSeconds(elapsed * (1 - done) / done) + ' (по прогрессу)';
      } else {
        etaEl.textContent = job.stage === 'download'
          ? 'оценку посчитаю, когда узнаю длительность ролика'
          : 'считаю по ходу дела…';
      }
    }
    return;
  }
  if (job.state === 'done') {
    etaEl.textContent = 'заняло ' + fmtSeconds((Date.now() - startedAt) / 1000);
    return;
  }
  if (job.state === 'error') { etaEl.textContent = ''; return; }
  etaEl.textContent = '';
}

function showPanel(title, text) {
  $('panel-title').textContent = title;
  // кнопка уборки относится к прошлому содержимому — иначе остаётся висеть
  panelEl.querySelectorAll('.sweep').forEach(btn => btn.remove());
  panelText.textContent = text;
  panelEl.hidden = false;
  emptyEl.style.display = 'none';
}
function hidePanel() {
  panelEl.hidden = true;
  panelText.textContent = '';
  if (!resultsEl.children.length) emptyEl.style.display = '';
}
$('panel-close').addEventListener('click', hidePanel);

async function openPanel(url, title, method) {
  showPanel(title, 'Загружаю…');
  try {
    const res = await fetch(url, method ? { method } : undefined);
    const data = await res.json();
    showPanel(title, data.text || JSON.stringify(data, null, 2));
  } catch (err) {
    showPanel(title, 'Ошибка: ' + err.message);
  }
}
async function openStatus() {
  showPanel('Статус', 'Загружаю…');
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    showPanel('Статус', data.text || '');
    if (data.expired) addSweepButton(data.expired);
  } catch (err) {
    showPanel('Статус', 'Ошибка: ' + err.message);
  }
}

function addSweepButton(count) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ghost danger sweep';
  btn.textContent = 'Удалить просроченные (' + count + ')';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Убираю…';
    try {
      const res = await fetch('/api/sweep', { method: 'POST' });
      const data = await res.json();
      showPanel('Статус', data.text || data.error || '');
      if (data.expired) addSweepButton(data.expired);
    } catch (err) {
      showPanel('Статус', 'Ошибка: ' + err.message);
    }
  });
  panelEl.querySelector('.panel-head').after(btn);
}

$('btn-status').addEventListener('click', openStatus);
$('btn-info').addEventListener('click', () => openPanel('/api/info', 'Справка'));
const confirmEl = $('confirm');
let confirmResolve = null;

function askConfirm(title, text, okLabel) {
  if (title) $('confirm-title').textContent = title;
  if (text) $('confirm-text').textContent = text;
  $('confirm-yes').textContent = okLabel || 'Удалить';
  confirmEl.hidden = false;
  $('confirm-no').focus();
  return new Promise(resolve => { confirmResolve = resolve; });
}
function closeConfirm(answer) {
  confirmEl.hidden = true;
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(answer);
}
$('confirm-yes').addEventListener('click', () => closeConfirm(true));
$('confirm-no').addEventListener('click', () => closeConfirm(false));
confirmEl.addEventListener('click', (e) => { if (e.target === confirmEl) closeConfirm(false); });
document.addEventListener('keydown', (e) => {
  if (confirmEl.hidden) return;
  if (e.key === 'Escape') { e.preventDefault(); closeConfirm(false); }
  if (e.key === 'Enter') { e.preventDefault(); closeConfirm(true); }
});

const promptEl = $('prompt'), promptInput = $('prompt-input'), promptHint = $('prompt-hint');
let promptResolve = null;

function askPrompt(title, value, message) {
  $('prompt-title').textContent = title;
  promptInput.value = value;
  promptHint.textContent = message || '30 — минут, 1:30 — час тридцать, 2ч — два часа.';
  promptHint.style.color = message ? 'var(--err)' : '';
  promptEl.hidden = false;
  promptInput.focus();
  promptInput.select();
  return new Promise(resolve => { promptResolve = resolve; });
}
function closePrompt(value) {
  promptEl.hidden = true;
  const resolve = promptResolve;
  promptResolve = null;
  if (resolve) resolve(value);
}
$('prompt-no').addEventListener('click', () => closePrompt(null));
$('prompt-yes').addEventListener('click', () => closePrompt(promptInput.value.trim()));
promptEl.addEventListener('click', (e) => { if (e.target === promptEl) closePrompt(null); });
promptInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); closePrompt(promptInput.value.trim()); }
});
document.addEventListener('keydown', (e) => {
  if (promptEl.hidden) return;
  if (e.key === 'Escape') { e.preventDefault(); closePrompt(null); }
});

$('btn-keep').addEventListener('click', async () => {
  let current = 30, message = '';
  try {
    const info = await (await fetch('/api/retention')).json();
    current = info.minutes || 30;
  } catch (err) { /* остаётся значение по умолчанию */ }
  for (;;) {
    const value = await askPrompt('Хранение файлов', String(current), message);
    if (value === null || value === '') return;
    try {
      const res = await fetch('/api/retention', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ value })
      });
      const data = await res.json();
      if (!res.ok) { message = data.error || 'не понял значение'; continue; }
      showPanel('Хранение', data.text);
      return;
    } catch (err) {
      message = 'нет связи: ' + err.message;
    }
  }
});

$('btn-clean').addEventListener('click', async () => {
  if (!(await askConfirm('Очистить всё?',
      'Все скачанные оригиналы и готовые клипы будут удалены. Отменить это нельзя.',
      'Удалить'))) return;
  showPanel('Очистка', 'Удаляю…');
  try {
    const res = await fetch('/api/clean', { method: 'POST' });
    const data = await res.json();
    resultsEl.innerHTML = '';
    setStatus('Файлы удалены');
    showPanel('Очистка', data.text || 'готово');
  } catch (err) {
    showPanel('Очистка', 'Ошибка: ' + err.message);
  }
});

const stopBtn = $('stop');
let currentJob = null;

stopBtn.addEventListener('click', async () => {
  if (!currentJob) return;
  stopBtn.disabled = true;
  setStatus('Останавливаю…', 'busy');
  try {
    const res = await fetch('/api/cancel/' + currentJob, { method: 'POST' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setStatus(data.error || 'не удалось остановить', 'err');
    }
  } catch (err) {
    setStatus('нет связи: ' + err.message, 'err');
  } finally {
    setTimeout(() => { stopBtn.disabled = false; }, 1200);
  }
});

function renderProgress(job) {
  // Полосу заменила вертушка перед текстом: она вращается, пока идёт работа,
  // а процент и стадия и так написаны рядом.
  statusEl.classList.toggle('working', job.state === 'running');
}

const choiceEl = $('layout-choice');
const cropPosEl = $('crop-pos'), kropEl = $('krop'), kropValEl = $('krop-val');

$('vertical').addEventListener('change', (e) => {
  choiceEl.hidden = !e.target.checked;
  cropPosEl.hidden = !e.target.checked;
});
function setKrop(percent) {
  const value = Math.max(0, Math.min(100, Math.round(percent)));
  kropEl.value = String(value);
  kropValEl.textContent = value + '%';
}
kropEl.addEventListener('input', () => setKrop(Number(kropEl.value)));

function currentSource() {
  return (urlEl.value.trim() || uploadedPath || '');
}

let kropTimer = null;
async function loadKrop() {
  const source = currentSource();
  clearTimeout(kropTimer);
  if (!source) { setKrop(50); return; }
  kropTimer = setTimeout(async () => {
    try {
      const res = await fetch('/api/vertical?source=' + encodeURIComponent(source));
      const data = await res.json();
      if (currentSource() !== source) return;      // пока грузили, источник сменили
      setKrop((data.horizontal ?? 0.5) * 100);
      if (data.saved) setStatus('Для этого источника запомнено положение окна ' +
        Math.round(data.horizontal * 100) + '%');
    } catch (err) { /* позиция не критична */ }
  }, 500);
}
urlEl.addEventListener('input', loadKrop);

$('krop-auto').addEventListener('click', async () => {
  const source = currentSource();
  if (!source) { setStatus('Сначала выбери файл или ссылку', 'err'); return; }
  const button = $('krop-auto');
  button.disabled = true;
  button.textContent = 'смотрю…';
  try {
    const res = await fetch('/api/vertical/auto?source=' + encodeURIComponent(source));
    const data = await res.json();
    setKrop((data.horizontal ?? 0.5) * 100);
    setStatus(data.note || 'позиция подобрана');
  } catch (err) {
    setStatus('авто не сработало: ' + err.message, 'err');
  } finally {
    button.disabled = false;
    button.textContent = 'авто';
  }
});
document.querySelectorAll('.seg').forEach(btn => {
  btn.addEventListener('click', () => {
    layout = btn.dataset.layout;
    document.querySelectorAll('.seg').forEach(x => x.classList.toggle('on', x === btn));
  });
});

function refreshGo() {
  goBtn.disabled = !(urlEl.value.trim() || uploadedPath);
}
urlEl.addEventListener('input', refreshGo);
refreshGo();

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return n + ' ' + one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return n + ' ' + few;
  return n + ' ' + many;
}

function formatSeconds(value) {
  return value.toFixed(2).replace('.', ',') + ' с';
}

function formatSize(bytes) {
  const mb = bytes / 1048576;
  if (mb >= 1) return mb.toFixed(1).replace('.', ',') + ' МБ';
  return Math.round(bytes / 1024) + ' КБ';
}

function modeName(mode) {
  if (mode.startsWith('whole')) return 'целиком';
  if (mode.includes('vertical-blur')) return '9:16 с фоном';
  if (mode.includes('vertical')) return '9:16';
  if (mode === 'fast') return 'без перекодирования';
  return 'точный';
}

function toSeconds(text) {
  const raw = String(text).trim();
  if (!raw) return 0;
  // Точка — не разделитель частей, а начало долей секунды (до трёх цифр).
  // «0:29.1000» (так раньше выглядел шаг кадра) — ошибка, а не 29 мин 1000 с.
  let body = raw, frac = 0;
  // точку ищем строковыми средствами: регулярка в HTML-строке Python теряет «\\»
  const dotAt = raw.lastIndexOf('.');
  if (dotAt !== -1) {
    const digits = raw.slice(dotAt + 1);
    if (digits.length > 3) return NaN;
    if (digits && !Number.isFinite(Number(digits))) return NaN;
    frac = digits ? Number('0.' + digits) : 0;
    body = raw.slice(0, dotAt);
  }
  // запятую, слэш и обратную косую черту считаем двоеточием
  let normal = body.split(String.fromCharCode(92)).join(':');
  normal = normal.split(',').join(':').split('/').join(':');
  const parts = normal.split(':').filter(part => part !== '');
  if (!parts.length) return frac;
  const nums = parts.map(part => {
    const value = parseFloat(part);
    return Number.isFinite(value) ? value : NaN;
  });
  if (nums.some(value => !Number.isFinite(value))) return NaN;
  if (nums.length === 1) return nums[0] + frac;
  if (nums.length === 2) return nums[0] * 60 + nums[1] + frac;
  return nums[0] * 3600 + nums[1] * 60 + nums[2] + frac;
}

function tcProblem(text) {
  if (!String(text).trim() || String(text).trim() === '*') return '';
  if (Number.isFinite(toSeconds(text))) return '';
  return 'Таймкод «' + String(text).trim() + '» не понят: после точки — до трёх цифр (доли секунды)';
}

function startTimer() {
  hidePanel();
  startedAt = Date.now();
  clearInterval(tick);
  tick = setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    timerEl.textContent = 'обработка ' + (s >= 60 ? Math.floor(s / 60) + ' мин ' : '') + (s % 60) + ' с';
  }, 1000);
}
function stopTimer() { clearInterval(tick); tick = null; }

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
fileInput.addEventListener('change', () => { if (fileInput.files[0]) upload(fileInput.files[0]); });
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('over'); }));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', ev => { const f = ev.dataTransfer.files[0]; if (f) upload(f); });

async function upload(file) {
  const mb = Math.round(file.size / 1048576);
  drop.classList.add('has-file');
  $('drop-title').textContent = file.name;
  $('drop-sub').textContent = 'загружаю · ' + mb + ' МБ';
  setStatus('Загружаю файл…', 'busy');
  try {
    const res = await fetch('/api/upload?name=' + encodeURIComponent(file.name), { method: 'POST', body: file });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'ошибка загрузки');
    uploadedPath = data.path;
    urlEl.value = '';
    $('drop-sub').textContent = 'файл готов · ' + mb + ' МБ';
    setStatus('Файл загружен — задай диапазон и нажми «Нарезать»');
    loadMedia();
  } catch (err) {
    drop.classList.remove('has-file');
    $('drop-title').textContent = 'Выбери файл или перетащи';
    $('drop-sub').textContent = 'mp4, mov, mkv, webm · до 4 ГБ';
    setStatus(err.message, 'err');
  }
  refreshGo();
}

// Кнопок-пресетов больше нет: длительность вводится таймкодом, а подсветка
// «какой пресет выбран» потеряла смысл — оставляем пустые заглушки для вызовов.
function markPreset() {}
document.querySelector('.presets .reset').addEventListener('click', () => {
  $('start').value = '0:00'; $('end').value = '0:30';
  segList = []; renderSegments();
});

let segList = [];
const segListEl = $('segments'), segCountEl = $('seg-count');

function segLength(seg) {
  if (seg.end === '*') return null;
  const len = toSeconds(seg.end) - toSeconds(seg.start);
  return Number.isFinite(len) && len > 0 ? len : null;
}

function renderSegments() {
  segListEl.innerHTML = '';
  segList.forEach((seg, index) => {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = seg.start + ' – ' + seg.end;
    label.title = 'вернуть в поля';
    label.addEventListener('click', () => {
      $('start').value = seg.start;
      $('end').value = seg.end;
      segList.splice(index, 1);
      renderSegments();
      syncPreset();
    });
    const del = document.createElement('button');
    del.type = 'button';
    del.textContent = '✕';
    del.title = 'убрать отрезок';
    del.addEventListener('click', () => { segList.splice(index, 1); renderSegments(); });
    li.append(label, del);
    segListEl.append(li);
  });
  segListEl.hidden = segList.length === 0;
  if (!segList.length) { segCountEl.textContent = ''; return; }
  const total = segList.reduce((sum, seg) => sum + (segLength(seg) || 0), 0);
  const unknown = segList.some(seg => segLength(seg) === null);
  segCountEl.textContent = 'в списке ' + plural(segList.length, 'отрезок', 'отрезка', 'отрезков') +
    (total > 0 ? ' · ' + fmtSeconds(total) : '') + (unknown ? ' + до конца' : '');
}

$('add-seg').addEventListener('click', () => {
  const start = $('start').value.trim(), end = $('end').value.trim();
  if (!start || !end) { setStatus('Заполни начало и конец, потом добавляй', 'err'); return; }
  const bad = tcProblem(start) || tcProblem(end);
  if (bad) { setStatus(bad, 'err'); return; }
  if (end !== '*' && toSeconds(end) <= toSeconds(start)) {
    setStatus('Конец должен быть больше начала', 'err'); return;
  }
  segList.push({ start, end });
  $('start').value = end === '*' ? '' : end;
  $('end').value = '';
  renderSegments();
  setStatus('Отрезок в списке — добавь ещё или нажми «Нарезать»');
});
function syncPreset() {}
[$('start'), $('end')].forEach(el => el.addEventListener('input', syncPreset));
syncPreset();

const previewEl = $('preview'), playerEl = $('player'), previewNoteEl = $('preview-note');
let mediaInfo = null, previewTimer = null;

function frameStep() { return mediaInfo && mediaInfo.fps ? 1 / mediaInfo.fps : 0.04; }

function fmtTC(seconds) {
  // считаем в целых миллисекундах: иначе округление давало «.1000»
  const totalMs = Math.round(Math.max(0, seconds) * 1000);
  const m = Math.floor(totalMs / 60000);
  const restMs = totalMs - m * 60000;
  const whole = Math.floor(restMs / 1000);
  const ms = restMs % 1000;
  const tail = String(whole).padStart(2, '0') +
    (ms ? '.' + String(ms).padStart(3, '0') : '');
  return m + ':' + tail;
}

let frameUntil = null;   // конец отрезка: окно накрывает обе рамки одной загрузкой

function frameUrl(tc) {
  const t = Math.max(0, toSeconds(tc));
  let url = '/api/frame?source=' + encodeURIComponent(currentSource()) +
    '&t=' + t.toFixed(3) + '&w=360';
  if (typeof frameUntil === 'number' && frameUntil > t) {
    url += '&until=' + frameUntil.toFixed(3);
  }
  return url + '&_=' + Date.now();
}

async function loadFrame(img, tc, onMissing, noteEl, suffix) {
  // Ответы на частые клики приходят вперемешку: номер запроса не даёт
  // устаревшему кадру перебить свежий и стереть уже показанную картинку.
  const seq = String(Number(img.dataset.seq || 0) + 1);
  img.dataset.seq = seq;
  const fresh = () => img.dataset.seq === seq;
  const mark = (text) => { if (noteEl && fresh()) noteEl.textContent = text; };

  // Кадр по ссылке идёт 5–10 с, и всё это время на экране старая картинка —
  // без пометки она выглядит как уже обновлённая. Показываем поверх затемнение,
  // вертушку и счётчик секунд; локальный файл отвечает мгновенно — не мигаем.
  const holder = img.closest('.thumb');
  const label = holder && holder.querySelector('.loading-text');
  const began = Date.now();
  let ticker = null, warmup = null;
  const paint = () => {
    if (!label) return;
    const secs = Math.floor((Date.now() - began) / 1000);
    label.textContent = secs > 0 ? 'обновляю… ' + secs + ' с' : 'обновляю…';
  };
  const showBusy = () => {
    warmup = null;
    if (!holder) return;
    holder.classList.add('busy');
    paint();
    ticker = setInterval(paint, 500);
  };
  const hideBusy = () => {
    if (warmup) { clearTimeout(warmup); warmup = null; }
    if (ticker) { clearInterval(ticker); ticker = null; }
    if (deadline) { clearTimeout(deadline); deadline = null; }
    if (holder) holder.classList.remove('busy');
  };
  let deadline = null;
  warmup = setTimeout(showBusy, 160);
  // страховка на зависшее соединение: сервер укладывается в 45–60 с, но если ответ
  // вообще не придёт, вертушка не должна крутиться минутами
  deadline = setTimeout(() => {
    if (fresh()) { hideBusy(); mark('· сервер не успел за минуту — нажми ещё раз'); }
  }, 60000);

  try {
    const res = await fetch(frameUrl(tc));
    if (!fresh()) return;
    if (!res.ok) {
      hideBusy();
      const data = await res.json().catch(() => ({}));
      // старый кадр не стираем, но и молчать нельзя: причина от сервера важнее
      // общей фразы — она говорит, что именно делать
      mark('· ' + (shortReason(data.error) || 'кадр не получен'));
      if (onMissing) onMissing(data.error || '');
      return;
    }
    const blob = await res.blob();
    if (!fresh()) return;
    const previous = img.dataset.url;
    const local = URL.createObjectURL(blob);
    img.dataset.url = local;
    img.src = local;
    img.alt = 'кадр ' + tc;
    img.onload = () => {
      if (previous && previous !== local) URL.revokeObjectURL(previous);
      img.onload = null;
    };
    mark(res.headers.get('X-Frame-Approx') ? '· ближайший кадр' : (suffix || ''));
  } catch (err) {
    mark('· сеть не ответила — нажми ещё раз');
    if (onMissing) onMissing('');
  }
  // busy снимаем только свежий запрос: иначе он погасит вертушку более нового
  if (fresh()) hideBusy();
}

function offerWindowDownload(message) {
  $('prep-frames').hidden = false;
  previewNoteEl.textContent = message ||
    'сайт не отдал кадр — можно скачать короткий участок (20 с) и посмотреть по нему';
}

function refreshThumbs() {
  const source = currentSource();
  if (!source) { previewEl.hidden = true; return; }
  const start = $('start').value.trim(), end = $('end').value.trim();
  const startSec = Math.max(0, toSeconds(start || '0'));
  const endSec = end && end !== '*' ? toSeconds(end) : null;
  $('cap-start').textContent = start || '0:00';
  $('cap-end').textContent = end || '—';
  // точные границы известны — не гоняем сервер за кадром, которого нет
  const beyond = (seconds) => mediaInfo && mediaInfo.duration && !mediaInfo.window &&
    seconds > mediaInfo.duration + 0.05;
  const missing = (error) => {
    // если кадр не выйдет в принципе (видео закрыто, ссылка битая или скопирована
    // с ошибкой), кнопка «скачать 20 с» только сбивает с толку
    if (error && /обрезан|недоступн|символ/i.test(error)) {
      $('prep-frames').hidden = true;
      previewNoteEl.textContent = error;
      return;
    }
    offerWindowDownload(error || '');
  };
  frameUntil = (endSec !== null) ? endSec
    : (mediaInfo && mediaInfo.duration ? mediaInfo.duration : null);
  if (beyond(startSec)) {
    $('note-start').textContent = '· за концом ролика (' + fmtTC(mediaInfo.duration) + ')';
  } else {
    loadFrame($('thumb-start'), start || '0', missing, $('note-start'));
  }
  if (endSec === null) {
    if (end === '*') {
      // «до конца»: показываем последний кадр ролика, а не пустую рамку
      if (mediaInfo && mediaInfo.duration) {
        const last = Math.max(0, mediaInfo.duration - 0.1);
        loadFrame($('thumb-end'), fmtTC(last), missing, $('note-end'), '· последний кадр ролика');
      } else {
        $('note-end').textContent = '· длительность ещё не знаю';
        if (!durationPending && source) {
          durationPending = true;
          durationOf(source).then((d) => {
            durationPending = false;
            if (d) loadMedia();
          });
        }
      }
    } else {
      const endImg = $('thumb-end');
      endImg.removeAttribute('src');
      endImg.alt = 'конец не задан';
      $('note-end').textContent = '';
    }
  } else if (beyond(endSec)) {
    $('note-end').textContent = '· за концом ролика (' + fmtTC(mediaInfo.duration) + ')';
  } else {
    loadFrame($('thumb-end'), end, missing, $('note-end'));
  }
}

let preparingFor = null;
const preparedKeys = new Set();

async function loadMedia() {
  const source = currentSource();
  if (!source) { previewEl.hidden = true; mediaInfo = null; return; }
  try {
    const res = await fetch('/api/media?source=' + encodeURIComponent(source) +
      '&t=' + Math.max(0, toSeconds($('start').value.trim() || '0')).toFixed(2));
    const data = await res.json();
    if (!res.ok) {
      previewEl.hidden = true;
      mediaInfo = null;
      previewNoteEl.textContent = data.error || '';
      return;
    }
    mediaInfo = data;
    previewEl.hidden = false;
    if (data.link_problem) {
      // заведомо битая ссылка: не дёргаем сервер и говорим прямо
      previewNoteEl.textContent = data.link_problem;
      $('prep-frames').hidden = true;
      $('play-seg').disabled = true;
      $('thumb-start').removeAttribute('src');
      $('thumb-end').removeAttribute('src');
      $('note-start').textContent = '';
      $('note-end').textContent = '';
      return;
    }
    // Ссылку тоже можно слушать: сервер сам подготовит короткий участок.
    $('play-seg').disabled = false;
    $('play-seg').title = data.needs_download
      ? 'первое нажатие скачает короткий участок (10–20 с), дальше играет сразу'
      : '';
    if (data.needs_download) {
      // файл не нужен: кадр берётся из потока прямо на сервере
      previewNoteEl.textContent = 'кадры тяну из потока — файл скачивать не нужно';
      refreshThumbs();
      return;
    }
    previewNoteEl.textContent = data.window
      ? (data.unknown_duration
          ? 'кадры из участка ' + fmtTC(data.offset) + '–' + fmtTC(data.window_end) +
            ' · длительность ролика сайт не отдал'
          : 'кадры из короткого участка · ролик ' + fmtSeconds(data.duration))
      : (data.duration
          ? 'в ролике ' + fmtSeconds(data.duration) +
            (data.fps ? ' · ' + data.fps.toFixed(2).replace('.', ',') + ' кадр/с' : '')
          : '');
    refreshThumbs();
  } catch (err) {
    previewEl.hidden = true;
  }
}

async function preparePreview(atSeconds, untilSeconds) {
  const source = currentSource();
  const t = Math.max(0, atSeconds === undefined ? toSeconds($('start').value.trim() || '0') : atSeconds);
  const until = (untilSeconds === undefined || untilSeconds === null) ? null : untilSeconds;
  const key = source + '@' + Math.round(t) + '-' + (until === null ? 'auto' : Math.round(until));
  if (!source || preparingFor || preparedKeys.has(key)) return;
  preparingFor = key;
  preparedKeys.add(key);
  previewNoteEl.textContent = 'скачиваю короткий участок для кадров — обычно 20–40 с';
  setStatus('Готовлю кадры: качаю первые 20 секунд ролика', 'busy');
  try {
    const res = await fetch('/api/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source, t, until})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'не удалось скачать участок');
    setStatus('Кадры готовы · участок ' + fmtSeconds(data.start) + '–' + fmtSeconds(data.end));
  } catch (err) {
    previewNoteEl.textContent = 'кадры не получились: ' + err.message;
    setStatus('Кадры не получились: ' + err.message, 'err');
    preparingFor = null;
    return;
  }
  preparingFor = null;
  loadMedia();
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadMedia, 350);
}

urlEl.addEventListener('input', schedulePreview);
[$('start'), $('end')].forEach(el => el.addEventListener('input', schedulePreview));
urlEl.addEventListener('blur', schedulePreview);

async function durationOf(source) {
  if (mediaInfo && mediaInfo.duration) return mediaInfo.duration;
  try {
    const res = await fetch('/api/duration?source=' + encodeURIComponent(source));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'сайт не сказал длительность');
    mediaInfo = Object.assign(mediaInfo || {}, data);
    return data.duration || 0;
  } catch (err) {
    setStatus('Не удалось узнать длительность: ' + err.message, 'err');
    return 0;
  }
}

async function stepField(field, delta) {
  const input = field === 'end' ? $('end') : $('start');
  const raw = input.value.trim();
  let base;
  let fromEnd = false;
  if (raw === '*') {
    // «до конца» — шагать не от чего, пока не знаем длительность ролика
    if (field !== 'end') {
      setStatus('В начале «до конца» не бывает — впиши время', 'err');
      return;
    }
    const source = currentSource();
    if (!source) { setStatus('Сначала выбери файл или ссылку', 'err'); return; }
    setStatus('Узнаю длительность ролика…', 'busy');
    const duration = await durationOf(source);
    if (!duration) {
      setStatus('Длительность неизвестна — впиши конец вручную', 'err');
      return;
    }
    base = duration;
    fromEnd = true;
  } else {
    base = toSeconds(raw || '0');
    if (!Number.isFinite(base)) {
      setStatus(tcProblem(raw), 'err');
      return;
    }
  }
  const known = (mediaInfo && mediaInfo.duration) || 0;
  let next = Math.max(0, base + delta);
  if (known && next > known) next = known;
  const startSec = toSeconds($('start').value.trim() || '0');
  if (field === 'end' && next <= startSec) {
    next = startSec + Math.abs(delta) || startSec + frameStep();
    setStatus('Конец раньше начала не бывает — поставил ' + fmtTC(next), 'err');
  } else if (next === base && delta > 0) {
    setStatus('Конец уже в конце ролика — дальше некуда, шагай назад', 'err');
    return;
  }
  input.value = fmtTC(next);
  syncPreset();
  schedulePreview();
  if (fromEnd) {
    setStatus('«До конца» заменено на точный конец: ' + input.value +
      ' (ролик длится ' + fmtTC(known) + ')');
  } else {
    setStatus('Правлю ' + (field === 'end' ? 'конец' : 'начало') + ': ' + input.value);
  }
}

document.querySelectorAll('.preview .stepper').forEach(box => {
  const field = box.dataset.field;
  box.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      const raw = btn.dataset.step;
      const delta = raw === 'frame' ? frameStep()
        : raw === '-frame' ? -frameStep()
        : Number(raw);
      stepField(field, delta);
    });
  });
});

$('prep-frames').addEventListener('click', () => {
  const startSec = Math.max(0, toSeconds($('start').value.trim() || '0'));
  const endRaw = $('end').value.trim();
  const endSec = endRaw && endRaw !== '*' ? toSeconds(endRaw) : null;
  $('prep-frames').hidden = true;
  preparePreview(startSec, endSec);
});

// Мобильные браузеры молча игнорируют seek, пока файл не готов: пробуем, пока не встанет.
function seekPlayer(target, token) {
  let tries = 0;
  const attempt = () => {
    if (playerEl.dataset.token !== token) return;
    try { playerEl.currentTime = target; } catch (_) {}
    if (Math.abs(playerEl.currentTime - target) > 0.3 && tries < 10) {
      tries += 1;
      setTimeout(attempt, 180);
    }
  };
  attempt();
}

$('play-seg').addEventListener('click', async () => {
  const source = currentSource();
  if (!source) { setStatus('Сначала выбери файл или ссылку', 'err'); return; }
  const startText = $('start').value.trim() || '0:00';
  const endText = $('end').value.trim();
  const start = toSeconds(startText);
  const end = (!endText || endText === '*')
    ? (mediaInfo && mediaInfo.duration ? mediaInfo.duration : 0)
    : toSeconds(endText);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    setStatus('Конец должен быть больше начала', 'err');
    return;
  }
  // Сервер один раз решает, какой файл играть и с каким смещением. Раньше страница
  // угадывала смещение из паспорта, а файл мог прийти другой — отрезок играл
  // с прежнего места при смене начала.
  previewNoteEl.textContent = 'готовлю отрезок…';
  let play;
  try {
    const res = await fetch('/api/preplay?source=' + encodeURIComponent(source) +
      '&t=' + start.toFixed(3) + '&until=' + end.toFixed(3) + '&_=' + Date.now());
    play = await res.json();
    if (!res.ok) throw new Error(play.error || 'не удалось открыть отрезок');
  } catch (err) {
    setStatus(err.message, 'err');
    previewNoteEl.textContent = '';
    return;
  }
  const offset = Number(play.offset) || 0;
  const localStart = Math.max(0, start - offset);
  const localEnd = Math.max(localStart + 0.2, end - offset);
  const token = String(Date.now()) + Math.random();
  playerEl.dataset.token = token;
  playerEl.dataset.from = String(localStart);
  playerEl.dataset.to = String(localEnd);
  playerEl.hidden = false;
  playerEl.onloadedmetadata = () => {
    if (playerEl.dataset.token !== token) return;
    seekPlayer(localStart, token);
  };
  playerEl.ontimeupdate = () => {
    if (playerEl.dataset.token !== token) return;
    const stop = Number(playerEl.dataset.to || 0);
    if (stop && playerEl.currentTime >= stop) {
      playerEl.pause();
      playerEl.currentTime = Math.max(Number(playerEl.dataset.from || 0), stop - 0.05);
    }
  };
  // После await браузер уже не считает вызов жестом: стартуем без звука
  // (это разрешено), а звук включаем, как только воспроизведение пошло.
  playerEl.muted = true;
  playerEl.src = play.url + '&_=' + Date.now();
  playerEl.load();
  playerEl.play().then(() => {
    playerEl.muted = false;
  }).catch(() => {
    playerEl.muted = false;
    previewNoteEl.textContent = 'браузер не дал автозапуск — нажми ▶ в плеере';
  });
  previewNoteEl.textContent = 'играю ' + fmtTC(start) + ' – ' + fmtTC(start + (end - start)) +
    ' — это тот же отрезок, что уйдёт в нарезку';
});

async function run() {
  resultsEl.innerHTML = '';
  emptyEl.style.display = 'none';
  const url = urlEl.value.trim();
  const source = url || uploadedPath;
  if (!source) { setStatus('Нужен файл или ссылка', 'err'); return; }
  let segments;
  if (segList.length) {
    segments = segList.slice();
  } else {
    const start = $('start').value.trim(), end = $('end').value.trim();
    if (!start || !end) { setStatus('Нужны начало и конец', 'err'); emptyEl.style.display = ''; return; }
    const bad = tcProblem(start) || tcProblem(end);
    if (bad) { setStatus(bad, 'err'); emptyEl.style.display = ''; return; }
    segments = [{ start, end }];
  }
  const body = {
    source,
    segments,
    fast: $('fast').checked,
    vertical: $('vertical').checked,
    layout: $('vertical').checked ? layout : 'crop',
    horizontal: $('vertical').checked ? Number(kropEl.value) / 100 : 0.5
  };
  goBtn.disabled = true; goBtn.textContent = 'Режу…';
  setStatus('Ставлю задание в очередь', 'busy');
  startTimer();
  etaEl.textContent = 'считаю оценку…';
  try {
    const res = await fetch('/api/clip', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'не удалось запустить');
    if (body.vertical) {
      // запомним выбранное окно для этого источника — в следующий раз подставится
      fetch('/api/vertical', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ source, horizontal: body.horizontal })
      }).catch(() => {});
    }
    await poll(data.job_id);
  } catch (err) {
    setStatus(err.message, 'err');
    emptyEl.style.display = '';
  } finally {
    stopTimer();
    goBtn.textContent = 'Нарезать';
    refreshGo();
  }
}

goBtn.addEventListener('click', run);
[urlEl, $('start'), $('end')].forEach(el => el.addEventListener('keydown', e => { if (e.key === 'Enter') run(); }));

async function poll(id) {
  currentJob = id;
  for (;;) {
    const job = await (await fetch('/api/jobs/' + id)).json();
    stopBtn.hidden = job.state !== 'running';
    renderProgress(job);
    renderEta(job);
    if (job.state === 'running') {
      const percent = typeof job.fraction === 'number' ? ' · ' + Math.round(job.fraction * 100) + '%' : '';
      const stage = { download: 'Скачиваю', encode: 'Кодирую' }[job.stage] || 'Работаю';
      const detail = job.detail ? ' · ' + job.detail : '';
      setStatus(stage + percent + detail, 'busy');
    }
    if (job.state === 'cancelled') {
      stopBtn.hidden = true;
      renderProgress(job);
      etaEl.textContent = '';
      timerEl.textContent = '';
      setStatus('Остановлено', 'err');
      emptyEl.style.display = 'none';
      return;
    }
    if (job.state === 'error') {
      stopBtn.hidden = true;
      renderProgress(job);
      renderEta(job);
      setStatus(job.error || 'ошибка', 'err');
      showPanel('Ошибка', job.error_details || job.error || 'ошибка');
      return;
    }
    if (job.state === 'done') {
      renderProgress(job);
      renderEta(job);
      setStatus('Готово · ' + plural(job.results.length, 'файл', 'файла', 'файлов'));
      job.results.forEach((r, i) => {
        const li = document.createElement('li');

        const left = document.createElement('div');
        const link = document.createElement('a');
        link.className = 'file';
        link.href = '/api/download/' + id + '/' + i;
        link.textContent = r.filename;
        const meta = document.createElement('div');
        meta.className = 'meta mono';
        meta.textContent = formatSeconds(r.duration) + ' · ' + modeName(r.mode);
        left.append(link, meta);

        const size = document.createElement('span');
        size.className = 'size';
        size.textContent = formatSize(r.size_bytes);

        const get = document.createElement('a');
        get.className = 'get';
        get.href = '/api/download/' + id + '/' + i;
        get.textContent = 'скачать';

        li.append(left, size, get);
        resultsEl.appendChild(li);
      });
      if (job.results.length > 1) {
        const zipLi = document.createElement('li');
        zipLi.className = 'zip';
        const info = document.createElement('div');
        info.className = 'meta';
        info.textContent = 'все ' + plural(job.results.length, 'клип', 'клипа', 'клипов') + ' одним архивом';
        const zip = document.createElement('a');
        zip.className = 'get';
        zip.href = '/api/zip/' + id;
        zip.textContent = 'скачать zip';
        zipLi.append(info, zip);
        resultsEl.appendChild(zipLi);
      }
      hidePanel();
      emptyEl.style.display = job.results.length ? 'none' : '';
      timerEl.textContent = '';
      return;
    }
    await new Promise(r => setTimeout(r, 700));
  }
}
</script>
</body>
</html>"""

# job_id -> {"state": running|done|error, "results": [...], "error": str, "message": str, "files": [paths]}
JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


class JobControl:
    """Per-job cancellation: the stop flag plus the ffmpeg processes it started."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self._procs: list[Any] = []
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        return self.event.is_set()

    def register(self, proc: Any) -> None:
        with self._lock:
            self._procs.append(proc)

    def stop(self) -> None:
        self.event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — процесс мог уже завершиться
                pass

    def reset(self) -> None:
        self.event.clear()


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "state": "running",
            "results": [],
            "error": "",
            "message": "Подготовка...",
            "stage": "",
            "fraction": None,
            "detail": "",
            "files": [],
            "control": JobControl(),
        }
    return job_id


def _job_update(job_id: str, **kwargs: Any) -> None:
    with _JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kwargs)


def _job_snapshot(job_id: str) -> dict[str, Any] | None:
    """JSON-safe copy: the control object must never leave the process."""
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        return {key: value for key, value in job.items() if key != "control"} if job else None


def _job_control(job_id: str) -> "JobControl | None":
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        control = job.get("control") if job else None
        return control if isinstance(control, JobControl) else None


def _saved_position(cfg: Config, source: str) -> float | None:
    from .storage import load_positions

    if not source:
        return None
    return load_positions(cfg.vertical_map_path()).get(source)


def _remember_position(cfg: Config, source: str, horizontal: float) -> None:
    from .cut import clamp_horizontal
    from .storage import save_position

    if source:
        save_position(cfg.vertical_map_path(), source, clamp_horizontal(horizontal))


def auto_horizontal(source: str, cfg: Config) -> tuple[float, str]:
    """Find the 9:16 window by the content inside the frame (ignores black bars).

    Works on a local file or an already cached download: pulling a fresh copy just
    to look at one frame would cost more than the whole cut.
    """
    from .cut import clamp_horizontal
    from .probe import probe_media
    from .source import find_cached_source, is_url

    if not source:
        return 0.5, "сначала выбери файл или ссылку"
    if is_url(source):
        cached = find_cached_source(Path(cfg.cache_dir).expanduser(), source, cfg.retention_minutes)
        if cached is None:
            return 0.5, "ролик ещё не скачан — авто посмотрит его после первой нарезки"
        path = cached
    else:
        path = Path(source).expanduser()
    if not path.is_file():
        return 0.5, "файл не найден"
    try:
        info = probe_media(str(path), cfg.ffprobe_path)
    except Exception as exc:  # noqa: BLE001
        return 0.5, f"не смог прочитать файл: {exc}"
    if not info.width or not info.height:
        return 0.5, "не знаю размеров кадра"
    window_w = round(info.height * 9 / 16)
    if window_w >= info.width:
        return 0.5, "кадр и так уже 9:16 — центрировать нечего"
    started = max(0.5, (info.duration or 0) * 0.3)
    cmd = [
        cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "info",
        "-ss", f"{started:.2f}", "-i", str(path), "-frames:v", "24",
        "-vf", "cropdetect=24:2:0", "-f", "null", "-",
    ]
    try:
        import subprocess

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 0.5, f"ffmpeg не смог посмотреть кадр: {exc}"
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr or "")
    if not matches:
        return 0.5, "границы содержимого не разглядел — оставил центр"
    width, _height, left, _top = (int(part) for part in matches[-1])
    if width >= info.width - 2:
        return 0.5, "чёрных полей нет — окно по центру"
    center = left + width / 2
    span = info.width - window_w
    position = clamp_horizontal((center - window_w / 2) / span if span > 0 else 0.5)
    return position, f"содержимое {width}px с {left}px → окно {position * 100:.0f}%"


# Сайт отказывает не мгновенно: после бот-чека бессмысленно долбить его каждым кадром.
_STREAM_COOLDOWN: dict[str, tuple[float, str]] = {}
STREAM_COOLDOWN_SECONDS = 60.0


def _stream_frame(source: str, cfg: Config, seconds: float, width: int):
    """Кадр из прямого потока и причина, если он не вышел."""
    until, message = _STREAM_COOLDOWN.get(source, (0.0, ""))
    if time.time() < until:
        return b"", message
    try:
        frame = grab_stream_frame(source, cfg, seconds, width)
    except Exception as exc:  # noqa: BLE001 - сеть, бот-чек, битая ссылка
        log.info("Кадр из потока не получился: %s", exc)
        message = _preview_error(exc)
        # «остывание» имеет смысл только для настоящего запрета (бот-чек, приватное
        # видео): медленный CDN — не повод 60 секунд не пробовать снова
        if is_bot_check(exc) or "unavailable" in str(exc).lower():
            _STREAM_COOLDOWN[source] = (time.time() + STREAM_COOLDOWN_SECONDS, message)
        return b"", message
    if not frame:
        return b"", "кадр из потока не пришёл вовремя"
    return frame, ""


def _media_duration(source: str, cfg: Config) -> dict:
    """Длительность и размеры источника: файл — из файла, ссылка — из потока."""
    if not source.startswith(("http://", "https://")):
        return probe_media(Path(source).expanduser())
    stream = cached_stream(source)
    if not stream.get("duration"):
        try:
            _, meta = stream_info(source, cfg)
            stream = meta or {}
        except Exception as exc:  # noqa: BLE001 - причина уйдёт в ответ
            log.info("Длительность для шага не получена: %s", str(exc)[:120])
            raise
    return {
        "duration": float(stream.get("duration") or 0.0),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": float(stream.get("fps") or 0.0),
    }


# Причины, при которых скачивать участок бессмысленно: видео нет, доступ закрыт
# или ссылка битая. Иначе сервис тратит минуту на заведомо провальные попытки.
PERMANENT_REASON_SIGNS = (
    "видео недоступно",
    "не робот",
    "символов id",
    "приклеился лишний",
    "скопирована не целиком",
)


def _reason_is_permanent(reason: str) -> bool:
    text = (reason or "").lower()
    return any(sign in text for sign in PERMANENT_REASON_SIGNS)


FRAME_WINDOW_BUDGET = 45.0


def _window_frame(source: str, cfg: Config, seconds: float, width: int, until=None):
    """Кадр из короткого участка: скачиваем его один раз, дальше всё локально."""
    try:
        resolved = resolve_for_time(source, cfg, seconds)
        if resolved is None:
            ensure_window(
                source, cfg, seconds, until=until,
                deadline=time.monotonic() + FRAME_WINDOW_BUDGET,
            )
            resolved = resolve_for_time(source, cfg, seconds)
        if resolved is None:
            return b"", ""
        media, local_time = resolved[0], resolved[1]
        if not _allowed_media(media, cfg):
            return b"", ""
        frame = grab_frame(media, local_time, width)
        if frame:
            return frame, ""
        return b"", "кадр из участка не получился"
    except Exception as exc:  # noqa: BLE001 - сеть, бот-чек, битый файл
        log.info("Участок для кадров не получился: %s", str(exc)[:120])
        return b"", _preview_error(exc)


def _frame_from_window(source: str, cfg: Config, seconds: float, width: int) -> bytes:
    """Кадр из короткого скачанного участка — страховка, когда поток медленный."""
    try:
        ensure_window(source, cfg, seconds)
    except Exception as exc:  # noqa: BLE001 - сеть, бот-чек, приватное видео
        log.info("Участок для кадров не скачался: %s", exc)
        return b""
    resolved = resolve_for_time(source, cfg, seconds)
    if resolved is None:
        return b""
    media, local_time = resolved[0], resolved[1]
    if not _allowed_media(media, cfg):
        return b""
    return grab_frame(media, local_time, width)


def _preview_error(exc: Exception) -> str:
    """Понятная причина, почему кадры не скачались."""
    text = str(exc)
    if "Incomplete YouTube ID" in text or "looks truncated" in text:
        return "ссылка похожа на обрезанную — скопируй её из адресной строки целиком"
    if "Sign in to confirm" in text:
        return (
            "сайт просит подтвердить, что мы не робот. Это ограничение IP и проходит само: "
            "повтори через 10–15 минут"
        )
    if isinstance(exc, TimeoutError) or "не успел" in text:
        return (
            "не успел скачать участок для кадров. Сайт сейчас отвечает медленно — "
            "нажми ещё раз через минуту"
        )
    if unavailable_video(exc):
        return (
            "видео недоступно: приватное, удалённое или ссылка неверна. "
            "Открой её в браузере — если ролик не играет, дело в ссылке или доступе"
        )
    return f"не удалось скачать кусок для кадров: {text}"


def _remote_media(source: str, cfg: Config) -> dict:
    """Что известно о ссылке до скачивания: длительность из метаданных сайта."""
    duration, width, height, fps = 0.0, 0, 0, 0.0
    if source.startswith(("http://", "https://")):
        # только из кэша: запрос к сайту занимает секунды, страница ждать не должна
        stream = cached_stream(source)
        if stream:
            duration = float(stream.get("duration") or 0.0)
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            fps = float(stream.get("fps") or 0.0)
        else:
            from .source import cached_metadata

            try:
                duration = float(cached_metadata(source).get("duration") or 0.0)
            except Exception:  # noqa: BLE001 - метаданные не критичны
                duration = 0.0
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": fps,
        "offset": 0.0,
        "needs_download": True,
        "stream": True,
        "window": False,
        "link_problem": link_problem(source),
    }


def _allowed_media(path: Path, cfg: Config) -> bool:
    """Отдавать по HTTP можно только то, что лежит в рабочих каталогах."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in (cfg.cache_dir, cfg.output_dir):
        try:
            resolved.relative_to(Path(root).expanduser().resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _free_mb(cfg: Config) -> float:
    from .storage import free_mb

    return free_mb(Path(cfg.output_dir).expanduser())


def _prepare_disk(cfg: Config) -> None:
    """Make room if the disk is tight; refuse the job when even that is not enough."""
    from .storage import make_room

    free = _free_mb(cfg)
    if free < cfg.auto_clean_free_mb:
        log.info("Свободно %.0f МБ — чищу старые файлы до %d МБ", free, cfg.auto_clean_free_mb)
        make_room(
            [Path(cfg.cache_dir).expanduser(), Path(cfg.output_dir).expanduser()],
            float(cfg.auto_clean_free_mb),
        )
        free = _free_mb(cfg)
    if free < cfg.min_free_mb:
        raise RuntimeError(
            f"Мало места на диске: свободно {free:.0f} МБ, нужно хотя бы {cfg.min_free_mb} МБ. "
            f"Освободи место или нажми «Очистить» — иначе клип запишется битым."
        )


def _estimate(
    source: str, segments: list[Segment], fast: bool, vertical: bool, layout: str
) -> tuple[float, float] | None:
    """Bot's model, minus the download part when the file is already on disk."""
    from .bot import DOWNLOAD_RATE, ENCODE_RATE

    if any(is_open_end(seg) for seg in segments):
        return None  # конец узнаем только после загрузки
    total = sum(seg.duration for seg in segments)
    if total <= 0:
        return None
    per_second = ENCODE_RATE["fast" if fast else "accurate"]
    overhead = ESTIMATE_LOCAL_OVERHEAD
    if is_url(source):
        per_second += DOWNLOAD_RATE
        overhead = ESTIMATE_URL_OVERHEAD
    if vertical:
        per_second *= 1.25
    if layout == "blur":
        per_second *= 1.15
    work = overhead + total * per_second
    return work * 0.55, work * 1.7


def _call_clipper(clipper: Callable[..., list[ClipResult]], args: tuple, variants: list[dict]):
    """Try richer keyword sets first; a clipper that predates them gets fewer."""
    last_error: TypeError | None = None
    for kwargs in variants:
        clean = {key: value for key, value in kwargs.items() if value is not None}
        try:
            return clipper(*args, **clean)
        except TypeError as exc:
            last_error = exc
            continue
    raise last_error if last_error is not None else RuntimeError("clipper не ответил")


def _run_job(
    job_id: str,
    source: str,
    segments: list[Segment],
    fast: bool,
    cfg: Config,
    clipper: Callable[..., list[ClipResult]],
    vertical: bool = False,
    layout: str = "crop",
    horizontal: float = 0.5,
) -> None:
    control = _job_control(job_id)
    started = time.monotonic()
    stop_heartbeat = threading.Event()
    mode_text = ", ".join(
        ["без перекодирования" if fast else "точный"]
        + (["9:16 с размытым фоном" if layout == "blur" else "9:16"] if vertical else [])
    )

    def finish(state: str, **extra: Any) -> None:
        """Update the job and append it to history.jsonl."""
        from .storage import record_history

        payload = extra.get("results") or []
        entry = {
            "ts": time.time(),
            "job": job_id,
            "source": source,
            "segments": len(segments),
            "mode": mode_text,
            "state": state,
            "seconds": round(time.monotonic() - started, 1),
            "clips": len(payload),
            "bytes": sum(int(item.get("size_bytes") or 0) for item in payload),
        }
        if extra.get("error"):
            entry["error"] = str(extra["error"])[:200]
        record_history(cfg.history_path(), entry)
        stop_heartbeat.set()
        _job_update(job_id, state=state, **extra)

    try:
        _prepare_disk(cfg)
        from .source import resolve_open_ends_for_source

        try:
            segments = resolve_open_ends_for_source(segments, source, cfg)
        except Exception as exc:  # noqa: BLE001 — метаданные могут молчать
            # Не фатально: настоящую длительность даст скачанный файл.
            log.info("Длительность заранее неизвестна (%s), конец определю после скачивания", exc)
        if has_open_ends(segments):
            # Метаданные сайта промолчали (бот-чек, Shorts), но длительность уже
            # знает поток — он же давал кадры. Иначе пропадает оценка времени.
            duration = float(cached_stream(source).get("duration") or 0.0)
            if duration > 0:
                segments = resolve_open_ends(segments, duration)
        estimate = _estimate(source, segments, fast, vertical, layout)
        _job_update(
            job_id,
            message="Готовлю источник...",
            stage="download",
            fraction=None,
            estimate=list(estimate) if estimate else None,
        )

        last_event = time.monotonic()

        def progress(*args) -> None:
            """Accept both the bot-style (stage, fraction, detail) and a plain message."""
            nonlocal last_event
            last_event = time.monotonic()
            if len(args) == 1:
                _job_update(job_id, message=str(args[0]))
                return
            stage = str(args[0]) if args else ""
            fraction = args[1] if len(args) > 1 else None
            detail = str(args[2]) if len(args) > 2 else ""
            _job_update(
                job_id,
                stage=stage,
                fraction=fraction,
                detail=detail,
                message=detail or {"download": "Скачиваю", "encode": "Кодирую"}.get(stage, stage),
            )
        cache_dir = Path(cfg.cache_dir).expanduser()

        def cache_files_mb() -> float:
            try:
                return sum(p.stat().st_size for p in cache_dir.rglob("*") if p.is_file()) / 1048576
            except OSError:
                return 0.0

        def heartbeat() -> None:
            from .timecode import human_duration

            base_mb = cache_files_mb()
            while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
                job = _job_snapshot(job_id)
                if not job or job.get("state") != "running":
                    return
                if job.get("fraction") is not None:
                    continue  # настоящий прогресс уже идёт
                idle = time.monotonic() - last_event
                stage = str(job.get("stage") or "")
                word = {"download": "Скачиваю", "encode": "Кодирую"}.get(stage, "Работаю")
                grown = cache_files_mb() - base_mb
                amount = f" · скачано {grown:.0f} МБ" if grown >= 1 else ""
                detail = f"уже {human_duration(idle)}{amount}"
                _job_update(job_id, detail=detail, message=f"{word} · {detail}")

        threading.Thread(target=heartbeat, daemon=True).start()
        results = _call_clipper(
            clipper,
            (source, segments, fast, cfg, progress),
            [
                {
                    "vertical": vertical,
                    "layout": layout,
                    "control": control,
                    "horizontal": horizontal,
                },
                {"vertical": vertical, "layout": layout, "control": control},
                {"vertical": vertical, "layout": layout},
                {"vertical": vertical},
                {},
            ],
        )
        if control is not None and control.is_set():
            finish(state="cancelled", error="Остановлено", error_details="", message="")
            return
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
        finish(state="done", results=payload, files=files, message="")
    except Exception as exc:  # noqa: BLE001 - reported to the page
        if (control is not None and control.is_set()) or isinstance(exc, CutCancelled):
            log.info("Задание %s остановлено пользователем", job_id)
            finish(state="cancelled", error="Остановлено", error_details="", message="")
            return
        log.exception("Ошибка задания %s", job_id)
        reason = str(exc) or exc.__class__.__name__
        finish(
            state="error",
            error=reason.splitlines()[0][:200],
            error_details=(
                f"{params_summary(source, segments, fast, vertical, layout)}\n\n{reason}"
            ),
            message="",
        )


def default_clipper(
    source: str,
    segments: list[Segment],
    fast: bool,
    cfg: Config,
    progress=None,
    vertical: bool = False,
    layout: str = "crop",
    control: "JobControl | None" = None,
    horizontal: float = 0.5,
) -> list[ClipResult]:
    """Download (if needed), validate and cut — the same path the CLI uses."""
    from .cut import cut_segment, pick_vertical_size, vertical_name
    from .source import prepare_source, shift_segments

    if vertical and layout not in ("crop", "blur"):
        layout = "crop"

    def report(stage: str, fraction: float | None = None, detail: str = "") -> None:
        if progress:
            progress(stage, fraction, detail)

    report("download", None, "готовлю источник")
    src = prepare_source(
        source,
        cfg,
        segments=[(s.start, s.end) for s in segments],
        cancel_event=control,
        progress=report,
    )
    try:
        from .source import resolve_open_ends_after_download

        segments = resolve_open_ends_after_download(segments, src)
        original_segments = segments
        segments = shift_segments(segments, src.offset)
        validate_segments(segments, duration=src.info.duration or None)
        out_dir = Path(cfg.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for i, seg in enumerate(segments):
            shown = original_segments[i] if i < len(original_segments) else seg
            total = len(segments)
            report("encode", 0.0, f"клип {i + 1}/{total}" if total > 1 else "")

            def clip_progress(
                stage: str, fraction: float | None, detail: str, _i: int = i, _total: int = total
            ) -> None:
                report(stage, fraction, f"клип {_i + 1}/{_total}" if _total > 1 else "")

            result = cut_segment(
                str(src.path),
                out_dir
                / vertical_name(
                    clip_filename(src.title, source, shown), vertical, layout, horizontal
                ),
                seg,
                fast=fast,
                ffmpeg_path=cfg.ffmpeg_path,
                has_audio=src.info.has_audio,
                vertical=vertical,
                layout=layout,
                vertical_size=pick_vertical_size(
                    src.info.height, cfg.vertical_width, cfg.vertical_height
                ),
                progress=clip_progress,
                cancel=control,
                on_process=control.register if control is not None else None,
                horizontal=horizontal,
            )
            result.segment = shown
            results.append(result)
        return results
    finally:
        src.cleanup()


def attachment_header(name: str) -> str:
    """Content-Disposition that survives non-ASCII names (headers are latin-1 only)."""
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


def prepare_play(source: str, cfg: Config, wanted: float, until: float | None = None):
    """Один раз решает, какой файл отдавать для проигрывания и с каким смещением.

    Два независимых запроса (HEAD и сам файл) могли получить разные участки:
    заголовок со смещением от одного, тело — от другого, и перемотка попадала
    в чужую шкалу. Поэтому файл выбирается здесь и дальше отдаётся по имени.
    """
    resolved = resolve_for_time(source, cfg, wanted)
    if resolved is None and source.startswith(("http://", "https://")):
        ensure_window(source, cfg, wanted, until=until)
        resolved = resolve_for_time(source, cfg, wanted)
    if resolved is None:
        return None
    local, local_time, offset, window_end, is_window = resolved
    return {
        "path": Path(local),
        "name": Path(local).name,
        "offset": float(offset),
        "start": float(local_time),
        "window": bool(is_window),
        "window_end": float(window_end or 0.0),
    }


def expired_files(cfg: Config) -> list[Path]:
    """Просроченные файлы — только посчитать. Удаляет их отдельная команда."""
    from .storage import expired

    return expired([Path(cfg.cache_dir), Path(cfg.output_dir)], cfg.retention_minutes)


def sweep_expired(cfg: Config) -> "SweepResult":
    """Удалить просроченное по явной команде пользователя."""
    from .storage import sweep

    return sweep([Path(cfg.cache_dir), Path(cfg.output_dir)], cfg.retention_minutes)


def status_text(cfg: Config) -> str:
    """Что лежит на машине и когда каждый файл исчезнет. Ничего не удаляет."""
    from .storage import describe
    from .timecode import format_moment, format_retention, human_duration

    minutes = cfg.retention_minutes
    running_jobs = sum(1 for job in JOBS.values() if job.get("state") == "running")
    overdue = expired_files(cfg)

    lines = []
    now = time.time()
    for label, directory in (("оригиналы", cfg.cache_dir), ("клипы", cfg.output_dir)):
        items = describe(Path(directory), minutes)
        total = sum(item["size"] for item in items) / 1048576
        lines.append(f"{label}: {len(items)} файл(ов), {total:.1f} МБ")
        for item in items[:8]:
            deadline = float(item.get("delete_at") or 0)  # type: ignore[arg-type]
            left = float(item.get("left_minutes") or 0)  # type: ignore[arg-type]
            if deadline and deadline <= now:
                # срок вышел — но файл лежит, пока пользователь не разрешит уборку
                lines.append(f"  · {item['name']} — срок вышел")
                continue
            when = format_moment(deadline) if deadline else "?"
            lines.append(f"  · {item['name']} — удалится {when} (через {human_duration(left * 60)})")
    cookies = Path(cfg.cookies).expanduser() if cfg.cookies else None
    if cookies and cookies.is_file():
        age = (time.time() - cookies.stat().st_mtime) / 86400
        lines.append(f"cookies: обновлены {age:.1f} дн. назад")
    else:
        lines.append("cookies: файла нет — YouTube может требовать авторизацию")
    if overdue:
        lines.append(
            f"просрочено: {len(overdue)} файл(ов) — удалить кнопкой «Удалить просроченные» в GUI или /clean"
        )
    lines.append(f"в работе: {running_jobs} задание(й)")
    from .storage import free_mb, read_history, total_mb

    out_dir = Path(cfg.output_dir).expanduser()
    free, total = free_mb(out_dir), total_mb(out_dir)
    used_percent = round(100 - free / total * 100) if total else 0
    lines.append(
        f"диск: свободно {free / 1024:.1f} ГБ из {total / 1024:.1f} ГБ ({used_percent}% занято)"
    )
    if free < cfg.auto_clean_free_mb:
        lines.append(
            f"  ⚠️ ниже порога {cfg.auto_clean_free_mb} МБ — перед заданием уберу старые файлы"
        )
    if free < cfg.min_free_mb:
        lines.append(f"  ⛔️ меньше {cfg.min_free_mb} МБ — новые задания будут отклонены")
    history = read_history(cfg.history_path(), 5)
    if history:
        lines.append("последние задания:")
        for item in history:
            state = {
                "done": "готово",
                "error": "ошибка",
                "cancelled": "остановлено",
            }.get(str(item.get("state")), str(item.get("state")))
            source = str(item.get("source") or "")
            short = Path(source).name if not source.startswith("http") else source[:40]
            lines.append(
                f"  · {format_moment(float(item.get('ts') or 0))} — {state}, "
                f"{item.get('clips', 0)} клип(ов), {item.get('seconds', 0)} с, "
                f"{item.get('mode', '')} · {short[:40]}"
            )
    lines.append(
        f"хранение: {format_retention(minutes)} тишины — меняется кнопкой «Хранение»"
    )
    lines.append(f"слушаю: {cfg.gui_host}:{cfg.gui_port}")
    return "\n".join(lines)


GUI_HELP = """Как пользоваться
  1. Слева укажи источник: перетащи файл или вставь ссылку (YouTube, Rutube, VK, TikTok).
  2. Задай начало и конец отрезка. «сброс» возвращает поля к 0:00 и 0:30.
  3. Нажми «Нарезать» — справа появится клип, его можно скачать.
     Пока идёт работа рядом с таймером есть кнопка «Стоп»: она обрывает загрузку и
     кодирование сразу, недописанный файл удаляется.

Несколько отрезков
  Заполни начало и конец, нажми «+ в список» — отрезок уйдёт в список, поля очистятся.
  Так добавь сколько нужно: «Нарезать» порежет все сразу, а кнопка «скачать zip» отдаст
  их одним архивом. Крестик убирает отрезок, клик по времени возвращает его в поля.

До конца ролика
  Звёздочка в поле «конец»: * — от начала до конца файла.
  В таймкоде то же самое: 20-* — с двадцатой секунды до конца.

Таймкоды
  45        — 45 секунд
  1:12      — 1 мин 12 с
  1:02:03   — 1 ч 2 мин 3 с
  Вместо «:» подойдёт , / \\ — «1/12» это 1 мин 12 с.
  Точка — это доли секунды: 1:12.5 — 1 мин 12,5 с.
  «20-*» — с 20-й секунды до конца ролика.

Режимы
  «без перекодирования» — режет по ключевому кадру: секунды вместо минут, зато начало
     может уехать до ближайшего ключевого кадра.
  Под диапазоном — кадры начала и конца: видно, что попадёт в клип, ещё до нарезки.
     «± кадр» и «±1 с» — свои кнопки под каждым кадром: под началом правят начало,
     под концом — конец (шаг кадра берётся из файла). «посмотреть отрезок» проигрывает
     ровно его — по исходнику, без нарезки.
     По ссылке кадры берутся из потока — файл скачивать не нужно; если сайт не отдал
     поток, появится кнопка «скачать 20 с» и кадры возьмутся из короткого участка.

  «вертикально 9:16» — кадр под смартфон, фон на выбор: обрезать бока или размытый.
     Источник 720p остаётся 720×1280 — без апскейла и мыла.
     Ползунок «окно» двигает кадр по горизонтали (0% слева, 50% центр, 100% справа),
     кнопка «авто» ищет содержимое без чёрных полей и ставит окно на него.
     Позиция запоминается для источника: в следующей нарезке подставится сама.

Хранение
  Файл до {max_upload} ГБ. Скачанные оригиналы и готовые клипы живут {retention} после
  последнего использования и потом удаляются сами. «Статус» показывает точное время
  удаления каждого файла, «Хранение» меняет срок (30 — минут, 1:30 — час тридцать, 2ч),
  «Очистить» удаляет всё сразу.

Служебное
  Адрес: {url}
  Пароль: {password}
  «Статус» в шапке — что лежит на машине, сколько свободно на диске и что было
  в последних заданиях (история пишется в history.jsonl рядом с конфигом).
  Перед заданием проверяется место: при нехватке сначала удаляются самые старые
  файлы, а если места всё равно мало — задание отклоняется с понятной ошибкой."""


def info_text(cfg: Config) -> str:
    """Памятка под веб-интерфейс, а не под чат бота."""
    password = cfg.gui_password_file
    if password and Path(str(password)).expanduser().is_file():
        where = str(Path(str(password)).expanduser())
    elif password:
        where = f"{password} (файла пока нет)"
    else:
        where = "не задан — вход только с localhost"
    from .bot import gui_url

    from .timecode import format_retention

    return GUI_HELP.format(
        max_upload=round(MAX_UPLOAD / 1024**3),
        retention=format_retention(cfg.retention_minutes),
        url=gui_url(cfg),
        password=where,
    )


def clean_text(cfg: Config) -> str:
    from .storage import sweep

    result = sweep([cfg.cache_dir, Path(cfg.output_dir)], minutes=0)
    return f"Удалено {result.count} файл(ов), {result.freed_bytes / 1048576:.1f} МБ"


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
                form = parse_qs(body.decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                form = {}
            password = (form.get("password") or [""])[0]
            remember = (form.get("remember") or [""])[0] in ("1", "on", "true", "yes")
            if not auth_gate.check_password(password):
                auth_gate.note_failure(client)
                log.warning("GUI: неверный пароль с %s", client)
                self._login_page("неверный пароль", 401)
                return
            auth_gate.clear(client)
            ttl = SESSION_REMEMBER_AGE if remember else SESSION_MAX_AGE
            cookie = f"{SESSION_COOKIE}={auth_gate.token(ttl)}; Path=/; HttpOnly; SameSite=Lax"
            if remember:
                cookie += f"; Max-Age={ttl}"
            self.send_response(303)
            self.send_header("Set-Cookie", cookie)
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

        def _send(
            self,
            code: int,
            body: bytes,
            content_type: str = "application/json",
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve_media(self, path: Path, extra: dict[str, str] | None = None) -> None:
            """Отдать исходник с поддержкой Range — браузер проигрывает отрезок без нарезки."""
            size = path.stat().st_size
            if not size:
                self._error(404, "пустой файл")
                return
            start, end, partial = 0, size - 1, False
            header = (self.headers.get("Range") or "").strip()
            if header.startswith("bytes="):
                spec = header[6:].split(",")[0].strip()
                left, _, right = spec.partition("-")
                try:
                    if left:
                        start = max(0, int(left))
                        end = int(right) if right else size - 1
                    elif right:
                        length = max(0, int(right))
                        start = max(0, size - length)
                    partial = True
                except ValueError:
                    start, end, partial = 0, size - 1, False
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            length = end - start + 1
            guess = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", guess)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with path.open("rb") as handle:
                    handle.seek(start)
                    left = length
                    while left > 0:
                        chunk = handle.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, code: int, message: str) -> None:
            self._send(code, _dumps({"error": message}))

        def _guard(self, handler) -> None:
            """Never let an exception kill the connection with no response."""
            try:
                handler()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001 — the browser deserves an answer
                log.exception("ошибка в обработчике %s", self.path)
                try:
                    self._error(500, "внутренняя ошибка, смотри журнал")
                except Exception:  # noqa: BLE001
                    pass

        def do_GET(self) -> None:  # noqa: N802
            self._guard(self._handle_get)

        def _handle_get(self) -> None:
            apply_retention_override(cfg)
            path = urlparse(self.path).path
            if path == "/api/health":
                self._send(
                    200,
                    _dumps(
                        {
                            "status": "ok",
                            "uptime_s": round(time.time() - STARTED_AT),
                            "version": page_version(),
                        }
                    ),
                )
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
            if path == "/api/status":
                self._send(
                    200,
                    _dumps({"text": status_text(cfg), "expired": len(expired_files(cfg))}),
                )
                return
            if path in ("/api/vertical", "/api/vertical/auto"):
                source = (parse_qs(urlparse(self.path).query).get("source") or [""])[0]
                if path == "/api/vertical/auto":
                    position, note = auto_horizontal(source, cfg)
                    self._send(200, _dumps({"horizontal": position, "note": note}))
                    return
                saved = _saved_position(cfg, source)
                self._send(
                    200,
                    _dumps(
                        {
                            "horizontal": 0.5 if saved is None else saved,
                            "saved": saved is not None,
                        }
                    ),
                )
                return
            if path == "/api/duration":
                # Нужен шагу «− кадр» от «до конца»: чтобы сдвинуть конец, надо знать
                # длительность ролика, даже если кадры ещё не запрашивались.
                source = (parse_qs(urlparse(self.path).query).get("source") or [""])[0]
                if not source:
                    self._error(400, "не указан источник")
                    return
                broken = link_problem(source)
                if broken:
                    self._error(409, broken)
                    return
                try:
                    info = _media_duration(source, cfg)
                except Exception as exc:  # noqa: BLE001 - сеть, бот-чек, битая ссылка
                    self._error(409, _preview_error(exc))
                    return
                if not info.get("duration"):
                    self._error(409, "сайт не сказал длительность ролика")
                    return
                self._send(200, _dumps(info))
                return
            if path == "/api/preplay":
                # Один раз выбираем файл и смещение, чтобы страница играла ровно то,
                # что ей назвали: иначе HEAD и само видео получали разные участки.
                query = parse_qs(urlparse(self.path).query)
                source = (query.get("source") or [""])[0]
                if not source:
                    self._error(400, "не указан источник")
                    return
                try:
                    wanted = float((query.get("t") or ["0"])[0] or 0)
                except (TypeError, ValueError):
                    self._error(400, "не понял начало отрезка")
                    return
                try:
                    until = float((query.get("until") or [""])[0] or 0) or None
                except (TypeError, ValueError):
                    until = None
                broken = link_problem(source)
                if broken:
                    self._error(409, broken)
                    return
                try:
                    play = prepare_play(source, cfg, wanted, until)
                except Exception as exc:  # noqa: BLE001 - причину показываем текстом
                    self._error(409, _preview_error(exc))
                    return
                if play is None:
                    self._error(409, "нечего играть: файл не найден")
                    return
                info = probe_media(play["path"]) if play["path"].is_file() else {}
                self._send(
                    200,
                    _dumps(
                        {
                            "url": "/api/source?name=" + quote(play["name"], safe=""),
                            "offset": play["offset"],
                            "start": play["start"],
                            "window": play["window"],
                            "duration": info.get("duration") or 0,
                        }
                    ),
                )
                return

            if path in ("/api/media", "/api/frame", "/api/source"):
                query = parse_qs(urlparse(self.path).query)
                frame_until = None
                if path == "/api/frame":
                    try:
                        raw_until = (query.get("until") or [""])[0]
                        frame_until = float(raw_until) if raw_until else None
                    except ValueError:
                        frame_until = None
                by_name = (query.get("name") or [""])[0]
                if path == "/api/source" and by_name:
                    wanted_file = Path(by_name).name
                    for folder in (cfg.cache_dir, cfg.output_dir):
                        candidate = Path(folder) / wanted_file
                        if candidate.is_file() and _allowed_media(candidate, cfg):
                            self._serve_media(candidate)
                            return
                        sub = Path(folder) / "preview" / wanted_file
                        if sub.is_file() and _allowed_media(sub, cfg):
                            self._serve_media(sub)
                            return
                    self._error(404, "файл не найден")
                    return
                source = (query.get("source") or [""])[0]
                raw_t = (query.get("t") or [""])[0]
                if raw_t:
                    try:
                        wanted = float(raw_t)
                    except (TypeError, ValueError):
                        self._error(400, "не понял время кадра")
                        return
                else:
                    wanted = 0.0
                try:
                    frame_width = int((query.get("w") or ["480"])[0] or 480)
                except ValueError:
                    frame_width = 480
                resolved = resolve_for_time(source, cfg, wanted)
                if resolved is None:
                    if path == "/api/media":
                        # ссылка: кадры можно взять прямо из потока, файл не нужен
                        info = _remote_media(source, cfg)
                        info["link_problem"] = link_problem(source)
                        self._send(200, _dumps(info))
                        return
                    broken = link_problem(source)
                    if broken:
                        self._error(409, broken)
                        return
                    if path == "/api/source" and source.startswith(("http://", "https://")):
                        # ссылку нечем играть, пока нет файла: готовим короткий участок
                        try:
                            until = float((query.get("until") or [""])[0] or 0) or None
                        except (TypeError, ValueError):
                            until = None
                        try:
                            ensure_window(source, cfg, wanted, until=until)
                        except Exception as exc:  # noqa: BLE001 - причину покажем как есть
                            self._error(409, _preview_error(exc))
                            return
                        prepared = resolve_for_time(source, cfg, wanted)
                        if prepared is None:
                            self._error(409, "не удалось подготовить участок для проигрывания")
                            return
                        local, _local_time, offset, _we, _iw = prepared
                        self._serve_media(local, {"X-Source-Offset": f"{offset:.3f}"})
                        return
                    if source.startswith(("http://", "https://")):
                        began = time.monotonic()
                        frame = cached_frame(source, wanted, frame_width)
                        reason = ""
                        approx = False
                        if not frame:
                            # Сначала короткий участок: он скачивается один раз
                            # (5–20 с) и дальше отдаёт кадры мгновенно. Поток по
                            # каждому клику — лотерея с таймаутами на медленном CDN.
                            frame, reason = _window_frame(
                                source, cfg, wanted, frame_width, until=frame_until
                            )
                        if not frame and not _reason_is_permanent(reason):
                            frame, reason = _stream_frame(source, cfg, wanted, frame_width)
                        if not frame:
                            frame = closest_frame(source, wanted, frame_width)
                            approx = bool(frame)
                        if frame:
                            self._send(
                                200, frame, "image/jpeg",
                                {"X-Frame-Approx": "1"} if approx else None,
                            )
                            return
                        if reason:
                            self._error(409, reason)
                            return
                    self._error(
                        409,
                        "кадр не получен — попробуй ещё раз или скачай короткий участок",
                    )
                    return
                local, local_time, offset, window_end, is_window = resolved
                if not _allowed_media(local, cfg):
                    self._error(403, "файл вне рабочих каталогов")
                    return
                if path == "/api/media":
                    info = probe_media(local)
                    info["offset"] = offset
                    info["needs_download"] = False
                    info["window"] = is_window
                    info["window_end"] = window_end or (offset + (info.get("duration") or 0))
                    if is_window:
                        # длительность ролика лежит в паспорте участка: сеть не нужна
                        info["clip_duration"] = info.get("duration")
                        window = find_window(source, cfg, wanted)
                        full = float(window[3]) if window else 0.0
                        info["duration"] = full
                        info["unknown_duration"] = not full
                    self._send(200, _dumps(info))
                    return
                if path == "/api/source":
                    # смещение участка: файл может начинаться не с нуля, и без этого
                    # страница ставила currentTime от начала файла — «перемотка не работает»
                    self._serve_media(local, {"X-Source-Offset": f"{offset:.3f}"})
                    return
                key = str(local)
                frame = cached_frame(key, local_time, frame_width)
                approx = False
                if not frame:
                    frame = grab_frame(local, local_time, frame_width)
                if not frame:
                    # не отдаём пустоту: лучше ближайший кадр, чем исчезнувшая картинка
                    frame = closest_frame(key, local_time, frame_width)
                    approx = bool(frame)
                if not frame:
                    self._error(404, "кадр не получился")
                    return
                self._send(
                    200, frame, "image/jpeg", {"X-Frame-Approx": "1"} if approx else None
                )
                return
            if path == "/api/retention":
                from .timecode import format_retention

                self._send(
                    200,
                    _dumps(
                        {
                            "minutes": cfg.retention_minutes,
                            "human": format_retention(cfg.retention_minutes),
                        }
                    ),
                )
                return
            if path == "/api/info":
                self._send(200, _dumps({"text": info_text(cfg)}))
                return
            if path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                job = _job_snapshot(job_id)
                if job is None:
                    self._error(404, "задание не найдено")
                    return
                self._send(200, _dumps(job))
                return
            if path.startswith("/api/zip/"):
                job_id = path.rsplit("/", 1)[-1]
                job = _job_snapshot(job_id)
                if not job or job["state"] != "done" or not job["files"]:
                    self._error(404, "файлы не готовы")
                    return
                files = [Path(name) for name in job["files"] if Path(name).is_file()]
                if not files:
                    self._error(404, "файлы удалены")
                    return
                bundle = Path(tempfile.mkdtemp(prefix="vc-zip-")) / f"clips-{job_id}.zip"
                with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as archive:
                    for item in files:
                        archive.write(item, item.name)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", attachment_header(bundle.name))
                self.send_header("Content-Length", str(bundle.stat().st_size))
                self.end_headers()
                try:
                    with bundle.open("rb") as src:
                        shutil.copyfileobj(src, self.wfile, 1 << 20)
                finally:
                    shutil.rmtree(bundle.parent, ignore_errors=True)
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
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Disposition", attachment_header(file_path.name))
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as src:
                    shutil.copyfileobj(src, self.wfile, 1 << 20)
                return
            self._error(404, "не найдено")

        def do_HEAD(self) -> None:  # noqa: N802
            self._guard(self._handle_get)

        def do_POST(self) -> None:  # noqa: N802
            self._guard(self._handle_post)

        def _handle_post(self) -> None:
            apply_retention_override(cfg)
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

            if parsed.path.startswith("/api/cancel/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                job = _job_snapshot(job_id)
                if job is None:
                    self._error(404, "задание не найдено")
                    return
                if job.get("state") != "running":
                    self._error(409, "задание уже завершено")
                    return
                control = _job_control(job_id)
                if control is not None:
                    control.stop()
                log.info("GUI: останавливаю задание %s", job_id)
                self._send(200, _dumps({"state": "cancelling"}))
                return

            if parsed.path == "/api/preview":
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = {}
                source = str(data.get("source") or "")
                if not source:
                    self._error(400, "нужна ссылка или файл")
                    return
                try:
                    seconds = float(data.get("t") or 0)
                except (TypeError, ValueError):
                    seconds = 0.0
                until = data.get("until")
                try:
                    until = float(until) if until is not None else None
                except (TypeError, ValueError):
                    until = None
                try:
                    path, start, end = ensure_window(source, cfg, seconds, until)
                except Exception as exc:  # сеть, cookies, бот-чек — показываем причину
                    self._error(502, _preview_error(exc))
                    return
                self._send(
                    200,
                    _dumps(
                        {
                            "file": path.name,
                            "start": round(start, 3),
                            "end": round(end, 3),
                            "folder": str(windows_dir(cfg)),
                        }
                    ),
                )
                return
            if parsed.path == "/api/vertical":
                try:
                    data = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    self._error(400, "ожидается JSON")
                    return
                source = str(data.get("source") or "").strip()
                if not source:
                    self._error(400, "не указан источник")
                    return
                position = clamp_horizontal(data.get("horizontal", 0.5))
                _remember_position(cfg, source, position)
                self._send(200, _dumps({"horizontal": position, "saved": True}))
                return

            if parsed.path == "/api/retention":
                from .timecode import format_retention, parse_retention

                try:
                    data = json.loads(body or b"{}")
                    minutes = parse_retention(str(data.get("value") or data.get("minutes") or ""))
                except (json.JSONDecodeError, ValueError) as exc:
                    self._error(400, str(exc))
                    return
                was = cfg.retention_minutes
                cfg.set_retention(minutes)
                log.info("GUI: хранение %d мин → %d мин", was, minutes)
                self._send(
                    200,
                    _dumps(
                        {
                            "minutes": minutes,
                            "text": f"Храню файлы {format_retention(minutes)} после последнего "
                            f"использования (было {format_retention(was)}).",
                        }
                    ),
                )
                return

            if parsed.path == "/api/sweep":
                running = sum(1 for job in JOBS.values() if job.get("state") == "running")
                if running:
                    self._send(
                        409,
                        _dumps({"error": "Идёт нарезка — уборку сделаю после неё"}),
                    )
                    return
                result = sweep_expired(cfg)
                note = (
                    f"Удалено {result.count} файл(ов), освобождено "
                    f"{result.freed_bytes / 1048576:.1f} МБ\n\n"
                    if result.count
                    else "Просроченных файлов нет\n\n"
                )
                self._send(
                    200,
                    _dumps(
                        {
                            "text": note + status_text(cfg),
                            "removed": result.count,
                            "expired": len(expired_files(cfg)),
                        }
                    ),
                )
                return

            if parsed.path == "/api/clean":
                self._send(200, _dumps({"text": clean_text(cfg)}))
                return

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
                    raw_segments = data.get("segments")
                    if isinstance(raw_segments, list) and raw_segments:
                        segments = [
                            Segment(
                                parse_timecode(item.get("start")),
                                parse_end_token(item.get("end")),
                            )
                            for item in raw_segments
                            if isinstance(item, dict)
                        ]
                        if not segments:
                            raise ValueError("пустой список отрезков")
                    else:
                        segments = [
                            Segment(
                                parse_timecode(data.get("start")),
                                parse_end_token(data.get("end")),
                            )
                        ]
                    validate_segments(segments, duration=None, allow_overlap=True)
                    vertical = bool(data.get("vertical", False))
                    layout = str(data.get("layout") or "crop")
                    if layout not in ("crop", "blur"):
                        layout = "crop"
                    horizontal = clamp_horizontal(data.get("horizontal", 0.5))
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
                        layout,
                        horizontal,
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
    httpd = ThreadingHTTPServer(
        (bind_host, bind_port), make_handler(cfg, clipper, Auth(password, cfg.gui_secret_file))
    )
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
