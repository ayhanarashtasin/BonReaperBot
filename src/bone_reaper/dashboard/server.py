"""A loopback-only HTTP server for the dashboard.

It answers GET requests for a fixed list of paths and has no route that writes, starts or sends
anything. The bind address is a constant rather than an option, because the page has no login.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from ..settings import Settings
from .data import ReportCache, build_state

LOG = logging.getLogger("bone_reaper.dashboard")
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class DashboardServer(ThreadingHTTPServer):
    # On Windows this flag lets a second process bind a port already in use, which would leave two
    # dashboards answering at random instead of reporting the conflict.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BoneReaperDashboard"
    root: ClassVar[Path]
    settings: ClassVar[Settings]
    cache: ClassVar[ReportCache]
    lock: ClassVar[threading.Lock]

    def _send(self, status: int, content_type: str, body: bytes, *, head: bool = False) -> None:
        self.send_response(status)
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _host_is_local(self) -> bool:
        """Refuse other host names, so a web page elsewhere cannot read this through DNS tricks."""
        host = urlsplit(f"//{self.headers.get('Host', '')}").hostname
        return host in _ALLOWED_HOSTNAMES

    def _respond(self, *, head: bool) -> None:
        if not self._host_is_local():
            self._send(403, "text/plain; charset=utf-8", b"Forbidden host\n", head=head)
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            try:
                with self.lock:
                    state = build_state(self.root, self.settings, self.cache)
                body = json.dumps(state, sort_keys=True).encode("utf-8")
            except Exception:
                # The page keeps showing its last good data and says it could not refresh.
                LOG.exception("dashboard state failed")
                self._send(500, "text/plain; charset=utf-8", b"State unavailable\n", head=head)
                return
            self._send(200, "application/json; charset=utf-8", body, head=head)
        elif path in _ASSETS:
            name, content_type = _ASSETS[path]
            body = files(__package__).joinpath("static", name).read_bytes()
            self._send(200, content_type, body, head=head)
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found\n", head=head)

    def do_GET(self) -> None:
        self._respond(head=False)

    def do_HEAD(self) -> None:
        self._respond(head=True)

    def log_message(self, format: str, *args: object) -> None:
        """Polling every few seconds would bury the terminal, so requests are not logged."""


def make_server(root: Path, settings: Settings, port: int = DEFAULT_PORT) -> DashboardServer:
    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {
            "root": root.resolve(),
            "settings": settings,
            "cache": ReportCache(),
            "lock": threading.Lock(),
        },
    )
    return DashboardServer((BIND_HOST, port), handler)
