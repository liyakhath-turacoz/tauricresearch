"""Local HTTP dashboard: Yahoo discovery, individual analysis and sequential bulk jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, unquote, urlparse

from analysis_jobs import ALLOWED_ANALYSTS, AnalysisJobs, today_ist
from automation import Schedules
from market_discovery import MarketCatalog
from price_ranges import PriceRanges
from tradingagents.default_config import DEFAULT_CONFIG
from ui_reports import _legacy_report_html, _report_records

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8765
TOKEN = secrets.token_urlsafe(32)
SESSION_TOKEN = secrets.token_urlsafe(32)
LOGIN_EMAIL = "admin@turacoz.com"
LOGIN_PASSWORD = "admin"
UI_VERSION = 5
PROJECT_ID = hashlib.sha256(str(ROOT).lower().encode("utf-8")).hexdigest()[:10].upper()
PID_PATH = Path(os.environ.get("TEMP", str(ROOT))) / "TradingAgentsLocalUI.pid"
RESULTS_DIR = Path(DEFAULT_CONFIG["results_dir"]).expanduser().resolve()
REPORTS_DIR = RESULTS_DIR / "reports"


class AppState:
    def __init__(self, root=ROOT, results_dir=RESULTS_DIR):
        self.results_dir = Path(results_dir)
        self.catalog = MarketCatalog(self.results_dir / "ui_data")
        self.ranges = PriceRanges(self.results_dir)
        self.jobs = AnalysisJobs(Path(root), self.results_dir, self.catalog, ranges=self.ranges)
        self.schedules = Schedules(
            Path(root), self.results_dir / "ui_data", self.jobs, self.catalog
        )


class LocalHTTPServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR allows a second listener to take the same port.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        TCPServer.server_bind(self)
        # HTTPServer's normal reverse-DNS lookup is unnecessary for loopback and
        # can stall startup on a disconnected/misconfigured Windows resolver.
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class LocalUIHandler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsLocalUI/2.0"

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body, content_type, *, cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, status, data, *, cookie=None):
        self._send(
            status,
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
            cookie=cookie,
        )

    def _logged_in(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookies.get("ta_session")
            return bool(value and hmac.compare_digest(value.value, SESSION_TOKEN))
        except (CookieError, TypeError):
            return False

    def _authorized(self):
        return (
            self.headers.get("Host", "") == f"{HOST}:{PORT}"
            and hmac.compare_digest(self.headers.get("X-Local-UI-Token", ""), TOKEN)
            and self.headers.get("Origin", "") == f"http://{HOST}:{PORT}"
        )

    def do_GET(self):
        catalog, jobs, schedules, ranges = (
            self.server.app.catalog,
            self.server.app.jobs,
            self.server.app.schedules,
            self.server.app.ranges,
        )
        if self.headers.get("Host", "") != f"{HOST}:{PORT}":
            return self._json(403, {"error": "Open the local dashboard at http://127.0.0.1:8765"})
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            if not self._logged_in():
                if route.path == "/api/status":
                    return self._json(200, {
                        "app_id": "TradingAgentsLocalUI", "project_id": PROJECT_ID, "ui_version": UI_VERSION,
                        "server_pid": os.getpid(), "running": jobs.snapshot().get("running", False),
                        "accepting_runs": not jobs.closing,
                    })
                if route.path.startswith("/api/"):
                    return self._json(401, {"error": "Please sign in to your local dashboard."})
                if route.path not in {"/login.js", "/local_ui.css"}:
                    page = (ROOT / "LOGIN.html").read_text(encoding="utf-8").replace("__LOCAL_UI_TOKEN__", TOKEN)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if route.path == "/":
                page = (
                    (ROOT / "LOCAL_UI.html")
                    .read_text(encoding="utf-8")
                    .replace("__LOCAL_UI_TOKEN__", TOKEN)
                )
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if route.path in {"/local_ui.css", "/local_ui.js", "/scheduling_ui.js", "/price_ranges_ui.js", "/login.js"}:
                kind = "text/css" if route.path.endswith(".css") else "application/javascript"
                return self._send(
                    200, (ROOT / route.path[1:]).read_bytes(), kind + "; charset=utf-8"
                )
            if route.path == "/api/status":
                return self._json(
                    200,
                    {
                        **jobs.snapshot(),
                        "app_id": "TradingAgentsLocalUI",
                        "ui_version": UI_VERSION,
                        "project_id": PROJECT_ID,
                        "server_pid": os.getpid(),
                        "accepting_runs": not jobs.closing,
                    },
                )
            if route.path == "/api/schedules":
                return self._json(200, schedules.snapshot())
            if route.path == "/api/ranges/settings":
                return self._json(200, ranges.settings())
            if route.path == "/api/discovery":
                catalog.refresh()
                return self._json(200, catalog.snapshot())
            if route.path == "/api/search":
                return self._json(200, {"assets": catalog.search(query.get("q", [""])[0])})
            if route.path == "/api/resolve":
                return self._json(200, catalog.resolve(query.get("symbol", [""])[0]))
            if route.path == "/api/sectors":
                return self._json(
                    200,
                    catalog.sector(
                        query.get("scope", ["global"])[0],
                        query.get("sector", ["technology"])[0],
                        int(query.get("limit", ["20"])[0]),
                    ),
                )
            if route.path == "/api/jobs":
                return self._json(200, {"jobs": jobs.history()})
            if route.path.startswith("/api/jobs/"):
                pieces = route.path.split("/")
                if len(pieces) == 5 and pieces[4] == "ranges":
                    jobs.read(pieces[3])
                    return self._json(200, ranges.read(pieces[3]))
                if len(pieces) == 5 and pieces[4] == "csv":
                    return self._send(200, jobs.csv(pieces[3]), "text/csv; charset=utf-8")
                if len(pieces) == 4:
                    return self._json(200, jobs.read(pieces[3]))
            if route.path == "/api/reports":
                records = _report_records(self.server.app.results_dir)
                with catalog.lock:
                    for record in records:
                        record["name"] = (
                            catalog.data["assets"]
                            .get(record["ticker"], {})
                            .get("name", record["ticker"])
                        )
                return self._json(200, {"reports": records})
            if route.path.startswith("/report/"):
                report_id = unquote(route.path[len("/report/") :])
                record = next(
                    (
                        r
                        for r in _report_records(self.server.app.results_dir)
                        if r["id"] == report_id
                    ),
                    None,
                )
                if not record:
                    return self._send(404, b"Report not found", "text/plain")
                if report_id.startswith("legacy_"):
                    page = _legacy_report_html(report_id, self.server.app.results_dir)
                else:
                    reports_dir = (self.server.app.results_dir / "reports").resolve()
                    target = (reports_dir / report_id / "complete_report.html").resolve()
                    if target.parent.parent != reports_dir:
                        return self._send(404, b"Report not found", "text/plain")
                    page = target.read_bytes()
                if page is None:
                    return self._send(404, b"Report not found", "text/plain")
                with catalog.lock:
                    name = (
                        catalog.data["assets"]
                        .get(record["ticker"], {})
                        .get("name", record["ticker"])
                    )
                # Add a name in the local view; the saved source report stays intact.
                banner = f'<div style="padding:12px 20px;font:14px system-ui;background:#101a2a;color:#e6edf6">{escape(name)} · {escape(record["ticker"])} · {escape(record["date"])}</div>'
                text = page.decode("utf-8")
                text = (
                    text.replace("<body>", "<body>" + banner, 1)
                    if "<body>" in text
                    else text.replace("</style>", "</style>" + banner, 1)
                )
                return self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")
            return self._send(404, b"Not found", "text/plain")
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            return self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        catalog, jobs, schedules, ranges = (
            self.server.app.catalog,
            self.server.app.jobs,
            self.server.app.schedules,
            self.server.app.ranges,
        )
        if not self._authorized():
            return self._json(
                403,
                {
                    "error": "Use the local dashboard for this action. Refresh the page after a server restart."
                },
            )
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096:
                    raise ValueError("Invalid login request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or payload.get("email") != LOGIN_EMAIL or payload.get("password") != LOGIN_PASSWORD:
                    return self._json(401, {"error": "Incorrect email or password."})
                return self._json(200, {"ok": True}, cookie=f"ta_session={SESSION_TOKEN}; HttpOnly; SameSite=Strict; Path=/")
            if not self._logged_in():
                return self._json(401, {"error": "Please sign in to your local dashboard."})
            if path == "/api/logout":
                return self._json(200, {"ok": True}, cookie="ta_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
            if path.startswith("/api/jobs/"):
                parts = path.split("/")
                if len(parts) == 5 and parts[4] in {"delete", "stop"}:
                    job = jobs.read(parts[3])
                    if parts[4] == "stop":
                        with jobs.lock:
                            if not jobs.job or jobs.job["id"] != parts[3] or not jobs.job.get("running"):
                                raise RuntimeError("This run is not active")
                            jobs.stop()
                    else:
                        if job.get("running"):
                            raise RuntimeError("Stop this run before deleting it")
                        schedules.forget_job(parts[3], (job.get("source") or {}).get("occurrence_id"))
                        jobs.delete(parts[3])
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "Unknown run action"})
            if path == "/api/stop":
                jobs.stop()
                return self._json(200, {"ok": True})
            if path == "/api/maintenance":
                jobs.enter_maintenance()
                schedules.stop()
                return self._json(200, {"ok": True})
            if path == "/api/shutdown":
                schedules.stop()
                jobs.shutdown()
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/discovery/refresh":
                catalog.refresh(force=True)
                return self._json(202, {"ok": True})
            if path == "/api/schedules/pause-all":
                schedules.pause_all()
                return self._json(200, schedules.snapshot())
            if path == "/api/schedules/background":
                schedules.sync_background()
                return self._json(200, schedules.snapshot())
            if path.startswith("/api/schedules/"):
                parts = path.split("/")
                if len(parts) == 6 and parts[3] == "occurrences" and parts[5] == "cancel":
                    schedules.cancel_occurrence(parts[4])
                    return self._json(200, schedules.snapshot())
                if len(parts) == 5 and parts[4] in {"delete", "run", "stop"}:
                    if parts[4] == "delete":
                        schedules.delete(parts[3])
                    elif parts[4] == "stop":
                        schedules.stop_schedule(parts[3])
                    else:
                        schedules.run_now(parts[3])
                    return self._json(200, schedules.snapshot())
                return self._json(404, {"error": "Unknown schedule action"})
            if path not in {"/api/run", "/api/bulk", "/api/schedules", "/api/ranges/settings"}:
                return self._json(404, {"error": "Unknown action"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 262144:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request must be an object")
            if path == "/api/ranges/settings":
                return self._json(200, ranges.configure(payload))
            if path == "/api/schedules":
                if not payload.get("id"):
                    payload["analysts"] = sorted(ALLOWED_ANALYSTS)
                payload["acknowledged"] = True
                return self._json(200, schedules.upsert(payload))
            # Dashboard runs are current-day only; historical CLI runs stay supported.
            payload.update(date=today_ist().isoformat(), analysts=sorted(ALLOWED_ANALYSTS))
            job_id = jobs.start(payload, mode="bulk" if path == "/api/bulk" else "individual")
            return self._json(202, {"ok": True, "job_id": job_id})
        except (ValueError, TypeError) as exc:
            return self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self._json(409, {"error": str(exc)})
        except Exception as exc:
            return self._json(
                503, {"error": f"Could not complete action: {type(exc).__name__}: {exc}"}
            )


def main():
    if (ROOT / ".scheduler-maintenance").exists():
        raise RuntimeError("Scheduled maintenance is in progress; the local server was not started")
    server = LocalHTTPServer((HOST, PORT), LocalUIHandler)
    server.daemon_threads = True
    # Acquire the port before loading/recovering jobs; a second launch must never
    # mark another live server's work interrupted before its bind fails.
    try:
        if (ROOT / ".scheduler-maintenance").exists():
            raise RuntimeError("Scheduled maintenance began during startup")
        server.app = AppState()
    except Exception:
        server.server_close()
        raise
    catalog, jobs, schedules = server.app.catalog, server.app.jobs, server.app.schedules
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    catalog.refresh()
    schedules.start()
    print(f"TradingAgents dashboard: http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        schedules.stop()
        jobs.shutdown()
        jobs.wait_stopped()
        server.server_close()
        try:
            if PID_PATH.read_text(encoding="ascii").strip() == str(os.getpid()):
                PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
