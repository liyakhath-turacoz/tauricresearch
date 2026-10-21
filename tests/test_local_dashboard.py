"""Local dashboard regression tests. All files, clocks, analysts and vendors are isolated."""

import copy
import csv
import io
import json
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

from analysis_jobs import AnalysisJobs
from automation import Schedules, next_run
from local_storage import read_json, write_json
from market_discovery import MarketCatalog, asset_from_quote
from ui_reports import _report_records

pytestmark = pytest.mark.unit


class Catalog:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"assets": {}}

    def resolve(self, symbol):
        return {
            "symbol": symbol,
            "name": "Name " + symbol,
            "asset_type": "crypto" if symbol.endswith("-USD") else "stock",
        }


class FixtureJobs(AnalysisJobs):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: datetime(2026, 10, 16, 12, tzinfo=pytz.UTC))
        super().__init__(*args, **kwargs)

    def _run_one(self, row, analysts):
        row.update(status="done", rating="Buy", recommendation="BUY")




@pytest.fixture
def schedule(tmp_path):
    clock = [pytz.UTC.localize(datetime(2026, 10, 16, 3, 59))]
    jobs = SimpleNamespace(busy=False, calls=[], records={})
    jobs.snapshot = lambda: {"running": jobs.busy}
    jobs.find_occurrence = lambda key: next(
        (v for v in jobs.records.values() if v["source"]["occurrence_id"] == key), None
    )
    jobs.read = lambda key: jobs.records[key]

    def start(payload, *, mode, source, cancel_event=None):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Cancelled")
        jobs.calls.append((copy.deepcopy(payload), mode))
        key = str(len(jobs.calls))
        jobs.records[key] = {
            "id": key,
            "running": True,
            "source": source,
            "state": "running",
            "message": "Working",
        }
        jobs.busy = True
        return key

    jobs.start = start
    scheduler = Schedules(
        tmp_path,
        tmp_path / "data",
        jobs,
        Catalog(),
        clock=lambda: clock[0],
        task_control=lambda enabled: {
            "state": "installed" if enabled else "not_required",
            "message": "Fake Windows task",
        },
    )
    return scheduler, clock, jobs


def definition(**overrides):
    return {
        "name": "Morning review",
        "mode": "individual",
        "frequency": "weekly",
        "time": "09:30",
        "weekdays": [4],
        "selection": {"kind": "symbols", "symbols": ["NVDA"]},
        "enabled": True,
        "acknowledged": True,
        "analysts": ["market"],
        **overrides,
    }


def test_atomic_recovery_retains_last_valid_json(tmp_path):
    path = tmp_path / "value.json"
    write_json(path, {"version": 1})
    write_json(path, {"version": 2})
    path.write_text("{bad", encoding="utf-8")
    assert read_json(path) == {"version": 1}
    assert path.read_text() == "{bad"


def test_atomic_invalid_number_does_not_modify_data(tmp_path):
    path = tmp_path / "value.json"
    write_json(path, {"value": 1})
    with pytest.raises(ValueError):
        write_json(path, {"value": float("nan")})
    assert read_json(path) == {"value": 1}


def test_scheduler_executes_ist_date_once_and_recovers(schedule):
    scheduler, clock, jobs = schedule
    item = scheduler.upsert(definition())
    assert item["next_run"] == "2026-10-16T04:00:00+00:00"
    clock[0] += timedelta(minutes=1)
    scheduler.tick()
    scheduler.tick()
    assert len(jobs.calls) == 1
    assert jobs.calls[0][0]["date"] == "2026-10-16"
    restored = Schedules(
        scheduler.root,
        scheduler.path.parent,
        jobs,
        Catalog(),
        clock=lambda: clock[0],
        task_control=lambda e: {},
    )
    restored.tick()
    assert len(jobs.calls) == 1


def test_scheduler_two_assets_wait_for_shared_runner(schedule):
    scheduler, clock, jobs = schedule
    scheduler.upsert(definition())
    scheduler.upsert(
        definition(
            name="Bulk", mode="bulk", selection={"kind": "symbols", "symbols": ["NVDA", "BTC-USD"]}
        )
    )
    clock[0] += timedelta(minutes=1)
    scheduler.tick()
    assert len(jobs.calls) == 1
    jobs.busy = False
    jobs.records["1"].update(running=False, state="done", message="Done")
    scheduler.tick()
    assert jobs.calls[1][0]["symbols"] == ["NVDA", "BTC-USD"]


def test_scheduler_missed_days_do_not_run(schedule):
    scheduler, clock, jobs = schedule
    scheduler.upsert(definition())
    clock[0] += timedelta(days=1)
    scheduler.tick()
    assert not jobs.calls
    assert scheduler.snapshot()["occurrences"] == []
    assert scheduler.snapshot()["schedules"][0]["last_status"] == "missed"


def test_pause_cancels_resolution_in_progress(schedule):
    scheduler, clock, jobs = schedule
    item = scheduler.upsert(definition())
    scheduler.run_now(item["id"])
    original = jobs.start

    def racing_start(*args, **kwargs):
        scheduler.pause_all()
        return original(*args, **kwargs)

    jobs.start = racing_start
    scheduler.tick()
    assert not jobs.calls
    assert scheduler.snapshot()["occurrences"][0]["status"] == "cancelled"


def test_repeat_does_not_overlap_and_run_now_is_not_duplicated(schedule):
    scheduler, clock, jobs = schedule
    item = scheduler.upsert(definition(frequency="daily"))
    scheduler.run_now(item["id"])
    with pytest.raises(RuntimeError, match="already"):
        scheduler.run_now(item["id"])
    scheduler.tick()
    clock[0] += timedelta(minutes=1)
    scheduler.tick()
    clock[0] += timedelta(days=1)
    scheduler.tick()
    assert len(jobs.calls) == 1
    assert scheduler.snapshot()["occurrences"][-1]["status"] == "skipped"




















def test_report_uses_analysis_date_and_underscore_ticker(tmp_path):
    folder = tmp_path / "reports" / "ABC_X_20261016_200000_abcdef12"
    folder.mkdir(parents=True)
    (folder / "complete_report.html").write_text("<html>Report</html>")
    (folder / "complete_report.md").write_text(
        "# Report\n- Analysis date: 2025-01-07\n- Rating: Buy\n"
    )
    legacy = tmp_path / "ABC_X" / "2025-01-07" / "reports"
    legacy.mkdir(parents=True)
    (legacy / "market.md").write_text("Analyst")
    records = _report_records(tmp_path)
    assert len(records) == 1
    assert records[0]["ticker"] == "ABC_X" and records[0]["date"] == "2025-01-07"


def test_failed_discovery_retains_age_and_search_has_no_refresh_lock(tmp_path, monkeypatch):
    import market_discovery

    catalog = MarketCatalog(tmp_path)
    asset = asset_from_quote({"symbol": "NVDA", "longName": "NVIDIA", "regularMarketPrice": 100})
    catalog.data["groups"]["global"] = [asset]
    catalog.data["group_status"]["global"]["updated_ts"] = 1
    monkeypatch.setattr(
        catalog, "_stock_lists", lambda *a: (_ for _ in ()).throw(OSError("Offline"))
    )
    monkeypatch.setattr(catalog, "_nse_pool", lambda: (_ for _ in ()).throw(OSError("Offline")))
    monkeypatch.setattr(
        market_discovery, "_screen", lambda *a, **k: (_ for _ in ()).throw(OSError("Offline"))
    )
    catalog._refresh()
    snapshot = catalog.snapshot()
    assert snapshot["group_status"]["global"]["updated_ts"] == 1
    assert snapshot["group_status"]["global"]["stale"] is True
    assert snapshot["groups"]["global"][0]["name"] == "NVIDIA"


def test_sector_cached_ten_then_twenty_explains_shortfall(tmp_path, monkeypatch):
    catalog = MarketCatalog(tmp_path)
    assets = [
        asset_from_quote(
            {"symbol": f"X{i}", "longName": f"Company {i}", "regularMarketVolume": i + 1}
        )
        for i in range(12)
    ]
    monkeypatch.setattr(catalog, "_stock_lists", lambda *a: [assets])
    assert len(catalog.sector("global", "technology", 10)["assets"]) == 10
    larger = catalog.sector("global", "technology", 20)
    assert "12/20" in larger["warning"]


@pytest.mark.parametrize(
    "weekdays,after,expected",
    [
        ([0], "2026-10-16T04:00:00+00:00", "2026-10-19T04:00:00+00:00"),
        ([4], "2026-10-16T04:00:00+00:00", "2026-10-23T04:00:00+00:00"),
        ([4], "2026-10-16T03:59:59+00:00", "2026-10-16T04:00:00+00:00"),
    ],
)
def test_next_weekday_boundary(weekdays, after, expected):
    value = next_run(definition(weekdays=weekdays), datetime.fromisoformat(after))
    assert value.isoformat() == expected


def test_cancel_waiting_does_not_pause_future_times(schedule):
    scheduler, clock, jobs = schedule
    item = scheduler.upsert(definition())
    key = scheduler.run_now(item["id"])
    scheduler.cancel_occurrence(key)
    scheduler.tick()
    assert not jobs.calls
    assert scheduler.snapshot()["schedules"][0]["enabled"]


def test_schedule_revision_refuses_an_outdated_edit(schedule):
    scheduler, clock, jobs = schedule
    original = copy.deepcopy(scheduler.upsert(definition()))
    scheduler.upsert({**original, "name": "Current name", "acknowledged": True})
    with pytest.raises(RuntimeError, match="another tab"):
        scheduler.upsert({**original, "acknowledged": True})
    assert scheduler.snapshot()["schedules"][0]["name"] == "Current name"


def test_failed_schedule_save_cannot_create_in_memory_paid_work(schedule, monkeypatch):
    scheduler, clock, jobs = schedule
    monkeypatch.setattr(scheduler, "_save", lambda: (_ for _ in ()).throw(OSError("Disk full")))
    with pytest.raises(OSError):
        scheduler.upsert(definition())
    assert scheduler.snapshot()["schedules"] == []
    assert not jobs.calls


def test_recovered_schedule_backup_is_paused_before_any_dispatch(schedule):
    scheduler, clock, jobs = schedule
    item = scheduler.upsert(definition())
    scheduler.upsert({**item, "name": "Changed", "acknowledged": True})
    scheduler.path.write_text('{"schema_version":1,"schedules":null}', encoding="utf-8")
    restored = Schedules(
        scheduler.root,
        scheduler.path.parent,
        jobs,
        Catalog(),
        clock=lambda: clock[0],
        task_control=lambda enabled: {},
    )
    clock[0] += timedelta(minutes=1)
    restored.tick()
    assert not jobs.calls
    assert all(not s["enabled"] for s in restored.snapshot()["schedules"])
    assert "recovered" in restored.snapshot()["error"]






def test_second_server_bind_failure_never_recovers_other_servers_jobs(monkeypatch):
    import local_ui

    monkeypatch.setattr(
        local_ui,
        "LocalHTTPServer",
        lambda *a: (_ for _ in ()).throw(OSError("Port already bound")),
    )
    monkeypatch.setattr(
        local_ui,
        "AppState",
        lambda: pytest.fail("Touched active server data before port ownership"),
    )
    with pytest.raises(OSError, match="bound"):
        local_ui.main()


def test_shutdown_during_symbol_resolution_does_not_start_late_worker(tmp_path):
    catalog = Catalog()
    entered, release = threading.Event(), threading.Event()

    def resolve(symbol):
        entered.set()
        assert release.wait(2)
        return {"symbol": symbol, "name": "NVIDIA", "asset_type": "stock"}

    catalog.resolve = resolve
    jobs = FixtureJobs(tmp_path, tmp_path, catalog)
    errors = []

    def start():
        try:
            jobs.start(
                {"ticker": "NVDA", "date": date.today().isoformat(), "analysts": ["market"]},
                mode="individual",
            )
        except RuntimeError as exc:
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(2)
    jobs.shutdown()
    release.set()
    worker.join(2)
    assert errors and jobs.worker is None and jobs.snapshot()["state"] == "idle"


def test_failed_and_partial_batches_are_not_reported_as_success(tmp_path):
    class Broken(FixtureJobs):
        def _run_one(self, row, analysts):
            if row["symbol"] == "NVDA":
                raise RuntimeError("Fixture failure")
            super()._run_one(row, analysts)

    jobs = Broken(tmp_path, tmp_path, Catalog())
    jobs.start(
        {"ticker": "NVDA", "date": date.today().isoformat(), "analysts": ["market"]},
        mode="individual",
    )
    jobs.wait_stopped()
    assert jobs.snapshot()["state"] == "failed"
    jobs.start(
        {"symbols": ["NVDA", "AAPL"], "date": date.today().isoformat(), "analysts": ["market"]},
        mode="bulk",
    )
    jobs.wait_stopped()
    assert jobs.snapshot()["state"] == "done_with_errors"


def test_all_running_jobs_recover_even_if_latest_json_is_corrupt(tmp_path):
    directory = tmp_path / "ui_runs"
    directory.mkdir()
    for i in (1, 2):
        key = f"20261016_12000{i}_1234abcd"
        write_json(
            directory / (key + ".json"),
            {
                "id": key,
                "running": True,
                "state": "running",
                "results": [{"symbol": "NVDA", "status": "running"}],
            },
        )
    (directory / "20261016_120003_1234abcd.json").write_text("broken", encoding="utf-8")
    jobs = FixtureJobs(tmp_path, tmp_path, Catalog())
    assert jobs.snapshot()["state"] == "interrupted"
    assert all(not jobs.read(f"20261016_12000{i}_1234abcd")["running"] for i in (1, 2))
    assert (directory / "20261016_120003_1234abcd.json").read_text() == "broken"


def test_non_usd_crypto_mode_is_explicit_cli_flag(monkeypatch):
    from typer.testing import CliRunner

    import cli.main

    received = []
    monkeypatch.setattr(cli.main, "run_analysis", lambda **kw: received.append(kw))
    result = CliRunner().invoke(cli.main.app, ["--ticker", "BTC-INR", "--asset-type", "crypto"])
    assert result.exit_code == 0, result.output
    assert received[0]["flags"]["asset_type"] == "crypto"


def test_analysis_timezone_uses_ist_day_at_utc_evening(monkeypatch):
    import tradingagents.dataflows.date_window as dates

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, 20, tzinfo=pytz.UTC).astimezone(tz)

    monkeypatch.setattr(dates, "datetime", Fixed)
    monkeypatch.setenv("TRADINGAGENTS_ANALYSIS_TIMEZONE", "Asia/Kolkata")
    assert dates.get_current_date() == "2026-10-17"


@pytest.fixture
def handler(tmp_path):
    from local_ui import SESSION_TOKEN, TOKEN, AppState, LocalUIHandler

    app = AppState(root=tmp_path, results_dir=tmp_path)
    value = LocalUIHandler.__new__(LocalUIHandler)
    value.server = SimpleNamespace(app=app)
    value.headers = {
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
        "X-Local-UI-Token": TOKEN,
        "Cookie": f"ta_session={SESSION_TOKEN}",
    }
    def record_json(code, body, **kwargs):
        value.response = (code, body)
        value.response_cookie = kwargs.get("cookie")

    value._json = record_json
    value._send = lambda code, body, kind, **kwargs: setattr(value, "response", (code, body))

    def invoke(path, payload=None, raw=None):
        value.path = path
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        value.rfile = io.BytesIO(body)
        value.headers["Content-Length"] = str(len(body))
        value.do_POST()
        return value.response

    return value, app, invoke


def test_login_required_before_private_actions(handler):
    h, app, invoke = handler
    h.headers.pop("Cookie")
    assert invoke("/api/run", {"ticker": "NVDA"})[0] == 401
    assert app.jobs.snapshot()["state"] == "idle"
    h.path = "/api/jobs"
    h.do_GET()
    assert h.response[0] == 401
    h.path = "/api/status"
    h.do_GET()
    assert h.response[0] == 200 and "results" not in h.response[1]
    h.path = "/"
    h.do_GET()
    assert b'loginForm' in h.response[1] and b'runBulk' not in h.response[1]


def test_login_and_logout_cookies(handler):
    h, app, invoke = handler
    h.headers.pop("Cookie")
    assert invoke("/api/login", {"email": "admin@turacoz.com", "password": "wrong"})[0] == 401
    assert invoke("/api/login", {"email": "admin@turacoz.com", "password": "admin"})[0] == 200
    cookie = h.response_cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    h.headers["Cookie"] = cookie.split(";", 1)[0]
    h.path = "/"
    h.do_GET()
    assert b'runBulk' in h.response[1]
    assert invoke("/api/logout")[0] == 200
    assert "Max-Age=0" in h.response_cookie


def test_stop_schedule_does_not_stop_another_schedules_job(schedule):
    scheduler, clock, jobs = schedule
    saved = scheduler.upsert(definition())
    jobs.lock = threading.RLock()
    stopped = []
    jobs.stop = lambda: stopped.append(True)
    jobs.snapshot = lambda: {"running": True, "source": {"schedule_id": "different"}}
    scheduler.stop_schedule(saved["id"])
    assert not stopped
    assert not scheduler.snapshot()["schedules"][0]["enabled"]


def test_stop_schedule_cancels_its_active_job(schedule):
    scheduler, clock, jobs = schedule
    saved = scheduler.upsert(definition())
    jobs.lock = threading.RLock()
    stopped = []
    jobs.stop = lambda: stopped.append(True)
    jobs.snapshot = lambda: {"running": True, "source": {"schedule_id": saved["id"]}}
    scheduler.stop_schedule(saved["id"])
    assert stopped == [True]
    assert not scheduler.snapshot()["schedules"][0]["enabled"]


def test_history_keeps_more_than_fifty_saved_runs(tmp_path):
    jobs = FixtureJobs(tmp_path, tmp_path, Catalog())
    for i in range(55):
        key = f"20261020_120000_{i:08x}"
        write_json(jobs.directory / (key + ".json"), {
            "id": key, "results": [], "state": "done", "mode": "individual",
            "source": {"type": "schedule", "schedule_name": "Saved fixture"},
        })
    history = jobs.history()
    assert len(history) == 55
    assert history[0]["source"]["type"] == "schedule"


def test_future_one_time_schedule_is_not_deleted_after_its_creation_day(schedule):
    scheduler, clock, jobs = schedule
    scheduler.upsert(definition(frequency="once", run_on="2026-10-23"))
    clock[0] += timedelta(days=2)
    scheduler.tick()
    assert len(scheduler.snapshot()["schedules"]) == 1
    assert not jobs.calls


def test_unrun_one_time_schedule_is_removed_after_scheduled_day(schedule):
    scheduler, clock, jobs = schedule
    scheduler.upsert(definition(frequency="once", run_on="2026-10-16"))
    clock[0] += timedelta(days=1)
    scheduler.tick()
    assert scheduler.snapshot()["schedules"] == []
    assert scheduler.snapshot()["occurrences"] == []
    assert not jobs.calls


def test_deleted_scheduled_run_cannot_be_dispatched_again(schedule):
    scheduler, clock, jobs = schedule
    scheduler.upsert(definition())
    clock[0] += timedelta(minutes=2)
    scheduler.tick()
    occurrence = scheduler.snapshot()["occurrences"][0]
    jobs.records["1"].update(running=False, state="done", message="Finished")
    jobs.busy = False
    scheduler.forget_job("1", occurrence["id"])
    jobs.records.clear()
    scheduler.tick()
    assert len(jobs.calls) == 1
    assert scheduler.snapshot()["occurrences"][0]["status"] == "deleted"


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/run", {}),
        ("/api/bulk", {"symbols": "NVDA"}),
        ("/api/schedules", {"mode": "bulk"}),
        ("/api/ranges/settings", {"horizon_hours": 2}),
        ("/api/ranges/settings", {"enabled": "false"}),
        ("/api/ranges/settings", {"horizon_hours": float("nan")}),
    ],
)
def test_api_rejects_bad_inputs_without_work(handler, path, payload):
    h, app, invoke = handler
    assert invoke(path, payload)[0] == 400
    assert app.jobs.snapshot()["state"] == "idle"


def test_api_requires_host_origin_and_token(handler):
    h, app, invoke = handler
    h.headers["Host"] = "wrong.example"
    assert invoke("/api/ranges/settings", {"enabled": True})[0] == 403
    assert not app.ranges.enabled()


def test_api_maintenance_atomically_blocks_new_work(handler):
    h, app, invoke = handler
    assert invoke("/api/maintenance")[0] == 200
    assert app.jobs.closing and app.schedules.stop_event.is_set()
    assert (
        invoke(
            "/api/run", {"ticker": "NVDA", "date": date.today().isoformat(), "analysts": ["market"]}
        )[0]
        == 409
    )


def test_api_maintenance_refuses_to_interrupt_active_analysis(handler):
    h, app, invoke = handler
    app.jobs.job = {"running": True}
    assert invoke("/api/maintenance")[0] == 409
    assert not app.jobs.closing


def test_api_bad_json_is_client_error(handler):
    h, app, invoke = handler
    assert invoke("/api/schedules", raw=b'{"bad":')[0] == 400


def test_scheduled_bulk_expires_at_next_day_without_new_analyses(tmp_path, monkeypatch):
    import analysis_jobs

    day = [date(2026, 10, 16)]
    monkeypatch.setattr(analysis_jobs, "today_ist", lambda: day[0])

    class Midnight(FixtureJobs):
        def _run_one(self, row, analysts):
            super()._run_one(row, analysts)
            day[0] += timedelta(days=1)

    jobs = Midnight(tmp_path, tmp_path, Catalog())
    jobs.start(
        {"symbols": ["NVDA", "AAPL"], "date": "2026-10-16", "analysts": ["market"]},
        mode="bulk",
        source={"type": "schedule"},
    )
    jobs.wait_stopped()
    result = jobs.snapshot()
    assert result["state"] == "expired" and result["completed"] == 1
    assert result["results"][1]["status"] == "skipped"




def test_newer_standalone_report_is_not_hidden_by_older_complete(tmp_path):
    import os

    folder = tmp_path / "reports" / "NVDA_20261016_120000"
    folder.mkdir(parents=True)
    html = folder / "complete_report.html"
    html.write_text("<html>Report</html>")
    (folder / "complete_report.md").write_text("- Analysis date: 2026-10-16\n- Rating: Buy\n")
    analyst = tmp_path / "NVDA" / "2026-10-16" / "reports" / "market.md"
    analyst.parent.mkdir(parents=True)
    analyst.write_text("New analyst report")
    os.utime(analyst, (html.stat().st_mtime + 10, html.stat().st_mtime + 10))
    assert len(_report_records(tmp_path)) == 2


def test_csv_formula_with_whitespace_is_neutralized(tmp_path):
    jobs = FixtureJobs(tmp_path, tmp_path, Catalog())
    jobs.start(
        {"ticker": "NVDA", "date": date.today().isoformat(), "analysts": ["market"]},
        mode="individual",
    )
    jobs.wait_stopped()
    jobs.job["results"][0]["summary"] = "\t=HYPERLINK(1)"
    row = next(csv.DictReader(io.StringIO(jobs.csv(jobs.job["id"]).decode("utf-8-sig"))))
    assert row["summary"].startswith("'\t=")






def test_main_import_does_not_construct_an_ai_graph(monkeypatch):
    import importlib
    import sys

    import tradingagents.graph.trading_graph as graph

    monkeypatch.setattr(
        graph, "TradingAgentsGraph", lambda **kw: pytest.fail("Import started a paid analysis")
    )
    sys.modules.pop("main", None)
    assert importlib.import_module("main").app is not None


def test_crypto_volume_falls_back_when_24h_field_is_null():
    item = asset_from_quote(
        {
            "symbol": "BTC-USD",
            "quoteType": "CRYPTOCURRENCY",
            "volume24Hr": None,
            "regularMarketVolume": 123,
        }
    )
    assert item["volume"] == 123


def test_http_server_owns_its_port_exclusively_without_reverse_dns(monkeypatch):
    import socket

    import local_ui

    monkeypatch.setattr(
        socket, "getfqdn", lambda *a: pytest.fail("Loopback startup performed reverse DNS")
    )
    server = local_ui.LocalHTTPServer(("127.0.0.1", 0), local_ui.LocalUIHandler)
    try:
        with pytest.raises(OSError):
            local_ui.LocalHTTPServer(server.server_address, local_ui.LocalUIHandler)
    finally:
        server.server_close()


@pytest.mark.parametrize("path,payload", [
    ("/api/run", {"ticker": "NVDA", "date": "2020-01-01", "analysts": []}),
    ("/api/bulk", {"symbols": ["NVDA", "AAPL"], "date": "2030-01-01", "analysts": ["market"]}),
])
def test_dashboard_ignores_stale_dates_and_always_uses_all_analysts(handler, monkeypatch, path, payload):
    import local_ui
    h, app, invoke = handler
    received = []
    monkeypatch.setattr(local_ui, "today_ist", lambda: date(2026, 10, 21))
    monkeypatch.setattr(h.server.app.jobs, "start", lambda body, **kw: received.append(body) or "fixture")
    assert invoke(path, payload)[0] == 202
    assert received[0]["date"] == "2026-10-21"
    assert set(received[0]["analysts"]) == {"market", "sentiment", "news", "fundamentals"}


def test_dashboard_schedule_defaults_do_not_capture_analysis_date(handler, monkeypatch):
    h, app, invoke = handler
    received = []
    monkeypatch.setattr(h.server.app.schedules, "upsert", lambda body: received.append(body) or body)
    assert invoke("/api/schedules", {"name": "Tomorrow", "frequency": "once", "run_on": "2026-10-22"})[0] == 200
    assert set(received[0]["analysts"]) == {"market", "sentiment", "news", "fundamentals"}
    assert received[0]["acknowledged"] is True and "date" not in received[0]
