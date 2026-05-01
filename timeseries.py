"""
SQLite-backed time-series storage for account and position snapshots.

The polling service writes one row per successful poll cycle.  The schema keeps
both account-level metrics for NLV charting and position-level marks for later
post-mortems on what changed between two points in time.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_HISTORY_DB_FILE = Path("history.sqlite3")

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

                CREATE INDEX IF NOT EXISTS idx_account_snapshots_account_asof
                    ON account_snapshots(account_filter, as_of);
                CREATE INDEX IF NOT EXISTS idx_position_snapshots_lookup
                    ON position_snapshots(account_snapshot_id, position_key);
                CREATE INDEX IF NOT EXISTS idx_contract_snapshots_lookup
                    ON contract_snapshots(account_snapshot_id, position_key);
                CREATE INDEX IF NOT EXISTS idx_daily_extremes_account_day
                    ON daily_account_extremes(account_filter, day);
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
            start_quantity = _float_or_none(before.get("quantity"))
            end_quantity = _float_or_none(after.get("quantity"))
            start_price = _float_or_none(before.get("market_price"))
            end_price = _float_or_none(after.get("market_price"))
            rows.append(
                {
                    "position_key": key,
                    "symbol": after.get("symbol") or before.get("symbol"),
                    "label": after.get("label") or before.get("label"),
                    "security_type": after.get("security_type") or before.get("security_type"),
                    "start_value": start_value,
                    "end_value": end_value,
                    "delta_value": end_value - start_value,
                    "abs_delta_value": abs(end_value - start_value),
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

        rows.sort(key=lambda item: item["abs_delta_value"], reverse=True)
        position_delta_sum = sum(row["delta_value"] for row in rows)
        rows = rows[:limit]
        start_nlv = _float_or_none(start["net_liquidation"]) or 0.0
        end_nlv = _float_or_none(end["net_liquidation"]) or 0.0

        return {
            "account_filter": account_filter,
            "level": level,
            "basis": basis,
            "start": dict(start),
            "end": dict(end),
            "net_liquidation_delta": end_nlv - start_nlv,
            "position_delta_sum": position_delta_sum,
            "rows": rows,
        }

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
                   {value_column} AS value
            FROM position_snapshots
            WHERE account_snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        return {
            row["position_key"]: {
                "symbol": row["symbol"],
                "label": row["symbol"],
                "security_type": row["security_type"],
                "quantity": row["quantity"],
                "market_price": row["market_price"],
                "value": row["value"],
            }
            for row in rows
        }

    def _contract_positions(self, conn, snapshot_id: int) -> dict:
        rows = conn.execute(
            """
            SELECT position_key, symbol, local_symbol, security_type, quantity,
                   market_price, market_value AS value
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
                "value": row["value"],
            }
            for row in rows
        }
