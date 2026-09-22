import unittest
from pathlib import Path

from src.service import QualityService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class QualityScoringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = QualityService.from_fixtures(FIXTURES, today="2026-09-22")

    def test_stable_source_grade_a(self):
        rating = self.service.ratings[("P-AIWEAR", "A0103", "2026-08")]
        self.assertEqual(rating.grade, "A")
        self.assertEqual(rating.total, 100)
        self.assertFalse(rating.blocked)

    def test_mallco_broad_mapping_and_partial_coverage(self):
        rating = self.service.ratings[("P-MALLCO", "A0207", "2026-08")]
        # 覆盖 2/3 类目(-15)、宽口径(-30)、断点未回溯(-15)集中在字典/覆盖维度，
        # 综合分为B但达不到A；其“仅内部参考”由审批条件与口径披露把关
        self.assertLess(rating.scores["dictionary"], 60)
        self.assertLess(rating.scores["coverage"], 100)
        self.assertEqual(rating.grade, "B")
        self.assertLess(rating.total, 90)
        self.assertTrue(any("MAPPING_OVER_BROAD" in d for d in rating.deductions))

    def test_golive_eyewear_after_api_break_carries_history_penalty(self):
        rating = self.service.ratings[("P-GOLIVE", "A0103", "2026-09")]
        self.assertTrue(any("HISTORICAL_BREAK" in d for d in rating.deductions))

    def test_golive_other_categories_not_penalized_for_eyewear_break(self):
        rating = self.service.ratings[("P-GOLIVE", "A0311", "2026-09")]
        self.assertFalse(any("HISTORICAL_BREAK" in d for d in rating.deductions))

    def test_resolving_anomaly_lifts_quarantine(self):
        ticket = self.service.tickets.open_ticket_for(
            "PL-AIWEAR-20260915-R", "A0103", "anomaly"
        )
        self.assertIsNotNone(ticket)
        self.service.close_ticket(
            ticket.ticket_id, actor="stat_chen", role="statistician",
            note="经平台凭证核实为新品首发拉动，增长真实", resolution_kind="confirmed_real",
            today="2026-09-22",
        )
        rating = self.service.ratings[("P-AIWEAR", "A0103", "2026-09")]
        self.assertFalse(rating.quarantined)
        # 仍有“待核实”声明与迟报等小扣分项的情况下，等级应高于隔离上限 C
        self.assertIn(rating.grade, ("A", "B"))


if __name__ == "__main__":
    unittest.main()
