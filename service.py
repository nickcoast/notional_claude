"""
IBPollingService — owns the IB connection and periodically fetches portfolio
snapshots in a background thread.  FastAPI (api.py) reads the cached snapshot;
all IB calls stay confined to the polling thread's event loop.
"""

import json
import logging
import os
import queue
import random
import re
import threading
import time
from datetime import date, datetime
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Optional

from ib_insync import IB, Stock

import portfolio as portfolio_module
from earnings import EarningsCache
from market_data import pick_price_from_ticker
from timeseries import DEFAULT_HISTORY_DB_FILE, TimeSeriesStore
from utils import (
    configure_locale,
    get_account_value,
    is_valid_number,
    safe_float_conversion,
    setup_asyncio_event_loop,
)

logger = logging.getLogger(__name__)

# Option delta cache is saved here between service restarts so last-known
# values survive market close (IB doesn't provide model greeks after hours).
DELTA_CACHE_FILE = Path("delta_cache.json")
PRICE_CACHE_FILE = Path("price_cache.json")
CONFIG_FILE = Path("config.json")
TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSY_VALUES = {"0", "false", "no", "off"}
OPEN_ORDER_SCOPES = {"local", "client", "all"}
DEFAULT_NEWS_CACHE_TTL = 300
DEFAULT_NEWS_LIMIT = 20
HEADLINE_TAG_RE = re.compile(r"^\{(?P<tag>[^}]*)\}\s*(?P<headline>.*)$")
HEADLINE_SOURCE_RE = re.compile(r"\s+--\s+[^-]+$")
HEADLINE_WORD_RE = re.compile(r"[a-z0-9]+")


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUTHY_VALUES:
        return True
    if normalized in FALSY_VALUES:
        return False
    logger.warning("Invalid boolean value for %s=%r; using %s", name, value, default)
    return default


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in choices:
        return normalized
    logger.warning("Invalid value for %s=%r; using %s", name, value, default)
    return default


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(min(int(value), max_value), min_value)
    except ValueError:
        logger.warning("Invalid integer value for %s=%r; using %s", name, value, default)
        return default


def _load_config() -> dict:
    """Load config.json; creates it with defaults if missing or unreadable."""
    defaults = {"account_names": {}, "selected_account": "ALL"}
    if not CONFIG_FILE.exists():
        _save_config(defaults)
        return defaults
    try:
        with CONFIG_FILE.open() as f:
            data = json.load(f)
        defaults.update(data)
        return defaults
    except Exception as exc:
        logger.warning("Failed to load config: %s", exc)
        return defaults


def _save_config(config: dict) -> None:
    """Persist config.json. Silently swallows write errors."""
    try:
        with CONFIG_FILE.open("w") as f:
            json.dump(config, f, indent=2)
    except Exception as exc:
        logger.warning("Failed to save config: %s", exc)


def _parse_headline(headline: str) -> dict:
    """Split IBKR's optional headline metadata tag from display text."""
    raw = headline or ""
    match = HEADLINE_TAG_RE.match(raw)
    tag = match.group("tag") if match else ""
    display = unescape((match.group("headline") if match else raw).strip())
    metadata = {}
    for part in tag.split(":"):
        if not part:
            continue
        if part in {"A", "L"}:
            metadata[part] = ""
        elif metadata:
            last_key = next(reversed(metadata))
            metadata[last_key] = part

    return {
        "raw": raw,
        "display": display or raw,
        "tag": tag,
        "language": metadata.get("L", ""),
        "attributes": metadata.get("A", ""),
    }


def _headline_similarity_key(headline: str) -> str:
    """
    Best-effort key for spotting near-duplicate provider updates.

    IBKR exposes articleId for a specific article, but not a canonical "story"
    id.  This key intentionally stays heuristic and display-only.
    """
    without_source = HEADLINE_SOURCE_RE.sub("", headline or "").lower()
    words = HEADLINE_WORD_RE.findall(without_source)
    stop_words = {
        "a", "about", "after", "and", "as", "at", "by", "for", "from",
        "in", "into", "of", "on", "the", "to", "with",
    }
    meaningful = [word for word in words if word not in stop_words]
    return " ".join(meaningful)


def _load_delta_cache() -> dict:
    """
    Load persisted option delta cache from disk.

    Returns {} if the file doesn't exist or can't be parsed.  The on-disk
    format is a JSON array of [[symbol, expiry, strike, right, mult, cls], delta]
    pairs — matching the tuple shape produced by option_contract_key().
    """
    if not DELTA_CACHE_FILE.exists():
        return {}
    try:
        with DELTA_CACHE_FILE.open() as f:
            entries = json.load(f)
        result = {}
        for key_list, value in entries:
            key = (
                key_list[0],          # symbol
                key_list[1],          # expiry
                float(key_list[2]),   # strike
                key_list[3],          # right
                key_list[4],          # multiplier string
                key_list[5],          # tradingClass
            )
            result[key] = float(value)
        logger.info("Loaded delta cache from disk (%d entries)", len(result))
        return result
    except Exception as exc:
        logger.warning("Failed to load delta cache: %s", exc)
        return {}


def _save_delta_cache(cache: dict) -> None:
    """Persist the option delta cache to disk.  Silently swallows write errors."""
    try:
        entries = [[list(key), value] for key, value in cache.items()]
        with DELTA_CACHE_FILE.open("w") as f:
            json.dump(entries, f)
        logger.debug("Delta cache saved (%d entries)", len(entries))
    except Exception as exc:
        logger.warning("Failed to save delta cache: %s", exc)


def _load_price_cache() -> dict:
    """Load persisted underlying price cache ({symbol: price}) from disk."""
    if not PRICE_CACHE_FILE.exists():
        return {}
    try:
        with PRICE_CACHE_FILE.open() as f:
            data = json.load(f)
        result = {sym: float(price) for sym, price in data.items() if isinstance(sym, str)}
        logger.info("Loaded price cache from disk (%d entries)", len(result))
        return result
    except Exception as exc:
        logger.warning("Failed to load price cache: %s", exc)
        return {}


def _save_price_cache(cache: dict) -> None:
    """Persist the underlying price cache to disk.  Silently swallows write errors."""
    try:
        with PRICE_CACHE_FILE.open("w") as f:
            json.dump({sym: price for sym, price in cache.items() if isinstance(sym, str)}, f)
        logger.debug("Price cache saved (%d entries)", len(cache))
    except Exception as exc:
        logger.warning("Failed to save price cache: %s", exc)


# IB doesn't publish a hard minimum for portfolio snapshot polling.  Pacing
# rules target individual request rates (50 msg/s) and historical data — not
# periodic full-portfolio refreshes.  10 s is a conservative floor that leaves
# plenty of headroom even for larger portfolios.
MIN_POLL_INTERVAL = 10
DEFAULT_POLL_INTERVAL = 15


# ── Snapshot serialization ────────────────────────────────────────────────────

def _serialize_snapshot(
    account_df,
    underlying_df,
    health: dict,
    as_of: float,
    earnings: Optional[dict] = None,
    news_counts: Optional[dict] = None,
    open_orders: Optional[list[dict]] = None,
) -> dict:
    """
    Convert portfolio DataFrames to a JSON-serializable snapshot dict.

    This is the canonical shape for both the REST snapshot endpoint and the
    SSE stream.  It is also the natural unit for future time-series storage —
    each call produces one timestamped record.
    """
    metrics = {}
    if account_df is not None:
        metrics = {
            "net_liquidation":        get_account_value(account_df, "NetLiquidation",                       numeric=True, default=0.0),
            "gross_position_value":   get_account_value(account_df, "GrossPositionValue",                   numeric=True, default=0.0),
            "sma":                    get_account_value(account_df, "SMA",                                  numeric=True, default=0.0),
            "portfolio_theta":        safe_float_conversion(get_account_value(account_df, "Portfolio Theta")),
            "ngav":                          get_account_value(account_df, "NGAV (Notional Gross Asset Value)", numeric=True, default=0.0),
            "ngav_gross":                    get_account_value(account_df, "NGAV Gross",                        numeric=True, default=0.0),
            "notional_leverage_ratio":       safe_float_conversion(get_account_value(account_df, "NLR (Notional Leverage Ratio)")),
            "gross_notional_leverage_ratio": safe_float_conversion(get_account_value(account_df, "Gross Notional Leverage Ratio")),
            "standard_leverage_ratio": safe_float_conversion(get_account_value(account_df, "Standard Leverage Ratio")),
            "buying_power":           get_account_value(account_df, "BuyingPower",                          numeric=True, default=0.0),
        }

    earnings = earnings or {}
    news_counts = news_counts or {}
    today = date.today()

    positions = []
    if underlying_df is not None and not underlying_df.empty:
        col_map = {
            "Symbol":                       "symbol",
            "Stock Count":                  "stock_count",
            "Stock Value":                  "stock_value",
            "Option Notional (Shares)":     "option_notional_shares",
            "Option Notional Value":        "option_notional_value",
            "Option Actual Value":          "option_actual_value",
            "Underlying Market Price":      "underlying_market_price",
            "Underlying Cost Basis":        "underlying_cost_basis",
            "Underlying Price Source":      "underlying_price_source",
            "Notional Position Value (NPV)": "npv",
        }
        str_cols = {"symbol", "underlying_price_source"}
        for _, row in underlying_df.iterrows():
            entry = {}
            for src_col, dst_key in col_map.items():
                val = row.get(src_col)
                if dst_key in str_cols:
                    entry[dst_key] = val
                else:
                    entry[dst_key] = float(val) if is_valid_number(val) else None

            # Earnings annotation
            sym = entry.get("symbol", "")
            edate_str = earnings.get(sym)
            if edate_str:
                try:
                    edate = date.fromisoformat(edate_str)
                    edays = (edate - today).days
                    entry["earnings_date"] = edate_str
                    entry["earnings_days"] = edays if edays >= 0 else None
                except ValueError:
                    entry["earnings_date"] = None
                    entry["earnings_days"] = None
            else:
                entry["earnings_date"] = None
                entry["earnings_days"] = None

            entry["news_count"] = news_counts.get(sym)
            positions.append(entry)

    orders = []
    for order in open_orders or []:
        entry = dict(order)
        sym = entry.get("symbol", "")
        edate_str = earnings.get(sym)
        if edate_str:
            try:
                edate = date.fromisoformat(edate_str)
                edays = (edate - today).days
                entry["earnings_date"] = edate_str
                entry["earnings_days"] = edays if edays >= 0 else None
            except ValueError:
                entry["earnings_date"] = None
                entry["earnings_days"] = None
        else:
            entry["earnings_date"] = None
            entry["earnings_days"] = None
        entry["news_count"] = news_counts.get(sym)
        orders.append(entry)

    return {
        "as_of":     as_of,
        "metrics":   metrics,
        "positions": positions,
        "orders":    orders,
        "health":    health or {},
    }


# ── Service ───────────────────────────────────────────────────────────────────

class IBPollingService:
    """
    Runs a background thread that connects to TWS and polls for portfolio data.

    Lifecycle:
        service = IBPollingService(poll_interval=15)
        service.start()          # call once at application startup
        ...
        service.stop()           # call once at application shutdown

    Thread safety:
        get_snapshot() / get_health() are safe to call from any thread.
        All IB operations are confined to the internal polling thread.
    """

    def __init__(self, poll_interval: int = DEFAULT_POLL_INTERVAL):
        self.poll_interval = max(int(poll_interval), MIN_POLL_INTERVAL)
        history_db_path = os.getenv("IB_HISTORY_DB", str(DEFAULT_HISTORY_DB_FILE))
        self._history_store = TimeSeriesStore(history_db_path)
        self._ib_readonly = _env_flag("IB_READONLY", True)
        self._open_order_scope = _env_choice("IB_OPEN_ORDER_SCOPE", "all", OPEN_ORDER_SCOPES)
        self._news_provider_override = os.getenv("IB_NEWS_PROVIDERS", "").strip()
        self._news_cache_ttl = _env_int("IB_NEWS_CACHE_TTL", DEFAULT_NEWS_CACHE_TTL, 30, 86400)
        self._news_keywords = [
            keyword.strip().lower()
            for keyword in os.getenv("IB_NEWS_KEYWORDS", "").split(",")
            if keyword.strip()
        ]

        self.ib = IB()
        self.ib.RequestTimeout = 20

        self._lock = threading.Lock()
        self._snapshot: Optional[dict] = None
        self._debug: Optional[dict] = None

        self._stop_event = threading.Event()
        self._fetch_now = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._ib_task_queue: queue.Queue = queue.Queue()

        # Survive across poll cycles (mirrors session_state in the Streamlit app).
        # Delta cache is also pre-loaded from disk so last-known values are
        # available immediately — including after a restart during market close.
        self._option_delta_cache: dict = _load_delta_cache()
        self._underlying_price_cache: dict = _load_price_cache()
        self._earnings_cache = EarningsCache()
        self._news_lock = threading.Lock()
        self._news_cache: dict[str, dict] = {}
        self._news_article_cache: dict[tuple[str, str], dict] = {}
        self._news_providers: list[dict] = []
        self._news_provider_codes: Optional[str] = None
        self._news_contract_cache: dict[str, int] = {}

        config = _load_config()
        self._account_names: dict = config.get("account_names", {})
        self._selected_account: str = config.get("selected_account", "ALL")
        self._managed_accounts: list = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        configure_locale()
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="ib-poll",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("IBPollingService started (interval=%ds)", self.poll_interval)

    def stop(self):
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=30)
        try:
            self.ib.disconnect()
        except Exception:
            pass
        logger.info("IBPollingService stopped")

    # ── Public reads (any thread) ─────────────────────────────────────────────

    def get_snapshot(self) -> Optional[dict]:
        with self._lock:
            return self._snapshot

    def get_debug(self) -> Optional[dict]:
        with self._lock:
            return self._debug

    def get_health(self) -> Optional[dict]:
        with self._lock:
            return self._snapshot.get("health") if self._snapshot else None

    def get_orders(self) -> Optional[dict]:
        with self._lock:
            if self._snapshot is None:
                return None
            orders = []
            for order in self._snapshot.get("orders", []):
                entry = dict(order)
                account = entry.get("account", "")
                entry["account_display"] = self._account_names.get(account, account)
                orders.append(entry)
            return {
                "as_of": self._snapshot.get("as_of"),
                "orders": orders,
                "health": dict(self._snapshot.get("health", {})),
            }

    def get_symbol_news(self, symbol: str, limit: int = DEFAULT_NEWS_LIMIT) -> dict:
        """Return cached or freshly fetched IBKR news headlines for a symbol."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        limit = max(1, min(int(limit), 100))

        cached = self._cached_symbol_news(symbol, limit)
        if cached is not None:
            return cached

        return self._call_on_ib_thread(lambda: self._fetch_symbol_news_ib(symbol, limit), timeout=30.0)

    def get_news_article(self, provider_code: str, article_id: str) -> dict:
        """Return cached or freshly fetched IBKR news article body."""
        provider_code = (provider_code or "").strip()
        article_id = (article_id or "").strip()
        if not provider_code or not article_id:
            raise ValueError("provider_code and article_id are required")

        key = (provider_code, article_id)
        with self._news_lock:
            cached = self._news_article_cache.get(key)
            if cached and time.time() - cached["fetched_at"] <= self._news_cache_ttl:
                data = dict(cached["data"])
                data["cached"] = True
                return data

        return self._call_on_ib_thread(
            lambda: self._fetch_news_article_ib(provider_code, article_id),
            timeout=30.0,
        )

    def get_history(
        self,
        account_filter: Optional[str] = None,
        limit: int = 1000,
        start_as_of: Optional[float] = None,
        end_as_of: Optional[float] = None,
    ) -> dict:
        account_filter = account_filter or self._selected_account
        return {
            "account": account_filter,
            "points": self._history_store.get_net_liquidation_history(
                account_filter=account_filter,
                limit=limit,
                start_as_of=start_as_of,
                end_as_of=end_as_of,
            ),
            "daily_extremes": self._history_store.get_daily_extremes(account_filter),
            "poll_interval": self.poll_interval,
        }

    def compare_history_positions(
        self,
        start_id: int,
        end_id: int,
        account_filter: Optional[str] = None,
        basis: str = "market_value",
        level: str = "symbol",
        limit: int = 100,
    ) -> Optional[dict]:
        return self._history_store.compare_positions(
            account_filter=account_filter or self._selected_account,
            start_id=start_id,
            end_id=end_id,
            basis=basis,
            level=level,
            limit=limit,
        )

    def _cached_symbol_news(self, symbol: str, limit: int) -> Optional[dict]:
        with self._news_lock:
            cached = self._news_cache.get(symbol)
            if not cached:
                return None
            if time.time() - cached["fetched_at"] > self._news_cache_ttl:
                return None
            data = dict(cached["data"])
            headlines = list(data.get("headlines", []))
            data["headlines"] = headlines[:limit]
            data["count"] = len(headlines)
            data["cached"] = True
            return data

    def _news_counts_snapshot(self) -> dict:
        cutoff = time.time() - self._news_cache_ttl
        with self._news_lock:
            return {
                symbol: len(item["data"].get("headlines", []))
                for symbol, item in self._news_cache.items()
                if item.get("fetched_at", 0) >= cutoff
            }

    def _call_on_ib_thread(self, func, timeout: float):
        if threading.current_thread() is self._poll_thread:
            return func()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            raise RuntimeError("IB polling service is not running")

        done = threading.Event()
        task = {"func": func, "done": done, "result": None, "error": None}
        self._ib_task_queue.put(task)
        self._fetch_now.set()
        if not done.wait(timeout=timeout):
            raise TimeoutError("Timed out waiting for IB response")
        if task["error"] is not None:
            raise task["error"]
        return task["result"]

    def _drain_ib_tasks(self) -> None:
        while True:
            try:
                task = self._ib_task_queue.get_nowait()
            except queue.Empty:
                return
            try:
                task["result"] = task["func"]()
            except Exception as exc:
                task["error"] = exc
            finally:
                task["done"].set()

    def _news_provider_code_string(self) -> str:
        if self._news_provider_override:
            return self._news_provider_override
        if self._news_provider_codes is not None:
            return self._news_provider_codes

        providers = self.ib.reqNewsProviders() or []
        provider_rows = [
            {
                "code": getattr(provider, "code", "") or getattr(provider, "providerCode", ""),
                "name": getattr(provider, "name", "") or getattr(provider, "providerName", ""),
            }
            for provider in providers
        ]
        provider_rows = [row for row in provider_rows if row["code"]]
        self._news_providers = provider_rows
        self._news_provider_codes = "+".join(row["code"] for row in provider_rows)
        return self._news_provider_codes

    def _news_contract_id(self, symbol: str) -> int:
        con_id = self._news_contract_cache.get(symbol)
        if con_id:
            return con_id

        contracts = self.ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not contracts:
            raise ValueError(f"IB could not qualify stock contract for {symbol}")
        con_id = int(getattr(contracts[0], "conId", 0) or 0)
        if not con_id:
            raise ValueError(f"IB did not return a contract id for {symbol}")
        self._news_contract_cache[symbol] = con_id
        return con_id

    def _fetch_symbol_news_ib(self, symbol: str, limit: int) -> dict:
        if not self.ib.isConnected():
            raise RuntimeError("Not connected to TWS")

        provider_codes = self._news_provider_code_string()
        if not provider_codes:
            data = {
                "symbol": symbol,
                "provider_codes": "",
                "providers": list(self._news_providers),
                "headlines": [],
                "count": 0,
                "cached": False,
                "note": "No API news providers are available for this TWS user.",
            }
            self._store_symbol_news(symbol, data)
            return data

        con_id = self._news_contract_id(symbol)
        articles = self.ib.reqHistoricalNews(con_id, provider_codes, "", "", limit, []) or []
        headlines = []
        for article in articles:
            headline = getattr(article, "headline", "") or ""
            parsed_headline = _parse_headline(headline)
            published = getattr(article, "time", None)
            if isinstance(published, datetime):
                published_ts = published.timestamp()
                published_text = published.isoformat(sep=" ", timespec="seconds")
            else:
                published_ts = None
                published_text = str(published) if published else ""
            headlines.append({
                "time": published_text,
                "timestamp": published_ts,
                "provider_code": getattr(article, "providerCode", "") or "",
                "article_id": getattr(article, "articleId", "") or "",
                "headline": parsed_headline["display"],
                "raw_headline": parsed_headline["raw"],
                "headline_metadata": parsed_headline["tag"],
                "headline_language": parsed_headline["language"],
                "headline_attributes": parsed_headline["attributes"],
                "headline_similarity_key": _headline_similarity_key(parsed_headline["display"]),
                "keyword_matches": self._keyword_matches(parsed_headline["display"]),
            })
        self._annotate_similar_headlines(headlines)

        data = {
            "symbol": symbol,
            "provider_codes": provider_codes,
            "providers": list(self._news_providers),
            "headlines": headlines,
            "count": len(headlines),
            "cached": False,
            "note": "",
        }
        self._store_symbol_news(symbol, data)
        return data

    def _annotate_similar_headlines(self, headlines: list[dict]) -> None:
        groups: list[dict] = []
        for headline in headlines:
            key = headline.get("headline_similarity_key", "")
            group = None
            for candidate in groups:
                if (
                    key
                    and candidate["key"]
                    and SequenceMatcher(None, key, candidate["key"]).ratio() >= 0.82
                ):
                    group = candidate
                    break
            if group is None:
                group = {"key": key, "items": []}
                groups.append(group)
            group["items"].append(headline)

        for index, group in enumerate(groups, start=1):
            count = len(group["items"])
            for item_index, headline in enumerate(group["items"], start=1):
                headline["similar_group"] = index
                headline["similar_count"] = count
                headline["similar_index"] = item_index

    def _store_symbol_news(self, symbol: str, data: dict) -> None:
        with self._news_lock:
            self._news_cache[symbol] = {
                "fetched_at": time.time(),
                "data": data,
            }

    def _fetch_news_article_ib(self, provider_code: str, article_id: str) -> dict:
        if not self.ib.isConnected():
            raise RuntimeError("Not connected to TWS")

        article = self.ib.reqNewsArticle(provider_code, article_id, [])
        article_type = int(getattr(article, "articleType", 0) or 0)
        article_text = getattr(article, "articleText", "") or ""
        data = {
            "provider_code": provider_code,
            "article_id": article_id,
            "article_type": article_type,
            "article_text": article_text,
            "keyword_matches": self._keyword_matches(article_text),
            "cached": False,
        }
        with self._news_lock:
            self._news_article_cache[(provider_code, article_id)] = {
                "fetched_at": time.time(),
                "data": data,
            }
        return data

    def _keyword_matches(self, text: str) -> list[str]:
        normalized = (text or "").lower()
        return [keyword for keyword in self._news_keywords if keyword in normalized]

    def get_accounts(self) -> dict:
        """Return account list and current selection (safe to call from any thread)."""
        return {
            "managed": list(self._managed_accounts),
            "names": dict(self._account_names),
            "selected": self._selected_account,
        }

    def set_account(self, account: str) -> None:
        """Persist and apply a new account selection (safe to call from any thread)."""
        self._selected_account = account
        config = _load_config()
        config["selected_account"] = account
        _save_config(config)
        self._fetch_now.set()
        logger.info("Account selection changed to: %s", account)

    def set_account_name(self, account: str, name: str) -> None:
        """Set or clear a display name for an account. Persisted to config.json."""
        config = _load_config()
        if name:
            config["account_names"][account] = name
        else:
            config["account_names"].pop(account, None)
        _save_config(config)
        self._account_names = config.get("account_names", {})
        logger.info("Account name updated: %s -> %r", account, name)

    # ── Background thread ──────────────────────────────────────────────────────

    def _connect(self):
        """Connect to TWS, blocking until successful or stop is requested."""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                if self.ib.isConnected():
                    return
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
                client_id = random.randint(1000, 9999)
                logger.info(
                    "Connecting to TWS (client_id=%d, attempt=%d, readonly=%s)",
                    client_id,
                    attempt + 1,
                    self._ib_readonly,
                )
                self.ib.connect(
                    "127.0.0.1",
                    7497,
                    clientId=client_id,
                    timeout=10,
                    readonly=self._ib_readonly,
                )
                self.ib.reqMarketDataType(portfolio_module.PREFERRED_MARKET_DATA_TYPE)
                portfolio_module.register_ib_error_handler(self.ib)
                self._managed_accounts = list(self.ib.managedAccounts())
                logger.info("Connected to TWS (accounts: %s)", self._managed_accounts)
                return
            except Exception as e:
                attempt += 1
                wait = min(5 * attempt, 60)
                logger.warning("TWS connection failed: %s — retrying in %ds", e, wait)
                self._stop_event.wait(timeout=wait)

    def _poll_loop(self):
        setup_asyncio_event_loop()
        self._connect()

        while not self._stop_event.is_set():
            try:
                if not self.ib.isConnected():
                    self._connect()
                    if self._stop_event.is_set():
                        break

                self._drain_ib_tasks()
                self._fetch_and_store()
                self._fetch_now.wait(timeout=self.poll_interval)
                self._fetch_now.clear()

            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)
                # Retry quickly so a transient failure resolves well within 30 s.
                self._stop_event.wait(timeout=5)

    def _fetch_and_store(self):
        account_df, underlying_df, positions_by_underlying, health = portfolio_module.get_portfolio_data_sync(
            self.ib,
            option_delta_cache=self._option_delta_cache,
            underlying_price_cache=self._underlying_price_cache,
            account_filter=self._selected_account,
        )
        open_orders = self._fetch_open_orders()
        contract_positions = []
        if health:
            contract_positions = health.pop("contract_positions", []) or []
        as_of = time.time()

        # Persist caches so last-known values survive a restart.
        if self._option_delta_cache:
            _save_delta_cache(self._option_delta_cache)
        if self._underlying_price_cache:
            _save_price_cache(self._underlying_price_cache)

        # Trigger a background earnings refresh whenever symbols are known.
        # The cache self-throttles to once per 24 h; this call is cheap.
        if underlying_df is not None and not underlying_df.empty:
            symbols = underlying_df["Symbol"].dropna().unique().tolist()
        else:
            symbols = []
        order_symbols = [order.get("symbol") for order in open_orders if order.get("symbol")]
        self._earnings_cache.refresh_if_stale(sorted(set(symbols + order_symbols)))

        snapshot = _serialize_snapshot(
            account_df, underlying_df, health, as_of,
            earnings=self._earnings_cache.snapshot(),
            news_counts=self._news_counts_snapshot(),
            open_orders=open_orders,
        )
        snapshot["account"] = self._selected_account

        debug = None
        if positions_by_underlying:
            debug = {
                "as_of": as_of,
                "account": self._selected_account,
                "symbols": {
                    sym: {
                        "underlying_price": data.get("underlying_market_price"),
                        "price_source": data.get("price_source"),
                        "stock_qty": data.get("stock_count", 0.0),
                        "options": sorted(
                            data.get("option_contracts", []),
                            key=lambda c: (c["expiry"], c["strike"], c["right"]),
                        ),
                        "total_delta_shares": data.get("option_notional", 0.0),
                        "total_notional_value": (
                            data.get("option_notional", 0.0) * data["underlying_market_price"]
                            if data.get("underlying_market_price") else None
                        ),
                    }
                    for sym, data in sorted(positions_by_underlying.items())
                },
            }

        with self._lock:
            self._snapshot = snapshot
            self._debug = debug

        try:
            self._history_store.insert_snapshot(
                snapshot,
                account_filter=self._selected_account,
                contract_positions=contract_positions,
            )
        except Exception as exc:
            logger.warning("Failed to persist time-series snapshot: %s", exc, exc_info=True)

        logger.debug("Snapshot stored (as_of=%.1f, positions=%d)", as_of, len(snapshot["positions"]))

    def _fetch_open_orders(self) -> list[dict]:
        """Fetch and normalize open IB orders without trading side effects."""
        if not self.ib.isConnected():
            return []

        getters = [("local open trades", self.ib.openTrades)]
        if self._open_order_scope == "all":
            getters.append(("all open orders", self.ib.reqAllOpenOrders))
        elif self._open_order_scope == "client":
            getters.append(("client open orders", self.ib.reqOpenOrders))

        trades = []
        for label, getter in getters:
            try:
                trades.extend(getter() or [])
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", label, exc)

        unique = {}
        for item in trades:
            order = getattr(item, "order", item)
            contract = getattr(item, "contract", None)
            status = getattr(item, "orderStatus", None)
            order_id = getattr(order, "orderId", None)
            perm_id = getattr(order, "permId", None)
            con_id = getattr(contract, "conId", None) if contract else None
            unique[(perm_id, order_id, con_id)] = (contract, order, status)

        orders = []
        for contract, order, status in unique.values():
            orders.append({
                "symbol": getattr(contract, "symbol", "") if contract else "",
                "local_symbol": getattr(contract, "localSymbol", "") if contract else "",
                "security_type": getattr(contract, "secType", "") if contract else "",
                "action": getattr(order, "action", ""),
                "order_type": getattr(order, "orderType", ""),
                "total_quantity": safe_float_conversion(getattr(order, "totalQuantity", 0)),
                "limit_price": safe_float_conversion(getattr(order, "lmtPrice", 0)),
                "aux_price": safe_float_conversion(getattr(order, "auxPrice", 0)),
                "time_in_force": getattr(order, "tif", ""),
                "status": getattr(status, "status", ""),
                "filled": safe_float_conversion(getattr(status, "filled", 0)),
                "remaining": safe_float_conversion(getattr(status, "remaining", 0)),
                "account": getattr(order, "account", ""),
                "exchange": getattr(contract, "exchange", "") if contract else "",
                "currency": getattr(contract, "currency", "") if contract else "",
                "order_id": int(safe_float_conversion(getattr(order, "orderId", 0))),
                "perm_id": int(safe_float_conversion(getattr(order, "permId", 0))),
                "parent_id": int(safe_float_conversion(getattr(order, "parentId", 0))),
            })

        orders.sort(key=lambda item: (item.get("symbol") or "", item.get("order_id") or 0))
        self._annotate_order_price_distance(orders, unique.values())
        return orders

    def _annotate_order_price_distance(self, orders: list[dict], order_items) -> None:
        """Add current/reference prices and absolute percent distance to orders."""
        contract_by_key = {}
        for contract, order, _status in order_items:
            if contract is None:
                continue
            key = (
                int(safe_float_conversion(getattr(order, "permId", 0))),
                int(safe_float_conversion(getattr(order, "orderId", 0))),
            )
            contract_by_key[key] = contract

        active = []
        for order in orders:
            order_price = self._order_comparison_price(order)
            order["order_price"] = order_price
            order["current_price"] = None
            order["price_distance_pct"] = None
            contract = contract_by_key.get((order.get("perm_id"), order.get("order_id")))
            if contract is None or not (is_valid_number(order_price) and float(order_price) > 0):
                continue
            try:
                ticker = self.ib.reqMktData(contract)
                active.append((order, contract, ticker))
            except Exception as exc:
                logger.debug("Failed to request order market data for %s: %s", order.get("symbol"), exc)

        if active:
            try:
                self.ib.sleep(0.8)
            except Exception as exc:
                logger.debug("Order market data wait failed: %s", exc)

        for order, contract, ticker in active:
            current_price = self._order_current_price(contract, ticker)
            if is_valid_number(current_price) and float(current_price) > 0:
                current_price = float(current_price)
                order_price = float(order["order_price"])
                order["current_price"] = current_price
                order["price_distance_pct"] = abs(order_price - current_price) / current_price * 100.0
            try:
                self.ib.cancelMktData(contract)
            except Exception:
                pass

    @staticmethod
    def _order_comparison_price(order: dict) -> Optional[float]:
        order_type = (order.get("order_type") or "").upper()
        limit_price = safe_float_conversion(order.get("limit_price"))
        aux_price = safe_float_conversion(order.get("aux_price"))

        # Stop-style orders become actionable at the stop/trigger price.
        if ("STP" in order_type or "STOP" in order_type) and aux_price > 0:
            return aux_price
        if limit_price > 0:
            return limit_price
        if aux_price > 0:
            return aux_price
        return None

    @staticmethod
    def _order_current_price(contract, ticker) -> Optional[float]:
        sec_type = getattr(contract, "secType", "")
        if sec_type == "OPT":
            if is_valid_number(getattr(ticker, "bid", None)) and is_valid_number(getattr(ticker, "ask", None)):
                bid = float(ticker.bid)
                ask = float(ticker.ask)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
        elif sec_type == "STK":
            last = getattr(ticker, "last", None)
            if is_valid_number(last) and float(last) > 0:
                return float(last)

        return pick_price_from_ticker(ticker)
