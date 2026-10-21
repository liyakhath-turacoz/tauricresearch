"""Hourly estimates and run lifetime checks; no network or AI provider access."""

import csv
import io
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from analysis_jobs import AnalysisJobs
from local_storage import write_json
from price_ranges import PriceRanges, estimate_range

pytestmark = pytest.mark.unit
KEY = "20261020_160000_1234abcd"
NOW = datetime(2026, 10, 20, 16, tzinfo=UTC)


def catalog():
    return SimpleNamespace(lock=threading.RLock(), data={"assets": {}})


def saved_job(*, state="stopped", created=None):
    return {
        "id": KEY,
        "date": "2026-10-20",
        "mode": "bulk",
        "state": state,
        "running": state == "running",
        "created_at": (created or NOW).isoformat(),
        "completed": 1,
        "total": 2,
        "results": [{"symbol": "NVDA", "status": "done"}],
        "message": "Fixture",
        "log": [],
    }


@pytest.fixture
def session_data():
    import exchange_calendars as calendars

    from price_ranges import candle_grid, session_segments

    calendar = calendars.get_calendar("XNYS")
    sessions = [
        session_segments(calendar, label.date())
        for label in calendar.sessions_in_range("2026-08-24", "2026-10-20")
    ]
    index = pd.DatetimeIndex([a for s in sessions for a, _ in candle_grid(s, 15)])
    close = 100 * np.exp(np.cumsum(np.random.default_rng(7).normal(0, 0.001, len(index))))
    frame = pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close}, index=index
    )
    return frame, {
        "currency": "USD",
        "segments": sessions[-1],
        "sessions": sessions,
        "now": pd.Timestamp(NOW),
    }


def estimate(data, minutes=15):
    frame, metadata = data
    return estimate_range(frame, minutes, **{k: v for k, v in metadata.items() if k != "currency"})


@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_candles_cover_only_remaining_session(session_data, minutes):
    result = estimate(session_data, minutes)
    assert 0 < result["low"] <= result["high"]
    assert result["interval_minutes"] == minutes and result["sample_windows"] >= 20
    assert pd.Timestamp(result["candles"][0]["start"]) == pd.Timestamp(NOW)
    assert pd.Timestamp(result["candles"][-1]["end"]) == session_data[1]["segments"][-1][1]
    assert all(pd.Timestamp(c["start"]) >= pd.Timestamp(NOW) for c in result["candles"])
    assert pd.Timestamp(result["reference_at"]) <= pd.Timestamp(NOW)
    assert not any("quantity" in key or "cash" in key for key in result)


@pytest.mark.parametrize("minutes", [True, 0, 24, "15", 1440])
def test_invalid_intervals_fail(session_data, minutes):
    with pytest.raises(ValueError):
        estimate(session_data, minutes)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1])
def test_invalid_reference_candle_is_not_used(session_data, value):
    frame, _ = session_data
    frame.loc[pd.Timestamp(NOW) - pd.Timedelta(minutes=15), "Close"] = value
    with pytest.raises(ValueError, match="reference"):
        estimate(session_data)


def test_invalid_training_bar_is_a_gap_not_an_invented_window(session_data):
    baseline = estimate(session_data)
    frame, metadata = session_data
    frame.loc[metadata["sessions"][10][0][0] + pd.Timedelta(hours=3), "Low"] = np.nan
    assert estimate(session_data)["sample_windows"] == baseline["sample_windows"] - 1


@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_current_incomplete_and_future_bars_never_affect_estimates(session_data, minutes):
    baseline = estimate(session_data, minutes)
    frame, _ = session_data
    frame.loc[frame.index >= pd.Timestamp(NOW), :] = 999999999.0
    assert estimate(session_data, minutes) == baseline


def test_insufficient_matching_sessions_is_unavailable(session_data):
    frame, metadata = session_data
    metadata["sessions"] = metadata["sessions"][-10:]
    with pytest.raises(ValueError, match="20"):
        estimate((frame, metadata))


@pytest.mark.parametrize("when", ["2026-10-20T12:00Z", "2026-10-20T20:00Z", "2026-10-21T13:00Z"])
def test_closed_session_never_forecasts_next_day(session_data, when):
    session_data[1]["now"] = pd.Timestamp(when)
    with pytest.raises(ValueError, match="closed"):
        estimate(session_data)


def test_first_candle_waits_for_a_completed_reference(session_data):
    session_data[1]["now"] = session_data[1]["segments"][0][0] + pd.Timedelta(minutes=5)
    with pytest.raises(ValueError, match="first completed"):
        estimate(session_data)


def test_early_close_and_lunch_break_are_not_trading_candles():
    import exchange_calendars as calendars

    from price_ranges import candle_grid, session_segments

    ny = session_segments(calendars.get_calendar("XNYS"), "2026-11-27")
    assert ny[-1][1] == pd.Timestamp("2026-11-27T18:00Z")
    hk = session_segments(calendars.get_calendar("XHKG"), "2026-10-20")
    grid = candle_grid(hk, 60)
    assert not any(a < hk[1][0] and b > hk[0][1] for a, b in grid)
    assert grid[-1][1] == hk[-1][1]


def test_indian_candles_anchor_at_0915_and_clip_close():
    from price_ranges import candle_grid

    segments = [(pd.Timestamp("2026-10-20T09:15+05:30"), pd.Timestamp("2026-10-20T15:30+05:30"))]
    grid = candle_grid(segments, 60)
    assert len(grid) == 7 and grid[0][0].minute == 15
    assert grid[-1][1] - grid[-1][0] == pd.Timedelta(minutes=15)


def test_feature_off_makes_no_requests_and_no_data(tmp_path, monkeypatch):
    ranges = PriceRanges(tmp_path)
    monkeypatch.setattr(
        ranges, "_history", lambda *a: pytest.fail("Disabled feature fetched history")
    )
    row = {"symbol": "NVDA", "date": "2026-10-20", "asset_type": "stock", "status": "done"}
    ranges.begin(KEY, [row])
    ranges.enrich(KEY, row, threading.Event())
    assert not ranges.directory.exists()


def test_range_toggle_and_csv_preserve_original_rating(tmp_path, session_data, monkeypatch):
    ranges = PriceRanges(tmp_path)
    ranges.configure({"enabled": True, "interval_minutes": 30})
    row = {
        "symbol": "NVDA",
        "date": "2026-10-20",
        "asset_type": "stock",
        "status": "done",
        "rating": "Underweight",
        "recommendation": "SELL",
    }
    monkeypatch.setattr(ranges, "_history", lambda *a: (session_data[0], dict(session_data[1])))
    ranges.begin(KEY, [row])
    ranges.enrich(KEY, row, threading.Event())
    assert ranges.read(KEY)["records"]["NVDA"]["interval_minutes"] == 30
    jobs = AnalysisJobs(tmp_path, tmp_path, catalog(), ranges, clock=lambda: NOW)
    job = saved_job(state="done")
    job["results"] = [row]
    jobs.job = job
    enabled = next(csv.DictReader(io.StringIO(jobs.csv(KEY).decode("utf-8-sig"))))
    assert (
        enabled["range_low"]
        and enabled["range_candles_json"]
        and enabled["rating"] == "Underweight"
    )
    ranges.configure({"enabled": False})
    disabled = next(csv.DictReader(io.StringIO(jobs.csv(KEY).decode("utf-8-sig"))))
    assert len(disabled) == 10 and not any(key.startswith("range_") for key in disabled)
    assert ranges.read("20261020_160001_1234abcd")["status"] == "not_recorded"


def test_missing_history_does_not_invent_a_range(tmp_path, monkeypatch):
    ranges = PriceRanges(tmp_path)
    ranges.configure({"enabled": True})
    row = {"symbol": "NVDA", "date": "2026-10-20", "asset_type": "stock", "status": "done"}
    ranges.begin(KEY, [row])
    monkeypatch.setattr(ranges, "_history", lambda *a: (_ for _ in ()).throw(ValueError("No data")))
    ranges.enrich(KEY, row, threading.Event())
    record = ranges.read(KEY)["records"]["NVDA"]
    assert record["status"] == "unavailable" and "low" not in record


@pytest.mark.parametrize("record", ["damaged", {"status": "ready", "low": float("nan")}])
def test_damaged_range_record_is_unavailable_not_a_fabricated_price(tmp_path, record):
    ranges = PriceRanges(tmp_path)
    path = ranges.directory / (KEY + ".json")
    path.parent.mkdir()
    import json

    path.write_text(json.dumps({"records": {"NVDA": record}}), encoding="utf-8")
    assert ranges.read(KEY)["status"] == "unavailable"


def test_old_hourly_preference_migrates_but_does_not_rewrite_saved_forecasts(tmp_path):
    path = tmp_path / "ui_data" / "price_range_settings.json"
    write_json(path, {"enabled": True, "horizon_hours": 24})
    ranges = PriceRanges(tmp_path)
    assert ranges.settings() == {"enabled": True, "interval_minutes": 60}
    record = {
        "status": "ready",
        "low": 90,
        "high": 110,
        "reference_price": 100,
        "currency": "USD",
        "horizon_hours": 24,
    }
    write_json(ranges._path(KEY), {"records": {"NVDA": record}})
    assert ranges.read(KEY)["records"]["NVDA"] == record
    assert "Original hourly" in ranges.csv_values(KEY, "NVDA")["range_note"]


@pytest.mark.parametrize("asset_type,symbol", [("stock", "NVDA"), ("crypto", "BTC-USD")])
def test_history_uses_timezone_and_completed_intraday_data(
    tmp_path, monkeypatch, session_data, asset_type, symbol
):
    import price_ranges as module

    frame, metadata = session_data
    start, end = metadata["segments"][0]
    calls = []
    fake = SimpleNamespace(
        history=lambda **kw: calls.append(kw) or frame,
        history_metadata={
            "currency": "USD",
            "exchangeName": "NMS",
            "exchangeTimezoneName": "America/New_York",
            "currentTradingPeriod": {
                "regular": {"start": start.timestamp(), "end": end.timestamp()}
            },
        },
    )
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake)
    ranges = PriceRanges(tmp_path, clock=lambda: NOW)
    data, result = ranges._history(
        {"symbol": symbol, "date": "2026-10-20", "asset_type": asset_type}, 30
    )
    assert calls[0]["interval"] == "15m" and calls[0]["prepost"] is False
    assert result["currency"] == "USD"
    assert result["segments"][-1][1] == (
        pd.Timestamp("2026-10-21T00:00Z") if asset_type == "crypto" else end
    )


def test_old_analysis_does_not_request_live_history(tmp_path, monkeypatch):
    import price_ranges as module

    monkeypatch.setattr(
        module.yf, "Ticker", lambda *a: pytest.fail("Requested live data for past day")
    )
    with pytest.raises(ValueError, match="today"):
        PriceRanges(tmp_path, clock=lambda: NOW)._history({"date": "2026-10-19"}, 15)


def test_original_creation_deadline_does_not_reset_on_restart(tmp_path):
    created = NOW - timedelta(hours=16)
    write_json(tmp_path / "ui_runs" / (KEY + ".json"), saved_job(created=created))
    jobs = AnalysisJobs(tmp_path, tmp_path, catalog(), clock=lambda: NOW)
    assert datetime.fromisoformat(jobs.snapshot()["expires_at"]) - NOW == timedelta(hours=8)
    restarted = AnalysisJobs(tmp_path, tmp_path, catalog(), clock=lambda: NOW + timedelta(hours=1))
    assert restarted.snapshot()["expires_at"] == jobs.snapshot()["expires_at"]


def test_expiry_removes_history_backups_and_range_data_not_report_files(tmp_path):
    clock = [NOW]
    jobs = AnalysisJobs(tmp_path, tmp_path, catalog(), clock=lambda: clock[0])
    path = jobs.directory / (KEY + ".json")
    write_json(path, saved_job())
    write_json(path, saved_job())
    write_json(tmp_path / "ui_ranges" / (KEY + ".json"), {"records": {}})
    report = tmp_path / "reports" / "saved.html"
    report.parent.mkdir()
    report.write_text("Completed research", encoding="utf-8")
    clock[0] += timedelta(hours=24)
    assert jobs.cleanup() == [KEY]
    assert not path.exists() and not path.with_suffix(".json.bak").exists()
    assert not (tmp_path / "ui_ranges" / (KEY + ".json")).exists()
    assert report.read_text() == "Completed research"


def test_completed_runs_do_not_expire(tmp_path):
    write_json(
        tmp_path / "ui_runs" / (KEY + ".json"),
        saved_job(state="done", created=NOW - timedelta(days=7)),
    )
    jobs = AnalysisJobs(tmp_path, tmp_path, catalog(), clock=lambda: NOW)
    assert jobs.history()[0]["expires_at"] is None


def test_active_run_must_finish_stopping_before_deletion(tmp_path):
    jobs = AnalysisJobs(tmp_path, tmp_path, catalog(), clock=lambda: NOW)
    jobs.job = saved_job(state="running")
    jobs._save()
    with pytest.raises(RuntimeError):
        jobs.delete(KEY)
    jobs.job["running"] = False
    jobs.job["state"] = "stopped"
    jobs._save()
    jobs.delete(KEY)
    assert jobs.snapshot()["state"] == "idle"


def test_expiry_stops_worker_before_removing_its_record(tmp_path):
    clock = [NOW]
    jobs = AnalysisJobs(tmp_path, tmp_path, catalog(), clock=lambda: clock[0])
    jobs.job = saved_job(state="running")
    jobs._save()
    clock[0] += timedelta(hours=24)
    assert jobs.cleanup() == []
    assert jobs.cancel.is_set() and jobs.job["running"]
    jobs.job.update(running=False, state="expired")
    jobs._save()
    assert jobs.cleanup() == [KEY]


@pytest.mark.parametrize("job_id", ["../outside", "/etc/passwd", "not-a-job"])
def test_delete_cannot_escape_history_directory(tmp_path, job_id):
    with pytest.raises(ValueError):
        AnalysisJobs(tmp_path, tmp_path, catalog()).delete(job_id)
