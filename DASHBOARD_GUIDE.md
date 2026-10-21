# Local Research Dashboard

Start in `E:\TauricResearch` with `.\Start-LocalUI.ps1`, then open
http://127.0.0.1:8765. Sign in with **admin@turacoz.com / admin**.
These simple credentials are intentionally for personal local use, not production.

## Discover and Run

Use Yahoo search, Global/India/Crypto discovery, or independently fetched sector
lists. Select one ticker in Individual analysis or multiple rows in Bulk discovery.
Immediate dashboard runs use today in IST and all analysts automatically.
Crypto skips company fundamentals. Each bulk asset runs sequentially. Starting
or scheduling authorizes configured AI API usage; charges may apply.

With Schedule unchecked, Run starts immediately and can incur configured AI API
charges. While active, decisions appear in the live results card. Stop cancels the
active worker and remaining queue; it cannot retract a provider call already sent.
Terminal stopped results are not repeatedly restored into the live view.

## Schedule Without Running Now

Check Schedule in the Individual or Bulk tab. A visible banner confirms that
nothing runs immediately; timing fields appear and the Run button becomes Schedule.
The saved definition captures exactly the current selection and all analysts.
Each occurrence analyses its scheduled IST day, never the creation day.

Choose Once with a future calendar date/time, or Every day with a time. Saving only creates the
definition. The Schedules tab is a list with next time, countdown, status, recent
activity and management actions, not a second creation form.

- Reschedule changes timing only and preserves the selected assets and analysts.
- Stop disables future times, cancels waiting occurrences and stops its own active
  job without stopping a different schedule's job.
- Resume enables future occurrences, not an immediate execution.
- Delete removes the definition and preserves saved history. Stop first if you
  also want to cancel currently running work.
- Cancel waiting removes one waiting occurrence without disabling future times.

All times use Asia/Kolkata / IST. A busy runner queues same-day work. Definitions
overdue by more than 15 minutes are marked missed rather than launching stale
analyses. Unstarted occurrences cannot launch after their IST day ends and are
removed at the next check. A one-time definition that never ran is then removed.
Future one-time and repeating definitions remain valid.

The Windows heartbeat can restart the server while schedules are enabled.
The PC must be awake and the account logged in. Closing the browser or signing out
does not stop schedules. Pause all schedules before stopping the server if you
want it to stay stopped. No exchange-holiday filter is applied.

## Saved Run Views

Saved runs & reports separates Running, Stopped / failed, Completed and All runs.
Open run expands its decisions, output and CSV. Close results collapses the details.
Running entries offer Stop; terminal entries offer Delete. A run must finish
stopping before it can be deleted.

Unfinished, stopped, failed and interrupted run records expire **24 hours after
their original creation time**. A run created 16 hours ago shows about 8 hours
remaining; refreshing or restarting does not reset it. At the deadline, active
work is stopped first. Cleanup removes the terminal record and optional range
data only after the worker has stopped. Completed runs are retained.

Cleanup is server-side every approximately 10 seconds, and also on startup/history
refresh. The server must be running to perform cleanup; elapsed deadlines are
applied on the next startup after downtime. Deletion removes JSON history and
recovery copies, not separately saved completed research report files.

## Optional Session Price Ranges

Expand **Optional price estimates** using its triangle. Enable low/high estimates
and choose **15-minute**, **30-minute** or **1-hour candles**. The setting applies
to individual, bulk and scheduled runs; each job captures the setting when it
starts, not when a future schedule is created. Switching off hides both extra
columns and restores the original CSV. Older runs are never backfilled.

New results show the remaining-session low/high alongside the unchanged rating.
Expand **Remaining candles & basis** for each candle's start/end (IST), low and
high, reference timestamp, currency and sample count. These are saved snapshots,
not continuously updating prices. The horizon ends at the actual current regular
session close, never tomorrow. Crypto uses the remainder of the UTC day.

Completed Yahoo 15-minute OHLC bars are aggregated from each session's opening
into the selected interval. Estimates use 10th/90th-percentile path extrema from
at least 20 matching completed historical sessions. Missing bars are gaps; no
future/incomplete candles or overnight returns enter training. A partial current
or closing candle uses the full historical candle conservatively. These are
historical scenarios, not calibrated forecasts, guaranteed bounds or orders.

Exchange calendars exclude holidays and lunch breaks; an earlier Yahoo close
is respected. NSE/BSE use Yahoo's actual daily session metadata. Closed markets,
the first incomplete candle, unsupported exchange calendars, or missing/invalid
data show **unavailable**, not invented prices. No extra LLM calls are made.
Yahoo needs internet and intraday coverage is limited; see the
[yfinance API documentation](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html)
and [exchange calendar documentation](https://github.com/gerrymanoim/exchange_calendars).

## Files and Checks

Existing research reports remain under the configured results directory.
Run histories are in `ui_runs`, price-range sidecars in `ui_ranges`, and schedule
definitions/settings in `ui_data`. Existing legacy calculation data is not loaded
or repurposed by the new feature.

For offline checks without provider calls:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -B -m pytest -q --disable-warnings -p no:cacheprovider
node tests/test_dashboard_ui.cjs
```

The optional real-browser harness uses Chrome and test-only Node dependencies:

```powershell
npm.cmd install --prefix "$env:TEMP\TradingAgentsUiAudit" jsdom playwright --no-fund --no-audit
node tests/test_dashboard_browser.cjs
```

It starts a separate loopback server with temporary histories, scripted AI models,
real graph/report code and fixture market data. No real Windows tasks or paid
analyses are started. `TA_BROWSER_EXE` can select another Chromium executable.

Restart the dashboard after backend updates, then reload and sign in. A UI/server
version mismatch blocks new analyses with a restart instruction. To stop the local
server explicitly, use `.\Start-LocalUI.ps1 -Stop`.
