"""Tiny HTTP CONNECT bridge in front of the SOCKS5 proxy.

Why: ffmpeg (and therefore yt-dlp's section downloads) speaks only HTTP proxies —
it cannot use a SOCKS5 proxy. On this host there is no direct route out, so any
ffmpeg network read fails with "Input/output error". This bridge listens on
127.0.0.1 and tunnels every CONNECT to the SOCKS5 upstream; point
``http_proxy``/``https_proxy`` at it and ffmpeg works.

Only CONNECT (HTTPS) is tunnelled, which is what video downloads use.
"""

from __future__ import annotations

import logging
import selectors
import socket
import socketserver
import threading
from urllib.parse import urlparse

log = logging.getLogger("video_clipper")

BUFFER = 65536
CONNECT_TIMEOUT = 30


class _Tunnel(socketserver.BaseRequestHandler):
    upstream: tuple[str, int] | None = None  # (host, port) of the SOCKS5 proxy

    def _open_upstream(self, host: str, port: int) -> socket.socket:
        if self.upstream:
            import socks

            sock = socks.socksocket()
            sock.set_proxy(socks.SOCKS5, self.upstream[0], self.upstream[1], rdns=True)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((host, port))
            return sock
        return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)

    def handle(self) -> None:
        try:
            request_line = self.request.recv(4096).decode("latin-1")
        except OSError:
            return
        if not request_line:
            return
        first = request_line.splitlines()[0] if request_line else ""
        if not first.upper().startswith("CONNECT "):
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        target = first.split()[1]
        host, _, port_text = target.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        try:
            upstream = self._open_upstream(host, port)
        except Exception as exc:  # noqa: BLE001 - reported to the client
            log.warning("Мост: не удалось подключиться к %s:%s — %s", host, port, exc)
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
            return

        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self._pump(self.request, upstream)

    def _pump(self, client: socket.socket, upstream: socket.socket) -> None:
        sel = selectors.DefaultSelector()
        sel.register(client, selectors.EVENT_READ, upstream)
        sel.register(upstream, selectors.EVENT_READ, client)
        try:
            while True:
                events = sel.select(timeout=120)
                if not events:
                    break
                for key, _ in events:
                    try:
                        data = key.fileobj.recv(BUFFER)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        key.data.sendall(data)
                    except OSError:
                        return
        finally:
            sel.close()
            try:
                upstream.close()
            except OSError:
                pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class HttpBridge:
    """Running bridge: use ``url`` as http_proxy/https_proxy for ffmpeg."""

    def __init__(self, proxy_url: str | None, host: str = "127.0.0.1", port: int = 0):
        self.proxy_url = proxy_url
        self._host = host
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Мост не запущен")
        return f"http://{self._host}:{self._server.server_address[1]}"

    def start(self) -> str:
        upstream = None
        if self.proxy_url:
            parsed = urlparse(self.proxy_url)
            if parsed.scheme in {"socks5", "socks5h"} and parsed.hostname:
                upstream = (parsed.hostname, parsed.port or 1080)
        handler = type("_TunnelWithUpstream", (_Tunnel,), {"upstream": upstream})
        self._server = _Server((self._host, self._port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.debug("HTTP-мост для ffmpeg: %s → %s", self.url, upstream or "напрямую")
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
