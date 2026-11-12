"""Tiny read-only dashboard: one HTML page + a few JSON endpoints.

Uses only the standard library (``ThreadingHTTPServer``) so the whole project
needs a single third-party dependency (fast-flights).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import storage
from .config import Config

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

IDLE_TIMEOUT_SEC = 15
MAX_WORKERS = 32
MAX_DAYS = 3650

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    ),
}


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with a hard cap on concurrent connections."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_handler(cfg: Config):
    expected_auth = ""
    if cfg.web_user and cfg.web_pass:
        token = base64.b64encode(f"{cfg.web_user}:{cfg.web_pass}".encode("utf-8")).decode("ascii")
        expected_auth = f"Basic {token}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "trackFlyPrice"
        protocol_version = "HTTP/1.1"
        timeout = IDLE_TIMEOUT_SEC  # drops idle/slow clients (slowloris)

        def version_string(self) -> str:
            return self.server_version  # keep the Python version private

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
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if body:
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
            except ValueError as exc:
                log.warning("bad request on %s: %s", self.path[:200], exc)
                self._json({"error": "bad request"}, 400)
            except Exception:  # noqa: BLE001 - never kill the server
                log.exception("request failed: %s", self.path[:200])
                try:
                    self._json({"error": "internal error"}, 500)
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
                days = max(1, min(int(query.get("days", ["90"])[0]), MAX_DAYS))
            except (TypeError, ValueError):
                days = 90
            # dated values are never interpreted as SQL, but keep them short
            depart = (query.get("depart", [""])[0] or "")[:10] or None
            ret = (query.get("return", [""])[0] or "")[:10] or None

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
    if cfg.web_host not in ("127.0.0.1", "localhost", "::1") and not cfg.web_user:
        log.warning(
            "dashboard is reachable from the network without authentication - "
            "set WEB_USER/WEB_PASS or keep WEB_HOST=127.0.0.1 and use an SSH tunnel"
        )
    try:
        httpd = BoundedThreadingHTTPServer((cfg.web_host, cfg.web_port), make_handler(cfg))
    except OSError as exc:
        log.error("dashboard could not start on %s:%d: %s", cfg.web_host, cfg.web_port, exc)
        return
    try:
        httpd.serve_forever()
    except OSError as exc:
        log.error("dashboard stopped: %s", exc)
