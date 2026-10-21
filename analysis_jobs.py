"""Single and bulk jobs share one sequential loop around the existing CLI."""

from __future__ import annotations

import copy
import csv
import io
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytz

from local_storage import read_json, write_json
from tradingagents.agents.rating import parse_rating
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component

ALLOWED_ANALYSTS = {"market", "sentiment", "news", "fundamentals"}
JOB_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[a-f0-9]{8}$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ACTION = {"Buy": "BUY", "Overweight": "BUY", "Hold": "HOLD", "Underweight": "SELL", "Sell": "SELL"}


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_ist():
    return datetime.now(pytz.timezone("Asia/Kolkata")).date()


class AnalysisJobs:
    def __init__(self, root: Path, results_dir: Path, catalog, ranges=None, *, asset_timeout=5400, clock=None):
        self.root, self.results_dir, self.catalog = root, results_dir, catalog
        self.ranges = ranges
        self.directory = results_dir / "ui_runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.process = None
        self.worker = None
        self.closing = False
        self.asset_timeout = asset_timeout
        self.clock = clock or (lambda: datetime.now(UTC))
        self.job = None
        files = sorted(self.directory.glob("*.json"), reverse=True)
        for saved_path in files:
            try:
                loaded = self._read_saved(saved_path)
                if loaded.get("running"):
                    for row in loaded["results"]:
                        if row["status"] in {"queued", "running"}:
                            row.update(
                                status="interrupted",
                                error="The local server exited before this asset finished.",
                            )
                    loaded.update(
                        running=False,
                        state="interrupted",
                        finished_at=timestamp(),
                        message="Previous job was interrupted. Completed results are retained.",
                    )
                    write_json(saved_path, loaded)
                    if self.ranges:
                        try:
                            self.ranges.finalize(loaded["id"], loaded["results"])
                        except (OSError, ValueError):
                            pass  # Optional data must not prevent base-job recovery.
                if self.job is None:
                    self.job = loaded
            except (ValueError, OSError, KeyError, TypeError):
                continue
        self.cleanup()

    def expires_at(self, job):
        if job.get("state") in {"done", "done_with_errors"}:
            return None
        try:
            created = datetime.fromisoformat(job["created_at"])
        except (KeyError, TypeError, ValueError):
            try:
                created = datetime.strptime(job["id"][:15], "%Y%m%d_%H%M%S").astimezone()
            except (KeyError, TypeError, ValueError):
                return None
        if created.tzinfo is None:
            created = created.astimezone()
        return created + timedelta(hours=24)

    def delete(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        with self.lock:
            if self.job and self.job["id"] == job_id and self.job.get("running"):
                raise RuntimeError("Stop this run and wait for its worker to finish before deleting")
            path = self.directory / (job_id + ".json")
            if not path.exists() and not (self.job and self.job["id"] == job_id):
                raise ValueError("Saved job not found")
            # Remove recovery files first so a deleted run cannot be resurrected.
            paths = [path]
            for folder in ("ui_ranges", "ui_paper"):
                paths.append(self.results_dir / folder / (job_id + ".json"))
            for item in paths:
                for suffix in (".bak", ".corrupt"):
                    item.with_suffix(item.suffix + suffix).unlink(missing_ok=True)
            for item in reversed(paths):
                item.unlink(missing_ok=True)
            if self.job and self.job["id"] == job_id:
                self.job = None

    def cleanup(self):
        removed = []
        with self.lock:
            if self.job and self.job.get("running"):
                deadline = self.expires_at(self.job)
                if deadline and self.clock() >= deadline and not self.job.get("expiry_requested"):
                    self.job["expiry_requested"] = True
                    self.stop()
                    self.job["message"] = "24-hour deadline reached. Stopping unfinished work."
                    self._save()
            for path in list(self.directory.glob("*.json")):
                try:
                    if self.job and self.job.get("running") and self.job["id"] == path.stem:
                        continue
                    data = self._read_saved(path)
                    deadline = self.expires_at(data)
                    if deadline and self.clock() >= deadline and not data.get("running"):
                        self.delete(data["id"])
                        removed.append(data["id"])
                except (OSError, ValueError):
                    continue
        return removed

    @staticmethod
    def _read_saved(path):
        return read_json(
            path,
            validate=lambda v: (
                isinstance(v.get("id"), str)
                and bool(JOB_ID_RE.fullmatch(v["id"]))
                and v["id"] == path.stem
                and isinstance(v.get("results"), list)
                and all(
                    isinstance(row, dict)
                    and isinstance(row.get("symbol"), str)
                    and row.get("status")
                    for row in v["results"]
                )
            ),
        )

    def _save(self):
        with self.catalog.lock:
            for row in self.job["results"]:
                name = self.catalog.data["assets"].get(row["symbol"], {}).get("name")
                if name and name != row["symbol"]:
                    row["name"] = name
        path = self.directory / (self.job["id"] + ".json")
        write_json(path, self.job)

    def _display_copy(self, job):
        result = copy.deepcopy(job)
        deadline = self.expires_at(job)
        result["expires_at"] = deadline.isoformat() if deadline else None
        with self.catalog.lock:
            for row in result["results"]:
                name = self.catalog.data["assets"].get(row["symbol"], {}).get("name")
                if name:
                    row["name"] = name
        return result

    def snapshot(self):
        with self.lock:
            if self.job is None:
                return {
                    "state": "idle",
                    "running": False,
                    "message": "Ready for individual or bulk analysis.",
                    "results": [],
                    "log": [],
                    "total": 0,
                    "completed": 0,
                }
            return self._display_copy(self.job)

    def history(self):
        self.cleanup()
        entries = []
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                data = self._read_saved(path)
                deadline = self.expires_at(data)
                data["expires_at"] = deadline.isoformat() if deadline else None
                entries.append(
                    {
                        key: data.get(key)
                        for key in (
                            "id",
                            "date",
                            "mode",
                            "state",
                            "total",
                            "completed",
                            "created_at",
                            "source",
                            "expires_at",
                            "running",
                        )
                    }
                )
            except (ValueError, OSError):
                continue
        return entries

    def read(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        with self.lock:
            if self.job and self.job["id"] == job_id:
                return self.snapshot()
        try:
            return self._display_copy(self._read_saved(self.directory / (job_id + ".json")))
        except (OSError, ValueError):
            raise ValueError("Saved job not found") from None

    def find_occurrence(self, occurrence_id):
        with self.lock:
            if self.job and self.job.get("source", {}).get("occurrence_id") == occurrence_id:
                return self.snapshot()
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                value = self._read_saved(path)
                if value.get("source", {}).get("occurrence_id") == occurrence_id:
                    return value
            except (ValueError, OSError):
                continue
        return None

    def start(self, payload, *, mode, source=None, cancel_event=None):
        if mode not in {"individual", "bulk"}:
            raise ValueError("Choose Individual or Bulk mode")
        run_date = str(payload.get("date", ""))
        try:
            parsed = date.fromisoformat(run_date)
            if parsed.isoformat() != run_date or parsed > today_ist():
                raise ValueError
        except ValueError:
            raise ValueError("Choose an analysis date no later than today") from None
        analysts = payload.get("analysts", [])
        if (
            not isinstance(analysts, list)
            or not analysts
            or any(not isinstance(a, str) or a not in ALLOWED_ANALYSTS for a in analysts)
        ):
            raise ValueError("Select at least one valid analyst")
        symbols = payload.get("symbols", []) if mode == "bulk" else [payload.get("ticker", "")]
        if (
            not isinstance(symbols, list)
            or not 1 <= len(symbols) <= 1000
            or any(not isinstance(s, str) for s in symbols)
        ):
            raise ValueError("Select between 1 and 1,000 assets")
        symbols = list(dict.fromkeys(safe_ticker_component(normalize_symbol(s)) for s in symbols))
        with self.lock:
            if self.closing or (cancel_event and cancel_event.is_set()):
                raise RuntimeError("Analysis start was cancelled")
            if self.job and self.job["running"]:
                raise RuntimeError(
                    "An analysis is already running. Stop it or wait for completion."
                )
        assets = [self.catalog.resolve(symbol) for symbol in symbols]
        if all(a["asset_type"] == "crypto" for a in assets) and set(analysts) == {"fundamentals"}:
            raise ValueError(
                "Company fundamentals are unavailable for cryptocurrencies. Select market, sentiment, or news."
            )
        with self.lock:
            if self.closing or (cancel_event and cancel_event.is_set()):
                raise RuntimeError("The local server is shutting down")
            if source and source.get("type") == "schedule" and parsed != today_ist():
                raise ValueError(
                    "The scheduled day ended while resolving assets; no analysis was started"
                )
            if self.job and self.job["running"]:
                raise RuntimeError("An analysis is already running")
            self.cancel.clear()
            previous = self.job
            self.job = {
                "id": datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8],
                "mode": mode,
                "state": "running",
                "running": True,
                "date": run_date,
                "analysts": list(dict.fromkeys(analysts)),
                "created_at": self.clock().isoformat(),
                "finished_at": None,
                "total": len(assets),
                "completed": 0,
                "ticker": None,
                "message": f"Queued {len(assets)} asset(s).",
                "log": [],
                "results": [
                    {
                        "symbol": a["symbol"],
                        "name": a["name"],
                        "asset_type": a["asset_type"],
                        "date": run_date,
                        "status": "queued",
                        "rating": None,
                        "recommendation": None,
                        "trader_action": None,
                        "summary": "",
                        "report_id": None,
                        "error": "",
                        "started_at": None,
                        "finished_at": None,
                    }
                    for a in assets
                ],
            }
            if source:
                self.job["source"] = copy.deepcopy(source)
            if self.ranges:
                try:
                    self.ranges.begin(self.job["id"], self.job["results"])
                except Exception as exc:
                    self.job["log"].append(
                        f"Optional price-range setup unavailable: {type(exc).__name__}: {exc}"
                    )
            try:
                self._save()
                self.worker = threading.Thread(target=self._run, daemon=True)
                self.worker.start()
            except Exception:
                failed = self.job
                failed.update(
                    running=False,
                    state="failed",
                    message="Could not start the analysis worker",
                    finished_at=timestamp(),
                )
                try:
                    self._save()
                finally:
                    self.job = previous
                raise
            return self.job["id"]

    def enter_maintenance(self):
        with self.lock:
            if self.job and self.job["running"]:
                raise RuntimeError("An analysis is running. Stop its queue before maintenance.")
            self.closing = True

    def shutdown(self):
        with self.lock:
            self.closing = True
            self.stop()

    def wait_stopped(self):
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=8)

    def stop(self):
        with self.lock:
            if not self.job or not self.job["running"]:
                return
            self.cancel.set()
            self.job.update(
                state="stopping",
                message="Stopping the active analysis and cancelling the remaining queue…",
            )
            if self.process and self.process.poll() is None:
                self.process.terminate()
            self._save()

    def _log(self, line):
        line = ANSI.sub("", line).rstrip("\r\n")
        if not line:
            return
        with self.lock:
            self.job["log"].append(line)
            self.job["log"] = self.job["log"][-250:]

    def _reports(self, symbol):
        directory = self.results_dir / "reports"
        pattern = re.compile(re.escape(symbol) + r"_\d{8}_\d{6}(?:_[a-f0-9]+)?$")
        return {
            p.name: p
            for p in directory.glob(symbol + "_*")
            if pattern.fullmatch(p.name) and p.is_dir() and (p / "complete_report.html").is_file()
        }

    def _run_one(self, row, analysts):
        before = set(self._reports(row["symbol"]))
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "TRADINGAGENTS_ANALYSIS_TIMEZONE": "Asia/Kolkata",
        }
        command = [
            sys.executable,
            str(self.root / "analysis_worker.py"),
            str(os.getpid()),
            "-m",
            "cli.main",
            "--ticker",
            row["symbol"],
            "--date",
            row["date"],
            "--asset-type",
            row["asset_type"],
            "--analysts",
            ",".join(analysts),
            "--save",
            "--no-show",
            "--html",
            "--no-checkpoint",
        ]
        with self.lock:
            if self.cancel.is_set():
                return
            self.process = subprocess.Popen(
                command,
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            proc = self.process
        timed_out = threading.Event()

        def expire():
            if proc.poll() is None:
                timed_out.set()
                proc.kill()

        watchdog = threading.Timer(self.asset_timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in proc.stdout:
                self._log(line)
            code = proc.wait()
        finally:
            watchdog.cancel()
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            with self.lock:
                self.process = None
        if self.cancel.is_set():
            row.update(status="cancelled", error="Stopped by user")
            return
        if code:
            if timed_out.is_set():
                raise RuntimeError(
                    f"CLI exceeded the {self.asset_timeout // 60:g}-minute per-asset limit; remaining queue will continue"
                )
            raise RuntimeError(f"CLI exited with code {code}. See the job output.")
        reports = self._reports(row["symbol"])
        fresh = [path for name, path in reports.items() if name not in before]
        if not fresh:
            raise RuntimeError(
                "CLI ended without saving a fresh HTML report. An older recommendation was not reused."
            )
        report = max(fresh, key=lambda path: path.stat().st_mtime)
        decision = (report / "5_portfolio" / "decision.md").read_text(encoding="utf-8")
        trader = report / "3_trading" / "trader.md"
        trader_text = trader.read_text(encoding="utf-8") if trader.is_file() else ""
        report_text = (report / "complete_report.md").read_text(encoding="utf-8")
        recorded_date = re.search(r"^- Analysis date:\s*(\d{4}-\d{2}-\d{2})\s*$", report_text, re.M)
        if recorded_date and recorded_date.group(1) != row["date"]:
            raise RuntimeError(
                "The fresh report has a different analysis date; its decision was not reused"
            )
        explicit = re.search(
            r"^- Rating:\s*(Buy|Overweight|Hold|Underweight|Sell|REVIEW)\s*$",
            report_text,
            re.M | re.I,
        )
        rating = (
            explicit.group(1).capitalize()
            if explicit and explicit.group(1).upper() != "REVIEW"
            else "REVIEW"
            if explicit
            else parse_rating(decision)
        )
        action = re.search(
            r"FINAL TRANSACTION PROPOSAL:\s*(?:\*\*)?(BUY|HOLD|SELL)\b", trader_text, re.I
        )
        summary = re.search(
            r"\*\*Executive Summary\*\*:\s*(.*?)(?=\n\s*\*\*(?:Investment Thesis|Price Target|Time Horizon)\*\*:|\Z)",
            decision,
            re.S,
        )
        row.update(
            status="done",
            rating=rating,
            recommendation=ACTION.get(rating, "REVIEW"),
            trader_action=action.group(1).upper() if action else None,
            report_id=report.name,
            summary=summary.group(1).strip() if summary else "",
        )

    def _run(self):
        expired = False
        try:
            for index, row in enumerate(self.job["results"]):
                with self.lock:
                    if self.cancel.is_set():
                        break
                    if (
                        self.job.get("source", {}).get("type") == "schedule"
                        and self.job["date"] != today_ist().isoformat()
                    ):
                        expired = True
                        break
                    row.update(status="running", started_at=timestamp())
                    self.job.update(
                        ticker=row["symbol"],
                        message=f"{index + 1}/{self.job['total']} · {row['name']} ({row['symbol']})",
                        log=[],
                    )
                    self._save()
                    analysts = [
                        a
                        for a in self.job["analysts"]
                        if not (row["asset_type"] == "crypto" and a == "fundamentals")
                    ]
                try:
                    if not analysts:
                        raise ValueError(
                            "Fundamentals is unavailable for cryptocurrencies; select market, sentiment, or news."
                        )
                    self._run_one(row, analysts)
                    if self.ranges and row["status"] == "done":
                        try:
                            self.ranges.enrich(self.job["id"], row, self.cancel)
                        except Exception as exc:
                            self._log(
                                f"Optional price range unavailable: {type(exc).__name__}: {exc}"
                            )
                except Exception as exc:
                    row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                    self._log(row["error"])
                    with self.lock:
                        row["log_tail"] = "\n".join(self.job["log"][-50:])[-12000:]
                with self.lock:
                    row["finished_at"] = timestamp()
                    self.job["completed"] = index + 1
                    self._save()
        except Exception as exc:
            self._log(f"Queue runner error: {type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                for row in self.job["results"]:
                    if row["status"] in {"queued", "running"}:
                        row.update(
                            status="skipped"
                            if expired
                            else "cancelled"
                            if self.cancel.is_set()
                            else "interrupted"
                        )
                        if expired:
                            row["error"] = (
                                "The scheduled day ended before this asset started; no agent analysis was started for it."
                            )
                failures = sum(row["status"] == "failed" for row in self.job["results"])
                if self.ranges:
                    try:
                        self.ranges.finalize(self.job["id"], self.job["results"])
                    except Exception as exc:
                        self._log(f"Price-range finalization unavailable: {type(exc).__name__}: {exc}")
                interrupted = any(row["status"] == "interrupted" for row in self.job["results"])
                state = (
                    "expired"
                    if expired or self.job.get("expiry_requested")
                    else "stopped"
                    if self.cancel.is_set()
                    else "failed"
                    if failures == self.job["total"] or interrupted
                    else "done_with_errors"
                    if failures
                    else "done"
                )
                self.job.update(
                    state=state,
                    running=False,
                    finished_at=timestamp(),
                    message="24-hour deadline reached. Unfinished run will be removed; report files are retained."
                    if self.job.get("expiry_requested")
                    else "Scheduled day ended. Unstarted assets were skipped; completed results are retained."
                    if expired
                    else "Queue stopped. Completed results are saved."
                    if self.cancel.is_set()
                    else "Queue ended unexpectedly. Check the saved rows and runner output."
                    if interrupted
                    else f"Finished {self.job['completed']}/{self.job['total']} assets · {failures} failed. Ready for another run.",
                )
                self.process = None
                self._save()

    def csv(self, job_id):
        job = self.read(job_id)
        output = io.StringIO(newline="")
        fields = [
            "name",
            "symbol",
            "asset_type",
            "date",
            "recommendation",
            "rating",
            "trader_action",
            "status",
            "summary",
            "error",
        ]
        include_ranges = self.ranges is not None and self.ranges.enabled()
        range_data = self.ranges.read(job_id) if include_ranges else None
        if include_ranges:
            fields += self.ranges.csv_fields
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in job["results"]:
            if include_ranges:
                row = {**row, **self.ranges.csv_values(job_id, row["symbol"], data=range_data)}
            writer.writerow(
                {
                    k: "'" + v
                    if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                    else v
                    for k, v in row.items()
                }
            )
        return output.getvalue().encode("utf-8-sig")
