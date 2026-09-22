import unittest
from pathlib import Path

from src.approvals import ApprovalRegistry, maximum_supported_scope
from src.service import QualityService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ApprovalGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = QualityService.from_fixtures(FIXTURES, today="2026-09-22")

    def test_stable_source_adoptable_for_public(self):
        report = self.service.lineage("P-AIWEAR", "A0103", "2026-08",
                                      required_scope="public_statistic")
        self.assertTrue(report.adoptable)
        self.assertEqual(report.supported_scope, "public_statistic")

    def test_quarantined_spike_not_adoptable(self):
        report = self.service.lineage("P-AIWEAR", "A0103", "2026-09",
                                      required_scope="public_statistic")
        self.assertFalse(report.adoptable)
        self.assertEqual(report.supported_scope, "internal_reference")
        self.assertTrue(any("隔离" in b for b in report.blockers))

    def test_api_break_limited_to_internal(self):
        public = self.service.lineage("P-GOLIVE", "A0103", "2026-08",
                                      required_scope="public_statistic")
        self.assertFalse(public.adoptable)
        internal = self.service.lineage("P-GOLIVE", "A0103", "2026-08",
                                        required_scope="internal_reference")
        self.assertTrue(internal.adoptable)

    def test_mallco_ecg_internal_only(self):
        trend = self.service.lineage("P-MALLCO", "A0207", "2026-08",
                                     required_scope="trend_comparison")
        self.assertFalse(trend.adoptable)
        internal = self.service.lineage("P-MALLCO", "A0207", "2026-08",
                                        required_scope="internal_reference")
        self.assertTrue(internal.adoptable)

    def test_reviewer_cannot_approve_above_supported_scope(self):
        approvals = ApprovalRegistry.load(FIXTURES / "approvals.json")
        pending = approvals.request("P-GOLIVE", "A0103", "public_statistic",
                                    requested_by="stat_chen", today="2026-09-22")
        with self.assertRaises(PermissionError):
            approvals.decide(
                pending.approval_id, approver_role="caliber_reviewer",
                approver="review_li", approve=True, rationale="试图超范围批准",
                max_supported_scope="internal_reference",
            )

    def test_max_scope_rules(self):
        self.assertEqual(
            maximum_supported_scope(grade="A", blocked=False, quarantined=False,
                                    has_open_critical=False, has_open_major=False,
                                    has_breakpoint=False),
            "public_statistic",
        )
        self.assertEqual(
            maximum_supported_scope(grade="B", blocked=False, quarantined=True,
                                    has_open_critical=False, has_open_major=False,
                                    has_breakpoint=False),
            "internal_reference",
        )
        self.assertIsNone(
            maximum_supported_scope(grade="D", blocked=True, quarantined=False,
                                    has_open_critical=False, has_open_major=False,
                                    has_breakpoint=False),
        )

    def test_platform_cannot_read_other_platform_lineage(self):
        with self.assertRaises(PermissionError):
            self.service.lineage("P-AIWEAR", "A0103", "2026-08",
                                 role="platform", acting_platform_id="P-GOLIVE")
        own = self.service.lineage("P-AIWEAR", "A0103", "2026-08",
                                   role="platform", acting_platform_id="P-AIWEAR")
        self.assertTrue(own.adoptable)


class LineageContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = QualityService.from_fixtures(FIXTURES, today="2026-09-22")

    def test_backfill_chain_shows_withdrawal_and_backfill(self):
        report = self.service.lineage("P-MALLCO", "A0207", "2026-07")
        events = {(item["batch_id"], item["event_type"]) for item in report.batch_chain}
        self.assertIn(("PL-MALLCO-20260814-R", "regular"), events)
        self.assertIn(("PL-MALLCO-20260822-WD", "withdrawal"), events)
        self.assertIn(("PL-MALLCO-20260828-BF", "backfill"), events)
        self.assertEqual(report.effective_batch.batch_id, "PL-MALLCO-20260828-BF")

    def test_methodology_snapshot_has_six_elements(self):
        report = self.service.lineage("P-AIWEAR", "A0103", "2026-08")
        self.assertEqual(
            set(report.methodology_snapshot),
            {"样本覆盖", "类目字典", "去重方法", "退款处理", "价格口径", "质量声明"},
        )

    def test_confidence_statement_explains_adoption(self):
        report = self.service.lineage("P-AIWEAR", "A0103", "2026-08")
        self.assertIn("综合质量评分", report.confidence_statement)
        self.assertIn("可用于公开统计", report.confidence_statement)
        blocked = self.service.lineage("P-GOLIVE", "A0103", "2026-08")
        self.assertIn("暂不可采用", blocked.confidence_statement)

    def test_compare_sources_lists_differences(self):
        comparison = self.service.compare("A0311", "2026-09")
        self.assertEqual(len(comparison["sources"]), 3)
        mallco = next(s for s in comparison["sources"] if s["platform_id"] == "P-MALLCO")
        self.assertTrue(mallco["covers_category"])
        aiwear = next(s for s in comparison["sources"] if s["platform_id"] == "P-AIWEAR")
        self.assertEqual(aiwear["batch_event"], "regular")
        self.assertTrue(comparison["caliber_differences"])
        self.assertIsNotNone(comparison["value_spread"])

    def test_compare_marks_uncovered_category(self):
        comparison = self.service.compare("A0103", "2026-08")
        mallco = next(s for s in comparison["sources"] if s["platform_id"] == "P-MALLCO")
        self.assertFalse(mallco["covers_category"])
        self.assertIsNone(mallco["sales"])


if __name__ == "__main__":
    unittest.main()
