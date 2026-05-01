import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


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
        self.assertEqual(
            [row["symbol"] for row in comparison["rows"]],
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


if __name__ == "__main__":
    unittest.main()
