import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
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
