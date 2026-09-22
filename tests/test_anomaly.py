import unittest
from pathlib import Path

from src.anomaly import AnomalyDetector
from src.service import QualityService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class AnomalyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = QualityService.from_fixtures(FIXTURES, today="2026-09-22")

    def test_only_two_true_spikes(self):
        keys = {(a.platform_id, a.category_code, a.report_period)
                for a in self.service.anomalies}
        self.assertEqual(
            keys,
            {("P-AIWEAR", "A0103", "2026-09"),
             ("P-GOLIVE", "A0311", "2026-09")},
        )

    def test_api_break_batch_not_treated_as_spike(self):
        # GOLIVE 智能眼镜 2026-08 环比 +19.3% 但属于口径断点，不报波动异常
        self.assertNotIn(
            ("P-GOLIVE", "A0103", "2026-08"),
            {(a.platform_id, a.category_code, a.report_period)
             for a in self.service.anomalies},
        )

    def test_smooth_growth_not_flagged(self):
        # 各平台 3%~4% 的平滑环比增长不应误报
        self.assertFalse(any(
            a.report_period in ("2026-07", "2026-08")
            for a in self.service.anomalies
        ))

    def test_spike_is_quarantined_in_rating(self):
        rating = self.service.ratings[("P-AIWEAR", "A0103", "2026-09")]
        self.assertTrue(rating.quarantined)
        self.assertEqual(rating.grade, "C")


if __name__ == "__main__":
    unittest.main()
