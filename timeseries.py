"""
SQLite-backed time-series storage for account, position, and order data.

The polling service writes account and position marks for successful poll
cycles.  Order state is compacted into segments so unchanged orders do not add a
new row every poll.  Execution fills live in the same DB so completed order
state can be reconciled without relying only on the current TWS session.
"""

from __future__ import annotations

import hashlib
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

ORDER_STATE_FIELDS = (
    "account",
    "symbol",
    "local_symbol",
    "security_type",
    "con_id",
    "action",
    "order_type",
    "total_quantity",
    "limit_price",
    "aux_price",
    "order_price",
    "time_in_force",
    "status",
    "filled",
    "remaining",
    "is_filled",
    "exchange",
    "currency",
    "order_id",
    "perm_id",
    "parent_id",
    "client_id",
)

ORDER_VOLATILE_STATE_FIELDS = {
    "account_display",
    "as_of",
    "current_price",
    "earnings_date",
    "earnings_days",
    "news_count",
    "price_distance_pct",
    "raw_json",
    "terminal_at",
    "terminal_source",
    "timestamp",
    "updated_at",
}

ORDER_TERMINAL_STATUSES = {
    "FILLED",
    "CANCELLED",
    "CANCELED",
    "APICANCELLED",
    "INACTIVE",
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
    intrinsic_value = _contract_intrinsic_value(row, underlying_price)
    if _contract_after_exercise_cutoff(row, as_of):
        return intrinsic_value

    price_source = (row["price_source"] or "").lower()
    if price_source == "cost_basis" or _is_option_cost_basis_fallback(row):
        value = 0.0
    else:
        value = _float_or_none(row["value"])

    if intrinsic_value is None:
        return value
    if value is None:
        value = 0.0
    quantity = _float_or_none(row["quantity"]) or 0.0
    if quantity > 0 and value < intrinsic_value:
        return intrinsic_value
    if quantity < 0 and value > intrinsic_value:
        return intrinsic_value
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


def _contract_after_exercise_cutoff(row, as_of) -> bool:
    if (row["security_type"] or "").upper() != "OPT":
        return False
    expiry = _option_expiry_date(row["expiry"])
    if expiry is None:
        return False

    as_of_ts = _float_or_none(as_of if as_of is not None else row["as_of"])
    if as_of_ts is None:
        return False
    as_of_et = datetime.fromtimestamp(as_of_ts, OPTION_EXERCISE_TIMEZONE)
    cutoff_et = datetime.combine(
        expiry,
        _option_exercise_cutoff_time(),
        tzinfo=OPTION_EXERCISE_TIMEZONE,
    )
    return as_of_et > cutoff_et


def _contract_intrinsic_value(row, underlying_price):
    if (row["security_type"] or "").upper() != "OPT":
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
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    ended_at REAL,
                    account_filter TEXT NOT NULL,
                    order_key TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    account TEXT,
                    symbol TEXT,
                    local_symbol TEXT,
                    security_type TEXT,
                    con_id INTEGER,
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
                    client_id INTEGER,
                    current_price REAL,
                    order_price REAL,
                    price_distance_pct REAL,
                    terminal_at REAL,
                    terminal_source TEXT,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_filter, order_key, first_seen_at, state_hash)
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
                CREATE INDEX IF NOT EXISTS idx_executions_time
                    ON executions(time);
                CREATE INDEX IF NOT EXISTS idx_executions_order
                    ON executions(perm_id, order_id);
                CREATE INDEX IF NOT EXISTS idx_earnings_dates_symbol_date
                    ON earnings_dates(symbol, earnings_date);
                """
            )
            self._ensure_order_snapshot_schema(conn)
            conn.commit()

    @staticmethod
    def _table_columns(conn, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}

    @staticmethod
    def _table_exists(conn, table_name: str) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    def _ensure_order_snapshot_schema(self, conn) -> None:
        columns = self._table_columns(conn, "order_snapshots")
        if "first_seen_at" not in columns:
            self._migrate_order_snapshots_to_segments(conn)
        elif self._table_exists(conn, "order_snapshots_legacy"):
            row = conn.execute("SELECT COUNT(*) AS count FROM order_snapshots").fetchone()
            if row and int(row["count"]) == 0:
                self._migrate_legacy_order_rows(conn, "order_snapshots_legacy")
        self._normalize_order_snapshot_keys(conn)
        self._create_order_snapshot_indexes(conn)

    @staticmethod
    def _create_order_snapshot_table(conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of REAL NOT NULL,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                ended_at REAL,
                account_filter TEXT NOT NULL,
                order_key TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                state_json TEXT NOT NULL,
                account TEXT,
                symbol TEXT,
                local_symbol TEXT,
                security_type TEXT,
                con_id INTEGER,
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
                client_id INTEGER,
                current_price REAL,
                order_price REAL,
                price_distance_pct REAL,
                terminal_at REAL,
                terminal_source TEXT,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_filter, order_key, first_seen_at, state_hash)
            );
            """
        )

    @staticmethod
    def _create_order_snapshot_indexes(conn) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_order_snapshots_current
                ON order_snapshots(account_filter, order_key, ended_at);
            CREATE INDEX IF NOT EXISTS idx_order_snapshots_seen
                ON order_snapshots(account_filter, first_seen_at, last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_order_snapshots_terminal
                ON order_snapshots(terminal_at);
            CREATE INDEX IF NOT EXISTS idx_order_snapshots_reconcile
                ON order_snapshots(perm_id, order_id, con_id);
            """
        )

    def _migrate_order_snapshots_to_segments(self, conn) -> None:
        if not self._table_exists(conn, "order_snapshots"):
            self._create_order_snapshot_table(conn)
            return

        legacy_table = "order_snapshots_legacy"
        if self._table_exists(conn, legacy_table):
            suffix = int(datetime.now().timestamp())
            legacy_table = f"order_snapshots_legacy_{suffix}"

        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_order_snapshots_account_asof;
            DROP INDEX IF EXISTS idx_order_snapshots_order;
            ALTER TABLE order_snapshots RENAME TO {legacy_table};
            """.format(legacy_table=legacy_table)
        )
        self._create_order_snapshot_table(conn)
        self._migrate_legacy_order_rows(conn, legacy_table)

    def _migrate_legacy_order_rows(self, conn, legacy_table: str) -> None:
        execution_index = self._execution_terminal_index(conn)
        cursor = conn.execute(
            f"""
            SELECT *
            FROM {legacy_table}
            ORDER BY account_filter, order_key, as_of, id
            """
        )

        pending = None
        for row in cursor:
            segment = self._order_segment_from_snapshot_row(row, execution_index)
            if (
                pending
                and pending["account_filter"] == segment["account_filter"]
                and pending["order_key"] == segment["order_key"]
                and pending["state_hash"] == segment["state_hash"]
            ):
                self._merge_order_segments(pending, segment)
                continue

            if pending is not None:
                if (
                    pending["account_filter"] == segment["account_filter"]
                    and pending["order_key"] == segment["order_key"]
                    and pending["ended_at"] is None
                ):
                    pending["ended_at"] = segment["first_seen_at"]
                self._insert_order_segment(conn, pending)
            pending = segment

        if pending is not None:
            self._insert_order_segment(conn, pending)

    @staticmethod
    def _normalize_order_snapshot_keys(conn) -> None:
        """Keep permId-based order keys stable across schema changes."""
        conn.execute(
            """
            UPDATE OR IGNORE order_snapshots
            SET order_key = 'PERM:' || perm_id || '|ORDER:' || COALESCE(order_id, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE perm_id IS NOT NULL
              AND perm_id != 0
              AND order_key != 'PERM:' || perm_id || '|ORDER:' || COALESCE(order_id, 0)
            """
        )

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
        net_liquidation = _float_or_none(metrics.get("net_liquidation"))
        if net_liquidation is None:
            return None
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
                net_liquidation=net_liquidation,
            )
            conn.commit()
            return snapshot_id

    def insert_order_snapshot(
        self,
        as_of: float,
        account_filter: str,
        orders: Iterable[dict],
        complete: bool = True,
    ) -> int:
        """Persist compact order-state segments observed during one poll cycle."""
        as_of_value = _float_or_none(as_of)
        if as_of_value is None:
            return 0

        account_filter = account_filter or "ALL"
        orders = list(orders)
        with self._connection() as conn:
            execution_index = self._execution_terminal_index(conn)
            observed_keys = set()
            inserted = 0
            for order in orders:
                segment = self._order_segment_from_order(
                    as_of_value,
                    account_filter,
                    order,
                    execution_index,
                )
                observed_keys.add(segment["order_key"])
                inserted += self._upsert_order_segment(conn, segment)

            if complete:
                self._close_missing_order_segments(
                    conn,
                    account_filter=account_filter,
                    as_of=as_of_value,
                    observed_order_keys=observed_keys,
                )

            conn.commit()
            return inserted

    def _upsert_order_segment(self, conn, segment: dict) -> int:
        latest = conn.execute(
            """
            SELECT id, state_hash, ended_at, terminal_at, terminal_source
            FROM order_snapshots
            WHERE account_filter = ? AND order_key = ?
            ORDER BY first_seen_at DESC, id DESC
            LIMIT 1
            """,
            (segment["account_filter"], segment["order_key"]),
        ).fetchone()

        if latest and latest["state_hash"] == segment["state_hash"]:
            if latest["ended_at"] is None or latest["terminal_at"] is not None:
                self._update_order_segment(conn, int(latest["id"]), latest, segment)
                return 0

        if latest and latest["ended_at"] is None:
            conn.execute(
                """
                UPDATE order_snapshots
                SET ended_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (segment["first_seen_at"], int(latest["id"])),
            )

        self._insert_order_segment(conn, segment)
        return 1

    def _update_order_segment(self, conn, segment_id: int, latest, segment: dict) -> None:
        terminal_at = latest["terminal_at"]
        terminal_source = latest["terminal_source"]
        ended_at = latest["ended_at"]
        if segment["terminal_at"] is not None and (
            terminal_at is None
            or (terminal_source != "execution" and segment["terminal_source"] == "execution")
        ):
            terminal_at = segment["terminal_at"]
            terminal_source = segment["terminal_source"]
            ended_at = segment["ended_at"]

        conn.execute(
            """
            UPDATE order_snapshots
            SET last_seen_at = ?,
                current_price = ?,
                order_price = ?,
                price_distance_pct = ?,
                terminal_at = ?,
                terminal_source = ?,
                ended_at = ?,
                raw_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                segment["last_seen_at"],
                segment["current_price"],
                segment["order_price"],
                segment["price_distance_pct"],
                terminal_at,
                terminal_source,
                ended_at,
                segment["raw_json"],
                segment_id,
            ),
        )

    def _close_missing_order_segments(
        self,
        conn,
        account_filter: str,
        as_of: float,
        observed_order_keys: set[str],
    ) -> None:
        params: list = [as_of, account_filter]
        where = [
            "account_filter = ?",
            "ended_at IS NULL",
            "terminal_at IS NULL",
        ]
        if observed_order_keys:
            placeholders = ", ".join("?" for _ in observed_order_keys)
            where.append(f"order_key NOT IN ({placeholders})")
            params.extend(sorted(observed_order_keys))

        conn.execute(
            f"""
            UPDATE order_snapshots
            SET ended_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE {" AND ".join(where)}
            """,
            params,
        )

    def _order_segment_from_snapshot_row(self, row, execution_index: dict) -> dict:
        raw = _json_loads_dict(row["raw_json"])
        order = dict(raw)
        for field in (
            "account",
            "symbol",
            "local_symbol",
            "security_type",
            "action",
            "order_type",
            "total_quantity",
            "limit_price",
            "aux_price",
            "time_in_force",
            "status",
            "filled",
            "remaining",
            "is_filled",
            "exchange",
            "currency",
            "order_id",
            "perm_id",
            "parent_id",
            "current_price",
            "order_price",
            "price_distance_pct",
            "con_id",
            "client_id",
        ):
            if field in row.keys() and row[field] is not None:
                order[field] = row[field]

        return self._order_segment_from_order(
            as_of=row["as_of"],
            account_filter=row["account_filter"],
            order=order,
            execution_index=execution_index,
            order_key=row["order_key"],
        )

    def _order_segment_from_order(
        self,
        as_of: float,
        account_filter: str,
        order: dict,
        execution_index: dict,
        order_key: Optional[str] = None,
    ) -> dict:
        order = dict(order)
        order_key = order_key or self._order_snapshot_key(order)
        state_json, state_hash = self._order_state_json_and_hash(order)
        terminal_at, terminal_source = self._order_terminal_info(
            order,
            observed_at=as_of,
            execution_index=execution_index,
        )

        return {
            "as_of": as_of,
            "first_seen_at": as_of,
            "last_seen_at": as_of,
            "ended_at": terminal_at,
            "account_filter": account_filter or "ALL",
            "order_key": order_key,
            "state_hash": state_hash,
            "state_json": state_json,
            "account": _text_or_none(order.get("account")),
            "symbol": _text_or_none(order.get("symbol")),
            "local_symbol": _text_or_none(order.get("local_symbol")),
            "security_type": _text_or_none(order.get("security_type")),
            "con_id": _int_or_none(order.get("con_id")),
            "action": _text_or_none(order.get("action")),
            "order_type": _text_or_none(order.get("order_type")),
            "total_quantity": _float_or_none(order.get("total_quantity")),
            "limit_price": _float_or_none(order.get("limit_price")),
            "aux_price": _float_or_none(order.get("aux_price")),
            "time_in_force": _text_or_none(order.get("time_in_force")),
            "status": _text_or_none(order.get("status")),
            "filled": _float_or_none(order.get("filled")),
            "remaining": _float_or_none(order.get("remaining")),
            "is_filled": 1 if order.get("is_filled") else 0,
            "exchange": _text_or_none(order.get("exchange")),
            "currency": _text_or_none(order.get("currency")),
            "order_id": _int_or_none(order.get("order_id")),
            "perm_id": _int_or_none(order.get("perm_id")),
            "parent_id": _int_or_none(order.get("parent_id")),
            "client_id": _int_or_none(order.get("client_id")),
            "current_price": _float_or_none(order.get("current_price")),
            "order_price": _float_or_none(order.get("order_price")),
            "price_distance_pct": _float_or_none(order.get("price_distance_pct")),
            "terminal_at": terminal_at,
            "terminal_source": terminal_source,
            "raw_json": _json_dumps(order),
        }

    @staticmethod
    def _merge_order_segments(pending: dict, segment: dict) -> None:
        pending["last_seen_at"] = segment["last_seen_at"]
        pending["current_price"] = segment["current_price"]
        pending["order_price"] = segment["order_price"]
        pending["price_distance_pct"] = segment["price_distance_pct"]
        pending["raw_json"] = segment["raw_json"]
        if segment["terminal_at"] is not None and (
            pending["terminal_at"] is None
            or (
                pending["terminal_source"] != "execution"
                and segment["terminal_source"] == "execution"
            )
        ):
            pending["terminal_at"] = segment["terminal_at"]
            pending["terminal_source"] = segment["terminal_source"]
            pending["ended_at"] = segment["ended_at"]

    @staticmethod
    def _insert_order_segment(conn, segment: dict) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO order_snapshots (
                as_of, first_seen_at, last_seen_at, ended_at, account_filter,
                order_key, state_hash, state_json, account, symbol, local_symbol,
                security_type, con_id, action, order_type, total_quantity,
                limit_price, aux_price, time_in_force, status, filled,
                remaining, is_filled, exchange, currency, order_id, perm_id,
                parent_id, client_id, current_price, order_price,
                price_distance_pct, terminal_at, terminal_source, raw_json
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                segment["as_of"],
                segment["first_seen_at"],
                segment["last_seen_at"],
                segment["ended_at"],
                segment["account_filter"],
                segment["order_key"],
                segment["state_hash"],
                segment["state_json"],
                segment["account"],
                segment["symbol"],
                segment["local_symbol"],
                segment["security_type"],
                segment["con_id"],
                segment["action"],
                segment["order_type"],
                segment["total_quantity"],
                segment["limit_price"],
                segment["aux_price"],
                segment["time_in_force"],
                segment["status"],
                segment["filled"],
                segment["remaining"],
                segment["is_filled"],
                segment["exchange"],
                segment["currency"],
                segment["order_id"],
                segment["perm_id"],
                segment["parent_id"],
                segment["client_id"],
                segment["current_price"],
                segment["order_price"],
                segment["price_distance_pct"],
                segment["terminal_at"],
                segment["terminal_source"],
                segment["raw_json"],
            ),
        )

    def _order_terminal_info(
        self,
        order: dict,
        observed_at: float,
        execution_index: dict,
    ) -> tuple[Optional[float], Optional[str]]:
        status = (_text_or_none(order.get("status")) or "").upper()
        is_terminal = bool(order.get("is_filled")) or status in ORDER_TERMINAL_STATUSES
        if not is_terminal:
            return None, None

        execution_at = self._lookup_execution_terminal_at(order, execution_index)
        if execution_at is not None:
            return execution_at, "execution"
        return observed_at, "observed"

    def _order_state_json_and_hash(self, order: dict) -> tuple[str, str]:
        payload = {
            field: self._normalized_order_state_value(field, order.get(field))
            for field in ORDER_STATE_FIELDS
        }
        extras = {}
        for key, value in sorted(order.items()):
            if key in ORDER_STATE_FIELDS or key in ORDER_VOLATILE_STATE_FIELDS:
                continue
            if key.startswith("_"):
                continue
            extras[key] = self._normalize_json_state_value(value)
        if extras:
            payload["extra"] = extras

        state_json = _json_dumps(payload)
        state_hash = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        return state_json, state_hash

    @staticmethod
    def _normalized_order_state_value(field: str, value):
        if field == "is_filled":
            return bool(value)
        if field in {"con_id", "order_id", "perm_id", "parent_id", "client_id"}:
            return _int_or_none(value)
        if field in {
            "total_quantity",
            "limit_price",
            "aux_price",
            "order_price",
            "filled",
            "remaining",
        }:
            return _float_or_none(value)
        return _text_or_none(value)

    @classmethod
    def _normalize_json_state_value(cls, value):
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, list):
            return [cls._normalize_json_state_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_json_state_value(value[key])
                for key in sorted(value)
            }
        text = str(value).strip()
        return text or None

    def _execution_terminal_index(self, conn) -> dict:
        index = {
            "perm_con": {},
            "perm": {},
            "order_con": {},
            "order": {},
        }
        rows = conn.execute(
            """
            SELECT account, perm_id, order_id, con_id, time
            FROM executions
            """
        ).fetchall()
        for row in rows:
            ts = self._execution_time_to_epoch(row["time"])
            if ts is None:
                continue
            account = _text_or_none(row["account"]) or ""
            con_id = _int_or_none(row["con_id"]) or 0
            perm_id = _int_or_none(row["perm_id"]) or 0
            order_id = _int_or_none(row["order_id"]) or 0
            if perm_id:
                self._update_execution_index(index["perm"], (account, perm_id), ts)
                self._update_execution_index(index["perm"], ("", perm_id), ts)
                self._update_execution_index(index["perm_con"], (account, perm_id, con_id), ts)
                self._update_execution_index(index["perm_con"], ("", perm_id, con_id), ts)
            if order_id:
                self._update_execution_index(index["order"], (account, order_id), ts)
                self._update_execution_index(index["order"], ("", order_id), ts)
                self._update_execution_index(index["order_con"], (account, order_id, con_id), ts)
                self._update_execution_index(index["order_con"], ("", order_id, con_id), ts)
        return index

    @staticmethod
    def _update_execution_index(index: dict, key: tuple, ts: float) -> None:
        existing = index.get(key)
        if existing is None or ts > existing:
            index[key] = ts

    def _lookup_execution_terminal_at(self, order: dict, execution_index: dict) -> Optional[float]:
        account = _text_or_none(order.get("account")) or ""
        con_id = _int_or_none(order.get("con_id")) or 0
        perm_id = _int_or_none(order.get("perm_id")) or 0
        order_id = _int_or_none(order.get("order_id")) or 0
        candidates = []
        if perm_id:
            candidates.extend([
                execution_index["perm_con"].get((account, perm_id, con_id)),
                execution_index["perm_con"].get(("", perm_id, con_id)),
                execution_index["perm"].get((account, perm_id)),
                execution_index["perm"].get(("", perm_id)),
            ])
        if order_id:
            candidates.extend([
                execution_index["order_con"].get((account, order_id, con_id)),
                execution_index["order_con"].get(("", order_id, con_id)),
                execution_index["order"].get((account, order_id)),
                execution_index["order"].get(("", order_id)),
            ])
        candidates = [candidate for candidate in candidates if candidate is not None]
        return max(candidates) if candidates else None

    @staticmethod
    def _execution_time_to_epoch(value) -> Optional[float]:
        if isinstance(value, datetime):
            return value.timestamp()
        number = _float_or_none(value)
        if number is not None and number > 0:
            return number

        text = str(value or "").strip()
        if not text:
            return None
        for candidate in (
            text,
            text.replace("Z", "+00:00"),
            text.replace(" ", "T", 1),
        ):
            try:
                return datetime.fromisoformat(candidate).timestamp()
            except ValueError:
                pass
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                pass
        return None

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
        con_id = _int_or_none(order.get("con_id"))
        if perm_id:
            return f"PERM:{perm_id}|ORDER:{order_id or 0}"
        if order_id:
            key = f"ORDER:{order_id}"
            if con_id:
                key += f"|CONID:{con_id}"
            return key

        parts = [
            _text_or_none(order.get("account")) or "",
            _text_or_none(order.get("symbol")) or "",
            _text_or_none(order.get("local_symbol")) or "",
            _text_or_none(order.get("security_type")) or "",
            str(con_id or ""),
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
            execution_flows = self._execution_flows(
                conn,
                account_filter=account_filter,
                start_as_of=start["as_of"],
                end_as_of=end["as_of"],
                level=level,
            )
            if level == "symbol":
                for key, flow in self._inferred_option_exercise_flows(
                    conn,
                    account_filter=account_filter,
                    start_id=start["id"],
                    end_id=end["id"],
                    start_as_of=start["as_of"],
                    end_as_of=end["as_of"],
                ).items():
                    execution_flows[key] = execution_flows.get(key, 0.0) + flow

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
                execution_flow=execution_flows.get(key),
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
                        end_price - start_price
                        if start_price is not None and end_price is not None
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

    def _execution_flows(
        self,
        conn,
        account_filter: str,
        start_as_of: float,
        end_as_of: float,
        level: str,
    ) -> dict[str, float]:
        """Return signed trade values within a comparison interval."""
        if level != "symbol":
            return {}

        where = [
            "CAST(strftime('%s', time) AS REAL) > ?",
            "CAST(strftime('%s', time) AS REAL) <= ?",
        ]
        params: list = [float(start_as_of), float(end_as_of)]
        if account_filter and account_filter != "ALL":
            where.append("account = ?")
            params.append(account_filter)

        rows = conn.execute(
            f"""
            SELECT symbol, security_type, side, shares, price
            FROM executions
            WHERE {" AND ".join(where)}
            """,
            params,
        ).fetchall()

        flows: dict[str, float] = {}
        for row in rows:
            symbol = _text_or_none(row["symbol"])
            if not symbol:
                continue
            security_type = (row["security_type"] or "").upper()
            if security_type not in {"STK", "OPT"}:
                continue
            shares = _float_or_none(row["shares"])
            price = _float_or_none(row["price"])
            if shares is None or price is None:
                continue

            side = (row["side"] or "").upper()
            if side in {"BOT", "BUY"}:
                sign = 1.0
            elif side in {"SLD", "SELL"}:
                sign = -1.0
            else:
                continue

            multiplier = 100.0 if security_type == "OPT" else 1.0
            key = f"SYMBOL:{symbol}"
            flows[key] = flows.get(key, 0.0) + sign * shares * price * multiplier
        return flows

    def _inferred_option_exercise_flows(
        self,
        conn,
        account_filter: str,
        start_id: int,
        end_id: int,
        start_as_of: float,
        end_as_of: float,
    ) -> dict[str, float]:
        """
        Infer cash flows from option exercise/assignment when IB has no fill.

        TWS executions do not always include an explicit stock "buy" for an
        exercised call or "sell" for an exercised put. Without that synthetic
        flow, History treats the resulting share position as market P&L.
        """
        start_date = datetime.fromtimestamp(
            float(start_as_of),
            OPTION_EXERCISE_TIMEZONE,
        ).date()
        end_date = datetime.fromtimestamp(
            float(end_as_of),
            OPTION_EXERCISE_TIMEZONE,
        ).date()
        stock_quantities = self._stock_quantities_by_snapshot(conn, start_id, end_id)
        stock_trade_shares = self._stock_execution_shares(
            conn,
            account_filter=account_filter,
            start_as_of=start_as_of,
            end_as_of=end_as_of,
        )

        unexplained_shares = {}
        for symbol in set(stock_quantities) | set(stock_trade_shares):
            quantities = stock_quantities.get(symbol, {})
            raw_delta = quantities.get("end", 0.0) - quantities.get("start", 0.0)
            unexplained = raw_delta - stock_trade_shares.get(symbol, 0.0)
            if abs(unexplained) >= 1e-9:
                unexplained_shares[symbol] = unexplained

        if not unexplained_shares:
            return {}

        candidates = conn.execute(
            """
            SELECT s.symbol, s.quantity AS start_quantity,
                   COALESCE(e.quantity, 0) AS end_quantity,
                   s.expiry, s.strike, s.right, s.multiplier
            FROM contract_snapshots s
            LEFT JOIN contract_snapshots e
              ON e.account_snapshot_id = ?
             AND e.position_key = s.position_key
            WHERE s.account_snapshot_id = ?
              AND UPPER(s.security_type) = 'OPT'
              AND s.quantity IS NOT NULL
              AND s.quantity != 0
            """,
            (end_id, start_id),
        ).fetchall()

        flows: dict[str, float] = {}
        remaining = dict(unexplained_shares)
        for row in candidates:
            symbol = _text_or_none(row["symbol"])
            if not symbol or symbol not in remaining:
                continue
            expiry = _option_expiry_date(row["expiry"])
            if expiry is None or expiry < start_date or expiry > end_date:
                continue

            start_quantity = _float_or_none(row["start_quantity"]) or 0.0
            end_quantity = _float_or_none(row["end_quantity"]) or 0.0
            if start_quantity == 0 or start_quantity * end_quantity < 0:
                continue
            closed_contracts = abs(start_quantity) - abs(end_quantity)
            if closed_contracts <= 0:
                continue

            strike = _float_or_none(row["strike"])
            multiplier = _float_or_none(row["multiplier"]) or 100.0
            if strike is None or strike <= 0 or multiplier <= 0:
                continue

            right = (row["right"] or "").upper()
            if right == "C":
                share_direction = 1.0 if start_quantity > 0 else -1.0
            elif right == "P":
                share_direction = -1.0 if start_quantity > 0 else 1.0
            else:
                continue

            candidate_shares = share_direction * closed_contracts * multiplier
            shares_left = remaining[symbol]
            if candidate_shares == 0 or candidate_shares * shares_left <= 0:
                continue

            inferred_shares = min(abs(candidate_shares), abs(shares_left))
            signed_inferred_shares = share_direction * inferred_shares
            key = f"SYMBOL:{symbol}"
            flows[key] = flows.get(key, 0.0) + signed_inferred_shares * strike
            remaining[symbol] = shares_left - signed_inferred_shares

        return flows

    @staticmethod
    def _stock_quantities_by_snapshot(conn, start_id: int, end_id: int) -> dict[str, dict[str, float]]:
        rows = conn.execute(
            """
            SELECT account_snapshot_id, symbol, SUM(quantity) AS quantity
            FROM contract_snapshots
            WHERE account_snapshot_id IN (?, ?)
              AND UPPER(security_type) = 'STK'
            GROUP BY account_snapshot_id, symbol
            """,
            (start_id, end_id),
        ).fetchall()
        quantities: dict[str, dict[str, float]] = {}
        for row in rows:
            symbol = _text_or_none(row["symbol"])
            if not symbol:
                continue
            bucket = quantities.setdefault(symbol, {"start": 0.0, "end": 0.0})
            key = "start" if row["account_snapshot_id"] == start_id else "end"
            bucket[key] = _float_or_none(row["quantity"]) or 0.0
        return quantities

    @staticmethod
    def _stock_execution_shares(
        conn,
        account_filter: str,
        start_as_of: float,
        end_as_of: float,
    ) -> dict[str, float]:
        where = [
            "CAST(strftime('%s', time) AS REAL) > ?",
            "CAST(strftime('%s', time) AS REAL) <= ?",
            "UPPER(security_type) = 'STK'",
        ]
        params: list = [float(start_as_of), float(end_as_of)]
        if account_filter and account_filter != "ALL":
            where.append("account = ?")
            params.append(account_filter)

        rows = conn.execute(
            f"""
            SELECT symbol, side, shares
            FROM executions
            WHERE {" AND ".join(where)}
            """,
            params,
        ).fetchall()

        shares_by_symbol: dict[str, float] = {}
        for row in rows:
            symbol = _text_or_none(row["symbol"])
            shares = _float_or_none(row["shares"])
            if not symbol or shares is None:
                continue
            side = (row["side"] or "").upper()
            if side in {"BOT", "BUY"}:
                sign = 1.0
            elif side in {"SLD", "SELL"}:
                sign = -1.0
            else:
                continue
            shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0.0) + sign * shares
        return shares_by_symbol

    @staticmethod
    def _value_contribution(
        before: dict,
        after: dict,
        raw_value_delta: float,
        basis: str,
        execution_flow: Optional[float] = None,
    ) -> tuple[float, float, str]:
        """
        Estimate how much a position contributed to NLV movement.

        Raw market-value delta is noisy when trades happen inside the interval
        because cash deployed into, or withdrawn from, a position shows up as
        "position value change."  When execution data is available, subtract
        the signed execution flow from the raw delta:

            contribution ~= value_delta - signed_execution_value

        A buy has positive flow; a sale has negative flow. If execution data is
        unavailable but quantity changed, fall back to cost-basis estimation:

            contribution ~= value_delta - quantity_delta * cost_basis

        This keeps additions/reductions from dwarfing the actual mark-to-market
        movement.  If no cost basis is available, fall back to raw delta.
        """
        if basis != "market_value":
            return raw_value_delta, 0.0, "raw_value_delta"

        if execution_flow is not None and abs(execution_flow) >= 1e-9:
            return (
                raw_value_delta - execution_flow,
                execution_flow,
                "execution_flow_adjusted",
            )

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
            SELECT position_key, account_filter, symbol, security_type, expiry,
                   strike, right, quantity,
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
            value = self._contract_value_with_fallback(
                conn,
                row,
                underlying_price=underlying_prices.get(symbol),
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
            SELECT position_key, account_filter, symbol, local_symbol,
                   security_type, expiry, strike, right, quantity, market_price, average_cost,
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
                "value": self._contract_value_with_fallback(
                    conn,
                    row,
                    underlying_price=underlying_prices.get(row["symbol"]),
                ),
            }
            for row in rows
        }

    def _contract_value_with_fallback(
        self,
        conn,
        row,
        underlying_price: Optional[float],
    ) -> Optional[float]:
        value = _sanitized_contract_market_value(
            row,
            underlying_price=underlying_price,
            as_of=row["as_of"],
        )
        if (row["price_source"] or "").lower() != "unavailable":
            return value

        prior = conn.execute(
            """
            SELECT market_value AS value, price_source, security_type, quantity,
                   market_price, average_cost, multiplier, expiry, strike, right,
                   as_of
            FROM contract_snapshots
            WHERE account_filter = ?
              AND position_key = ?
              AND as_of < ?
              AND date(as_of, 'unixepoch', 'localtime') =
                  date(?, 'unixepoch', 'localtime')
              AND price_source NOT IN ('unavailable', 'cost_basis')
            ORDER BY as_of DESC
            LIMIT 1
            """,
            (
                row["account_filter"],
                row["position_key"],
                row["as_of"],
                row["as_of"],
            ),
        ).fetchone()
        if prior is None:
            return value
        return _sanitized_contract_market_value(
            prior,
            underlying_price=underlying_price,
            as_of=row["as_of"],
        )

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
