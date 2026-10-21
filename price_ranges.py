"""Optional same-session historical candle estimates. No orders or LLM calls."""

from __future__ import annotations

import json
import math
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import exchange_calendars as calendars
import numpy as np
import pandas as pd
import yfinance as yf

from local_storage import read_json, write_json

JOB_RE = re.compile(r"\d{8}_\d{6}_[a-f0-9]{8}")
DEFAULTS = {"enabled": False, "interval_minutes": 15}
EXCHANGES = {
    "NMS": "XNYS",
    "NGM": "XNYS",
    "NCM": "XNYS",
    "NAS": "XNYS",
    "NYQ": "XNYS",
    "NYE": "XNYS",
    "ASE": "XNYS",
    "PCX": "XNYS",
    "HKG": "XHKG",
    "SHH": "XSHG",
    "SHZ": "XSHG",
    "JPX": "XTKS",
    "SES": "XSES",
    "TAI": "XTAI",
    "KSC": "XKRX",
    "KOE": "XKRX",
    "LSE": "XLON",
    "TOR": "XTSE",
    "ASX": "XASX",
    "JKT": "XIDX",
    "GER": "XETR",
    "PAR": "XPAR",
    "AMS": "XAMS",
    "EBS": "XSWX",
}
SUFFIXES = {
    "HK": "XHKG",
    "SS": "XSHG",
    "SZ": "XSHG",
    "T": "XTKS",
    "SI": "XSES",
    "TW": "XTAI",
    "KS": "XKRX",
    "KQ": "XKRX",
    "L": "XLON",
    "TO": "XTSE",
    "AX": "XASX",
    "JK": "XIDX",
    "DE": "XETR",
    "PA": "XPAR",
    "AS": "XAMS",
    "SW": "XSWX",
}


def timestamp(value):
    result = (
        pd.Timestamp(value, unit="s", tz="UTC")
        if type(value) in {int, float}
        else pd.Timestamp(value)
    )
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Session times must include a timezone")
    return result.tz_convert("UTC")


def candle_grid(segments, minutes):
    """Clip final candles to close and never create candles during lunch breaks."""
    delta = pd.Timedelta(minutes=minutes)
    return [
        (start, min(start + delta, end))
        for begin, end in segments
        for start in pd.date_range(begin, end, freq=delta, inclusive="left")
    ]


def completed_candles(frame, sessions, minutes, now):
    """Resample inside each segment, anchored to its actual opening time."""
    blocks = []
    for segments in sessions:
        for begin, end in segments:
            grid = pd.date_range(begin, end, freq="15min", inclusive="left")
            data = frame.reindex(grid)
            grouped = data.resample(f"{minutes}min", origin=begin)
            bars = grouped.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
            expected = pd.Series(1, index=grid).resample(f"{minutes}min", origin=begin).sum()
            valid = grouped["Close"].count().eq(expected)
            ends = pd.DatetimeIndex(
                [min(t + pd.Timedelta(minutes=minutes), end) for t in bars.index]
            )
            bars.loc[~valid | (ends > now), :] = np.nan
            blocks.append(bars)
    return pd.concat(blocks) if blocks else frame.iloc[:0]


def estimate_range(frame, minutes, *, segments, sessions, now):
    """Match completed historical sessions at the same candle offset, not overnight returns."""
    if type(minutes) is not int or minutes not in {15, 30, 60}:
        raise ValueError("Choose 15-minute, 30-minute or 1-hour candles")
    now = timestamp(now)
    if not any(start <= now < end for start, end in segments):
        raise ValueError("Market is closed or on a session break; no remaining-session estimate")
    frame = frame[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    valid = np.isfinite(frame).all(axis=1) & (frame > 0).all(axis=1)
    valid &= frame["High"] >= frame[["Open", "Low", "Close"]].max(axis=1)
    valid &= frame["Low"] <= frame[["Open", "High", "Close"]].min(axis=1)
    # Missing/invalid bars remain gaps; do not invent observations across them.
    frame.loc[~valid, :] = np.nan
    frame = completed_candles(
        frame, [s for s in sessions if s[-1][1] < segments[0][0]] + [segments], minutes, now
    )
    grid = candle_grid(segments, minutes)
    offset = next(i for i, (start, end) in enumerate(grid) if start <= now < end)
    if not offset:
        raise ValueError("Wait for the first completed candle of this session")
    reference_start, reference_at = grid[offset - 1]
    reference = float(frame.reindex([reference_start])["Close"].iloc[0])
    if not math.isfinite(reference) or reference <= 0 or reference_at > now:
        raise ValueError("The latest completed reference candle is missing or invalid")
    targets = grid[offset:]
    lows, highs = [], []
    for historical in sessions:
        if historical[-1][1] >= segments[0][0]:
            continue
        needed = candle_grid(historical, minutes)[offset - 1 : offset + len(targets)]
        if len(needed) != len(targets) + 1:
            continue
        # A shorter historic candle cannot stand in for a longer target candle.
        if any(b - a < d - c for (a, b), (c, d) in zip(needed[1:], targets, strict=True)):
            continue
        sample = frame.reindex([start for start, _ in needed])
        if sample.isna().any().any():
            continue
        base = float(sample["Close"].iloc[0])
        lows.append(sample["Low"].iloc[1:].to_numpy() / base)
        highs.append(sample["High"].iloc[1:].to_numpy() / base)
    if len(lows) < 20:
        raise ValueError("At least 20 complete matching historical sessions are needed")
    lows, highs = np.asarray(lows), np.asarray(highs)
    path = [
        {
            "start": max(start, now).isoformat(),
            "end": end.isoformat(),
            "low": float(reference * np.quantile(lows[:, i], 0.1)),
            "high": float(reference * np.quantile(highs[:, i], 0.9)),
        }
        for i, (start, end) in enumerate(targets)
    ]
    return {
        "low": float(reference * np.quantile(lows.min(axis=1), 0.1)),
        "high": float(reference * np.quantile(highs.max(axis=1), 0.9)),
        "reference_price": reference,
        "reference_at": reference_at.isoformat(),
        "interval_minutes": minutes,
        "sample_windows": len(lows),
        "candles": path,
        "session_close": segments[-1][1].isoformat(),
        "calculated_at": now.isoformat(),
        "method": "remaining_session_v1",
    }


def session_segments(calendar, day):
    schedule = calendar.schedule.loc[str(day)]
    start, end = schedule["open"], schedule["close"]
    pause, resume = schedule["break_start"], schedule["break_end"]
    return (
        [(start, pause), (resume, end)] if pd.notna(pause) and pd.notna(resume) else [(start, end)]
    )


class PriceRanges:
    csv_fields = [
        "range_status",
        "range_low",
        "range_high",
        "range_currency",
        "range_interval_minutes",
        "range_reference_at",
        "range_session_close",
        "range_candles_json",
        "range_note",
    ]

    def __init__(self, results_dir, *, clock=None):
        self.directory = Path(results_dir) / "ui_ranges"
        self.settings_path = Path(results_dir) / "ui_data" / "price_range_settings.json"
        self.lock = threading.RLock()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.config = dict(DEFAULTS)
        try:
            recovered = []
            config = read_json(self.settings_path, on_recover=recovered.append)
            # Migrate the preference only; saved forecasts are never recalculated.
            if (
                isinstance(config, dict)
                and "horizon_hours" in config
                and set(config) <= {"enabled", "horizon_hours"}
            ):
                config = {"enabled": config.get("enabled", False), "interval_minutes": 60}
            self.config = self._validate(config)
            if recovered:
                self.config["enabled"] = False
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _validate(payload):
        if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
            raise ValueError("Unknown price-range setting")
        enabled, minutes = payload.get("enabled", False), payload.get("interval_minutes", 15)
        if type(enabled) is not bool or type(minutes) is not int or minutes not in {15, 30, 60}:
            raise ValueError("Use an on/off toggle and 15, 30 or 60 minute candles")
        return {"enabled": enabled, "interval_minutes": minutes}

    def settings(self):
        with self.lock:
            return dict(self.config)

    def configure(self, payload):
        with self.lock:
            if not isinstance(payload, dict):
                raise ValueError("Settings must be an object")
            config = self._validate({**self.config, **payload})
            write_json(self.settings_path, config)
            self.config = config
            return self.settings()

    def enabled(self):
        return self.settings()["enabled"]

    def _path(self, job_id):
        if not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        return self.directory / (job_id + ".json")

    def begin(self, job_id, rows):
        with self.lock:
            if self.enabled():
                write_json(
                    self._path(job_id),
                    {
                        "config": self.settings(),
                        "records": {r["symbol"]: {"status": "pending"} for r in rows},
                    },
                )

    def read(self, job_id):
        with self.lock:
            try:
                return read_json(self._path(job_id), validate=self._valid_store)
            except FileNotFoundError:
                return {"status": "not_recorded", "records": {}}
            except (OSError, ValueError):
                return {"status": "unavailable", "records": {}}

    @staticmethod
    def _valid_store(value):
        if not isinstance(value, dict) or not isinstance(value.get("records"), dict):
            return False
        for record in value["records"].values():
            if not isinstance(record, dict) or not isinstance(record.get("status"), str):
                return False
            if record["status"] != "ready":
                continue
            prices = [record.get(key) for key in ("low", "high", "reference_price")]
            if not all(type(v) in {int, float} and math.isfinite(v) and v > 0 for v in prices):
                return False
            if prices[0] > prices[1] or not isinstance(record.get("currency"), str):
                return False
            if record.get("method") == "remaining_session_v1":
                if type(record.get("interval_minutes")) is not int or record[
                    "interval_minutes"
                ] not in {15, 30, 60}:
                    return False
                candles = record.get("candles")
                if not isinstance(candles, list) or not 1 <= len(candles) <= 96:
                    return False
                try:
                    previous, close = (
                        timestamp(record["calculated_at"]),
                        timestamp(record["session_close"]),
                    )
                    for candle in candles:
                        start, end = timestamp(candle["start"]), timestamp(candle["end"])
                        if not previous <= start < end <= close:
                            return False
                        if (
                            not all(
                                type(candle[k]) in {int, float}
                                and math.isfinite(candle[k])
                                and candle[k] > 0
                                for k in ("low", "high")
                            )
                            or candle["low"] > candle["high"]
                        ):
                            return False
                        previous = end
                except (ValueError, TypeError, KeyError):
                    return False
            elif type(record.get("horizon_hours")) is not int or record["horizon_hours"] not in {
                1,
                4,
                24,
            }:
                return False
        return True

    def _history(self, row, minutes):
        now = timestamp(self.clock())
        if row["date"] != now.tz_convert("Asia/Kolkata").date().isoformat():
            raise ValueError("Remaining-session estimates are only available for today's analysis")
        ticker = yf.Ticker(row["symbol"])
        frame = ticker.history(
            period="60d", interval="15m", prepost=False, auto_adjust=True, actions=False, timeout=10
        )
        if (
            frame is None
            or frame.empty
            or not isinstance(frame.index, pd.DatetimeIndex)
            or frame.index.tz is None
        ):
            raise ValueError("No timezone-aware intraday history is available")
        metadata = ticker.history_metadata or {}
        currency = metadata.get("currency")
        if not isinstance(currency, str) or not currency:
            raise ValueError("The quote currency is unavailable")
        now = timestamp(self.clock())
        if row["date"] != now.tz_convert("Asia/Kolkata").date().isoformat():
            raise ValueError("The analysis day ended while loading price history")
        frame = frame.sort_index().tz_convert("UTC")
        if row["asset_type"] == "crypto":
            today = now.normalize()
            segments = [(today, today + pd.Timedelta(days=1))]
            sessions = [
                [(day, day + pd.Timedelta(days=1))]
                for day in pd.date_range(
                    frame.index[0].normalize(), today, freq="D", inclusive="left"
                )
            ]
        else:
            regular = (metadata.get("currentTradingPeriod") or {}).get("regular") or {}
            try:
                start, end = timestamp(regular["start"]), timestamp(regular["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "The exchange's current regular-session times are unavailable"
                ) from exc
            if not start <= now < end or not pd.Timedelta(0) < end - start <= pd.Timedelta(days=1):
                raise ValueError("Market is closed; no current-session estimate")
            suffix = row["symbol"].rsplit(".", 1)[-1] if "." in row["symbol"] else ""
            name = EXCHANGES.get(metadata.get("exchangeName")) or SUFFIXES.get(suffix)
            if name:
                calendar = calendars.get_calendar(name)
                day = now.tz_convert(calendar.tz).date()
                if not calendar.is_session(str(day)):
                    raise ValueError("The exchange calendar marks today as closed")
                segments = session_segments(calendar, day)
                # Neither provider nor calendar is allowed to extend the other's close.
                segments = [
                    (max(a, start), min(b, end)) for a, b in segments if max(a, start) < min(b, end)
                ]
                sessions = [
                    session_segments(calendar, label.date())
                    for label in calendar.sessions_in_range(
                        str(frame.index[0].tz_convert(calendar.tz).date()), str(day)
                    )
                ]
            elif suffix in {"NS", "BO"} and metadata.get("exchangeTimezoneName") == "Asia/Kolkata":
                # These exchanges have no lunch break. Yahoo supplies actual daily bounds.
                segments = [(start, end)]
                periods = metadata.get("tradingPeriods")
                if not isinstance(periods, pd.DataFrame) or not {"start", "end"} <= set(periods):
                    raise ValueError("Historical exchange session times are unavailable")
                sessions = [
                    [(timestamp(a), timestamp(b))]
                    for a, b in zip(periods["start"], periods["end"], strict=True)
                ]
            else:
                raise ValueError("Reliable exchange calendar unavailable for this market")
        if not segments or not any(a <= now < b for a, b in segments):
            raise ValueError("Market is closed or on a session break")
        return frame, {"currency": currency, "segments": segments, "sessions": sessions, "now": now}

    def enrich(self, job_id, row, cancel):
        with self.lock:
            data = self.read(job_id)
            if not self.enabled() or cancel.is_set() or data.get("status"):
                return
            minutes = data["config"].get("interval_minutes", 60)
        try:
            frame, metadata = self._history(row, minutes)
            record = {
                "status": "ready",
                "currency": metadata.pop("currency"),
                **estimate_range(frame, minutes, **metadata),
                "note": "Historical 10th/90th percentile path extrema from matching session offsets; not calibrated forecasts or guaranteed limits. Reference is the last completed candle. First and final partial candles use full historical candles conservatively. Lunch breaks and overnight hours are excluded.",
            }
        except Exception as exc:
            record = {"status": "unavailable", "note": str(exc)}
        with self.lock:
            if cancel.is_set() or not self.enabled():
                return
            data = self.read(job_id)
            if data.get("status"):
                return
            data["records"][row["symbol"]] = record
            write_json(self._path(job_id), data)

    def finalize(self, job_id, rows):
        with self.lock:
            data = self.read(job_id)
            if data.get("status"):
                return
            changed = False
            for row in rows:
                if data["records"].get(row["symbol"], {}).get("status") == "pending":
                    data["records"][row["symbol"]] = {
                        "status": "not_generated",
                        "note": "No range was generated for this result.",
                    }
                    changed = True
            if changed:
                write_json(self._path(job_id), data)

    def csv_values(self, job_id, symbol, *, data=None):
        data = self.read(job_id) if data is None else data
        record = data["records"].get(symbol, {})
        note = record.get("note", "")
        if record.get("status") == "ready" and record.get("method") != "remaining_session_v1":
            note = f"Original hourly estimate ({record.get('horizon_hours')} market hours), not a remaining-session estimate. {note}"
        return {
            "range_status": record.get("status", data.get("status", "not_recorded")),
            **{
                "range_" + key: record.get(key)
                for key in (
                    "low",
                    "high",
                    "currency",
                    "interval_minutes",
                    "reference_at",
                    "session_close",
                )
            },
            "range_candles_json": json.dumps(record["candles"]) if record.get("candles") else "",
            "range_note": note,
        }
