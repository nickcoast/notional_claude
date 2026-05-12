import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import (  # noqa: E402
    option_after_exercise_cutoff,
    option_intrinsic_floor_value,
    option_intrinsic_value,
)


class PortfolioOptionValueTest(unittest.TestCase):
    def test_expiring_call_has_intrinsic_floor_before_exercise_cutoff(self):
        contract = SimpleNamespace(
            lastTradeDateOrContractMonth="20260506",
            strike=207.5,
            right="C",
        )
        now_ts = datetime(
            2026,
            5,
            6,
            13,
            25,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()

        floor_value = option_intrinsic_floor_value(
            contract,
            underlying_price=208.70,
            quantity=9,
            multiplier=100,
            now_ts=now_ts,
        )

        self.assertAlmostEqual(floor_value, 1080.0)

    def test_expiring_option_has_no_intrinsic_floor_after_exercise_cutoff(self):
        contract = SimpleNamespace(
            lastTradeDateOrContractMonth="20260506",
            strike=207.5,
            right="C",
        )
        now_ts = datetime(
            2026,
            5,
            6,
            14,
            25,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()

        floor_value = option_intrinsic_floor_value(
            contract,
            underlying_price=208.70,
            quantity=9,
            multiplier=100,
            now_ts=now_ts,
        )

        self.assertIsNone(floor_value)

    def test_expired_otm_put_has_zero_intrinsic_value_after_cutoff(self):
        contract = SimpleNamespace(
            lastTradeDateOrContractMonth="20260508",
            strike=703.0,
            right="P",
        )
        now_ts = datetime(
            2026,
            5,
            8,
            16,
            7,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        ).timestamp()

        self.assertTrue(option_after_exercise_cutoff(contract, now_ts))
        self.assertEqual(
            option_intrinsic_value(
                contract,
                underlying_price=711.70,
                quantity=10,
                multiplier=100,
            ),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
