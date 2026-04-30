"""
IBPollingService — owns the IB connection and periodically fetches portfolio
snapshots in a background thread.  FastAPI (api.py) reads the cached snapshot;
all IB calls stay confined to the polling thread's event loop.
"""

import json
import logging
import random
import threading
import time
from datetime import date
from pathlib import Path
from typing import Optional

from ib_insync import IB

import portfolio as portfolio_module
from earnings import EarningsCache
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

        self.ib = IB()
        self.ib.RequestTimeout = 20

        self._lock = threading.Lock()
        self._snapshot: Optional[dict] = None
        self._debug: Optional[dict] = None

        self._stop_event = threading.Event()
        self._fetch_now = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Survive across poll cycles (mirrors session_state in the Streamlit app).
        # Delta cache is also pre-loaded from disk so last-known values are
        # available immediately — including after a restart during market close.
        self._option_delta_cache: dict = _load_delta_cache()
        self._underlying_price_cache: dict = _load_price_cache()
        self._earnings_cache = EarningsCache()

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
            return {
                "as_of": self._snapshot.get("as_of"),
                "orders": list(self._snapshot.get("orders", [])),
                "health": dict(self._snapshot.get("health", {})),
            }

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
                logger.info("Connecting to TWS (client_id=%d, attempt=%d)", client_id, attempt + 1)
                self.ib.connect("127.0.0.1", 7497, clientId=client_id, timeout=10)
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
            open_orders=open_orders,
        )

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

        # ── Time-series hook ──────────────────────────────────────────────────
        # When you're ready to persist snapshots, add a call here.
        # The snapshot dict is the natural unit of storage: one timestamped
        # record per successful fetch.  Example:
        #
        #     db.insert_snapshot(snapshot)
        #
        logger.debug("Snapshot stored (as_of=%.1f, positions=%d)", as_of, len(snapshot["positions"]))

    def _fetch_open_orders(self) -> list[dict]:
        """Fetch and normalize open IB orders without trading side effects."""
        if not self.ib.isConnected():
            return []

        trades = []
        for label, getter in (
            ("all open orders", self.ib.reqAllOpenOrders),
            ("client open orders", self.ib.reqOpenOrders),
            ("local open trades", self.ib.openTrades),
        ):
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
        return orders
