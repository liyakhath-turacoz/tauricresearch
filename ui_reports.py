"""Local views for saved HTML and Markdown reports."""

import re
from datetime import datetime
from pathlib import Path
from html import escape as html_escape
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.report_html import _markdown as _report_markdown

RESULTS_DIR = Path(DEFAULT_CONFIG["results_dir"]).expanduser().resolve()
REPORTS_DIR = RESULTS_DIR / "reports"
TICKER_RE = re.compile(r"^[A-Za-z0-9.^=_&+-]{1,32}$")


def _report_records(results_dir=None) -> list[dict]:
    results_dir = Path(results_dir) if results_dir is not None else RESULTS_DIR
    reports_dir = results_dir / "reports"
    records = []
    completed_dates: dict[tuple[str, str], float] = {}
    candidates: list[tuple[float, dict]] = []
    if reports_dir.is_dir():
        for folder in reports_dir.iterdir():
            report = folder / "complete_report.html"
            markdown = folder / "complete_report.md"
            if (
                not folder.is_dir()
                or not report.is_file()
                or folder.resolve().parent != reports_dir.resolve()
            ):
                continue
            name_match = re.fullmatch(r"(.+)_(\d{8})_(\d{6})(?:_[a-f0-9]+)?", folder.name)
            ticker = name_match.group(1) if name_match else folder.name.partition("_")[0]
            analysis_date = ""
            rating = ""
            if markdown.is_file():
                try:
                    text = markdown.read_text(encoding="utf-8")
                    match = re.search(r"^- Rating: (.+)$", text, re.M)
                    rating = match.group(1).strip() if match else ""
                    match = re.search(r"^- Analysis date:\s*(\d{4}-\d{2}-\d{2})\s*$", text, re.M)
                    analysis_date = match.group(1) if match else ""
                except OSError:
                    pass
            if analysis_date:
                key = (ticker.upper(), analysis_date.replace("-", ""))
                completed_dates[key] = max(completed_dates.get(key, 0), report.stat().st_mtime)
            candidates.append(
                (
                    report.stat().st_mtime,
                    {
                        "id": folder.name,
                        "ticker": ticker,
                        "date": analysis_date,
                        "generated": datetime.fromtimestamp(report.stat().st_mtime)
                        .astimezone()
                        .strftime("%Y-%m-%d %H:%M"),
                        "rating": rating,
                        "kind": "complete",
                    },
                )
            )

    # The CLI also keeps its individual analyst reports under
    # logs/<ticker>/<YYYY-MM-DD>/reports/*.md. Include those runs even when
    # the user chose not to save a consolidated HTML report.
    if results_dir.is_dir():
        for ticker_dir in results_dir.iterdir():
            if not ticker_dir.is_dir() or not TICKER_RE.fullmatch(ticker_dir.name):
                continue
            for date_dir in ticker_dir.iterdir():
                if not date_dir.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir.name):
                    continue
                reports_dir = date_dir / "reports"
                markdown_files = sorted(reports_dir.glob("*.md")) if reports_dir.is_dir() else []
                if not markdown_files:
                    continue
                timestamp = max(file.stat().st_mtime for file in markdown_files)
                complete_time = completed_dates.get(
                    (ticker_dir.name.upper(), date_dir.name.replace("-", ""))
                )
                if complete_time is not None and timestamp <= complete_time + 1:
                    continue
                candidates.append(
                    (
                        timestamp,
                        {
                            "id": f"legacy_{ticker_dir.name}_{date_dir.name.replace('-', '')}",
                            "ticker": ticker_dir.name,
                            "date": date_dir.name,
                            "generated": datetime.fromtimestamp(timestamp)
                            .astimezone()
                            .strftime("%Y-%m-%d %H:%M"),
                            "rating": "",
                            "kind": "analyst_reports",
                            "files": len(markdown_files),
                        },
                    )
                )

    for _, record in sorted(candidates, key=lambda item: item[0], reverse=True):
        records.append(record)
    return records


def _legacy_report_html(report_id: str, results_dir=None) -> bytes | None:
    results_dir = Path(results_dir).resolve() if results_dir is not None else RESULTS_DIR
    match = re.fullmatch(r"legacy_([A-Za-z0-9.^=_&+-]{1,32})_(\d{8})", report_id)
    if not match:
        return None
    ticker, stamp = match.groups()
    run_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    report_dir = (results_dir / ticker / run_date / "reports").resolve()
    if report_dir.parent.parent.parent != results_dir or not report_dir.is_dir():
        return None
    sections = []
    md = _report_markdown()
    for markdown_file in sorted(report_dir.glob("*.md")):
        try:
            content = markdown_file.read_text(encoding="utf-8")
        except OSError:
            continue
        sections.append(
            f"<section><h2>{html_escape(markdown_file.stem.replace('_', ' ').title())}</h2>"
            f"<article>{md.render(content)}</article></section>"
        )
    if not sections:
        return None
    page = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html_escape(ticker)} analyst reports</title>"
        "<style>*{box-sizing:border-box}body{max-width:920px;margin:0 auto;padding:38px 22px 70px;background:#fbfbfa;color:#1c2126;font:16px/1.7 system-ui,-apple-system,'Segoe UI',sans-serif}h1{margin:0 0 28px;font-size:34px;line-height:1.2;letter-spacing:-.03em}section{margin:22px 0;padding:24px 28px;background:#fff;border:1px solid #e2e5e8;border-radius:12px;box-shadow:0 5px 18px #1c212608}section h2{margin:0 0 18px;padding-bottom:12px;border-bottom:1px solid #e2e5e8;color:#087a59;font-size:21px;line-height:1.3}article h1,article h2,article h3,article h4{margin:1.5em 0 .5em;letter-spacing:-.02em;line-height:1.3}article h1{font-size:1.7em}article h2{font-size:1.45em;color:#1c2126;border:0;padding:0}article h3{font-size:1.2em;color:#087a59}article p,article ul,article ol,article blockquote{margin:.65em 0 1em}article ul,article ol{padding-left:1.5em}article li{margin:.25em 0}article strong{font-weight:700}article blockquote{padding:.2em 1em;border-left:3px solid #d3d8dc;color:#5d6670}article a{color:#087a59;overflow-wrap:anywhere}article code{padding:.12em .35em;border-radius:4px;background:#f2f4f5;font: .9em ui-monospace,Consolas,monospace}article pre{overflow:auto;padding:14px;border-radius:7px;background:#f2f4f5}article pre code{padding:0;background:transparent}.table{overflow-x:auto;margin:1em 0}table{width:100%;border-collapse:collapse;font-size:.9em}th,td{padding:9px 11px;border-bottom:1px solid #e2e5e8;text-align:left;vertical-align:top}th{font-weight:700;background:#f6f7f7}hr{border:0;border-top:1px solid #e2e5e8;margin:1.5em 0}@media(max-width:600px){body{padding:22px 12px 48px}h1{font-size:27px}section{padding:18px 15px}}</style>"
        f"<h1>{html_escape(ticker)} analyst reports · {run_date}</h1>"
        + "".join(sections)
        + "</html>"
    )
    return page.encode("utf-8")
