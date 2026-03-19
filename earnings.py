"""
Earnings calendar via yfinance.

Fetches the next earnings date for each portfolio symbol.  Results are cached
per-symbol so refresh frequency can vary:
  - Future date known  → re-check after 24 h (date could be revised)
  - No date (ETF/N/A)  → re-check after 7 days
  - Cached date passed → re-check after 6 h (look for next upcoming date)

Refreshes run in a daemon background thread so the IB polling cycle is never
blocked by Yahoo Finance network calls.
"""

import logging
import threading
import time
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

_TTL_HAS_DATE   = 24 * 3600        # known future date  → check daily
_TTL_NO_DATE    = 7  * 24 * 3600   # no date (ETF etc.) → check weekly
_TTL_PAST_DATE  = 30 * 24 * 3600   # date passed        → check monthly

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
    Thread-safe earnings date cache with per-symbol TTLs.

    refresh_if_stale(symbols) is safe to call from any thread; it spawns a
    daemon thread to fetch only the symbols whose TTL has expired and returns
    immediately.  snapshot() always returns whatever is currently cached.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._dates: dict[str, Optional[date]] = {}       # symbol → next date or None
        self._fetched_at: dict[str, float] = {}            # symbol → unix timestamp
        self._refreshing = False

    def _ttl_for(self, sym: str) -> float:
        """Return the TTL (seconds) appropriate for a symbol's current cached state."""
        cached = self._dates.get(sym)
        if cached is None:
            return _TTL_NO_DATE
        if cached < date.today():
            return _TTL_PAST_DATE
        return _TTL_HAS_DATE

    def refresh_if_stale(self, symbols: list[str]) -> None:
        """Trigger a background refresh for any symbols whose TTL has expired."""
        with self._lock:
            if self._refreshing:
                return
            now = time.time()
            stale = [
                sym for sym in symbols
                if now - self._fetched_at.get(sym, 0.0) > self._ttl_for(sym)
            ]
            if not stale:
                return
            self._refreshing = True

        thread = threading.Thread(
            target=self._do_refresh,
            args=(stale,),
            name="earnings-refresh",
            daemon=True,
        )
        thread.start()

    def _do_refresh(self, symbols: list[str]) -> None:
        logger.info("Refreshing earnings calendar for %d symbol(s): %s", len(symbols), symbols)
        new_dates: dict[str, Optional[date]] = {}
        for sym in symbols:
            new_dates[sym] = _fetch_one(sym)
        now = time.time()
        with self._lock:
            self._dates.update(new_dates)
            for sym in symbols:
                self._fetched_at[sym] = now
            self._refreshing = False
        found = sum(1 for d in new_dates.values() if d is not None)
        logger.info("Earnings calendar refreshed (%d/%d have dates)", found, len(symbols))

    def snapshot(self) -> dict[str, Optional[str]]:
        """Return {symbol: 'YYYY-MM-DD' or None} — JSON-serializable."""
        with self._lock:
            return {
                sym: dt.isoformat() if dt else None
                for sym, dt in self._dates.items()
            }
