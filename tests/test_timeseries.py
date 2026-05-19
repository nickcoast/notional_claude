import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from timeseries import TimeSeriesStore  # noqa: E402


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "live_history_sanitized.json"


def load_fixture():
    with FIXTURE_PATH.open() as f:
        return json.load(f)


def snapshot_from_fixture(entry):
    return {
        "as_of": entry["as_of"],
        "metrics": entry["metrics"],
        "positions": entry["positions"],
    }


def position_value(position, basis="market_value"):
    if basis == "market_value":
        return (position.get("stock_value") or 0.0) + (
            position.get("option_actual_value") or 0.0
        )
    return position.get(basis) or 0.0


def expected_position_order(start, end, basis="market_value"):
    start_by_symbol = {p["symbol"]: p for p in start["positions"]}
    end_by_symbol = {p["symbol"]: p for p in end["positions"]}
    rows = []
    for symbol in sorted(set(start_by_symbol) | set(end_by_symbol)):
        start_value = position_value(start_by_symbol.get(symbol, {}), basis)
        end_value = position_value(end_by_symbol.get(symbol, {}), basis)
        rows.append((symbol, end_value - start_value))
    rows.sort(key=lambda item: abs(item[1]), reverse=True)
    return rows


class TimeSeriesStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.sqlite3"
        self.store = TimeSeriesStore(self.db_path)

    def insert_fixture(self):
        fixture = load_fixture()
        ids = []
        for entry in fixture["snapshots"]:
            ids.append(
                self.store.insert_snapshot(
                    snapshot_from_fixture(entry),
                    account_filter=fixture["account_filter"],
                )
            )
        return fixture, ids

    def account_snapshot_count(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]

    def table_count(self, table):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def order_segments(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM order_snapshots
                ORDER BY first_seen_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_legacy_order_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE order_snapshots (
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
                CREATE INDEX idx_order_snapshots_account_asof
                    ON order_snapshots(account_filter, as_of);
                CREATE INDEX idx_order_snapshots_order
                    ON order_snapshots(perm_id, order_id);

                CREATE TABLE executions (
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
                """
            )

    def insert_legacy_order(self, as_of, **overrides):
        order = {
            "account": "TEST_ACCOUNT",
            "symbol": "AAPL",
            "local_symbol": "AAPL",
            "security_type": "STK",
            "action": "BUY",
            "order_type": "LMT",
            "total_quantity": 10,
            "limit_price": 190.0,
            "aux_price": 0.0,
            "time_in_force": "DAY",
            "status": "Submitted",
            "filled": 0.0,
            "remaining": 10.0,
            "is_filled": False,
            "exchange": "SMART",
            "currency": "USD",
            "order_id": 11,
            "perm_id": 22,
            "parent_id": 0,
            "current_price": 191.0,
            "order_price": 190.0,
            "price_distance_pct": 0.52,
        }
        order.update(overrides)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO order_snapshots (
                    as_of, account_filter, order_key, account, symbol,
                    local_symbol, security_type, action, order_type,
                    total_quantity, limit_price, aux_price, time_in_force,
                    status, filled, remaining, is_filled, exchange, currency,
                    order_id, perm_id, parent_id, current_price, order_price,
                    price_distance_pct, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    as_of,
                    "TEST_ACCOUNT",
                    "PERM:22|ORDER:11",
                    order["account"],
                    order["symbol"],
                    order["local_symbol"],
                    order["security_type"],
                    order["action"],
                    order["order_type"],
                    order["total_quantity"],
                    order["limit_price"],
                    order["aux_price"],
                    order["time_in_force"],
                    order["status"],
                    order["filled"],
                    order["remaining"],
                    1 if order["is_filled"] else 0,
                    order["exchange"],
                    order["currency"],
                    order["order_id"],
                    order["perm_id"],
                    order["parent_id"],
                    order["current_price"],
                    order["order_price"],
                    order["price_distance_pct"],
                    json.dumps(order, sort_keys=True),
                ),
            )

    def insert_execution(self, exec_id="0001.01", time="1970-01-01T00:16:30+00:00"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO executions (
                    exec_id, time, account, symbol, local_symbol, security_type,
                    side, shares, price, avg_price, order_id, perm_id,
                    client_id, con_id, exchange, currency, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exec_id,
                    time,
                    "TEST_ACCOUNT",
                    "AAPL",
                    "AAPL",
                    "STK",
                    "BOT",
                    10,
                    190.0,
                    190.0,
                    11,
                    22,
                    1234,
                    265598,
                    "SMART",
                    "USD",
                    "{}",
                ),
            )

    def test_live_fixture_inserts_history_and_daily_extremes(self):
        fixture, _ids = self.insert_fixture()

        history = self.store.get_net_liquidation_history(fixture["account_filter"])
        self.assertEqual(len(history), len(fixture["snapshots"]))
        self.assertEqual(
            [row["as_of"] for row in history],
            [entry["as_of"] for entry in fixture["snapshots"]],
        )
        self.assertEqual(
            [row["net_liquidation"] for row in history],
            [entry["metrics"]["net_liquidation"] for entry in fixture["snapshots"]],
        )

        extremes = self.store.get_daily_extremes(fixture["account_filter"])
        self.assertEqual(len(extremes), 1)
        expected_values = [
            entry["metrics"]["net_liquidation"] for entry in fixture["snapshots"]
        ]
        self.assertEqual(extremes[0]["snapshot_count"], len(fixture["snapshots"]))
        self.assertAlmostEqual(extremes[0]["high_net_liquidation"], max(expected_values))
        self.assertAlmostEqual(extremes[0]["low_net_liquidation"], min(expected_values))

    def test_duplicate_snapshot_insert_does_not_duplicate_rows(self):
        fixture = load_fixture()
        first = fixture["snapshots"][0]

        first_id = self.store.insert_snapshot(
            snapshot_from_fixture(first),
            account_filter=fixture["account_filter"],
        )
        duplicate_id = self.store.insert_snapshot(
            snapshot_from_fixture(first),
            account_filter=fixture["account_filter"],
        )

        self.assertEqual(duplicate_id, first_id)
        self.assertEqual(self.account_snapshot_count(), 1)

    def test_snapshot_without_net_liquidation_is_not_persisted(self):
        snapshot_id = self.store.insert_snapshot(
            {"as_of": 1000.0, "metrics": {}, "positions": []},
            account_filter="TEST_ACCOUNT",
        )

        self.assertIsNone(snapshot_id)
        self.assertEqual(self.account_snapshot_count(), 0)

    def test_zero_net_liquidation_snapshot_can_be_persisted(self):
        snapshot_id = self.store.insert_snapshot(
            {"as_of": 1000.0, "metrics": {"net_liquidation": 0.0}, "positions": []},
            account_filter="TEST_ACCOUNT",
        )

        self.assertIsNotNone(snapshot_id)
        self.assertEqual(self.account_snapshot_count(), 1)

    def test_order_snapshots_and_executions_are_persisted(self):
        orders = [
            {
                "account": "TEST_ACCOUNT",
                "symbol": "AAPL",
                "local_symbol": "AAPL",
                "security_type": "STK",
                "action": "BUY",
                "order_type": "LMT",
                "total_quantity": 10,
                "limit_price": 190.0,
                "status": "Submitted",
                "filled": 0,
                "remaining": 10,
                "order_id": 11,
                "perm_id": 22,
                "con_id": 265598,
                "client_id": 1234,
                "order_price": 190.0,
                "current_price": 191.0,
                "price_distance_pct": 0.52,
            }
        ]
        executions = [
            {
                "exec_id": "0001.01",
                "time": "20260508 12:00:00",
                "account": "TEST_ACCOUNT",
                "symbol": "AAPL",
                "local_symbol": "AAPL",
                "security_type": "STK",
                "side": "BOT",
                "shares": 10,
                "price": 190.0,
                "avg_price": 190.0,
                "order_id": 11,
                "perm_id": 22,
                "client_id": 1234,
                "con_id": 265598,
                "exchange": "NASDAQ",
                "currency": "USD",
            }
        ]

        self.assertEqual(
            self.store.insert_order_snapshot(1000.0, "TEST_ACCOUNT", orders),
            1,
        )
        self.assertEqual(self.store.insert_executions(executions), 1)
        self.assertEqual(self.store.insert_executions(executions), 0)

        self.assertEqual(self.table_count("order_snapshots"), 1)
        self.assertEqual(self.table_count("executions"), 1)
        self.assertEqual(self.store.get_recent_executions()[0]["exec_id"], "0001.01")

    def test_perm_id_order_key_ignores_con_id(self):
        key = self.store._order_snapshot_key({
            "perm_id": 22,
            "order_id": 0,
            "con_id": 265598,
        })

        self.assertEqual(key, "PERM:22|ORDER:0")

    def test_order_snapshot_updates_market_fields_without_new_segment(self):
        order = {
            "account": "TEST_ACCOUNT",
            "symbol": "AAPL",
            "local_symbol": "AAPL",
            "security_type": "STK",
            "con_id": 265598,
            "action": "BUY",
            "order_type": "LMT",
            "total_quantity": 10,
            "limit_price": 190.0,
            "status": "Submitted",
            "filled": 0,
            "remaining": 10,
            "order_id": 11,
            "perm_id": 22,
            "client_id": 1234,
            "order_price": 190.0,
            "current_price": 191.0,
            "price_distance_pct": 0.52,
        }

        self.assertEqual(self.store.insert_order_snapshot(1000.0, "TEST_ACCOUNT", [order]), 1)
        updated = dict(order, current_price=192.0, price_distance_pct=1.04)
        self.assertEqual(self.store.insert_order_snapshot(1010.0, "TEST_ACCOUNT", [updated]), 0)

        segments = self.order_segments()
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["first_seen_at"], 1000.0)
        self.assertEqual(segments[0]["last_seen_at"], 1010.0)
        self.assertEqual(segments[0]["current_price"], 192.0)
        self.assertNotIn("current_price", segments[0]["state_json"])

    def test_order_snapshot_price_change_creates_new_segment(self):
        order = {
            "account": "TEST_ACCOUNT",
            "symbol": "AAPL",
            "security_type": "STK",
            "action": "SELL",
            "order_type": "STP LMT",
            "total_quantity": 10,
            "limit_price": 190.0,
            "aux_price": 189.0,
            "status": "PreSubmitted",
            "filled": 0,
            "remaining": 10,
            "order_id": 11,
            "perm_id": 22,
            "order_price": 189.0,
        }

        self.store.insert_order_snapshot(1000.0, "TEST_ACCOUNT", [order])
        self.store.insert_order_snapshot(
            1010.0,
            "TEST_ACCOUNT",
            [dict(order, aux_price=188.5, order_price=188.5)],
        )

        segments = self.order_segments()
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["ended_at"], 1010.0)
        self.assertEqual(segments[1]["aux_price"], 188.5)

    def test_missing_order_closes_only_after_complete_poll(self):
        order = {
            "account": "TEST_ACCOUNT",
            "symbol": "AAPL",
            "security_type": "STK",
            "action": "BUY",
            "order_type": "LMT",
            "total_quantity": 10,
            "limit_price": 190.0,
            "status": "Submitted",
            "filled": 0,
            "remaining": 10,
            "order_id": 11,
            "perm_id": 22,
        }

        self.store.insert_order_snapshot(1000.0, "TEST_ACCOUNT", [order])
        self.store.insert_order_snapshot(1010.0, "TEST_ACCOUNT", [], complete=False)
        self.assertIsNone(self.order_segments()[0]["ended_at"])

        self.store.insert_order_snapshot(1020.0, "TEST_ACCOUNT", [], complete=True)
        self.assertEqual(self.order_segments()[0]["ended_at"], 1020.0)

    def test_legacy_order_migration_collapses_repeated_state(self):
        self.tmpdir.cleanup()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.sqlite3"
        self.create_legacy_order_schema()
        self.insert_legacy_order(1000.0, current_price=191.0, price_distance_pct=0.52)
        self.insert_legacy_order(1010.0, current_price=192.0, price_distance_pct=1.04)
        self.insert_legacy_order(1020.0, limit_price=191.0, order_price=191.0)

        self.store = TimeSeriesStore(self.db_path)

        self.assertEqual(self.table_count("order_snapshots_legacy"), 3)
        segments = self.order_segments()
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["first_seen_at"], 1000.0)
        self.assertEqual(segments[0]["last_seen_at"], 1010.0)
        self.assertEqual(segments[0]["ended_at"], 1020.0)
        self.assertEqual(segments[0]["current_price"], 192.0)
        self.assertEqual(segments[1]["limit_price"], 191.0)

    def test_legacy_filled_order_uses_execution_terminal_time(self):
        self.tmpdir.cleanup()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.sqlite3"
        self.create_legacy_order_schema()
        self.insert_execution(time="1970-01-01T00:16:30+00:00")
        self.insert_legacy_order(
            1000.0,
            status="Filled",
            filled=10.0,
            remaining=0.0,
            is_filled=True,
        )

        self.store = TimeSeriesStore(self.db_path)

        segment = self.order_segments()[0]
        self.assertEqual(segment["terminal_at"], 990.0)
        self.assertEqual(segment["terminal_source"], "execution")
        self.assertEqual(segment["ended_at"], 990.0)

    def test_legacy_filled_order_falls_back_to_observed_terminal_time(self):
        self.tmpdir.cleanup()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.sqlite3"
        self.create_legacy_order_schema()
        self.insert_legacy_order(
            1000.0,
            status="Filled",
            filled=10.0,
            remaining=0.0,
            is_filled=True,
        )

        self.store = TimeSeriesStore(self.db_path)

        segment = self.order_segments()[0]
        self.assertEqual(segment["terminal_at"], 1000.0)
        self.assertEqual(segment["terminal_source"], "observed")

    def test_startup_normalizes_con_id_order_key_for_perm_id(self):
        order = {
            "account": "TEST_ACCOUNT",
            "symbol": "AAPL",
            "security_type": "STK",
            "con_id": 265598,
            "action": "BUY",
            "order_type": "LMT",
            "total_quantity": 10,
            "limit_price": 190.0,
            "status": "Submitted",
            "filled": 0,
            "remaining": 10,
            "order_id": 0,
            "perm_id": 22,
        }
        self.store.insert_order_snapshot(1000.0, "TEST_ACCOUNT", [order])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE order_snapshots
                SET order_key = 'PERM:22|ORDER:0|CONID:265598'
                """
            )

        self.store = TimeSeriesStore(self.db_path)

        self.assertEqual(
            self.order_segments()[0]["order_key"],
            "PERM:22|ORDER:0",
        )

    def test_earnings_results_preserve_historical_dates(self):
        self.store.upsert_earnings_result("AAPL", "2026-05-01", 1000.0)
        self.store.upsert_earnings_result("AAPL", "2026-08-01", 2000.0)
        self.store.upsert_earnings_result("ETF", None, 3000.0)

        cache_entries = {
            row["symbol"]: row
            for row in self.store.get_earnings_cache_entries()
        }
        self.assertEqual(cache_entries["AAPL"]["earnings_date"], "2026-08-01")
        self.assertEqual(cache_entries["ETF"]["earnings_date"], None)

        with sqlite3.connect(self.db_path) as conn:
            dates = conn.execute(
                """
                SELECT earnings_date
                FROM earnings_dates
                WHERE symbol = 'AAPL'
                ORDER BY earnings_date
                """
            ).fetchall()
        self.assertEqual([row[0] for row in dates], ["2026-05-01", "2026-08-01"])

    def test_live_fixture_compares_symbols_by_largest_value_change(self):
        fixture, ids = self.insert_fixture()

        comparison = self.store.compare_positions(
            fixture["account_filter"],
            start_id=ids[0],
            end_id=ids[1],
            basis="market_value",
        )

        expected = expected_position_order(
            fixture["snapshots"][0],
            fixture["snapshots"][1],
            basis="market_value",
        )
        position_rows = [
            row for row in comparison["rows"]
            if row["contribution_source"] != "reconciliation"
        ]
        self.assertEqual(
            [row["symbol"] for row in position_rows],
            [symbol for symbol, _delta in expected],
        )
        self.assertAlmostEqual(
            comparison["net_liquidation_delta"],
            fixture["snapshots"][1]["metrics"]["net_liquidation"]
            - fixture["snapshots"][0]["metrics"]["net_liquidation"],
        )
        self.assertAlmostEqual(
            comparison["position_delta_sum"],
            sum(delta for _symbol, delta in expected),
        )
        displayed_sum = sum(row["delta_value"] for row in comparison["rows"])
        self.assertAlmostEqual(displayed_sum, comparison["net_liquidation_delta"])

    def test_symbol_comparison_adjusts_for_added_stock_flow(self):
        first = {
            "as_of": 1700000200.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "SYM300",
                    "stock_count": 5.0,
                    "underlying_market_price": 1137.8,
                    "underlying_cost_basis": 1100.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 5689.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 5689.0,
                }
            ],
        }
        second = {
            "as_of": 1700000215.0,
            "metrics": {"net_liquidation": 1025.0},
            "positions": [
                {
                    "symbol": "SYM300",
                    "stock_count": 20.0,
                    "underlying_market_price": 1152.5,
                    "underlying_cost_basis": 1141.6,
                    "underlying_price_source": "snapshot",
                    "stock_value": 23050.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 23050.0,
                }
            ],
        }

        first_id = self.store.insert_snapshot(first, "TEST_ACCOUNT")
        second_id = self.store.insert_snapshot(second, "TEST_ACCOUNT")
        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            basis="market_value",
        )

        row = comparison["rows"][0]
        raw_delta = second["positions"][0]["stock_value"] - first["positions"][0]["stock_value"]
        expected_contribution = (
            5.0 * (1152.5 - 1137.8)
            + 15.0 * (1152.5 - 1141.6)
        )
        self.assertAlmostEqual(row["raw_value_delta"], raw_delta)
        self.assertAlmostEqual(row["flow_adjustment"], 15.0 * 1141.6)
        self.assertAlmostEqual(row["delta_value"], expected_contribution)
        self.assertEqual(row["contribution_source"], "flow_adjusted")
        reconciliation = comparison["rows"][-1]
        self.assertEqual(reconciliation["contribution_source"], "reconciliation")
        self.assertAlmostEqual(
            row["delta_value"] + reconciliation["delta_value"],
            comparison["net_liquidation_delta"],
        )

    def test_symbol_comparison_uses_execution_flows_for_sold_stock(self):
        start_as_of = 1700000200.0
        end_as_of = 1700000300.0
        first = {
            "as_of": start_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "SALE",
                    "stock_count": 10.0,
                    "underlying_market_price": 100.0,
                    "underlying_cost_basis": 50.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 1000.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 1000.0,
                }
            ],
        }
        second = {
            "as_of": end_as_of,
            "metrics": {"net_liquidation": 1025.0},
            "positions": [
                {
                    "symbol": "SALE",
                    "stock_count": 5.0,
                    "underlying_market_price": 110.0,
                    "underlying_cost_basis": 50.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 550.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 550.0,
                }
            ],
        }

        first_id = self.store.insert_snapshot(first, "TEST_ACCOUNT")
        second_id = self.store.insert_snapshot(second, "TEST_ACCOUNT")
        self.store.insert_executions([
            {
                "exec_id": "sale-1",
                "time": datetime.fromtimestamp(
                    start_as_of + 20,
                    timezone.utc,
                ).isoformat(sep=" "),
                "account": "TEST_ACCOUNT",
                "symbol": "SALE",
                "local_symbol": "SALE",
                "security_type": "STK",
                "side": "SLD",
                "shares": 5,
                "price": 108.0,
                "avg_price": 108.0,
            }
        ])

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "SALE")
        self.assertAlmostEqual(row["raw_value_delta"], -450.0)
        self.assertAlmostEqual(row["flow_adjustment"], -540.0)
        self.assertAlmostEqual(row["delta_value"], 90.0)
        self.assertEqual(row["contribution_source"], "execution_flow_adjusted")

    def test_closed_position_does_not_report_zero_price_delta(self):
        start_as_of = 1700000200.0
        end_as_of = 1700000300.0
        first = {
            "as_of": start_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "CLOSED",
                    "stock_count": -10.0,
                    "underlying_market_price": 425.0,
                    "underlying_cost_basis": 428.0,
                    "underlying_price_source": "portfolio",
                    "stock_value": -4250.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": -4250.0,
                }
            ],
        }
        second = {
            "as_of": end_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [],
        }

        first_id = self.store.insert_snapshot(first, "TEST_ACCOUNT")
        second_id = self.store.insert_snapshot(second, "TEST_ACCOUNT")
        self.store.insert_executions([
            {
                "exec_id": "cover-1",
                "time": datetime.fromtimestamp(
                    start_as_of + 20,
                    timezone.utc,
                ).isoformat(sep=" "),
                "account": "TEST_ACCOUNT",
                "symbol": "CLOSED",
                "local_symbol": "CLOSED",
                "security_type": "STK",
                "side": "BOT",
                "shares": 10,
                "price": 441.0,
                "avg_price": 441.0,
            }
        ])

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "CLOSED")
        self.assertIsNone(row["end_price"])
        self.assertIsNone(row["price_delta"])
        self.assertAlmostEqual(row["delta_value"], -160.0)

    def test_combo_bag_executions_do_not_create_stock_flow(self):
        start_as_of = datetime(
            2026,
            5,
            11,
            6,
            38,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        end_as_of = datetime(
            2026,
            5,
            13,
            1,
            13,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        start = {
            "as_of": start_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "QQQ",
                    "stock_count": 1.25,
                    "underlying_market_price": 711.40,
                    "underlying_cost_basis": 506.42,
                    "underlying_price_source": "portfolio",
                    "stock_value": 889.25,
                    "option_actual_value": 30.50,
                    "option_notional_value": 0.0,
                    "npv": 889.25,
                }
            ],
        }
        end = {
            "as_of": end_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "QQQ",
                    "stock_count": 1.25,
                    "underlying_market_price": 713.17,
                    "underlying_cost_basis": 506.42,
                    "underlying_price_source": "portfolio",
                    "stock_value": 891.46,
                    "option_actual_value": 21.62,
                    "option_notional_value": 0.0,
                    "npv": 891.46,
                }
            ],
        }
        start_id = self.store.insert_snapshot(
            start,
            "TEST_ACCOUNT",
            contract_positions=[
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "QQQ",
                    "local_symbol": "QQQ",
                    "security_type": "STK",
                    "con_id": 1,
                    "quantity": 1.25,
                    "market_price": 711.40,
                    "market_value": 889.25,
                    "average_cost": 506.42,
                    "price_source": "portfolio",
                },
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "QQQ",
                    "local_symbol": "QQQ 260511P00693000",
                    "security_type": "OPT",
                    "con_id": 2,
                    "expiry": "20260511",
                    "strike": 693.0,
                    "right": "P",
                    "quantity": 3,
                    "market_price": 0.10,
                    "market_value": 30.0,
                    "multiplier": 100,
                    "price_source": "ib_portfolio",
                },
            ],
        )
        end_id = self.store.insert_snapshot(
            end,
            "TEST_ACCOUNT",
            contract_positions=[
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "QQQ",
                    "local_symbol": "QQQ",
                    "security_type": "STK",
                    "con_id": 1,
                    "quantity": 1.25,
                    "market_price": 713.17,
                    "market_value": 891.46,
                    "average_cost": 506.42,
                    "price_source": "portfolio",
                },
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "QQQ",
                    "local_symbol": "QQQ 260513P00691000",
                    "security_type": "OPT",
                    "con_id": 3,
                    "expiry": "20260513",
                    "strike": 691.0,
                    "right": "P",
                    "quantity": 5,
                    "market_price": 0.04324,
                    "market_value": 21.62,
                    "multiplier": 100,
                    "price_source": "ib_portfolio",
                },
            ],
        )
        self.store.insert_executions([
            {
                "exec_id": "combo-parent",
                "time": datetime.fromtimestamp(
                    start_as_of + 100,
                    timezone.utc,
                ).isoformat(sep=" "),
                "account": "TEST_ACCOUNT",
                "symbol": "QQQ",
                "local_symbol": "QQQ",
                "security_type": "BAG",
                "side": "BOT",
                "shares": 3,
                "price": 0.31,
                "avg_price": 0.31,
            },
            {
                "exec_id": "combo-short-leg",
                "time": datetime.fromtimestamp(
                    start_as_of + 100,
                    timezone.utc,
                ).isoformat(sep=" "),
                "account": "TEST_ACCOUNT",
                "symbol": "QQQ",
                "local_symbol": "QQQ 260512P00694000",
                "security_type": "OPT",
                "side": "SLD",
                "shares": 3,
                "price": 0.32,
                "avg_price": 0.32,
            },
            {
                "exec_id": "combo-long-leg",
                "time": datetime.fromtimestamp(
                    start_as_of + 100,
                    timezone.utc,
                ).isoformat(sep=" "),
                "account": "TEST_ACCOUNT",
                "symbol": "QQQ",
                "local_symbol": "QQQ 260512P00700000",
                "security_type": "OPT",
                "side": "BOT",
                "shares": 3,
                "price": 0.63,
                "avg_price": 0.63,
            },
        ])

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=start_id,
            end_id=end_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "QQQ")
        self.assertAlmostEqual(row["raw_value_delta"], -6.17)
        self.assertAlmostEqual(row["flow_adjustment"], 93.0)
        self.assertAlmostEqual(row["delta_value"], -99.17)

    def test_symbol_comparison_infers_option_exercise_stock_flow(self):
        start_as_of = datetime(
            2026,
            5,
            8,
            16,
            34,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        end_as_of = datetime(
            2026,
            5,
            11,
            4,
            18,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        start = {
            "as_of": start_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "DDOG",
                    "stock_count": 175.0,
                    "underlying_market_price": 200.75,
                    "underlying_cost_basis": 147.83,
                    "underlying_price_source": "snapshot",
                    "stock_value": 35131.25,
                    "option_actual_value": 450.0,
                    "option_notional_value": 0.0,
                    "npv": 35131.25,
                }
            ],
        }
        end = {
            "as_of": end_as_of,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "DDOG",
                    "stock_count": 575.0,
                    "underlying_market_price": 198.64,
                    "underlying_cost_basis": 188.61,
                    "underlying_price_source": "portfolio",
                    "stock_value": 114218.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 114218.0,
                }
            ],
        }
        start_id = self.store.insert_snapshot(
            start,
            "TEST_ACCOUNT",
            contract_positions=[
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "DDOG",
                    "local_symbol": "DDOG",
                    "security_type": "STK",
                    "con_id": 1,
                    "quantity": 175,
                    "market_price": 200.75,
                    "market_value": 35131.25,
                    "average_cost": 147.83,
                    "price_source": "snapshot",
                },
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "DDOG",
                    "local_symbol": "DDOG 260508C00200000",
                    "security_type": "OPT",
                    "con_id": 2,
                    "expiry": "20260508",
                    "strike": 200.0,
                    "right": "C",
                    "quantity": 6,
                    "market_price": 0.75,
                    "market_value": 450.0,
                    "multiplier": 100,
                    "price_source": "option_market_data",
                },
            ],
        )
        end_id = self.store.insert_snapshot(
            end,
            "TEST_ACCOUNT",
            contract_positions=[
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "DDOG",
                    "local_symbol": "DDOG",
                    "security_type": "STK",
                    "con_id": 1,
                    "quantity": 575,
                    "market_price": 198.64,
                    "market_value": 114218.0,
                    "average_cost": 188.61,
                    "price_source": "portfolio",
                }
            ],
        )
        self.store.insert_executions([
            {
                "exec_id": "ddog-sale-1",
                "time": datetime.fromtimestamp(
                    end_as_of - 120,
                    timezone.utc,
                ).isoformat(sep=" "),
                "account": "TEST_ACCOUNT",
                "symbol": "DDOG",
                "local_symbol": "DDOG",
                "security_type": "STK",
                "side": "SLD",
                "shares": 200,
                "price": 198.95,
                "avg_price": 198.95,
            }
        ])

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=start_id,
            end_id=end_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "DDOG")
        self.assertAlmostEqual(row["raw_value_delta"], 78636.75)
        self.assertAlmostEqual(row["flow_adjustment"], 80210.0)
        self.assertAlmostEqual(row["delta_value"], -1573.25)
        self.assertEqual(row["contribution_source"], "execution_flow_adjusted")

    def test_symbol_comparison_uses_last_contract_mark_when_quote_unavailable(self):
        first = {
            "as_of": 1700000400.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "OPT",
                    "stock_count": 0.0,
                    "underlying_market_price": 100.0,
                    "underlying_cost_basis": 0.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 0.0,
                    "option_actual_value": 300.0,
                    "option_notional_value": 0.0,
                    "npv": 0.0,
                }
            ],
        }
        second = {
            "as_of": 1700000500.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "OPT",
                    "stock_count": 0.0,
                    "underlying_market_price": 100.0,
                    "underlying_cost_basis": 0.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 0.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 0.0,
                }
            ],
        }
        valid_contract = {
            "account": "TEST_ACCOUNT",
            "symbol": "OPT",
            "local_symbol": "OPT 260618C00100000",
            "security_type": "OPT",
            "con_id": 7001,
            "expiry": "20260618",
            "strike": 100.0,
            "right": "C",
            "quantity": 3,
            "market_price": 1.0,
            "market_value": 300.0,
            "multiplier": 100,
            "price_source": "option_market_data",
        }
        unavailable_contract = dict(valid_contract)
        unavailable_contract.update({
            "market_price": 0.0,
            "market_value": 0.0,
            "price_source": "unavailable",
        })

        first_id = self.store.insert_snapshot(
            first,
            "TEST_ACCOUNT",
            contract_positions=[valid_contract],
        )
        second_id = self.store.insert_snapshot(
            second,
            "TEST_ACCOUNT",
            contract_positions=[unavailable_contract],
        )

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "OPT")
        self.assertAlmostEqual(row["start_value"], 300.0)
        self.assertAlmostEqual(row["end_value"], 300.0)
        self.assertAlmostEqual(row["delta_value"], 0.0)

    def test_expired_otm_option_ignores_stale_market_value_after_cutoff(self):
        start_ts = datetime(
            2026,
            5,
            8,
            13,
            39,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        end_ts = datetime(
            2026,
            5,
            8,
            16,
            7,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        start = {
            "as_of": start_ts,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "QQQ",
                    "stock_count": 0.0,
                    "underlying_market_price": 711.14,
                    "underlying_cost_basis": 0.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 0.0,
                    "option_actual_value": 20.0,
                    "option_notional_value": 0.0,
                    "npv": 0.0,
                }
            ],
        }
        end = {
            "as_of": end_ts,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "QQQ",
                    "stock_count": 0.0,
                    "underlying_market_price": 711.70,
                    "underlying_cost_basis": 0.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 0.0,
                    "option_actual_value": 8340.0,
                    "option_notional_value": 0.0,
                    "npv": 0.0,
                }
            ],
        }
        start_contract = {
            "account": "TEST_ACCOUNT",
            "symbol": "QQQ",
            "local_symbol": "QQQ 260508P00703000",
            "security_type": "OPT",
            "con_id": 8001,
            "expiry": "20260508",
            "strike": 703.0,
            "right": "P",
            "quantity": 10,
            "market_price": 0.02,
            "market_value": 20.0,
            "multiplier": 100,
            "price_source": "option_market_data",
        }
        stale_end_contract = dict(start_contract)
        stale_end_contract.update({
            "market_price": 8.34,
            "market_value": 8340.0,
            "price_source": "option_market_data",
        })

        start_id = self.store.insert_snapshot(
            start,
            "TEST_ACCOUNT",
            contract_positions=[start_contract],
        )
        end_id = self.store.insert_snapshot(
            end,
            "TEST_ACCOUNT",
            contract_positions=[stale_end_contract],
        )

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=start_id,
            end_id=end_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "QQQ")
        self.assertAlmostEqual(row["start_value"], 20.0)
        self.assertAlmostEqual(row["end_value"], 0.0)
        self.assertAlmostEqual(row["delta_value"], -20.0)

    def test_contract_level_comparison_uses_contract_marks(self):
        first = {
            "as_of": 1700000100.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [],
        }
        second = {
            "as_of": 1700000115.0,
            "metrics": {"net_liquidation": 1025.0},
            "positions": [],
        }
        first_contracts = [
            {
                "account": "TEST_ACCOUNT",
                "symbol": "SYM100",
                "local_symbol": "SYM100 OPT",
                "security_type": "OPT",
                "con_id": 1001,
                "quantity": 2,
                "market_price": 1.5,
                "market_value": 300.0,
            },
            {
                "account": "TEST_ACCOUNT",
                "symbol": "SYM200",
                "security_type": "STK",
                "con_id": 2001,
                "quantity": 5,
                "market_price": 40.0,
                "market_value": 200.0,
            },
        ]
        second_contracts = [
            {
                "account": "TEST_ACCOUNT",
                "symbol": "SYM100",
                "local_symbol": "SYM100 OPT",
                "security_type": "OPT",
                "con_id": 1001,
                "quantity": 2,
                "market_price": 2.2,
                "market_value": 440.0,
            },
            {
                "account": "TEST_ACCOUNT",
                "symbol": "SYM200",
                "security_type": "STK",
                "con_id": 2001,
                "quantity": 4,
                "market_price": 41.0,
                "market_value": 164.0,
            },
        ]

        first_id = self.store.insert_snapshot(
            first,
            "TEST_ACCOUNT",
            contract_positions=first_contracts,
        )
        second_id = self.store.insert_snapshot(
            second,
            "TEST_ACCOUNT",
            contract_positions=second_contracts,
        )

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            level="contract",
        )

        self.assertEqual(comparison["level"], "contract")
        self.assertEqual(comparison["basis"], "market_value")
        self.assertEqual(comparison["rows"][0]["label"], "SYM100 OPT")
        self.assertAlmostEqual(comparison["rows"][0]["delta_value"], 140.0)
        self.assertAlmostEqual(comparison["rows"][1]["quantity_delta"], -1.0)

    def test_symbol_comparison_ignores_option_avg_cost_fallback_marks(self):
        first = {
            "as_of": 1700000300.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "DUOL",
                    "stock_count": 4.0,
                    "underlying_market_price": 106.0,
                    "underlying_cost_basis": 102.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 424.0,
                    "option_actual_value": 15.0,
                    "option_notional_value": 0.0,
                    "npv": 424.0,
                }
            ],
        }
        second = {
            "as_of": 1700000315.0,
            "metrics": {"net_liquidation": 1001.0},
            "positions": [
                {
                    "symbol": "DUOL",
                    "stock_count": 4.0,
                    "underlying_market_price": 106.25,
                    "underlying_cost_basis": 102.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 425.0,
                    "option_actual_value": 459.44,
                    "option_notional_value": 0.0,
                    "npv": 425.0,
                }
            ],
        }
        first_contracts = [
            {
                "account": "TEST_ACCOUNT",
                "symbol": "DUOL",
                "security_type": "STK",
                "con_id": 2001,
                "quantity": 4,
                "market_price": 106.0,
                "market_value": 424.0,
                "average_cost": 102.0,
            },
            {
                "account": "TEST_ACCOUNT",
                "symbol": "DUOL",
                "local_symbol": "DUOL OPT",
                "security_type": "OPT",
                "con_id": 1001,
                "quantity": 3,
                "market_price": 0.05,
                "market_value": 15.0,
                "average_cost": 153.14796665,
                "multiplier": 100,
            },
        ]
        second_contracts = [
            {
                "account": "TEST_ACCOUNT",
                "symbol": "DUOL",
                "security_type": "STK",
                "con_id": 2001,
                "quantity": 4,
                "market_price": 106.25,
                "market_value": 425.0,
                "average_cost": 102.0,
            },
            {
                "account": "TEST_ACCOUNT",
                "symbol": "DUOL",
                "local_symbol": "DUOL OPT",
                "security_type": "OPT",
                "con_id": 1001,
                "quantity": 3,
                "market_price": 1.5314796665,
                "market_value": 459.44389995,
                "average_cost": 153.14796665,
                "multiplier": 100,
            },
        ]

        first_id = self.store.insert_snapshot(
            first,
            "TEST_ACCOUNT",
            contract_positions=first_contracts,
        )
        second_id = self.store.insert_snapshot(
            second,
            "TEST_ACCOUNT",
            contract_positions=second_contracts,
        )

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "DUOL")
        self.assertAlmostEqual(row["start_value"], 439.0)
        self.assertAlmostEqual(row["end_value"], 425.0)
        self.assertAlmostEqual(row["delta_value"], -14.0)

    def test_symbol_comparison_ignores_stock_cost_basis_fallback_marks(self):
        first = {
            "as_of": 1700000400.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "ACB",
                    "stock_count": 10.0,
                    "underlying_market_price": 100.0,
                    "underlying_cost_basis": 100.0,
                    "underlying_price_source": "cost_basis",
                    "stock_value": 1000.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 1000.0,
                }
            ],
        }
        second = {
            "as_of": 1700000415.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "ACB",
                    "stock_count": 10.0,
                    "underlying_market_price": 110.0,
                    "underlying_cost_basis": 110.0,
                    "underlying_price_source": "cost_basis",
                    "stock_value": 1100.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 1100.0,
                }
            ],
        }
        first_id = self.store.insert_snapshot(
            first,
            "TEST_ACCOUNT",
            contract_positions=[
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "ACB",
                    "security_type": "STK",
                    "con_id": 3001,
                    "quantity": 10,
                    "market_price": 100.0,
                    "market_value": 1000.0,
                    "average_cost": 100.0,
                    "price_source": "cost_basis",
                }
            ],
        )
        second_id = self.store.insert_snapshot(
            second,
            "TEST_ACCOUNT",
            contract_positions=[
                {
                    "account": "TEST_ACCOUNT",
                    "symbol": "ACB",
                    "security_type": "STK",
                    "con_id": 3001,
                    "quantity": 10,
                    "market_price": 110.0,
                    "market_value": 1100.0,
                    "average_cost": 110.0,
                    "price_source": "cost_basis",
                }
            ],
        )

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=first_id,
            end_id=second_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "ACB")
        self.assertAlmostEqual(row["start_value"], 0.0)
        self.assertAlmostEqual(row["end_value"], 0.0)
        self.assertAlmostEqual(row["delta_value"], 0.0)

    def test_symbol_comparison_replaces_option_greeks_stock_marks(self):
        bad_start = {
            "as_of": 1700000500.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "GOOGX",
                    "stock_count": 10.0,
                    "underlying_market_price": 356.0,
                    "underlying_cost_basis": 329.0,
                    "underlying_price_source": "option_greeks",
                    "stock_value": 3560.0,
                    "option_actual_value": 5.0,
                    "option_notional_shares": 1.0,
                    "option_notional_value": 356.0,
                    "npv": 3916.0,
                }
            ],
        }
        reliable_midday = {
            "as_of": 1700000600.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "GOOGX",
                    "stock_count": 10.0,
                    "underlying_market_price": 385.0,
                    "underlying_cost_basis": 329.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 3850.0,
                    "option_actual_value": 5.0,
                    "option_notional_shares": 1.0,
                    "option_notional_value": 385.0,
                    "npv": 4235.0,
                }
            ],
        }
        reliable_end = {
            "as_of": 1700000700.0,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "GOOGX",
                    "stock_count": 10.0,
                    "underlying_market_price": 386.0,
                    "underlying_cost_basis": 329.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 3860.0,
                    "option_actual_value": 5.0,
                    "option_notional_shares": 1.0,
                    "option_notional_value": 386.0,
                    "npv": 4246.0,
                }
            ],
        }

        start_id = self.store.insert_snapshot(bad_start, "TEST_ACCOUNT")
        self.store.insert_snapshot(reliable_midday, "TEST_ACCOUNT")
        end_id = self.store.insert_snapshot(reliable_end, "TEST_ACCOUNT")

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=start_id,
            end_id=end_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "GOOGX")
        self.assertAlmostEqual(row["start_price"], 385.0)
        self.assertAlmostEqual(row["start_value"], 3855.0)
        self.assertAlmostEqual(row["end_value"], 3865.0)
        self.assertAlmostEqual(row["price_delta"], 1.0)
        self.assertAlmostEqual(row["delta_value"], 10.0)

    def test_symbol_comparison_floors_expiring_option_at_intrinsic_value(self):
        start_ts = datetime(
            2026,
            5,
            6,
            13,
            0,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        peak_ts = datetime(
            2026,
            5,
            6,
            13,
            25,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()
        start = {
            "as_of": start_ts,
            "metrics": {"net_liquidation": 1000.0},
            "positions": [
                {
                    "symbol": "NVDA",
                    "stock_count": 0.0,
                    "underlying_market_price": 207.40,
                    "underlying_cost_basis": 0.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 0.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 0.0,
                }
            ],
        }
        peak = {
            "as_of": peak_ts,
            "metrics": {"net_liquidation": 2080.0},
            "positions": [
                {
                    "symbol": "NVDA",
                    "stock_count": 0.0,
                    "underlying_market_price": 208.70,
                    "underlying_cost_basis": 0.0,
                    "underlying_price_source": "snapshot",
                    "stock_value": 0.0,
                    "option_actual_value": 0.0,
                    "option_notional_value": 0.0,
                    "npv": 0.0,
                }
            ],
        }
        option_contract = {
            "account": "TEST_ACCOUNT",
            "symbol": "NVDA",
            "local_symbol": "NVDA 260506C00207500",
            "security_type": "OPT",
            "con_id": 9001,
            "expiry": "20260506",
            "strike": 207.5,
            "right": "C",
            "quantity": 9,
            "market_price": 0.0,
            "market_value": 0.0,
            "multiplier": 100,
            "price_source": "unavailable",
        }

        start_id = self.store.insert_snapshot(
            start,
            "TEST_ACCOUNT",
            contract_positions=[option_contract],
        )
        peak_id = self.store.insert_snapshot(
            peak,
            "TEST_ACCOUNT",
            contract_positions=[option_contract],
        )

        comparison = self.store.compare_positions(
            "TEST_ACCOUNT",
            start_id=start_id,
            end_id=peak_id,
            basis="market_value",
        )

        row = next(row for row in comparison["rows"] if row["symbol"] == "NVDA")
        self.assertAlmostEqual(row["start_value"], 0.0)
        self.assertAlmostEqual(row["end_value"], 1080.0)
        self.assertAlmostEqual(row["delta_value"], 1080.0)
        self.assertAlmostEqual(row["option_actual_delta_value"], 1080.0)


if __name__ == "__main__":
    unittest.main()
