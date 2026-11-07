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
</style>
</head>
<body>
<form class="card" method="post" action="/login">
  <div class="brand">video-clipper <span>· вход</span></div>
  <p class="sub">Локальный сервер нарезки. Пароль — в файле gui_password_file.</p>
  {error}
  <label for="pw">пароль</label>
  <input id="pw" type="password" name="password" autofocus autocomplete="current-password">
  <button type="submit">Войти</button>
  <div class="foot">сессия — 12 часов</div>
</form>
</body></html>"""


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
     display:grid;grid-template-rows:auto minmax(0,1fr);height:100vh;height:100dvh;
     overflow:hidden}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}

.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;
     padding:13px 22px;border-bottom:1px solid var(--line);background:#101317}
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
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.panel pre{margin:0;flex:1;min-height:0;overflow:auto;white-space:pre-wrap;
  font:12.5px/1.65 var(--mono);color:var(--ink-2)}
.progress{height:4px;border-radius:999px;background:var(--line);overflow:hidden}
.progress[hidden]{display:none}
.progress i{display:block;height:100%;width:0;background:var(--accent);
  transition:width .25s linear}
.progress.indeterminate i{width:35%;animation:slide 1.2s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
@media (prefers-reduced-motion:reduce){.progress.indeterminate i{animation:none;width:100%}}
.modal{position:fixed;inset:0;background:#05070acc;backdrop-filter:blur(2px);
  display:flex;align-items:center;justify-content:center;padding:20px;z-index:20}
.modal[hidden]{display:none}
.modal-box{background:var(--panel);border:1px solid var(--line);border-radius:calc(var(--r) + 2px);
  width:min(400px,100%);padding:20px 22px 18px;box-shadow:0 24px 60px #000a}
.modal-box h3{margin:0 0 8px;font-size:15.5px;font-weight:600;letter-spacing:-.01em}
.modal-box p{margin:0 0 18px;color:var(--ink-2);font-size:13.5px;line-height:1.55}
.modal-actions{display:flex;justify-content:flex-end;gap:9px}
.danger{background:#e0574a;color:#180b08;border:1px solid transparent;border-radius:8px;
  padding:9px 14px;font-size:13px;font-weight:600;cursor:pointer;min-height:38px}
.danger:hover{filter:brightness(1.07)}
.danger:focus-visible,.modal .ghost:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.grid{display:grid;grid-template-columns:minmax(320px,420px) minmax(0,1fr);
      min-height:0;overflow:hidden}
.col-out{overflow-y:auto}
html{scrollbar-width:thin;scrollbar-color:#3a424e transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#3a424e;border-radius:6px}
::-webkit-scrollbar-track{background:transparent}
.col-form{padding:22px;border-right:1px solid var(--line);display:flex;
          flex-direction:column;gap:18px}
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
.layout-choice span{font-size:12px;color:var(--ink-3);margin-right:2px}
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
.timer{font-size:12.5px;color:var(--ink-3)}

.status{color:var(--ink-3);font-size:13.5px;min-height:22px;display:flex;
  align-items:center;gap:9px;padding:0}
.status.busy,.status.err{border:1px solid var(--line);background:var(--panel);
  border-radius:var(--r);padding:12px 14px;min-height:46px}
.status.busy{color:var(--ink)}
.status.err{border-color:#5f2f28;background:#1e1513;color:var(--err)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);flex:none;
  animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}*{transition:none!important}}

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
@media (max-width:520px){
  .bar{padding:12px 16px}
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
        <label><span>начало</span><input id="start" class="mono" type="text" value="0:00" inputmode="numeric" aria-label="Начало"></label>
        <span class="dash">→</span>
        <label><span>конец</span><input id="end" class="mono" type="text" value="0:30" inputmode="numeric" aria-label="Конец"></label>
      </div>
      <div class="presets">
        <button type="button" data-len="15">15 с</button>
        <button type="button" data-len="30">30 с</button>
        <button type="button" data-len="60">1 мин</button>
        <button type="button" data-len="180">3 мин</button>
        <button type="button" class="reset" data-reset="1">сброс</button>
      </div>
      <p class="hint" style="margin-top:9px">Таймкоды: <b>45</b>, <b>1:12</b>, <b>0:00:10</b>.<br>Вместо «-» можно «=»: <b>1:00=0:30</b> — с 1:00 тридцать секунд.</p>
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
        <button type="button" class="seg on" data-layout="crop">обрезать боки</button>
        <button type="button" class="seg" data-layout="blur">размытый фон</button>
      </div>
    </div>

    <button id="go" class="primary" disabled>Нарезать</button>
  </section>

  <section class="col-out">
    <div class="out-head">
      <span class="label">Результат</span>
      <span class="timer mono" id="timer"></span>
    </div>
    <div class="progress" id="progress" hidden><i id="bar"></i></div>
    <div class="status" id="status">Ждёт задание</div>
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

fetch('/api/health').then(r => r.json()).then(d => {
  $('svc-text').textContent = d.status === 'ok' ? 'сервис работает' : 'нет связи';
  if (d.status === 'ok') $('svc').classList.add('ok');
}).catch(() => { $('svc-text').textContent = 'нет связи'; });

function setStatus(text, kind) {
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
  statusEl.textContent = '';
  if (kind === 'busy') {
    const dot = document.createElement('span');
    dot.className = 'dot';
    statusEl.append(dot);
  }
  statusEl.append(document.createTextNode(text));
}

const panelEl = $('panel'), panelText = $('panel-text');
const progressEl = $('progress'), barEl = $('bar');

function showPanel(title, text) {
  $('panel-title').textContent = title;
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
$('btn-status').addEventListener('click', () => openPanel('/api/status', 'Статус'));
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

function renderProgress(job) {
  const running = job.state === 'running';
  progressEl.hidden = !running;
  if (!running) { progressEl.classList.remove('indeterminate'); barEl.style.width = '0'; return; }
  const fraction = typeof job.fraction === 'number' ? job.fraction : null;
  if (fraction === null) {
    progressEl.classList.add('indeterminate');
    barEl.style.width = '';
  } else {
    progressEl.classList.remove('indeterminate');
    barEl.style.width = Math.max(2, Math.round(fraction * 100)) + '%';
  }
}

const choiceEl = $('layout-choice');
$('vertical').addEventListener('change', (e) => { choiceEl.hidden = !e.target.checked; });
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
  let buf = String(text).trim();
  [':', ',', '.', '/', String.fromCharCode(92)].forEach(sep => { buf = buf.split(sep).join(':'); });
  const parts = buf.split(':').filter(p => p !== '').map(p => parseFloat(p) || 0);
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] * 3600 + parts[1] * 60 + parts[2];
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
  } catch (err) {
    drop.classList.remove('has-file');
    $('drop-title').textContent = 'Выбери файл или перетащи';
    $('drop-sub').textContent = 'mp4, mov, mkv, webm · до 4 ГБ';
    setStatus(err.message, 'err');
  }
  refreshGo();
}

const presetButtons = [...document.querySelectorAll('.presets button[data-len]')];
function markPreset(len) {
  presetButtons.forEach(b => b.classList.toggle('on', Number(b.dataset.len) === len));
}
presetButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    const len = Number(btn.dataset.len);
    $('end').value = len >= 60 ? Math.floor(len / 60) + ':' + String(len % 60).padStart(2, '0')
                               : '0:' + String(len).padStart(2, '0');
    markPreset(len);
  });
});
document.querySelector('.presets .reset').addEventListener('click', () => {
  $('start').value = '0:00'; $('end').value = '0:30'; markPreset(30);
});
function syncPreset() {
  const len = Math.round(toSeconds($('end').value) - toSeconds($('start').value));
  markPreset(len);
}
[$('start'), $('end')].forEach(el => el.addEventListener('input', syncPreset));
syncPreset();

async function run() {
  resultsEl.innerHTML = '';
  emptyEl.style.display = 'none';
  const url = urlEl.value.trim();
  const source = url || uploadedPath;
  if (!source) { setStatus('Нужен файл или ссылка', 'err'); return; }
  const body = {
    source,
    start: $('start').value.trim(),
    end: $('end').value.trim(),
    fast: $('fast').checked,
    vertical: $('vertical').checked,
    layout: $('vertical').checked ? layout : 'crop'
  };
  goBtn.disabled = true; goBtn.textContent = 'Режу…';
  setStatus('Ставлю задание в очередь', 'busy');
  startTimer();
  try {
    const res = await fetch('/api/clip', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'не удалось запустить');
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
  for (;;) {
    const job = await (await fetch('/api/jobs/' + id)).json();
    renderProgress(job);
    if (job.state === 'running') {
      const percent = typeof job.fraction === 'number' ? ' · ' + Math.round(job.fraction * 100) + '%' : '';
      const stage = { download: 'Скачиваю', encode: 'Кодирую' }[job.stage] || 'Работаю';
      const detail = job.detail ? ' · ' + job.detail : '';
      setStatus(stage + percent + detail, 'busy');
    }
    if (job.state === 'error') { renderProgress(job); setStatus(job.error || 'ошибка', 'err'); return; }
    if (job.state === 'done') {
      renderProgress(job);
      hidePanel();
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
        }
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
    layout: str = "crop",
) -> None:
    try:
        _job_update(job_id, message="Готовлю источник...", stage="download", fraction=None)

        def progress(*args) -> None:
            """Accept both the bot-style (stage, fraction, detail) and a plain message."""
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
        try:
            results = clipper(
                source, segments, fast, cfg, progress, vertical=vertical, layout=layout
            )
        except TypeError:  # a custom clipper without the vertical/layout parameters (tests)
            try:
                results = clipper(source, segments, fast, cfg, progress, vertical=vertical)
            except TypeError:
                results = clipper(source, segments, fast, cfg, progress)
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
        progress=report,
    )
    try:
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
                progress=clip_progress,
            )
            result.segment = shown
            results.append(result)
        return results
    finally:
        src.cleanup()


def status_text(cfg: Config) -> str:
    """Что лежит на машине — тот же ответ, что /status у бота."""
    from .storage import describe

    lines = []
    for label, directory in (("оригиналы", cfg.cache_dir), ("клипы", cfg.output_dir)):
        items = describe(Path(directory))
        total = sum(item["size"] for item in items) / 1048576
        lines.append(f"{label}: {len(items)} файл(ов), {total:.1f} МБ")
        for item in items[:8]:
            lines.append(f"  · {item['name']} — {item['age_minutes']} мин")
    cookies = Path(cfg.cookies).expanduser() if cfg.cookies else None
    if cookies and cookies.is_file():
        age = (time.time() - cookies.stat().st_mtime) / 86400
        lines.append(f"cookies: обновлены {age:.1f} дн. назад")
    else:
        lines.append("cookies: файла нет — YouTube может требовать авторизацию")
    running = sum(1 for job in JOBS.values() if job.get("state") == "running")
    lines.append(f"в работе: {running} задание(й)")
    lines.append(f"хранение: {cfg.retention_minutes} мин тишины")
    lines.append(f"слушаю: {cfg.gui_host}:{cfg.gui_port}")
    return "\n".join(lines)


def info_text(cfg: Config) -> str:
    """Та же памятка, что бот отдаёт по /info."""
    from .bot import build_info

    return build_info(cfg)


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
            if path == "/api/status":
                self._send(200, _dumps({"text": status_text(cfg)}))
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
                    start = parse_timecode(data.get("start"))
                    end = parse_timecode(data.get("end"))
                    segments = [Segment(start, end)]
                    validate_segments(segments, duration=None)
                    vertical = bool(data.get("vertical", False))
                    layout = str(data.get("layout") or "crop")
                    if layout not in ("crop", "blur"):
                        layout = "crop"
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
