"""
Earnings calendar via yfinance.

Fetches the next earnings date for each portfolio symbol.  Results are cached
with a 24-hour TTL and refreshed in a daemon background thread so the IB
polling cycle is never blocked by Yahoo Finance network calls.

On startup the cache is empty; the dashboard shows no earnings data until the
first background refresh completes (typically a few seconds to a minute
depending on portfolio size).
"""

import logging
import threading
import time
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 24 * 3600  # refresh once per day

try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
except ImportError:
    _YFINANCE_AVAILABLE = False
    logger.warning("yfinance not installed; earnings dates will be unavailable")


def _fetch_one(symbol: str) -> Optional[date]:
    """Return the next upcoming earnings date for *symbol*, or None."""
    if not _YFINANCE_AVAILABLE:
        return None
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar

        if cal is None:
            return None

        # yfinance has returned both dicts and DataFrames across versions.
        if hasattr(cal, "to_dict"):
            cal = cal.to_dict()

        if not isinstance(cal, dict):
            return None

        dates = cal.get("Earnings Date") or []
        if not dates:
            return None

        today = date.today()
        for dt in dates:
            try:
                d = dt.date() if hasattr(dt, "date") else dt
                if isinstance(d, date) and d >= today:
                    return d
            except Exception:
                continue

        return None

    except Exception as exc:
        logger.debug("yfinance fetch failed for %s: %s", symbol, exc)
        return None


class EarningsCache:
    """
    Thread-safe, TTL-backed cache of next earnings dates.

    refresh_if_stale(symbols) is safe to call from any thread; it spawns a
    daemon thread to do the actual network work and returns immediately.
    snapshot() always returns whatever is currently cached (possibly empty
    on first call before the initial refresh completes).
    """

    def __init__(self, ttl_seconds: int = _CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._dates: dict[str, Optional[date]] = {}
        self._fetched_at: float = 0.0
        self._refreshing = False

    def refresh_if_stale(self, symbols: list[str]) -> None:
        """Trigger a background refresh if the cache is empty or older than TTL."""
        with self._lock:
            age = time.time() - self._fetched_at
            if (age < self._ttl and self._dates) or self._refreshing:
                return
            self._refreshing = True

        thread = threading.Thread(
            target=self._do_refresh,
            args=(list(symbols),),
            name="earnings-refresh",
            daemon=True,
        )
        thread.start()

    def _do_refresh(self, symbols: list[str]) -> None:
        logger.info("Refreshing earnings calendar for %d symbols", len(symbols))
        new_dates: dict[str, Optional[date]] = {}
        for sym in symbols:
            new_dates[sym] = _fetch_one(sym)
        with self._lock:
            self._dates = new_dates
            self._fetched_at = time.time()
            self._refreshing = False
        found = sum(1 for d in new_dates.values() if d is not None)
        logger.info("Earnings calendar refreshed (%d/%d symbols have dates)", found, len(symbols))

    def snapshot(self) -> dict[str, Optional[str]]:
        """Return {symbol: 'YYYY-MM-DD' or None} — JSON-serializable."""
        with self._lock:
            return {
                sym: dt.isoformat() if dt else None
                for sym, dt in self._dates.items()
            }
