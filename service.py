"""
IBPollingService — owns the IB connection and periodically fetches portfolio
snapshots in a background thread.  FastAPI (api.py) reads the cached snapshot;
all IB calls stay confined to the polling thread's event loop.
"""

import logging
import random
import threading
import time
from datetime import date
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
            "ngav":                   get_account_value(account_df, "NGAV (Notional Gross Asset Value)",     numeric=True, default=0.0),
            "notional_leverage_ratio": safe_float_conversion(get_account_value(account_df, "NLR (Notional Leverage Ratio)")),
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

    return {
        "as_of":     as_of,
        "metrics":   metrics,
        "positions": positions,
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

        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Survive across poll cycles (mirrors session_state in the Streamlit app).
        self._option_delta_cache: dict = {}
        self._underlying_price_cache: dict = {}
        self._earnings_cache = EarningsCache()

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

    def get_health(self) -> Optional[dict]:
        with self._lock:
            return self._snapshot.get("health") if self._snapshot else None

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
                logger.info("Connected to TWS")
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
                self._stop_event.wait(timeout=self.poll_interval)

            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)
                # Retry quickly so a transient failure resolves well within 30 s.
                self._stop_event.wait(timeout=5)

    def _fetch_and_store(self):
        account_df, underlying_df, _, health = portfolio_module.get_portfolio_data_sync(
            self.ib,
            option_delta_cache=self._option_delta_cache,
            underlying_price_cache=self._underlying_price_cache,
        )
        as_of = time.time()

        # Trigger a background earnings refresh whenever symbols are known.
        # The cache self-throttles to once per 24 h; this call is cheap.
        if underlying_df is not None and not underlying_df.empty:
            symbols = underlying_df["Symbol"].dropna().unique().tolist()
            self._earnings_cache.refresh_if_stale(symbols)

        snapshot = _serialize_snapshot(
            account_df, underlying_df, health, as_of,
            earnings=self._earnings_cache.snapshot(),
        )

        with self._lock:
            self._snapshot = snapshot

        # ── Time-series hook ──────────────────────────────────────────────────
        # When you're ready to persist snapshots, add a call here.
        # The snapshot dict is the natural unit of storage: one timestamped
        # record per successful fetch.  Example:
        #
        #     db.insert_snapshot(snapshot)
        #
        logger.debug("Snapshot stored (as_of=%.1f, positions=%d)", as_of, len(snapshot["positions"]))
