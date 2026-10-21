"""Persistent local schedules, a durable occurrence ledger, and one shared analysis runner."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from analysis_jobs import ALLOWED_ANALYSTS
from local_storage import read_json, write_json
from market_discovery import SECTORS, SYMBOL_RE
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component

UTC = pytz.UTC
TIMEZONE = "Asia/Kolkata"
TZ = pytz.timezone(TIMEZONE)


def parse_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Saved schedule timestamps must include their timezone")
    return stamp.astimezone(UTC)


def next_run(schedule, after):
    """Strictly later than after; weekly weekdays use Monday=0 through Sunday=6."""
    local = after.astimezone(TZ)
    hour, minute = map(int, schedule["time"].split(":"))
    if schedule["frequency"] == "once":
        day = datetime.strptime(schedule["run_on"], "%Y-%m-%d").date()
        candidate = TZ.localize(
            datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        )
        return candidate.astimezone(UTC) if candidate > after else None
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if schedule["frequency"] == "weekly" and day.weekday() not in schedule["weekdays"]:
            continue
        candidate = TZ.localize(
            datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        )
        if candidate > after:
            return candidate.astimezone(UTC)
    return None


class Schedules:
    def __init__(
        self, root: Path, directory: Path, jobs, catalog, *, clock=None, task_control=None
    ):
        self.root, self.path, self.jobs, self.catalog = (
            root,
            directory / "schedules.json",
            jobs,
            catalog,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.task_control = task_control or self._windows_task
        self.lock, self.tick_lock = threading.RLock(), threading.Lock()
        self.background_lock = threading.Lock()
        self.dispatch_events = {}
        self.last_background_need = None
        self.last_error = ""
        self.storage_error = ""
        self.stop_event = threading.Event()
        self.thread = None
        self.data = {
            "schema_version": 1,
            "schedules": [],
            "occurrences": [],
            "background": {"state": "not_required", "message": "No enabled schedules."},
        }
        try:
            recovered = []
            value = read_json(self.path, validate=self._valid_store, on_recover=recovered.append)
            self.data.update(value)
            if recovered:
                for item in self.data["schedules"]:
                    item["enabled"] = False
                for occurrence in self.data["occurrences"]:
                    if occurrence["status"] in {"queued", "dispatching"}:
                        occurrence.update(
                            status="cancelled",
                            message="Recovered from backup; waiting work requires a fresh user review",
                        )
                self.data["recovery_warning"] = (
                    "Schedules were recovered from a backup and paused. Review each definition before re-enabling it."
                )
                write_json(self.path, self.data, preserve_backup=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.storage_error = "Saved schedules are damaged; original files were retained. Restore schedules.json or its recovery copy and restart."
            self.data["background"] = {"state": "error", "message": self.storage_error}

    def _valid_store(self, value):
        if (
            value.get("schema_version") != 1
            or not isinstance(value.get("schedules"), list)
            or not isinstance(value.get("occurrences"), list)
        ):
            return False
        background = value.get("background", {})
        if (
            not isinstance(background, dict)
            or not isinstance(background.get("state"), str)
            or not isinstance(background.get("message"), str)
        ):
            return False
        ids = set()
        for item in value["schedules"]:
            self._validate({**item, "acknowledged": True})
            key = item.get("id", "")
            if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{16}", key) or key in ids:
                return False
            ids.add(key)
            if item.get("next_run"):
                parse_time(item["next_run"])
        for occurrence in value["occurrences"]:
            if not isinstance(occurrence, dict) or not all(
                k in occurrence
                for k in ("id", "schedule_id", "status", "run_date", "definition", "scheduled_at")
            ):
                return False
            self._validate({**occurrence["definition"], "acknowledged": True})
            parse_time(occurrence["scheduled_at"])
            if (
                datetime.strptime(occurrence["run_date"], "%Y-%m-%d").strftime("%Y-%m-%d")
                != occurrence["run_date"]
            ):
                return False
            if occurrence["status"] not in {
                "queued",
                "dispatching",
                "running",
                "completed",
                "done",
                "done_with_errors",
                "failed",
                "stopped",
                "cancelled",
                "missed",
                "skipped",
                "interrupted",
                "expired",
                "deleted",
            }:
                return False
        return True

    def _save(self):
        if self.storage_error:
            raise ValueError(self.storage_error)
        write_json(self.path, self.data)

    @contextmanager
    def _edit(self):
        with self.lock:
            previous = copy.deepcopy(self.data)
            events = {key: event.is_set() for key, event in self.dispatch_events.items()}
            try:
                yield
                self._save()
            except Exception:
                if self.data != previous:
                    self.data = previous
                for key in list(self.dispatch_events):
                    if key not in events:
                        self.dispatch_events.pop(key)
                    elif events[key]:
                        self.dispatch_events[key].set()
                    else:
                        self.dispatch_events[key].clear()
                raise

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.data)
            active = self.jobs.snapshot()
            for occurrence in value["occurrences"]:
                if occurrence["status"] in {"queued", "dispatching"}:
                    day = parse_time(occurrence["scheduled_at"]).astimezone(TZ).date()
                    occurrence["expires_at"] = TZ.localize(datetime.combine(day + timedelta(days=1), datetime.min.time())).isoformat()
                elif occurrence.get("job_id") == active.get("id") and active.get("running"):
                    occurrence["expires_at"] = active.get("expires_at")
            return {
                **value,
                "timezone": TIMEZONE,
                "now": self.clock().astimezone(TZ).isoformat(),
                "server_required": True,
                "missed_run_grace_minutes": 15,
                "error": self.last_error
                or self.storage_error
                or self.data.get("recovery_warning", ""),
            }

    def _validate(self, payload):
        if not isinstance(payload.get("name"), str):
            raise ValueError("Enter a schedule name")
        name = payload["name"].strip()[:80]
        mode, frequency = payload.get("mode"), payload.get("frequency")
        if (
            not name
            or mode not in {"individual", "bulk"}
            or frequency not in {"once", "daily", "weekly"}
        ):
            raise ValueError("Enter a name, Individual/Bulk mode, and a valid repeat option")
        time = str(payload.get("time", ""))
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            raise ValueError("Choose a time in HH:MM format (IST)")
        analysts = payload.get("analysts", [])
        if (
            not isinstance(analysts, list)
            or not analysts
            or any(not isinstance(a, str) or a not in ALLOWED_ANALYSTS for a in analysts)
        ):
            raise ValueError("Choose at least one analyst")
        weekdays = payload.get("weekdays", [])
        if not isinstance(weekdays, list) or any(
            type(day) is not int or not 0 <= day <= 6 for day in weekdays
        ):
            raise ValueError("Invalid selected days")
        if frequency == "weekly" and not weekdays:
            raise ValueError("Select at least one day of the week")
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("Enabled must be true or false")
        if enabled and payload.get("acknowledged") is not True:
            raise ValueError(
                "Acknowledge that each scheduled analysis uses your configured AI APIs"
            )
        selection = payload.get("selection", {})
        if not isinstance(selection, dict):
            raise ValueError("Choose the assets to run")
        kind = selection.get("kind")
        if kind == "symbols":
            symbols = selection.get("symbols", [])
            if not isinstance(symbols, list) or not 1 <= len(symbols) <= (
                1 if mode == "individual" else 1000
            ):
                raise ValueError(
                    "Individual schedules need one ticker; bulk schedules need 1–1,000 tickers"
                )
            if any(
                not isinstance(s, str) or not SYMBOL_RE.fullmatch(s.strip().upper())
                for s in symbols
            ):
                raise ValueError("Invalid ticker in schedule")
            symbols = list(
                dict.fromkeys(safe_ticker_component(normalize_symbol(s)) for s in symbols)
            )
            selection = {"kind": kind, "symbols": symbols}
        elif (
            mode == "bulk"
            and kind == "group"
            and selection.get("group") in {"global", "india", "crypto"}
        ):
            limit = selection.get("limit", 0)
            if type(limit) is not int or limit not in {0, 2, 10, 20, 50, 100, 200}:
                raise ValueError("Choose All or a valid asset limit")
            selection = {"kind": kind, "group": selection["group"], "limit": limit}
        elif (
            mode == "bulk"
            and kind == "sector"
            and selection.get("scope") in {"global", "india"}
            and selection.get("sector") in SECTORS
            and selection.get("limit") in {10, 20}
        ):
            selection = {k: selection[k] for k in ("kind", "scope", "sector", "limit")}
        else:
            raise ValueError(
                "Choose an individual ticker, saved bulk selection, discovery group, or sector"
            )
        run_on = str(payload.get("run_on", ""))
        if frequency == "once":
            try:
                if datetime.strptime(run_on, "%Y-%m-%d").strftime("%Y-%m-%d") != run_on:
                    raise ValueError
            except ValueError:
                raise ValueError("Choose the calendar date for this one-time run") from None
        return {
            "name": name,
            "mode": mode,
            "frequency": frequency,
            "time": time,
            "run_on": run_on,
            "weekdays": sorted(set(weekdays)),
            "timezone": TIMEZONE,
            "enabled": enabled,
            "selection": selection,
            "analysts": list(dict.fromkeys(analysts)),
        }

    def upsert(self, payload):
        item = self._validate(payload)
        upcoming = next_run(item, self.clock())
        if item["enabled"] and upcoming is None:
            raise ValueError("The selected day/time has passed. Choose a future time.")
        with self._edit():
            if self.storage_error:
                raise ValueError(self.storage_error)
            schedule_id = payload.get("id") or uuid.uuid4().hex[:16]
            previous = next((s for s in self.data["schedules"] if s["id"] == schedule_id), None)
            if payload.get("id") and not previous:
                raise ValueError("Schedule no longer exists")
            if (
                previous
                and "revision" in payload
                and payload["revision"] != previous.get("revision", 0)
            ):
                raise RuntimeError(
                    "This schedule was changed in another tab. Reload its current definition before saving."
                )
            if previous is None and len(self.data["schedules"]) >= 50:
                raise ValueError("Maximum 50 schedules")
            item.update(
                id=schedule_id,
                next_run=upcoming.isoformat() if upcoming else None,
                created_at=previous["created_at"] if previous else self.clock().isoformat(),
                updated_at=self.clock().isoformat(),
                last_status=previous.get("last_status", "never") if previous else "never",
                last_job_id=previous.get("last_job_id") if previous else None,
                revision=previous.get("revision", 0) + 1 if previous else 1,
            )
            self.data["schedules"] = [
                s for s in self.data["schedules"] if s["id"] != schedule_id
            ] + [item]
            self._cancel_pending(schedule_id, "Schedule edited or paused")
        self.sync_background()
        return item

    def _cancel_pending(self, schedule_id, reason):
        for occurrence in self.data["occurrences"]:
            if occurrence["schedule_id"] == schedule_id and occurrence["status"] in {
                "queued",
                "dispatching",
            }:
                existing = self.jobs.find_occurrence(occurrence["id"])
                if existing:
                    occurrence.update(
                        status="running" if existing["running"] else existing["state"],
                        job_id=existing["id"],
                    )
                    self._update_schedule(occurrence)
                    continue
                occurrence.update(status="cancelled", message=reason)
                self.dispatch_events.setdefault(occurrence["id"], threading.Event()).set()
                self._update_schedule(occurrence)

    def delete(self, schedule_id):
        with self._edit():
            if not any(s["id"] == schedule_id for s in self.data["schedules"]):
                raise ValueError("Schedule not found")
            self.data["schedules"] = [s for s in self.data["schedules"] if s["id"] != schedule_id]
            self._cancel_pending(schedule_id, "Schedule deleted")
        self.sync_background()

    def pause_all(self):
        with self._edit():
            for item in self.data["schedules"]:
                item["enabled"] = False
                item["revision"] = item.get("revision", 0) + 1
                self._cancel_pending(item["id"], "All schedules paused")
        self.sync_background()

    def forget_job(self, job_id, occurrence_id=None):
        with self._edit():
            for occurrence in self.data["occurrences"]:
                if occurrence.get("job_id") == job_id or (occurrence_id and occurrence["id"] == occurrence_id):
                    occurrence.update(status="deleted", job_id=None,
                                      message="Run deleted; separately saved report files are retained")
                    self._update_schedule(occurrence)
            for item in self.data["schedules"]:
                if item.get("last_job_id") == job_id:
                    item["last_job_id"] = None

    def stop_schedule(self, schedule_id):
        with self._edit():
            item = next((s for s in self.data["schedules"] if s["id"] == schedule_id), None)
            if item is None:
                raise ValueError("Schedule not found")
            item["enabled"] = False
            item["revision"] = item.get("revision", 0) + 1
            self._cancel_pending(schedule_id, "Schedule stopped")
            # Hold the job lock so a finishing job cannot be replaced before stop.
            with self.jobs.lock:
                job = self.jobs.snapshot()
                if job.get("running") and job.get("source", {}).get("schedule_id") == schedule_id:
                    self.jobs.stop()
        self.sync_background()

    def cancel_occurrence(self, occurrence_id):
        with self._edit():
            occurrence = next(
                (o for o in self.data["occurrences"] if o["id"] == occurrence_id), None
            )
            if not occurrence:
                raise ValueError("Scheduled occurrence not found")
            if occurrence["status"] not in {"queued", "dispatching"}:
                raise RuntimeError(
                    "Only waiting occurrences can be cancelled here. Use Stop queue for an active analysis."
                )
            if self.jobs.find_occurrence(occurrence_id):
                raise RuntimeError(
                    "This occurrence has already started. Use its results and Stop queue instead."
                )
            occurrence.update(
                status="cancelled",
                message="Waiting occurrence cancelled by user; future schedule times remain enabled",
            )
            self.dispatch_events.setdefault(occurrence_id, threading.Event()).set()
            self._update_schedule(occurrence)
        self.sync_background()

    def _new_occurrence(self, schedule, due, *, manual=False):
        key = f"{schedule['id']}_{due.strftime('%Y%m%d%H%M')}" + (
            "_" + uuid.uuid4().hex[:6] if manual else ""
        )
        if any(o["id"] == key for o in self.data["occurrences"]):
            return None
        item = {
            "id": key,
            "schedule_id": schedule["id"],
            "name": schedule["name"],
            "scheduled_at": due.isoformat(),
            "run_date": due.astimezone(TZ).date().isoformat(),
            "status": "queued",
            "job_id": None,
            "message": "Waiting for the analysis runner",
            "definition": copy.deepcopy(schedule),
            "manual": manual,
        }
        self.data["occurrences"].append(item)
        active = [
            o
            for o in self.data["occurrences"]
            if o["status"] in {"queued", "dispatching", "running"}
        ]
        finished = [
            o
            for o in self.data["occurrences"]
            if o["status"] not in {"queued", "dispatching", "running"}
        ]
        self.data["occurrences"] = sorted(active + finished[-500:], key=lambda o: o["scheduled_at"])
        self._update_schedule(item)
        return item

    def run_now(self, schedule_id):
        with self._edit():
            schedule = next((s for s in self.data["schedules"] if s["id"] == schedule_id), None)
            if not schedule:
                raise ValueError("Schedule not found")
            if any(
                o["schedule_id"] == schedule_id
                and o["status"] in {"queued", "dispatching", "running"}
                for o in self.data["occurrences"]
            ):
                raise RuntimeError("This schedule already has a waiting or running occurrence")
            if (
                sum(o["status"] in {"queued", "dispatching"} for o in self.data["occurrences"])
                >= 200
            ):
                raise RuntimeError("The scheduled queue is full; wait for earlier work or pause it")
            occurrence = self._new_occurrence(schedule, self.clock(), manual=True)
        self.sync_background()
        return occurrence["id"]

    def _payload(self, occurrence):
        schedule = occurrence["definition"]
        selection = schedule["selection"]
        if selection["kind"] == "symbols":
            symbols = selection["symbols"]
        elif selection["kind"] == "group":
            self.catalog.refresh()
            snapshot = self.catalog.snapshot()
            status = snapshot.get("group_status", {}).get(selection["group"], {})
            if status.get("stale", snapshot["stale"]):
                return None
            assets = snapshot["groups"][selection["group"]]
            symbols = [a["symbol"] for a in assets[: selection["limit"] or len(assets)]]
        else:
            result = self.catalog.sector(
                selection["scope"], selection["sector"], selection["limit"]
            )
            if result.get("stale"):
                raise RuntimeError(
                    "Waiting for a fresh sector snapshot; cached rankings were not analysed automatically"
                )
            assets = result["assets"]
            symbols = [a["symbol"] for a in assets]
        if not symbols:
            raise ValueError("No assets were available for this scheduled selection")
        return {
            "date": occurrence["run_date"],
            "analysts": schedule["analysts"],
            **(
                {"ticker": symbols[0]} if schedule["mode"] == "individual" else {"symbols": symbols}
            ),
        }

    def _update_schedule(self, occurrence):
        for item in self.data["schedules"]:
            if item["id"] == occurrence["schedule_id"]:
                item.update(last_status=occurrence["status"], last_job_id=occurrence.get("job_id"))

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            cleanup = getattr(self.jobs, "cleanup", None)
            expired_jobs = set(cleanup() if callable(cleanup) else [])
            with self.lock:
                if self.storage_error:
                    return
                previous = copy.deepcopy(self.data)
                for occurrence in self.data["occurrences"]:
                    if occurrence.get("job_id") in expired_jobs:
                        occurrence.update(status="expired", job_id=None,
                                          message="Unfinished run removed after its original 24-hour deadline")
                        self._update_schedule(occurrence)
                for item in self.data["schedules"]:
                    if not item["enabled"] or not item.get("next_run"):
                        continue
                    due = parse_time(item["next_run"])
                    if due > now:
                        continue
                    occurrence = self._new_occurrence(item, due)
                    if occurrence and any(
                        o["id"] != occurrence["id"]
                        and o["schedule_id"] == item["id"]
                        and o["status"] in {"queued", "dispatching", "running"}
                        for o in self.data["occurrences"]
                    ):
                        occurrence.update(
                            status="skipped",
                            message="An earlier occurrence of this schedule is still waiting or running; overlapping repeats are skipped",
                        )
                        self._update_schedule(occurrence)
                    if occurrence and (
                        now - due > timedelta(minutes=15)
                        or due.astimezone(TZ).date() != now.astimezone(TZ).date()
                    ):
                        occurrence.update(
                            status="missed",
                            message="Missed by more than 15 minutes or scheduled for a previous day; no analysis launched",
                        )
                        self._update_schedule(occurrence)
                    upcoming = next_run(item, now)
                    item["next_run"] = upcoming.isoformat() if upcoming else None
                for occurrence in self.data["occurrences"]:
                    if occurrence["status"] == "running":
                        try:
                            job = self.jobs.read(occurrence["job_id"])
                            if not job["running"]:
                                occurrence.update(
                                    status="completed" if job["state"] == "done" else job["state"],
                                    message=job["message"],
                                )
                                self._update_schedule(occurrence)
                        except ValueError:
                            occurrence.update(
                                status="deleted", job_id=None,
                                message="Saved run is no longer available (deleted or expired)"
                            )
                            self._update_schedule(occurrence)
                    if occurrence["status"] in {"queued", "dispatching"}:
                        existing = self.jobs.find_occurrence(occurrence["id"])
                        if existing:
                            occurrence.update(
                                job_id=existing["id"],
                                status="running" if existing["running"] else existing["state"],
                                message="Recovered the existing job; it was not run twice",
                            )
                            self._update_schedule(occurrence)
                        elif occurrence["run_date"] != now.astimezone(TZ).date().isoformat():
                            occurrence.update(
                                status="missed",
                                message="Runner remained busy past this scheduled day",
                            )
                            self._update_schedule(occurrence)
                today = now.astimezone(TZ).date()
                stale = {o["id"] for o in self.data["occurrences"]
                         if not o.get("job_id") and o["status"] in {"queued", "dispatching", "missed", "skipped", "cancelled"}
                         and parse_time(o["scheduled_at"]).astimezone(TZ).date() < today}
                for key in stale:
                    self.dispatch_events.setdefault(key, threading.Event()).set()
                self.data["occurrences"] = [o for o in self.data["occurrences"] if o["id"] not in stale]
                active_schedules = {o["schedule_id"] for o in self.data["occurrences"]
                                    if o["status"] in {"queued", "dispatching", "running"}}
                self.data["schedules"] = [s for s in self.data["schedules"]
                    if not (s["frequency"] == "once" and s["run_on"] < today.isoformat()
                            and not s.get("last_job_id") and s["id"] not in active_schedules)]
                if self.data != previous:
                    self._save()
                pending = [
                    o for o in self.data["occurrences"] if o["status"] in {"queued", "dispatching"}
                ]
            for occurrence in pending:
                if self.stop_event.is_set() or self.jobs.snapshot()["running"]:
                    break
                try:
                    with self.lock:
                        event = self.dispatch_events.setdefault(occurrence["id"], threading.Event())
                    payload = self._payload(occurrence)
                    if payload is None:
                        continue
                    with self.lock:
                        occurrence = next(
                            (o for o in self.data["occurrences"] if o["id"] == occurrence["id"]),
                            occurrence,
                        )
                        if self.stop_event.is_set() or occurrence["status"] not in {
                            "queued",
                            "dispatching",
                        }:
                            continue
                        occurrence["status"] = "dispatching"
                        self._save()
                    source = {
                        "type": "schedule",
                        "schedule_id": occurrence["schedule_id"],
                        "schedule_name": occurrence["name"],
                        "occurrence_id": occurrence["id"],
                        "scheduled_at": occurrence["scheduled_at"],
                    }
                    job_id = self.jobs.start(
                        payload,
                        mode=occurrence["definition"]["mode"],
                        source=source,
                        cancel_event=event,
                    )
                    with self.lock:
                        occurrence.update(
                            status="running", job_id=job_id, message="Analysis started"
                        )
                        self._update_schedule(occurrence)
                        self._save()
                    break
                except RuntimeError as exc:
                    with self.lock:
                        if occurrence["status"] not in {"cancelled", "missed"}:
                            occurrence.update(status="queued", message=str(exc))
                        self._update_schedule(occurrence)
                        self._save()
                except Exception as exc:
                    with self.lock:
                        if occurrence["status"] != "cancelled":
                            occurrence.update(
                                status="failed", message=f"{type(exc).__name__}: {exc}"
                            )
                        self._update_schedule(occurrence)
                        self._save()
            with self.lock:
                active_ids = {
                    o["id"]
                    for o in self.data["occurrences"]
                    if o["status"] in {"queued", "dispatching"}
                }
                self.dispatch_events = {
                    key: value for key, value in self.dispatch_events.items() if key in active_ids
                }
        finally:
            self.tick_lock.release()

    def _windows_task(self, enabled):
        if os.name != "nt":
            return {
                "state": "local_only",
                "message": "Leave the local server running for schedules.",
            }
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.root / "Manage-SchedulerTask.ps1"),
            "-Action",
            "Install" if enabled else "Remove",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return {
                "state": "local_only",
                "message": "Windows background task could not be registered. Keep the local server running. "
                + result.stderr.strip()[-300:],
            }
        return json.loads(result.stdout.strip())

    def _background_needed(self):
        with self.lock:
            return any(s["enabled"] and s.get("next_run") for s in self.data["schedules"]) or any(
                o["status"] in {"queued", "dispatching", "running"}
                for o in self.data["occurrences"]
            )

    def sync_background(self):
        with self.background_lock:
            if self.storage_error:
                return
            while True:
                enabled = self._background_needed()
                with self.lock:
                    if not enabled and self.data["background"]["state"] == "not_required":
                        self.last_background_need = False
                        return
                try:
                    result = self.task_control(enabled)
                except Exception as exc:
                    result = {
                        "state": "local_only",
                        "message": f"Background task unavailable ({type(exc).__name__}); keep the local server running.",
                    }
                with self.lock:
                    if enabled != self._background_needed():
                        continue
                    self.data["background"] = result
                    self.last_background_need = enabled
                    self._save()
                    return

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        self.sync_background()
        while not self.stop_event.is_set():
            try:
                self.tick()
                if self._background_needed() != self.last_background_need:
                    self.sync_background()
                self.last_error = ""
            except Exception as exc:
                self.last_error = f"Scheduler error: {type(exc).__name__}: {exc}. It will retry; saved work was retained."
            self.stop_event.wait(10)

    def stop(self):
        with self.lock:
            self.stop_event.set()
            for event in self.dispatch_events.values():
                event.set()
