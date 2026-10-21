"""Disposable browser-test server. Never uses production histories or Windows tasks.

Only the test entry point exposes clock controls; the application does not.
The real LangGraph and report writer run with scripted models and vendor fixtures.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exchange_calendars as calendars
import numpy as np
import pandas as pd
from pytest import MonkeyPatch
from test_graph_end_to_end import ScriptedModel, _graph, offline

import analysis_jobs
import local_ui
from analysis_jobs import AnalysisJobs
from automation import Schedules
from price_ranges import PriceRanges, candle_grid, session_segments


class Catalog:
    def __init__(self):
        self.lock = threading.RLock()
        self.assets = [self.resolve(s) for s in ("NVDA", "AAPL")]
        self.data = {"assets": {a["symbol"]: a for a in self.assets}}

    def resolve(self, symbol):
        if not symbol or symbol == "INVALID":
            raise ValueError("Unknown test ticker")
        return {
            "symbol": symbol,
            "name": symbol + " fixture",
            "asset_type": "stock",
            "price": 100,
            "currency": "USD",
            "volume": 1000,
            "change_percent": 1,
            "tags": [],
        }

    def refresh(self):
        pass

    def snapshot(self):
        return {
            "state": "ready",
            "groups": {"global": self.assets, "india": [], "crypto": []},
            "coverage": [],
            "warnings": [],
            "stale": False,
            "updated_at": "2026-10-21T15:00Z",
        }

    def search(self, query):
        return [a for a in self.assets if query.upper() in a["symbol"]]

    def sector(self, scope, sector, limit):
        return {
            "scope": scope,
            "sector": sector,
            "assets": self.assets[:limit],
            "stale": False,
            "ranking": "Fixture order",
            "warning": "",
        }


def serve():
    root = Path(__file__).resolve().parents[1]
    clock = [datetime(2026, 10, 21, 15, tzinfo=UTC)]
    with (
        tempfile.TemporaryDirectory(prefix="ta-browser-") as directory,
        MonkeyPatch.context() as patch,
    ):
        results = Path(directory)
        called = offline.__wrapped__(patch, results)
        patch.setattr(
            analysis_jobs,
            "today_ist",
            lambda: clock[0].astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).date(),
        )
        patch.setattr(local_ui, "today_ist", analysis_jobs.today_ist)
        metrics = {"graphs": 0, "model_calls": 0}

        class GraphJobs(AnalysisJobs):
            def _run_one(self, row, analysts):
                if row["symbol"] == "SLOW":
                    self.cancel.wait(30)
                    return
                model = ScriptedModel(structured=True)
                graph = _graph(results / "graph", patch, model)
                graph.config["results_dir"] = str(results)
                state, rating = graph.propagate(row["symbol"], row["date"])
                for key in (
                    "market_report",
                    "sentiment_report",
                    "news_report",
                    "fundamentals_report",
                    "investment_plan",
                    "trader_investment_plan",
                    "final_trade_decision",
                ):
                    assert state[key], key
                path = graph.default_report_path(row["symbol"])
                graph.save_reports(state, row["symbol"], path, html=True)
                metrics["graphs"] += 1
                metrics["model_calls"] += len(model.calls)
                row.update(
                    status="done",
                    rating=rating,
                    recommendation="BUY",
                    report_id=path.name,
                    summary="Scripted full-graph test; not investment research.",
                )

        catalog = Catalog()
        ranges = PriceRanges(results, clock=lambda: clock[0])
        calendar = calendars.get_calendar("XNYS")
        sessions = [
            session_segments(calendar, day.date())
            for day in calendar.sessions_in_range("2026-08-24", "2026-10-21")
        ]
        index = pd.DatetimeIndex([a for segments in sessions for a, _ in candle_grid(segments, 15)])
        prices = 100 * np.exp(np.cumsum(np.random.default_rng(10).normal(0, 0.001, len(index))))
        frame = pd.DataFrame(
            {"Open": prices, "Close": prices, "High": prices * 1.01, "Low": prices * 0.99},
            index=index,
        )
        ranges._history = lambda *args: (
            frame,
            {
                "currency": "USD",
                "segments": sessions[-1],
                "sessions": sessions,
                "now": pd.Timestamp(clock[0]),
            },
        )
        jobs = GraphJobs(root, results, catalog, ranges, clock=lambda: clock[0])
        schedules = Schedules(
            root,
            results / "ui_data",
            jobs,
            catalog,
            clock=lambda: clock[0],
            task_control=lambda enabled: {
                "state": "installed",
                "message": "Isolated test task; Windows is untouched",
            },
        )
        app = SimpleNamespace(
            results_dir=results, catalog=catalog, jobs=jobs, ranges=ranges, schedules=schedules
        )

        class Handler(local_ui.LocalUIHandler):
            def do_POST(self):
                if self.path == "/__test/tick" and self._authorized() and self._logged_in():
                    clock[0] += timedelta(minutes=1)
                    schedules.tick()
                    return self._json(200, schedules.snapshot())
                return super().do_POST()

            def do_GET(self):
                if self.path == "/__test/metrics" and self._logged_in():
                    return self._json(200, {**metrics, "vendor_methods": sorted(called)})
                return super().do_GET()

        server = local_ui.LocalHTTPServer(("127.0.0.1", 0), Handler)
        server.app = app
        local_ui.PORT = server.server_address[1]
        print(
            json.dumps({"url": f"http://127.0.0.1:{local_ui.PORT}", "isolated": True}), flush=True
        )
        try:
            server.serve_forever(poll_interval=0.05)
        finally:
            jobs.shutdown()
            jobs.wait_stopped()
            server.server_close()


if __name__ == "__main__":
    serve()
