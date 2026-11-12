"""Tiny read-only dashboard: one HTML page + a few JSON endpoints.

Uses only the standard library (``ThreadingHTTPServer``) so the whole project
needs a single third-party dependency (fast-flights).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import storage
from .config import Config

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def make_handler(cfg: Config):
    expected_auth = ""
    if cfg.web_user and cfg.web_pass:
        token = base64.b64encode(f"{cfg.web_user}:{cfg.web_pass}".encode("utf-8")).decode("ascii")
        expected_auth = f"Basic {token}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "trackFlyPrice"
        protocol_version = "HTTP/1.1"

        # -- helpers -------------------------------------------------------

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _authorized(self) -> bool:
            if not expected_auth:
                return True
            header = self.headers.get("Authorization", "")
            if hmac.compare_digest(header, expected_auth):
                return True
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="trackFlyPrice"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _html(self) -> None:
            page = STATIC_DIR / "index.html"
            try:
                body = page.read_bytes()
            except OSError as exc:
                self._json({"error": f"dashboard file missing: {exc}"}, 500)
                return
            self._send_bytes(body, "text/html; charset=utf-8")

        # -- routing -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return

            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            try:
                if path in ("/", "/index.html"):
                    self._html()
                elif path == "/healthz":
                    self._json({"ok": True})
                elif path == "/api/summary":
                    self._api_summary()
                elif path == "/api/series":
                    self._api_series(query)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001 - never kill the server
                log.exception("request failed: %s", self.path)
                try:
                    self._json({"error": str(exc)}, 500)
                except OSError:
                    pass

        # -- endpoints -----------------------------------------------------

        def _api_summary(self) -> None:
            conn = storage.connect(cfg.db_path)
            try:
                self._json(storage.summary(conn, cfg.origin, cfg.destination, cfg.currency))
            finally:
                conn.close()

        def _api_series(self, query: dict[str, list[str]]) -> None:
            try:
                days = int(query.get("days", ["90"])[0])
            except ValueError:
                days = 90
            depart = query.get("depart", [""])[0] or None
            ret = query.get("return", [""])[0] or None

            conn = storage.connect(cfg.db_path)
            try:
                points = storage.series(conn, cfg.origin, cfg.destination, depart, ret, days)
            finally:
                conn.close()

            self._json(
                {
                    "route": {"origin": cfg.origin, "destination": cfg.destination, "currency": cfg.currency},
                    "depart": depart,
                    "return": ret,
                    "points": points,
                }
            )

    return Handler


def serve(cfg: Config) -> None:
    """Blocking server - call from a daemon thread when running the watcher."""
    log.info(
        "dashboard: http://%s:%d/  (basic auth: %s)",
        cfg.web_host,
        cfg.web_port,
        "on" if cfg.web_user else "off",
    )
    try:
        httpd = ThreadingHTTPServer((cfg.web_host, cfg.web_port), make_handler(cfg))
    except OSError as exc:
        log.error("dashboard could not start on %s:%d: %s", cfg.web_host, cfg.web_port, exc)
        return
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except OSError as exc:
        log.error("dashboard stopped: %s", exc)
