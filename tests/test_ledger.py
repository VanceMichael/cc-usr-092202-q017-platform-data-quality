import unittest
from pathlib import Path

from src.ledger import Ledger, deadline_for_period

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger.load(FIXTURES / "submissions.json")

    def test_deadline_rule(self):
        self.assertEqual(deadline_for_period("2026-06").isoformat(), "2026-07-15")
        self.assertEqual(deadline_for_period("2026-12").isoformat(), "2027-01-15")

    def test_withdrawn_and_backfilled_excluded(self):
        withdrawn = self.ledger.withdrawn_ids()
        self.assertIn("PL-MALLCO-20260814-R", withdrawn)
        backfilled = self.ledger.backfilled_ids()
        self.assertIn("PL-MALLCO-20260814-R", backfilled)
        effective_ids = [b.batch_id for b in self.ledger.effective_batches("P-MALLCO")]
        self.assertNotIn("PL-MALLCO-20260814-R", effective_ids)
        # 2026-07 的有效批次是回补批 PL-MALLCO-20260828-BF
        july = self.ledger.effective_batch("P-MALLCO", "2026-07")
        self.assertEqual(july.batch_id, "PL-MALLCO-20260828-BF")
        # 撤回批无数据，不会出现在有效序列中
        wd = next(b for b in self.ledger.batches if b.event_type == "withdrawal")
        self.assertFalse(wd.is_data_batch)

    def test_late_batch_recorded_as_new_batch(self):
        late = [b for b in self.ledger.for_platform("P-MALLCO") if b.event_type == "late"]
        self.assertEqual(len(late), 1)
        self.assertTrue(self.ledger.is_late(late[0]))

    def test_api_change_is_data_batch(self):
        api = next(b for b in self.ledger.batches if b.event_type == "api_change")
        self.assertTrue(api.is_data_batch)
        self.assertEqual(api.report_period, "2026-08")

    def test_invalid_supersedes_chain_rejected(self):
        from src.ledger import Batch, Observation
        bad = Ledger(
            version=1,
            batches=[
                Batch("B1", "P-AIWEAR", "2026-01", "regular", "2026-02-15",
                      None, "x", {}, tuple(), {"statement": "ok"}),
                Batch("B2", "P-GOLIVE", "2026-01", "backfill", "2026-02-20",
                      "B1", "跨平台替代", {}, tuple(), {"statement": "ok"}),
            ],
        )
        with self.assertRaises(ValueError):
            bad.validate_structure()


if __name__ == "__main__":
    unittest.main()
