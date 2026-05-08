import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import IBPollingService, _expiry_date, _normalized_order_fill  # noqa: E402


class OrderFillNormalizationTest(unittest.TestCase):
    def test_expiry_date_parses_ib_option_expiry(self):
        self.assertEqual(_expiry_date("20260508"), date(2026, 5, 8))
        self.assertEqual(_expiry_date("NVDA 20260508 C"), date(2026, 5, 8))
        self.assertIsNone(_expiry_date(""))

    def test_executions_override_stale_submitted_status(self):
        status = SimpleNamespace(status="Submitted", filled=0.0, remaining=25.0)

        fill = _normalized_order_fill(
            total_quantity=25.0,
            status=status,
            execution_filled=25.0,
        )

        self.assertEqual(fill["status"], "Filled")
        self.assertTrue(fill["is_filled"])
        self.assertEqual(fill["filled"], 25.0)
        self.assertEqual(fill["remaining"], 0.0)

    def test_partial_execution_keeps_order_open(self):
        status = SimpleNamespace(status="Submitted", filled=0.0, remaining=25.0)

        fill = _normalized_order_fill(
            total_quantity=25.0,
            status=status,
            execution_filled=10.0,
        )

        self.assertEqual(fill["status"], "Submitted")
        self.assertFalse(fill["is_filled"])
        self.assertEqual(fill["filled"], 10.0)
        self.assertEqual(fill["remaining"], 15.0)

    def test_execution_today_filter_accepts_current_day_only(self):
        today = date.today().strftime("%Y%m%d")

        self.assertTrue(
            IBPollingService._execution_is_today({"time": f"{today} 12:00:00"})
        )
        self.assertFalse(
            IBPollingService._execution_is_today({"time": "20000101 12:00:00"})
        )


if __name__ == "__main__":
    unittest.main()
