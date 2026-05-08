"""
SQLite-backed time-series storage for account, position, and order data.

The polling service writes one row per successful poll cycle.  The schema keeps
both account-level metrics for NLV charting and position-level marks for later
post-mortems on what changed between two points in time.  Order snapshots and
execution fills live in the same DB so completed order state can be reconciled
without relying only on the current TWS session.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time as datetime_time
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


DEFAULT_HISTORY_DB_FILE = Path("history.sqlite3")
OPTION_EXERCISE_TIMEZONE = ZoneInfo("America/New_York")
OPTION_EXERCISE_CUTOFF_ET = os.getenv("IB_OPTION_EXERCISE_CUTOFF_ET", "17:20")

ACCOUNT_METRIC_COLUMNS = (
    "net_liquidation",
    "gross_position_value",
    "buying_power",
    "sma",
    "portfolio_theta",
    "ngav",
    "ngav_gross",
    "notional_leverage_ratio",
    "gross_notional_leverage_ratio",
    "standard_leverage_ratio",
)

SYMBOL_BASIS_COLUMNS = {
    "market_value": "market_value",
    "npv": "npv",
    "stock_value": "stock_value",
    "option_actual_value": "option_actual_value",
    "option_notional_value": "option_notional_value",
}


def _float_or_none(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _int_or_none(value) -> Optional[int]:
    number = _float_or_none(value)
    return int(number) if number is not None else None


def _text_or_none(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_dumps(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads_dict(data: Optional[str]) -> dict:
    if not data:
        return {}
    try:
        loaded = json.loads(data)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _is_option_cost_basis_fallback(row) -> bool:
    """Detect old option rows where avg cost was stored as market price."""
    security_type = (row["security_type"] or "").upper()
    if security_type != "OPT":
        return False
    market_price = _float_or_none(row["market_price"])
    average_cost = _float_or_none(row["average_cost"])
    multiplier = _float_or_none(row["multiplier"]) or 100.0
    if market_price is None or average_cost is None or multiplier <= 0:
        return False
    expected_price = average_cost / multiplier
    tolerance = max(0.000001, abs(expected_price) * 0.000001)
    return abs(market_price - expected_price) <= tolerance


def _sanitized_contract_market_value(
    row,
    underlying_price: Optional[float] = None,
    as_of: Optional[float] = None,
) -> Optional[float]:
    """Return contract market value with bad fallbacks removed and floors applied."""
    price_source = (row["price_source"] or "").lower()
    if price_source == "cost_basis" or _is_option_cost_basis_fallback(row):
        value = 0.0
    else:
        value = _float_or_none(row["value"])

    intrinsic_floor = _contract_intrinsic_floor_value(row, underlying_price, as_of)
    if intrinsic_floor is None:
        return value
    if value is None:
        value = 0.0
    quantity = _float_or_none(row["quantity"]) or 0.0
    if quantity > 0 and value < intrinsic_floor:
        return intrinsic_floor
    if quantity < 0 and value > intrinsic_floor:
        return intrinsic_floor
    return value


def _option_expiry_date(expiry):
    match = re.search(r"\d{8}", str(expiry or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%Y%m%d").date()
    except ValueError:
        return None


def _option_exercise_cutoff_time():
    try:
        hour_text, minute_text = OPTION_EXERCISE_CUTOFF_ET.split(":", 1)
        return datetime_time(int(hour_text), int(minute_text))
    except (TypeError, ValueError):
        return datetime_time(17, 20)


def _contract_intrinsic_floor_value(row, underlying_price, as_of):
    if (row["security_type"] or "").upper() != "OPT":
        return None
    expiry = _option_expiry_date(row["expiry"])
    if expiry is None:
        return None

    as_of_ts = _float_or_none(as_of if as_of is not None else row["as_of"])
    if as_of_ts is None:
        return None
    as_of_et = datetime.fromtimestamp(as_of_ts, OPTION_EXERCISE_TIMEZONE)
    cutoff_et = datetime.combine(
        expiry,
        _option_exercise_cutoff_time(),
        tzinfo=OPTION_EXERCISE_TIMEZONE,
    )
    if as_of_et > cutoff_et:
        return None

    price = _float_or_none(underlying_price)
    strike = _float_or_none(row["strike"])
    quantity = _float_or_none(row["quantity"]) or 0.0
    multiplier = _float_or_none(row["multiplier"]) or 100.0
    if price is None or price <= 0 or strike is None or strike <= 0:
        return None
    if quantity == 0 or multiplier <= 0:
        return None

    right = (row["right"] or "").upper()
    if right == "C":
        intrinsic = max(price - strike, 0.0)
    elif right == "P":
        intrinsic = max(strike - price, 0.0)
    else:
        return None

    if intrinsic <= 0:
        return 0.0
    floor_abs = intrinsic * multiplier * abs(quantity)
    return floor_abs if quantity > 0 else -floor_abs


RELIABLE_UNDERLYING_PRICE_SOURCES = {
    "portfolio",
    "portfolio_derived",
    "portfolio_value",
    "snapshot",
    "snapshot_retry",
    "stream_retry",
    "cached",
    "derived",
}


class TimeSeriesStore:
    """Persist and query portfolio history in a local SQLite database."""

    def __init__(self, db_path: Path | str = DEFAULT_HISTORY_DB_FILE):
        self.db_path = Path(db_path)
        if self.db_path.parent != Path("."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of REAL NOT NULL,
                    account_filter TEXT NOT NULL,
                    net_liquidation REAL,
                    gross_position_value REAL,
                    buying_power REAL,
                    sma REAL,
                    portfolio_theta REAL,
                    ngav REAL,
                    ngav_gross REAL,
                    notional_leverage_ratio REAL,
                    gross_notional_leverage_ratio REAL,
                    standard_leverage_ratio REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_filter, as_of)
                );

                CREATE TABLE IF NOT EXISTS position_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_snapshot_id INTEGER NOT NULL
                        REFERENCES account_snapshots(id) ON DELETE CASCADE,
                    as_of REAL NOT NULL,
                    account_filter TEXT NOT NULL,
                    position_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    security_type TEXT NOT NULL DEFAULT 'UNDERLYING_AGG',
                    quantity REAL,
                    market_price REAL,
                    market_value REAL,
                    price_source TEXT,
                    stock_count REAL,
                    stock_value REAL,
                    option_notional_shares REAL,
                    option_notional_value REAL,
                    option_actual_value REAL,
                    npv REAL,
                    raw_json TEXT NOT NULL,
                    UNIQUE(account_snapshot_id, position_key)
                );

                CREATE TABLE IF NOT EXISTS contract_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_snapshot_id INTEGER NOT NULL
                        REFERENCES account_snapshots(id) ON DELETE CASCADE,
                    as_of REAL NOT NULL,
                    account_filter TEXT NOT NULL,
                    position_key TEXT NOT NULL,
                    account TEXT,
                    symbol TEXT NOT NULL,
                    local_symbol TEXT,
                    security_type TEXT NOT NULL,
                    con_id INTEGER,
                    expiry TEXT,
                    strike REAL,
                    right TEXT,
                    multiplier REAL,
                    quantity REAL,
                    market_price REAL,
                    market_value REAL,
                    average_cost REAL,
                    unrealized_pnl REAL,
                    realized_pnl REAL,
                    currency TEXT,
                    price_source TEXT,
                    raw_json TEXT NOT NULL,
                    UNIQUE(account_snapshot_id, position_key)
                );

                CREATE TABLE IF NOT EXISTS daily_account_extremes (
                    day TEXT NOT NULL,
                    account_filter TEXT NOT NULL,
                    high_net_liquidation REAL,
                    high_as_of REAL,
                    low_net_liquidation REAL,
                    low_as_of REAL,
                    first_as_of REAL,
                    last_as_of REAL,
                    snapshot_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, account_filter)
                );

                CREATE TABLE IF NOT EXISTS order_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of REAL NOT NULL,
                    account_filter TEXT NOT NULL,
                    order_key TEXT NOT NULL,
                    account TEXT,
                    symbol TEXT,
                    local_symbol TEXT,
                    security_type TEXT,
                    action TEXT,
                    order_type TEXT,
                    total_quantity REAL,
                    limit_price REAL,
                    aux_price REAL,
                    time_in_force TEXT,
                    status TEXT,
                    filled REAL,
                    remaining REAL,
                    is_filled INTEGER NOT NULL DEFAULT 0,
                    exchange TEXT,
                    currency TEXT,
                    order_id INTEGER,
                    perm_id INTEGER,
                    parent_id INTEGER,
                    current_price REAL,
                    order_price REAL,
                    price_distance_pct REAL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_filter, as_of, order_key)
                );

                CREATE TABLE IF NOT EXISTS executions (
                    exec_id TEXT PRIMARY KEY,
                    time TEXT,
                    account TEXT,
                    symbol TEXT,
                    local_symbol TEXT,
                    security_type TEXT,
                    side TEXT,
                    shares REAL,
                    price REAL,
                    avg_price REAL,
                    order_id INTEGER,
                    perm_id INTEGER,
                    client_id INTEGER,
                    con_id INTEGER,
                    exchange TEXT,
                    currency TEXT,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS earnings_dates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    earnings_date TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'yfinance',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    fetched_at REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, earnings_date, source)
                );

                CREATE TABLE IF NOT EXISTS earnings_fetch_state (
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'yfinance',
                    earnings_date TEXT,
                    fetched_at REAL NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(symbol, source)
                );

                CREATE INDEX IF NOT EXISTS idx_account_snapshots_account_asof
                    ON account_snapshots(account_filter, as_of);
                CREATE INDEX IF NOT EXISTS idx_position_snapshots_lookup
                    ON position_snapshots(account_snapshot_id, position_key);
                CREATE INDEX IF NOT EXISTS idx_contract_snapshots_lookup
                    ON contract_snapshots(account_snapshot_id, position_key);
                CREATE INDEX IF NOT EXISTS idx_daily_extremes_account_day
                    ON daily_account_extremes(account_filter, day);
                CREATE INDEX IF NOT EXISTS idx_order_snapshots_account_asof
                    ON order_snapshots(account_filter, as_of);
                CREATE INDEX IF NOT EXISTS idx_order_snapshots_order
                    ON order_snapshots(perm_id, order_id);
                CREATE INDEX IF NOT EXISTS idx_executions_time
                    ON executions(time);
                CREATE INDEX IF NOT EXISTS idx_executions_order
                    ON executions(perm_id, order_id);
                CREATE INDEX IF NOT EXISTS idx_earnings_dates_symbol_date
                    ON earnings_dates(symbol, earnings_date);
                """
            )
            conn.commit()

    def insert_snapshot(
        self,
        snapshot: dict,
        account_filter: str,
        contract_positions: Optional[Iterable[dict]] = None,
    ) -> Optional[int]:
        """Insert one account snapshot plus its symbol and contract marks."""
        as_of = _float_or_none(snapshot.get("as_of"))
        if as_of is None:
            return None

        account_filter = account_filter or "ALL"
        metrics = snapshot.get("metrics") or {}
        account_values = [_float_or_none(metrics.get(column)) for column in ACCOUNT_METRIC_COLUMNS]

        with self._connection() as conn:
            columns = ", ".join(("as_of", "account_filter") + ACCOUNT_METRIC_COLUMNS)
            placeholders = ", ".join(["?"] * (2 + len(ACCOUNT_METRIC_COLUMNS)))
            cursor = conn.execute(
                f"""
                INSERT OR IGNORE INTO account_snapshots ({columns})
                VALUES ({placeholders})
                """,
                [as_of, account_filter] + account_values,
            )
            if cursor.rowcount == 0:
                conn.commit()
                return self._snapshot_id(conn, account_filter, as_of)

            snapshot_id = int(cursor.lastrowid)
            self._insert_symbol_positions(
                conn,
                snapshot_id=snapshot_id,
                as_of=as_of,
                account_filter=account_filter,
                positions=snapshot.get("positions") or [],
            )
            self._insert_contract_positions(
                conn,
                snapshot_id=snapshot_id,
                as_of=as_of,
                account_filter=account_filter,
                contract_positions=contract_positions or [],
            )
            self._upsert_daily_extremes(
                conn,
                as_of=as_of,
                account_filter=account_filter,
                net_liquidation=_float_or_none(metrics.get("net_liquidation")),
            )
            conn.commit()
            return snapshot_id

    def insert_order_snapshot(
        self,
        as_of: float,
        account_filter: str,
        orders: Iterable[dict],
    ) -> int:
        """Persist normalized order rows observed during one poll cycle."""
        as_of_value = _float_or_none(as_of)
        if as_of_value is None:
            return 0

        rows = []
        for order in orders:
            rows.append(
                (
                    as_of_value,
                    account_filter or "ALL",
                    self._order_snapshot_key(order),
                    _text_or_none(order.get("account")),
                    _text_or_none(order.get("symbol")),
                    _text_or_none(order.get("local_symbol")),
                    _text_or_none(order.get("security_type")),
                    _text_or_none(order.get("action")),
                    _text_or_none(order.get("order_type")),
                    _float_or_none(order.get("total_quantity")),
                    _float_or_none(order.get("limit_price")),
                    _float_or_none(order.get("aux_price")),
                    _text_or_none(order.get("time_in_force")),
                    _text_or_none(order.get("status")),
                    _float_or_none(order.get("filled")),
                    _float_or_none(order.get("remaining")),
                    1 if order.get("is_filled") else 0,
                    _text_or_none(order.get("exchange")),
                    _text_or_none(order.get("currency")),
                    _int_or_none(order.get("order_id")),
                    _int_or_none(order.get("perm_id")),
                    _int_or_none(order.get("parent_id")),
                    _float_or_none(order.get("current_price")),
                    _float_or_none(order.get("order_price")),
                    _float_or_none(order.get("price_distance_pct")),
                    _json_dumps(order),
                )
            )

        if not rows:
            return 0

        with self._connection() as conn:
            cursor = conn.executemany(
                """
                INSERT OR IGNORE INTO order_snapshots (
                    as_of, account_filter, order_key, account, symbol, local_symbol,
                    security_type, action, order_type, total_quantity, limit_price,
                    aux_price, time_in_force, status, filled, remaining, is_filled,
                    exchange, currency, order_id, perm_id, parent_id, current_price,
                    order_price, price_distance_pct, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0

    def insert_executions(self, executions: Iterable[dict]) -> int:
        """Persist execution fills, de-duplicated by IB execution id."""
        rows = []
        for execution in executions:
            exec_id = _text_or_none(execution.get("exec_id"))
            if not exec_id:
                continue
            rows.append(
                (
                    exec_id,
                    _text_or_none(execution.get("time")),
                    _text_or_none(execution.get("account")),
                    _text_or_none(execution.get("symbol")),
                    _text_or_none(execution.get("local_symbol")),
                    _text_or_none(execution.get("security_type")),
                    _text_or_none(execution.get("side")),
                    _float_or_none(execution.get("shares")),
                    _float_or_none(execution.get("price")),
                    _float_or_none(execution.get("avg_price")),
                    _int_or_none(execution.get("order_id")),
                    _int_or_none(execution.get("perm_id")),
                    _int_or_none(execution.get("client_id")),
                    _int_or_none(execution.get("con_id")),
                    _text_or_none(execution.get("exchange")),
                    _text_or_none(execution.get("currency")),
                    _json_dumps(execution),
                )
            )

        if not rows:
            return 0

        with self._connection() as conn:
            cursor = conn.executemany(
                """
                INSERT OR IGNORE INTO executions (
                    exec_id, time, account, symbol, local_symbol, security_type,
                    side, shares, price, avg_price, order_id, perm_id, client_id,
                    con_id, exchange, currency, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0

    def get_recent_executions(self, days: int = 7, limit: int = 5000) -> list[dict]:
        """Return recently stored executions for order reconciliation."""
        days = max(1, min(int(days), 30))
        limit = max(1, min(int(limit), 20000))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT exec_id, time, account, symbol, local_symbol, security_type,
                       side, shares, price, avg_price, order_id, perm_id,
                       client_id, con_id, exchange, currency
                FROM executions
                WHERE created_at >= datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"-{days} days", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_earnings_result(
        self,
        symbol: str,
        earnings_date: Optional[str],
        fetched_at: float,
        source: str = "yfinance",
    ) -> None:
        """Persist one earnings-calendar lookup without deleting old dates."""
        symbol = (_text_or_none(symbol) or "").upper()
        source = _text_or_none(source) or "yfinance"
        fetched_at_value = _float_or_none(fetched_at)
        if not symbol or fetched_at_value is None:
            return

        earnings_date = _text_or_none(earnings_date)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO earnings_fetch_state (
                    symbol, source, earnings_date, fetched_at, updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol, source) DO UPDATE SET
                    earnings_date = excluded.earnings_date,
                    fetched_at = excluded.fetched_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (symbol, source, earnings_date, fetched_at_value),
            )
            if earnings_date:
                conn.execute(
                    """
                    INSERT INTO earnings_dates (
                        symbol, earnings_date, source, first_seen_at,
                        last_seen_at, fetched_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(symbol, earnings_date, source) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        fetched_at = excluded.fetched_at,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        symbol,
                        earnings_date,
                        source,
                        fetched_at_value,
                        fetched_at_value,
                        fetched_at_value,
                    ),
                )
            conn.commit()

    def get_earnings_cache_entries(self, source: str = "yfinance") -> list[dict]:
        """Return latest per-symbol earnings cache state for warm startup."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT symbol, earnings_date, fetched_at, source
                FROM earnings_fetch_state
                WHERE source = ?
                """,
                (_text_or_none(source) or "yfinance",),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _order_snapshot_key(order: dict) -> str:
        perm_id = _int_or_none(order.get("perm_id"))
        order_id = _int_or_none(order.get("order_id"))
        if perm_id or order_id:
            return f"PERM:{perm_id or 0}|ORDER:{order_id or 0}"

        parts = [
            _text_or_none(order.get("account")) or "",
            _text_or_none(order.get("symbol")) or "",
            _text_or_none(order.get("local_symbol")) or "",
            _text_or_none(order.get("security_type")) or "",
            _text_or_none(order.get("action")) or "",
            str(_float_or_none(order.get("total_quantity")) or ""),
            str(_float_or_none(order.get("order_price")) or ""),
        ]
        return "ORDER:" + "|".join(parts)

    def _snapshot_id(self, conn, account_filter: str, as_of: float) -> Optional[int]:
        row = conn.execute(
            """
            SELECT id
            FROM account_snapshots
            WHERE account_filter = ? AND as_of = ?
            """,
            (account_filter, as_of),
        ).fetchone()
        return int(row["id"]) if row else None

    def _insert_symbol_positions(
        self,
        conn,
        snapshot_id: int,
        as_of: float,
        account_filter: str,
        positions: Iterable[dict],
    ) -> None:
        rows = []
        for position in positions:
            symbol = _text_or_none(position.get("symbol"))
            if not symbol:
                continue
            stock_value = _float_or_none(position.get("stock_value"))
            option_actual_value = _float_or_none(position.get("option_actual_value"))
            market_value = None
            if stock_value is not None or option_actual_value is not None:
                market_value = (stock_value or 0.0) + (option_actual_value or 0.0)

            rows.append(
                (
                    snapshot_id,
                    as_of,
                    account_filter,
                    f"SYMBOL:{symbol}",
                    symbol,
                    "UNDERLYING_AGG",
                    _float_or_none(position.get("stock_count")),
                    _float_or_none(position.get("underlying_market_price")),
                    market_value,
                    _text_or_none(position.get("underlying_price_source")),
                    _float_or_none(position.get("stock_count")),
                    stock_value,
                    _float_or_none(position.get("option_notional_shares")),
                    _float_or_none(position.get("option_notional_value")),
                    option_actual_value,
                    _float_or_none(position.get("npv")),
                    _json_dumps(position),
                )
            )

        if not rows:
            return

        conn.executemany(
            """
            INSERT OR IGNORE INTO position_snapshots (
                account_snapshot_id, as_of, account_filter, position_key, symbol,
                security_type, quantity, market_price, market_value, price_source,
                stock_count, stock_value, option_notional_shares,
                option_notional_value, option_actual_value, npv, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _insert_contract_positions(
        self,
        conn,
        snapshot_id: int,
        as_of: float,
        account_filter: str,
        contract_positions: Iterable[dict],
    ) -> None:
        rows = []
        for position in contract_positions:
            symbol = _text_or_none(position.get("symbol"))
            if not symbol:
                continue
            rows.append(
                (
                    snapshot_id,
                    as_of,
                    account_filter,
                    self._contract_position_key(position),
                    _text_or_none(position.get("account")),
                    symbol,
                    _text_or_none(position.get("local_symbol")),
                    _text_or_none(position.get("security_type")) or "UNKNOWN",
                    _int_or_none(position.get("con_id")),
                    _text_or_none(position.get("expiry")),
                    _float_or_none(position.get("strike")),
                    _text_or_none(position.get("right")),
                    _float_or_none(position.get("multiplier")),
                    _float_or_none(position.get("quantity")),
                    _float_or_none(position.get("market_price")),
                    _float_or_none(position.get("market_value")),
                    _float_or_none(position.get("average_cost")),
                    _float_or_none(position.get("unrealized_pnl")),
                    _float_or_none(position.get("realized_pnl")),
                    _text_or_none(position.get("currency")),
                    _text_or_none(position.get("price_source")),
                    _json_dumps(position),
                )
            )

        if not rows:
            return

        conn.executemany(
            """
            INSERT OR IGNORE INTO contract_snapshots (
                account_snapshot_id, as_of, account_filter, position_key, account,
                symbol, local_symbol, security_type, con_id, expiry, strike, right,
                multiplier, quantity, market_price, market_value, average_cost,
                unrealized_pnl, realized_pnl, currency, price_source, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    @staticmethod
    def _contract_position_key(position: dict) -> str:
        account = _text_or_none(position.get("account")) or ""
        con_id = _int_or_none(position.get("con_id"))
        if con_id:
            return f"ACCOUNT:{account}|CONID:{con_id}"

        parts = [
            account,
            _text_or_none(position.get("security_type")) or "",
            _text_or_none(position.get("symbol")) or "",
            _text_or_none(position.get("expiry")) or "",
            str(_float_or_none(position.get("strike")) or ""),
            _text_or_none(position.get("right")) or "",
            _text_or_none(position.get("local_symbol")) or "",
        ]
        return "CONTRACT:" + "|".join(parts)

    def _upsert_daily_extremes(
        self,
        conn,
        as_of: float,
        account_filter: str,
        net_liquidation: Optional[float],
    ) -> None:
        if net_liquidation is None:
            return
        day = datetime.fromtimestamp(as_of).date().isoformat()
        conn.execute(
            """
            INSERT INTO daily_account_extremes (
                day, account_filter, high_net_liquidation, high_as_of,
                low_net_liquidation, low_as_of, first_as_of, last_as_of,
                snapshot_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(day, account_filter) DO UPDATE SET
                high_net_liquidation = CASE
                    WHEN excluded.high_net_liquidation > daily_account_extremes.high_net_liquidation
                    THEN excluded.high_net_liquidation
                    ELSE daily_account_extremes.high_net_liquidation
                END,
                high_as_of = CASE
                    WHEN excluded.high_net_liquidation > daily_account_extremes.high_net_liquidation
                    THEN excluded.high_as_of
                    ELSE daily_account_extremes.high_as_of
                END,
                low_net_liquidation = CASE
                    WHEN excluded.low_net_liquidation < daily_account_extremes.low_net_liquidation
                    THEN excluded.low_net_liquidation
                    ELSE daily_account_extremes.low_net_liquidation
                END,
                low_as_of = CASE
                    WHEN excluded.low_net_liquidation < daily_account_extremes.low_net_liquidation
                    THEN excluded.low_as_of
                    ELSE daily_account_extremes.low_as_of
                END,
                first_as_of = MIN(daily_account_extremes.first_as_of, excluded.first_as_of),
                last_as_of = MAX(daily_account_extremes.last_as_of, excluded.last_as_of),
                snapshot_count = daily_account_extremes.snapshot_count + 1
            """,
            (
                day,
                account_filter,
                net_liquidation,
                as_of,
                net_liquidation,
                as_of,
                as_of,
                as_of,
            ),
        )

    def get_net_liquidation_history(
        self,
        account_filter: str,
        limit: int = 1000,
        start_as_of: Optional[float] = None,
        end_as_of: Optional[float] = None,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 10000))
        where = ["account_filter = ?"]
        params: list = [account_filter]
        if start_as_of is not None:
            where.append("as_of >= ?")
            params.append(float(start_as_of))
        if end_as_of is not None:
            where.append("as_of <= ?")
            params.append(float(end_as_of))

        where_sql = " AND ".join(where)
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT id, as_of, account_filter, net_liquidation,
                           gross_position_value, buying_power, sma,
                           portfolio_theta, ngav, ngav_gross,
                           notional_leverage_ratio,
                           gross_notional_leverage_ratio,
                           standard_leverage_ratio
                    FROM account_snapshots
                    WHERE {where_sql}
                    ORDER BY as_of DESC
                    LIMIT ?
                )
                ORDER BY as_of ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_extremes(self, account_filter: str, days: int = 90) -> list[dict]:
        days = max(1, min(int(days), 3660))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT day, account_filter, high_net_liquidation, high_as_of,
                       low_net_liquidation, low_as_of, first_as_of, last_as_of,
                       snapshot_count
                FROM daily_account_extremes
                WHERE account_filter = ?
                ORDER BY day DESC
                LIMIT ?
                """,
                (account_filter, days),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def compare_positions(
        self,
        account_filter: str,
        start_id: int,
        end_id: int,
        basis: str = "market_value",
        level: str = "symbol",
        limit: int = 100,
    ) -> Optional[dict]:
        level = level if level in {"symbol", "contract"} else "symbol"
        if level == "contract":
            basis = "market_value"
        if basis not in SYMBOL_BASIS_COLUMNS:
            raise ValueError(f"Unsupported comparison basis: {basis}")

        limit = max(1, min(int(limit), 500))
        with self._connection() as conn:
            start = self._account_snapshot(conn, account_filter, start_id)
            end = self._account_snapshot(conn, account_filter, end_id)
            if start is None or end is None:
                return None
            if start["as_of"] > end["as_of"]:
                start, end = end, start

            if level == "contract":
                start_positions = self._contract_positions(conn, start["id"])
                end_positions = self._contract_positions(conn, end["id"])
            else:
                column = SYMBOL_BASIS_COLUMNS[basis]
                start_positions = self._symbol_positions(conn, start["id"], column)
                end_positions = self._symbol_positions(conn, end["id"], column)

        rows = []
        for key in sorted(set(start_positions) | set(end_positions)):
            before = start_positions.get(key, {})
            after = end_positions.get(key, {})
            start_value = _float_or_none(before.get("value")) or 0.0
            end_value = _float_or_none(after.get("value")) or 0.0
            raw_value_delta = end_value - start_value
            stock_delta_value = self._component_delta(before, after, "stock_value")
            option_actual_delta_value = self._component_delta(
                before,
                after,
                "option_actual_value",
            )
            start_quantity = _float_or_none(before.get("quantity"))
            end_quantity = _float_or_none(after.get("quantity"))
            start_price = _float_or_none(before.get("market_price"))
            end_price = _float_or_none(after.get("market_price"))
            contribution, flow_adjustment, contribution_source = self._value_contribution(
                before=before,
                after=after,
                raw_value_delta=raw_value_delta,
                basis=basis,
            )
            rows.append(
                {
                    "position_key": key,
                    "symbol": after.get("symbol") or before.get("symbol"),
                    "label": after.get("label") or before.get("label"),
                    "security_type": after.get("security_type") or before.get("security_type"),
                    "start_value": start_value,
                    "end_value": end_value,
                    "delta_value": contribution,
                    "abs_delta_value": abs(contribution),
                    "raw_value_delta": raw_value_delta,
                    "stock_delta_value": stock_delta_value,
                    "option_actual_delta_value": option_actual_delta_value,
                    "flow_adjustment": flow_adjustment,
                    "contribution_source": contribution_source,
                    "start_quantity": start_quantity,
                    "end_quantity": end_quantity,
                    "quantity_delta": (
                        (end_quantity or 0.0) - (start_quantity or 0.0)
                        if start_quantity is not None or end_quantity is not None
                        else None
                    ),
                    "start_price": start_price,
                    "end_price": end_price,
                    "price_delta": (
                        (end_price or 0.0) - (start_price or 0.0)
                        if start_price is not None or end_price is not None
                        else None
                    ),
                }
            )

        start_nlv = _float_or_none(start["net_liquidation"]) or 0.0
        end_nlv = _float_or_none(end["net_liquidation"]) or 0.0
        net_liquidation_delta = end_nlv - start_nlv
        rows.sort(key=lambda item: item["abs_delta_value"], reverse=True)
        position_delta_sum = sum(row["delta_value"] for row in rows)
        omitted_position_count = max(len(rows) - limit, 0)
        rows = rows[:limit]
        displayed_position_delta_sum = sum(row["delta_value"] for row in rows)
        reconciliation_delta = net_liquidation_delta - displayed_position_delta_sum
        if abs(reconciliation_delta) >= 0.005:
            note = (
                "Cash movement, fees, timing differences, and other account "
                "changes not explained by displayed positions."
            )
            if omitted_position_count:
                note = (
                    "Cash movement, fees, timing differences, omitted rows, "
                    "and other account changes not explained by displayed "
                    "positions."
                )
            rows.append({
                "position_key": "ACCOUNT:RECONCILIATION",
                "symbol": "ACCOUNT",
                "label": "Other",
                "note": note,
                "security_type": "ACCOUNT",
                "start_value": None,
                "end_value": None,
                "delta_value": reconciliation_delta,
                "abs_delta_value": abs(reconciliation_delta),
                "raw_value_delta": reconciliation_delta,
                "stock_delta_value": None,
                "option_actual_delta_value": None,
                "flow_adjustment": 0.0,
                "contribution_source": "reconciliation",
                "start_quantity": None,
                "end_quantity": None,
                "quantity_delta": None,
                "start_price": None,
                "end_price": None,
                "price_delta": None,
            })

        return {
            "account_filter": account_filter,
            "level": level,
            "basis": basis,
            "start": dict(start),
            "end": dict(end),
            "net_liquidation_delta": net_liquidation_delta,
            "position_delta_sum": position_delta_sum,
            "displayed_position_delta_sum": displayed_position_delta_sum,
            "reconciliation_delta": reconciliation_delta,
            "omitted_position_count": omitted_position_count,
            "rows": rows,
        }

    @staticmethod
    def _component_delta(before: dict, after: dict, key: str) -> Optional[float]:
        start = _float_or_none(before.get(key))
        end = _float_or_none(after.get(key))
        if start is None and end is None:
            return None
        return (end or 0.0) - (start or 0.0)

    @staticmethod
    def _value_contribution(
        before: dict,
        after: dict,
        raw_value_delta: float,
        basis: str,
    ) -> tuple[float, float, str]:
        """
        Estimate how much a position contributed to NLV movement.

        Raw market-value delta is noisy when quantity changes because new cash
        deployed into a position shows up as "position value change."  For the
        actual-value basis, subtract the estimated trade flow from the raw delta
        when a cost basis is available:

            contribution ~= value_delta - quantity_delta * cost_basis

        This keeps additions/reductions from dwarfing the actual mark-to-market
        movement.  If no cost basis is available, fall back to raw delta.
        """
        if basis != "market_value":
            return raw_value_delta, 0.0, "raw_value_delta"

        start_quantity = _float_or_none(before.get("quantity")) or 0.0
        end_quantity = _float_or_none(after.get("quantity")) or 0.0
        quantity_delta = end_quantity - start_quantity
        if abs(quantity_delta) < 1e-9:
            return raw_value_delta, 0.0, "mark_to_market"

        cost_basis = _float_or_none(after.get("cost_basis"))
        if cost_basis is None:
            cost_basis = _float_or_none(before.get("cost_basis"))
        if cost_basis is None:
            return raw_value_delta, 0.0, "raw_value_delta"

        flow_adjustment = quantity_delta * cost_basis
        return raw_value_delta - flow_adjustment, flow_adjustment, "flow_adjusted"

    def _account_snapshot(self, conn, account_filter: str, snapshot_id: int):
        return conn.execute(
            """
            SELECT *
            FROM account_snapshots
            WHERE id = ? AND account_filter = ?
            """,
            (snapshot_id, account_filter),
        ).fetchone()

    def _symbol_positions(self, conn, snapshot_id: int, value_column: str) -> dict:
        rows = conn.execute(
            f"""
            SELECT position_key, symbol, security_type, quantity, market_price,
                   price_source, stock_value, option_actual_value,
                   option_notional_shares, option_notional_value, npv,
                   raw_json, as_of, account_filter, {value_column} AS value
            FROM position_snapshots
            WHERE account_snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        result = {}
        for row in rows:
            raw = _json_loads_dict(row["raw_json"])
            quantity = _float_or_none(row["quantity"]) or 0.0
            market_price = _float_or_none(row["market_price"])
            stock_value = _float_or_none(row["stock_value"]) or 0.0
            option_notional_value = _float_or_none(row["option_notional_value"]) or 0.0
            value = _float_or_none(row["value"])
            if (
                value_column in {
                    "market_value",
                    "stock_value",
                    "option_notional_value",
                    "npv",
                }
                and quantity != 0
                and (row["price_source"] or "").lower() == "option_greeks"
            ):
                reliable_price = self._nearest_reliable_symbol_price(conn, row)
                if reliable_price is not None:
                    market_price = reliable_price
                    stock_value = quantity * reliable_price
                    option_notional_value = (
                        (_float_or_none(row["option_notional_shares"]) or 0.0)
                        * reliable_price
                    )
                    option_actual_value = _float_or_none(row["option_actual_value"]) or 0.0
                    if value_column == "market_value":
                        value = stock_value + option_actual_value
                    elif value_column == "stock_value":
                        value = stock_value
                    elif value_column == "option_notional_value":
                        value = option_notional_value
                    elif value_column == "npv":
                        value = stock_value + option_notional_value
            result[row["position_key"]] = {
                "symbol": row["symbol"],
                "label": row["symbol"],
                "security_type": row["security_type"],
                "quantity": row["quantity"],
                "market_price": market_price,
                "value": value,
                "cost_basis": _float_or_none(raw.get("underlying_cost_basis")),
                "stock_value": stock_value,
                "option_actual_value": row["option_actual_value"],
                "option_notional_value": option_notional_value,
                "npv": row["npv"],
            }
        if value_column in {"market_value", "option_actual_value"}:
            overrides = self._contract_symbol_value_overrides(conn, snapshot_id)
            for key, override in overrides.items():
                if key not in result:
                    continue
                result[key]["value"] = (
                    override["market_value"]
                    if value_column == "market_value"
                    else override["option_actual_value"]
                )
                result[key]["stock_value"] = override["stock_value"]
                result[key]["option_actual_value"] = override["option_actual_value"]
        return result

    def _nearest_reliable_symbol_price(self, conn, row) -> Optional[float]:
        """Find a same-day stock mark to replace bad historical option undPrice."""
        placeholders = ",".join("?" for _ in RELIABLE_UNDERLYING_PRICE_SOURCES)
        reliable_row = conn.execute(
            f"""
            SELECT market_price
            FROM position_snapshots
            WHERE account_filter = ?
              AND symbol = ?
              AND market_price IS NOT NULL
              AND market_price > 0
              AND price_source IN ({placeholders})
              AND date(as_of, 'unixepoch', 'localtime') =
                  date(?, 'unixepoch', 'localtime')
            ORDER BY abs(as_of - ?) ASC, as_of ASC
            LIMIT 1
            """,
            (
                row["account_filter"],
                row["symbol"],
                *sorted(RELIABLE_UNDERLYING_PRICE_SOURCES),
                row["as_of"],
                row["as_of"],
            ),
        ).fetchone()
        if reliable_row is None:
            return None
        return _float_or_none(reliable_row["market_price"])

    def _contract_symbol_value_overrides(self, conn, snapshot_id: int) -> dict:
        underlying_prices = self._snapshot_reliable_underlying_prices(conn, snapshot_id)
        rows = conn.execute(
            """
            SELECT symbol, security_type, expiry, strike, right, quantity,
                   market_price, market_value AS value, average_cost,
                   multiplier, price_source, as_of
            FROM contract_snapshots
            WHERE account_snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        by_symbol = {}
        for row in rows:
            symbol = row["symbol"]
            if not symbol:
                continue
            entry = by_symbol.setdefault(
                f"SYMBOL:{symbol}",
                {"stock_value": 0.0, "option_actual_value": 0.0, "market_value": 0.0},
            )
            value = _sanitized_contract_market_value(
                row,
                underlying_price=underlying_prices.get(symbol),
                as_of=row["as_of"],
            ) or 0.0
            if (row["security_type"] or "").upper() == "OPT":
                entry["option_actual_value"] += value
            else:
                entry["stock_value"] += value

        for entry in by_symbol.values():
            entry["market_value"] = entry["stock_value"] + entry["option_actual_value"]
        return by_symbol

    def _contract_positions(self, conn, snapshot_id: int) -> dict:
        underlying_prices = self._snapshot_reliable_underlying_prices(conn, snapshot_id)
        rows = conn.execute(
            """
            SELECT position_key, symbol, local_symbol, security_type, expiry,
                   strike, right, quantity, market_price, average_cost,
                   multiplier, price_source, as_of, market_value AS value
            FROM contract_snapshots
            WHERE account_snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        return {
            row["position_key"]: {
                "symbol": row["symbol"],
                "label": row["local_symbol"] or row["symbol"],
                "security_type": row["security_type"],
                "quantity": row["quantity"],
                "market_price": row["market_price"],
                "cost_basis": row["average_cost"],
                "value": _sanitized_contract_market_value(
                    row,
                    underlying_price=underlying_prices.get(row["symbol"]),
                    as_of=row["as_of"],
                ),
            }
            for row in rows
        }

    def _snapshot_reliable_underlying_prices(self, conn, snapshot_id: int) -> dict:
        placeholders = ",".join("?" for _ in RELIABLE_UNDERLYING_PRICE_SOURCES)
        rows = conn.execute(
            f"""
            SELECT symbol, market_price
            FROM position_snapshots
            WHERE account_snapshot_id = ?
              AND market_price IS NOT NULL
              AND market_price > 0
              AND price_source IN ({placeholders})
            """,
            (snapshot_id, *sorted(RELIABLE_UNDERLYING_PRICE_SOURCES)),
        ).fetchall()
        return {
            row["symbol"]: _float_or_none(row["market_price"])
            for row in rows
            if row["symbol"]
        }
