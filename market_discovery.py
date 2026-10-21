"""Cached discovery and names for the local dashboard. Market quotes are Yahoo data."""

from __future__ import annotations

import csv
import copy
import io
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
import yfinance as yf
from yfinance.data import YfData
from yfinance.const import EQUITY_SCREENER_EQ_MAP
from tradingagents.dataflows.symbols import normalize_symbol
from local_storage import read_json, write_json

SECTORS = {
    "technology": "Technology",
    "healthcare": "Healthcare",
    "financial-services": "Financial Services",
    "consumer-cyclical": "Consumer Cyclical",
    "consumer-defensive": "Consumer Defensive",
    "communication-services": "Communication Services",
    "industrials": "Industrials",
    "basic-materials": "Basic Materials",
    "energy": "Energy",
    "real-estate": "Real Estate",
    "utilities": "Utilities",
}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=_&+-]{1,32}$")
INDUSTRY_SECTORS = {
    "Information Technology": "Technology",
    "Healthcare": "Healthcare",
    "Financial Services": "Financial Services",
    "Fast Moving Consumer Goods": "Consumer Defensive",
    "Telecommunication": "Communication Services",
    "Media, Entertainment & Publication": "Communication Services",
    "Automobile and Auto Components": "Consumer Cyclical",
    "Consumer Durables": "Consumer Cyclical",
    "Consumer Services": "Consumer Cyclical",
    "Textiles": "Consumer Cyclical",
    "Oil Gas & Consumable Fuels": "Energy",
    "Power": "Utilities",
    "Realty": "Real Estate",
    "Metals & Mining": "Basic Materials",
    "Chemicals": "Basic Materials",
    "Construction Materials": "Basic Materials",
    "Capital Goods": "Industrials",
    "Construction": "Industrials",
    "Services": "Industrials",
    "Diversified": "Industrials",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def number(value):
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def asset_from_quote(q: dict, *, default_type="stock") -> dict | None:
    raw_symbol = q.get("symbol")
    if not isinstance(raw_symbol, str):
        return None
    symbol = raw_symbol.upper()
    if not SYMBOL_RE.fullmatch(symbol):
        return None
    crypto = str(q.get("quoteType", "")).upper() == "CRYPTOCURRENCY" or symbol.endswith(
        ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")
    )
    name = (
        q.get("longName")
        or q.get("longname")
        or q.get("shortName")
        or q.get("shortname")
        or q.get("displayName")
        or q.get("name")
        or symbol
    )
    return {
        "symbol": symbol,
        "name": str(name),
        "asset_type": "crypto" if crypto else default_type,
        "price": number(q.get("regularMarketPrice", q.get("fulldayPrice"))),
        "change_percent": number(
            q.get(
                "regularMarketChangePercent",
                q.get("regularMarketPercentChange", q.get("fulldayChangePercent")),
            )
        ),
        "volume": number(
            q.get("volume24Hr")
            if crypto and q.get("volume24Hr") is not None
            else q.get("regularMarketVolume")
        ),
        "market_cap": number(q.get("marketCap")),
        "currency": q.get("currency", "USD" if crypto else ""),
        "exchange": q.get("fullExchangeName") or q.get("exchDisp") or q.get("exchange") or "",
        "sector": q.get("sector") or q.get("sectorDisp") or "",
        "market": q.get("market") or "",
        "quote_time": number(q.get("regularMarketTime")),
        "tags": [],
        "source": "Yahoo Finance",
    }


def _screen(
    query: dict, sort: str, *, ascending=False, crypto=False, size=250, region="US"
) -> list[dict]:
    body = {
        "offset": 0,
        "size": size,
        "sortField": sort,
        "sortType": "ASC" if ascending else "DESC",
        "quoteType": "CRYPTOCURRENCY" if crypto else "EQUITY",
        "query": query,
        "userId": "",
        "userIdType": "guid",
    }
    response = YfData().post(
        "https://query1.finance.yahoo.com/v1/finance/screener",
        params={
            "corsDomain": "finance.yahoo.com",
            "formatted": "false",
            "lang": "en-US",
            "region": region,
        },
        data=json.dumps(body),
        timeout=25,
    )
    response.raise_for_status()
    data = response.json().get("finance", {})
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return (data.get("result") or [{}])[0].get("quotes", [])


def _eq(field, value):
    return {"operator": "EQ", "operands": [field, value]}


def _and(*queries):
    return {"operator": "AND", "operands": list(queries)}


def _ranked_mix(lists: list[list[dict]], limit: int, exclude=()) -> list[dict]:
    """Equal turns through gainers/losers/activity; skip duplicate symbols."""

    def identity(item):
        return re.sub(r"[^a-z0-9]", "", item["name"].casefold())

    seen = {item["symbol"] if isinstance(item, dict) else item for item in exclude}
    names = {
        identity(item)
        for item in exclude
        if isinstance(item, dict) and item["asset_type"] == "stock"
    }
    result = []
    for rank in range(max((len(items) for items in lists), default=0)):
        for items in lists:
            if rank >= len(items):
                continue
            item = items[rank]
            if item["symbol"] in seen or (
                item["asset_type"] == "stock" and identity(item) in names
            ):
                continue
            seen.add(item["symbol"])
            if item["asset_type"] == "stock":
                names.add(identity(item))
            result.append(dict(item))
            if len(result) >= limit:
                return result
    return result


class MarketCatalog:
    TTL = 900

    def __init__(self, directory: Path):
        self.path = directory / "market_catalog.json"
        self.lock = threading.RLock()
        self.network_lock = threading.RLock()
        self.sector_locks = {}
        self.nse_lock = threading.RLock()
        self.nse_cache = None
        self.sector_cache = {}
        self.data = {
            "state": "empty",
            "updated_at": None,
            "updated_ts": 0,
            "message": "Loading Yahoo discovery…",
            "groups": {"global": [], "india": [], "crypto": []},
            "warnings": [],
            "assets": {},
            "coverage": [],
            "group_status": {},
            "attempted_ts": 0,
        }
        try:
            loaded = read_json(
                self.path,
                validate=lambda v: (
                    isinstance(v.get("groups"), dict)
                    and isinstance(v.get("assets"), dict)
                    and all(
                        isinstance(v["groups"].get(key), list)
                        and all(
                            isinstance(a, dict)
                            and isinstance(a.get("symbol"), str)
                            and isinstance(a.get("name"), str)
                            for a in v["groups"][key]
                        )
                        for key in ("global", "india", "crypto")
                    )
                ),
            )
            self.data.update(loaded)
            self.data["state"] = "ready" if any(self.data["groups"].values()) else "empty"
        except (OSError, ValueError, TypeError):
            pass
        self.data["assets"] = {
            s: a
            for s, a in self.data["assets"].items()
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a.get("symbol") == s
        }
        for field in ("updated_ts", "attempted_ts"):
            value = number(self.data.get(field)) or 0
            self.data[field] = value if 0 <= value <= time.time() + 60 else 0
        if not isinstance(self.data["group_status"], dict):
            self.data["group_status"] = {}
        for key in ("global", "india", "crypto"):
            for item in self.data["groups"][key]:
                for field in ("price", "change_percent", "volume", "market_cap", "quote_time"):
                    item[field] = number(item.get(field))
                item.setdefault("asset_type", "crypto" if key == "crypto" else "stock")
            status = self.data["group_status"].get(key, {})
            stamp = number(status.get("updated_ts")) if isinstance(status, dict) else 0
            self.data["group_status"][key] = {
                "state": status.get("state", "cached") if isinstance(status, dict) else "error",
                "updated_ts": (stamp if stamp is not None and 0 <= stamp <= time.time() + 60 else 0)
                if status
                else self.data["updated_ts"]
                if self.data["groups"][key]
                else 0,
            }

    def _save(self):
        write_json(self.path, self.data)

    def snapshot(self) -> dict:
        with self.lock:
            statuses = {
                key: {
                    **value,
                    "stale": time.time() - value["updated_ts"] > self.TTL
                    or value["state"] == "error",
                }
                for key, value in self.data["group_status"].items()
            }
            return {
                **copy.deepcopy({k: v for k, v in self.data.items() if k != "assets"}),
                "sectors": SECTORS,
                "group_status": statuses,
                "stale": any(value["stale"] for value in statuses.values()),
                "targets": {"global": 100, "india": 100, "crypto": 200},
            }

    def refresh(self, force=False):
        with self.lock:
            statuses = self.snapshot()["group_status"]
            retry = 60 if any(s["stale"] for s in statuses.values()) else self.TTL
            if self.data["state"] == "loading" or (
                not force
                and time.time() - max(self.data["attempted_ts"], self.data["updated_ts"]) < retry
            ):
                return
            self.data.update(
                state="loading",
                attempted_ts=time.time(),
                message="Fetching Yahoo gainers, losers and most active assets…",
            )
        threading.Thread(target=self._refresh, daemon=True).start()

    def _remember(self, items):
        changed = False
        for item in items:
            prior = self.data["assets"].get(item["symbol"], {})
            # A sparse search result must not erase an already resolved name/quote.
            values = {k: v for k, v in item.items() if v not in (None, "", [])}
            if (
                values.get("name") == item["symbol"]
                and prior.get("name")
                and prior["name"] != item["symbol"]
            ):
                values.pop("name", None)
            combined = {**prior, **values}
            if combined != prior:
                self.data["assets"][item["symbol"]] = combined
                changed = True
        return changed

    def _stock_lists(self, scope="global", sector=None):
        regions = list(EQUITY_SCREENER_EQ_MAP["region"])
        region_query = (
            _eq("region", "in")
            if scope == "india"
            else {"operator": "OR", "operands": [_eq("region", region) for region in regions]}
        )
        filters = [
            region_query,
            {"operator": "GT", "operands": ["dayvolume", 10000]},
            {"operator": "GT", "operands": ["intradayprice", 0]},
            {"operator": "GT", "operands": ["intradaymarketcap", 0]},
        ]
        if sector:
            filters.append(_eq("sector", sector))
        lists = []
        for tag, sort, asc in [
            ("Gainer", "percentchange", False),
            ("Loser", "percentchange", True),
            ("Active", "dayvolume", False),
        ]:
            quotes = _screen(
                _and(*filters), sort, ascending=asc, region="IN" if scope == "india" else "US"
            )
            items = [
                a
                for q in quotes
                if (a := asset_from_quote(q))
                and a["asset_type"] == "stock"
                and a["name"] != a["symbol"]
                and (a["market_cap"] or 0) > 0
                and str(q.get("quoteType", "EQUITY")).upper() == "EQUITY"
            ]
            if sector:
                for item in items:
                    item["sector"] = sector
            items.sort(
                key=lambda a: a["volume"] or 0, reverse=True
            ) if tag == "Active" else items.sort(
                key=lambda a: a["change_percent"] or 0, reverse=not asc
            )
            if tag == "Gainer":
                items = [a for a in items if (a["change_percent"] or 0) > 0]
            if tag == "Loser":
                items = [a for a in items if (a["change_percent"] or 0) < 0]
            for item in items:
                item["tags"] = [tag]
            lists.append(items)
        return lists

    def _nse_pool(self) -> list[dict]:
        with self.nse_lock:
            return self._fetch_nse_pool()

    def _fetch_nse_pool(self) -> list[dict]:
        """Fallback symbol roster only; prices and activity still come from Yahoo."""
        if self.nse_cache and time.time() - self.nse_cache[0] < self.TTL:
            return copy.deepcopy(self.nse_cache[1])
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}
        response = requests.get(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers=headers,
            timeout=25,
        )
        response.raise_for_status()
        rows = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
        roster = {}
        for row in rows:
            row = {key.strip(): str(value or "").strip() for key, value in row.items() if key}
            if row.get("SERIES", "EQ") == "EQ" and SYMBOL_RE.fullmatch(row.get("SYMBOL", "")):
                roster[row["SYMBOL"] + ".NS"] = {
                    "name": row.get("NAME OF COMPANY", row["SYMBOL"]),
                    "sector": "",
                }
        if not roster:
            raise RuntimeError("NSE symbol directory was empty")
        try:
            response = requests.get(
                "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            for row in csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))):
                symbol = str(row.get("Symbol", "")) + ".NS"
                if symbol in roster:
                    roster[symbol]["sector"] = INDUSTRY_SECTORS.get(row.get("Industry", ""), "")
        except requests.RequestException:
            pass
        items = []
        symbols = list(roster)
        for start in range(0, len(symbols), 150):
            response = YfData().get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(symbols[start : start + 150]), "formatted": "false"},
                timeout=25,
            )
            response.raise_for_status()
            for q in response.json().get("quoteResponse", {}).get("result", []):
                item = asset_from_quote(q)
                if item and item["symbol"] in roster and item["price"] is not None:
                    metadata = roster[item["symbol"]]
                    item.update(
                        name=q.get("longName") or metadata["name"],
                        sector=item["sector"] or metadata["sector"],
                    )
                    items.append(item)
        self.nse_cache = (time.time(), items)
        return copy.deepcopy(items)

    @staticmethod
    def _lists_from_pool(pool):
        gainers = sorted(
            [dict(a, tags=["Gainer"]) for a in pool if (a["change_percent"] or 0) > 0],
            key=lambda a: a["change_percent"],
            reverse=True,
        )
        losers = sorted(
            [dict(a, tags=["Loser"]) for a in pool if (a["change_percent"] or 0) < 0],
            key=lambda a: a["change_percent"],
        )
        active = sorted(
            [dict(a, tags=["Active"]) for a in pool], key=lambda a: a["volume"] or 0, reverse=True
        )
        return [gainers, losers, active]

    def _refresh(self):
        warnings, groups, all_assets = [], {}, []

        def crypto_lists():
            query = _and(_eq("exchange", "CCC"), _eq("currency", "USD"))
            lists = []
            for sort, tag in [("dayvolume", "24h activity"), ("intradaymarketcap", "Market cap")]:
                items = [
                    a
                    for q in _screen(query, sort, crypto=True)
                    if (a := asset_from_quote(q)) and a["symbol"].endswith("-USD")
                ]
                for item in items:
                    item["tags"] = [tag]
                lists.append(items)
            return lists

        # Discovery no longer owns the search/resolve lock across dozens of calls.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "global": pool.submit(self._stock_lists, "global"),
                "india": pool.submit(self._stock_lists, "india"),
                "crypto": pool.submit(crypto_lists),
            }
            for scope, target in [("global", 100), ("india", 100), ("crypto", 200)]:
                try:
                    try:
                        lists = futures[scope].result()
                    except Exception:
                        if scope != "india":
                            raise
                        lists = []
                    excluded = groups.get("global", []) if scope == "india" else []
                    items = _ranked_mix(lists, target, excluded)
                    if scope == "india" and len(items) < target:
                        try:
                            roster = self._nse_pool()
                            lists = self._lists_from_pool(roster)
                            items = _ranked_mix(lists, target, excluded)
                            warnings.append(
                                "India uses the NSE ordinary-equity roster where Yahoo's India screener was incomplete. Yahoo supplies prices and rankings; BSE-only listings are outside this fallback."
                            )
                        except Exception as exc:
                            warnings.append(f"NSE fallback unavailable ({type(exc).__name__}).")
                    if not items:
                        raise ValueError("Empty discovery result")
                    all_assets.extend(a for values in lists for a in values)
                    groups[scope] = items
                    with self.lock:
                        self.data["group_status"][scope] = {
                            "state": "ready",
                            "updated_ts": time.time(),
                        }
                except Exception as exc:
                    with self.lock:
                        groups[scope] = copy.deepcopy(self.data["groups"].get(scope, []))
                        self.data["group_status"][scope]["state"] = "error"
                    warnings.append(
                        f"{scope.title()} refresh unavailable ({type(exc).__name__}); cached results retain their original age."
                    )
        for scope, target in [("global", 100), ("india", 100), ("crypto", 200)]:
            if len(groups[scope]) < target:
                warnings.append(
                    f"{scope.title()}: Yahoo returned {len(groups[scope])}/{target} requested assets. Missing assets are not fabricated."
                )
        coverage = sorted({a["exchange"] for a in groups["global"] if a.get("exchange")})
        with self.lock:
            self._remember(all_assets)
            healthy = all(s["state"] == "ready" for s in self.data["group_status"].values())
            self.data.update(
                groups=groups,
                warnings=warnings,
                coverage=coverage,
                state="ready" if healthy else "partial",
                updated_at=now(),
                updated_ts=time.time() if healthy else self.data["updated_ts"],
                message="Yahoo discovery snapshot ready."
                if healthy
                else "Some groups could not refresh. Review coverage and cached-data ages.",
            )
            try:
                self._save()
            except OSError:
                self.data["warnings"].append(
                    "Discovery could not be saved to disk; this snapshot is available only in memory."
                )

    def search(self, text: str) -> list[dict]:
        text = text.strip()[:100]
        if not text:
            return []
        with self.lock:
            matches = [
                a
                for a in self.data["assets"].values()
                if text.casefold() in (a["symbol"] + " " + a.get("name", a["symbol"])).casefold()
            ]
        matches.sort(
            key=lambda a: (
                a["symbol"].casefold() != text.casefold(),
                not a["symbol"].casefold().startswith(text.casefold()),
                a["name"],
            )
        )
        found = {a["symbol"]: a for a in matches[:12]}
        try:
            quotes = yf.Search(text, max_results=12, news_count=0, lists_count=0, timeout=15).quotes
            for q in quotes:
                if str(q.get("quoteType", "")).upper() not in {"EQUITY", "ETF", "CRYPTOCURRENCY"}:
                    continue
                if item := asset_from_quote(q):
                    prior = found.get(item["symbol"], {})
                    values = {k: v for k, v in item.items() if v not in (None, "", [])}
                    if (
                        item["name"] == item["symbol"]
                        and prior.get("name")
                        and prior["name"] != item["symbol"]
                    ):
                        values.pop("name", None)
                    found[item["symbol"]] = {**prior, **values}
        except Exception:
            if not found:
                raise RuntimeError(
                    "Yahoo search is unavailable. Try an exact ticker or a cached asset."
                ) from None
        result = sorted(
            found.values(),
            key=lambda a: (
                a["symbol"].casefold() != text.casefold(),
                not a["symbol"].casefold().startswith(text.casefold()),
                a["name"],
            ),
        )[:15]
        with self.lock:
            if self._remember(result):
                self._save()
        return result

    def resolve(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("Invalid ticker symbol")
        with self.lock:
            item = self.data["assets"].get(symbol)
            if item and item.get("name") and item["name"] != symbol:
                return dict(item)
        try:
            matches = self.search(symbol)
        except RuntimeError:
            matches = []
        for item in matches:
            if item["symbol"] == symbol and item.get("name") != symbol:
                return item
        # Yahoo search can omit the long name for an otherwise valid symbol.
        response = YfData().get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": symbol, "formatted": "false"},
            timeout=15,
        )
        response.raise_for_status()
        for quote in response.json().get("quoteResponse", {}).get("result", []):
            item = asset_from_quote(quote)
            if item and item["symbol"] == symbol and item["name"] != symbol:
                with self.lock:
                    self._remember([item])
                    self._save()
                return item
        raise ValueError(
            f"Yahoo could not resolve {symbol}. Choose a search result with a company/coin name."
        )

    def sector(self, scope: str, key: str, limit: int) -> dict:
        if (
            scope not in {"global", "india"}
            or key not in SECTORS
            or type(limit) is not int
            or limit not in {10, 20}
        ):
            raise ValueError("Choose Global or India, a valid sector, and 10 or 20 assets")
        with self.lock:
            lock = self.sector_locks.setdefault((scope, key), threading.Lock())
        with lock:
            return self._sector(scope, key, limit)

    @staticmethod
    def _sector_view(result, limit):
        view = copy.deepcopy(result)
        if len(view["assets"]) < limit:
            view["warning"] += (
                f" Coverage supplies {len(view['assets'])}/{limit} requested equities."
            )
        view["assets"] = view["assets"][:limit]
        return view

    def _sector(self, scope, key, limit):
        cache_key = (scope, key)
        with self.lock:
            cached = self.sector_cache.get(cache_key)
            if cached and time.time() - cached[0] < (60 if cached[1].get("stale") else self.TTL):
                return self._sector_view(cached[1], limit)
        sector_name, warning = SECTORS[key], ""
        fresh = False
        # This is a targeted extra fetch, never just a filter of the first 100.
        try:
            lists = self._stock_lists(scope, sector_name)
            pool = {a["symbol"]: a for items in lists for a in items}
            fresh = bool(pool)
        except Exception:
            pool = {}
            warning = "The sector screener failed; displaying cached coverage. "
        if scope == "india" and len(_ranked_mix([list(pool.values())], 20)) < limit:
            try:
                roster = self._nse_pool()
                for item in roster:
                    if item.get("sector") == sector_name:
                        pool.setdefault(item["symbol"], item)
                        fresh = True
            except Exception:
                warning += "The NSE sector fallback was unavailable. "
        with self.lock:
            for a in self.data["assets"].values():
                if a.get("sector") == sector_name and (
                    scope != "india" or a["symbol"].endswith((".NS", ".BO"))
                ):
                    pool.setdefault(a["symbol"], a)
        items = _ranked_mix(
            [
                sorted(
                    pool.values(),
                    key=lambda a: (a["volume"] or 0, a["market_cap"] or 0),
                    reverse=True,
                )
            ],
            20,
        )
        result = {
            "scope": scope,
            "sector": sector_name,
            "assets": items,
            "warning": warning,
            "stale": not fresh,
            "updated_at": now() if fresh else None,
            "ranking": "Ranked by daily trading volume within the independently fetched sector universe.",
        }
        with self.lock:
            self._remember(items)
            self.sector_cache[cache_key] = (time.time(), result)
            self._save()
        return self._sector_view(result, limit)
