import copy
import json
import unittest
from pathlib import Path

from src.dataset import Dataset, load_dataset
from src.model import (
    BatchKind,
    Confidence,
    Grade,
    Role,
    TicketStatus,
    UsageScope,
)
from src.service import AccessDenied, QualityService

FIXTURE = Path("fixtures/sample_dataset.json")


class FixtureDataTest(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(FIXTURE)

    def test_classification_table_contains_target_categories(self):
        names = {c.name for c in self.ds.categories.values()}
        self.assertIn("智能眼镜", names)
        self.assertIn("心电监护仪", names)
        self.assertIn("运动相机", names)

    def test_withdrawal_must_not_carry_methodology(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        wd = next(b for b in data["batches"] if b["kind"] == "withdrawal")
        wd["methodology"] = {"coverage_note": "x"}
        with self.assertRaises(ValueError):
            Dataset.from_dict(data)

    def test_unknown_category_reference_rejected(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for b in data["batches"]:
            if b.get("observations"):
                b["observations"][0]["category"] = "G-NOT-EXIST"
                break
        with self.assertRaises(ValueError):
            Dataset.from_dict(data)


class BatchReplayTest(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(FIXTURE)

    def test_api_revision_becomes_effective_without_deleting_old(self):
        eff = self.ds.effective_batch("P-XINGLIAN", "2026-08")
        self.assertEqual(eff.batch_id, "B-XL-202608-REV")
        self.assertEqual(eff.kind, BatchKind.API_REVISION)
        self.assertEqual(eff.supersedes, "B-XL-202608-OLD")
        # 旧批次仍然可追溯
        self.assertIn("B-XL-202608-OLD", {b.batch_id for b in self.ds.batches})
        self.assertEqual(
            self.ds.effective_batch("P-XINGLIAN", "2026-08").supersedes,
            "B-XL-202608-OLD",
        )

    def test_withdrawal_leaves_no_effective_batch(self):
        self.assertIsNone(self.ds.effective_batch("P-CHAODIAN", "2026-07"))
        self.assertIn("B-CD-202607", self.ds.withdrawn_batch_ids())

    def test_backfill_is_new_batch_not_rewrite(self):
        eff = self.ds.effective_batch("P-XINGLIAN", "2026-05")
        self.assertEqual(eff.batch_id, "B-XL-202605-BF")
        self.assertEqual(eff.kind, BatchKind.BACKFILL)

    def test_late_batch_is_replayed_as_normal_current(self):
        eff = self.ds.effective_batch("P-XINGLIAN", "2026-07")
        self.assertEqual(eff.kind, BatchKind.LATE)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(FIXTURE)
        self.svc = QualityService(self.ds)

    def finding_codes(self, batch_id):
        return [f.code for f in self.svc.reports[batch_id].findings]

    def test_unmapped_platform_category_quarantined(self):
        # 星链6月观测用“AI眼镜”，但6月字典中只有“智能眼镜”
        report = self.svc.reports["B-XL-202606"]
        self.assertIn("XL-0601", report.quarantined_obs)
        self.assertIn("CATEGORY_UNMAPPED", self.finding_codes("B-XL-202606"))

    def test_missing_refund_handling_blocks_superseded_batch(self):
        report = self.svc.reports["B-XL-202608-OLD"]
        self.assertTrue(report.blocked)
        self.assertIn("METHODOLOGY_MISSING", self.finding_codes("B-XL-202608-OLD"))

    def test_low_coverage_blocks_aggregate_use(self):
        report = self.svc.reports["B-CD-202606"]
        self.assertTrue(report.blocked)
        self.assertIn("COVERAGE_TOO_LOW", self.finding_codes("B-CD-202606"))

    def test_late_submission_flagged_only_for_routine_batches(self):
        self.assertIn("LATE_SUBMISSION", self.finding_codes("B-XL-202607"))
        # 回补虽晚于 due_at，不判迟报
        self.assertNotIn("LATE_SUBMISSION", self.finding_codes("B-XL-202605-BF"))

    def test_calibre_changes_detected(self):
        codes = self.finding_codes("B-XL-202607")
        # 字典版本 dict-v1 -> dict-v3、促销口径变化、新增 AI眼镜 映射
        self.assertIn("CALIBRE_CHANGED", codes)
        self.assertIn("CATEGORY_MAP_CHANGED", codes)

    def test_anomalous_growth_goes_to_quarantine_not_publication(self):
        # 云帆智能眼镜 35% 与 星链心电监护仪 34% 均触发异常
        self.assertIn("YF-0801", self.svc.reports["B-YF-202608"].quarantined_obs)
        self.assertIn("XL-0802", self.svc.reports["B-XL-202608-REV"].quarantined_obs)
        for bid, obs in [("B-YF-202608", "YF-0801"), ("B-XL-202608-REV", "XL-0802")]:
            codes = self.finding_codes(bid)
            self.assertIn("ANOMALY_HISTORICAL", codes)
            self.assertIn("ANOMALY_CROSS_SOURCE", codes)

    def test_stable_batch_has_no_findings(self):
        self.assertEqual(self.finding_codes("B-YF-202607"), [])


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.svc = QualityService(load_dataset(FIXTURE))

    def score(self, batch_id):
        return self.svc.scores[batch_id]

    def test_clean_batch_scores_grade_a(self):
        s = self.score("B-YF-202607")
        self.assertEqual(s.score, 100.0)
        self.assertEqual(s.grade, Grade.A)

    def test_batch_level_blocker_caps_grade_d(self):
        s = self.score("B-CD-202606")
        self.assertLessEqual(s.score, 49.0)
        self.assertEqual(s.grade, Grade.D)

    def test_late_and_calibre_change_reduce_score(self):
        s = self.score("B-XL-202607")
        self.assertLess(s.components["timeliness"], 1.0)
        self.assertLess(s.components["stability"], 1.0)
        # 迟报+口径变更后仍因其余维度稳健达到 A
        self.assertGreaterEqual(s.score, 85.0)

    def test_anomaly_reduces_score_until_cleared(self):
        before = self.score("B-YF-202608")
        self.assertLess(before.components["anomaly"], 1.0)
        self.svc.confirm_quarantine(
            "B-YF-202608", "YF-0801", "审查员甲",
            "新品促销活动导致真实高增长，平台提供活动台账", True,
        )
        after = self.score("B-YF-202608")
        self.assertEqual(after.components["anomaly"], 1.0)
        self.assertEqual(after.grade, Grade.A)

    def test_confirmed_error_keeps_quarantine(self):
        res = self.svc.confirm_quarantine(
            "B-XL-202608-REV", "XL-0802", "审查员甲",
            "接口重算错误，待平台更正", False,
        )
        self.assertFalse(res["cleared"])
        self.assertIn("XL-0802", self.svc.reports["B-XL-202608-REV"].quarantined_obs)


class TicketTest(unittest.TestCase):
    def setUp(self):
        self.svc = QualityService(load_dataset(FIXTURE))

    def test_tickets_auto_created_for_warnings_and_errors(self):
        codes = {t.code for t in self.svc.tickets}
        self.assertIn("LATE_SUBMISSION", codes)
        self.assertIn("ANOMALY_HISTORICAL", codes)
        self.assertIn("CATEGORY_UNMAPPED", codes)

    def test_superseded_and_withdrawn_tickets_voided(self):
        status = {t.batch_id: t.status for t in self.svc.tickets}
        for t in self.svc.tickets:
            if t.batch_id == "B-XL-202608-OLD":
                self.assertEqual(t.status, TicketStatus.VOIDED_SUPERSEDED)
            if t.batch_id == "B-CD-202607":
                self.assertEqual(t.status, TicketStatus.VOIDED_WITHDRAWN)

    def test_resolve_ticket_records_review(self):
        t = next(t for t in self.svc.tickets if t.code == "LATE_SUBMISSION")
        self.svc.resolve_ticket(t.id, "迟报情况已知悉并备案", "审查员甲")
        self.assertEqual(self.svc._require_ticket(t.id).status, TicketStatus.RESOLVED)
        self.assertEqual(self.svc._require_ticket(t.id).resolution["by"], "审查员甲")


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = QualityService(load_dataset(FIXTURE))

    def test_platform_sees_only_own_tickets(self):
        tickets = self.svc.visible_tickets(Role.PLATFORM, "P-YUNFAN")
        self.assertTrue(tickets)
        self.assertTrue(all(t.platform_id == "P-YUNFAN" for t in tickets))

    def test_platform_identity_required(self):
        with self.assertRaises(AccessDenied):
            self.svc.visible_tickets(Role.PLATFORM)

    def test_internal_roles_see_all_tickets(self):
        all_ids = {t.id for t in self.svc.tickets}
        for role in (Role.STATISTICIAN, Role.REVIEWER, Role.PUBLISHER):
            self.assertEqual({t.id for t in self.svc.visible_tickets(role)}, all_ids)

    def test_source_comparison_statistician_only(self):
        with self.assertRaises(AccessDenied):
            self.svc.compare_sources(Role.PLATFORM, "2026-08")
        rows = self.svc.compare_sources(Role.STATISTICIAN, "2026-08", "G-SMART-GLASSES")
        self.assertEqual(len(rows), 3)
        yunfan = next(r for r in rows if r.platform_id == "P-YUNFAN")
        # 云帆 35% 显著高于三家平台中位数
        self.assertGreaterEqual(abs(yunfan.deviation), 0.20)
        self.assertTrue(yunfan.quarantined)

    def test_platform_sees_only_own_batches(self):
        batches = self.svc.visible_batches(Role.PLATFORM, "P-CHAODIAN")
        self.assertTrue(all(b.platform_id == "P-CHAODIAN" for b in batches))


class ApprovalAndLineageTest(unittest.TestCase):
    def setUp(self):
        self.svc = QualityService(load_dataset(FIXTURE))

    def test_aggregate_blocked_while_anomaly_open(self):
        d = self.svc.request_approval(
            "P-XINGLIAN", "2026-08", UsageScope.AGGREGATE_TREND,
            "审查员甲", Role.REVIEWER, "2026-09-15",
        )
        self.assertFalse(d.approved)
        self.assertTrue(any("阻断" in r for r in d.reasons))

    def test_public_release_flow_and_lineage(self):
        self.svc.confirm_quarantine(
            "B-YF-202608", "YF-0801", "审查员甲",
            "新品上市促销真实拉动", True,
        )
        d = self.svc.request_approval(
            "P-YUNFAN", "2026-08", UsageScope.PUBLIC_RELEASE,
            "发布员乙", Role.PUBLISHER, "2026-09-16",
        )
        self.assertTrue(d.approved, d.reasons)

        lin = self.svc.lineage("P-YUNFAN", "2026-08")
        self.assertTrue(lin.usable)
        self.assertEqual(lin.confidence, Confidence.HIGH.value)
        self.assertEqual(lin.approved_scope, UsageScope.PUBLIC_RELEASE.value)
        self.assertEqual(lin.batch_id, "B-YF-202608")
        self.assertIn("去重方法", lin.calibre)
        self.assertEqual(lin.dictionary_version, "dict-v3")
        self.assertTrue(lin.quality_statement)
        self.assertEqual(lin.open_tickets, 0)

    def test_wrong_approver_role_rejected(self):
        d = self.svc.request_approval(
            "P-YUNFAN", "2026-07", UsageScope.AGGREGATE_TREND,
            "发布员乙", Role.PUBLISHER, "2026-09-16",
        )
        self.assertFalse(d.approved)
        self.assertTrue(any("须由" in r for r in d.reasons))

    def test_withdrawn_period_lineage_unusable(self):
        lin = self.svc.lineage("P-CHAODIAN", "2026-07")
        self.assertFalse(lin.usable)
        self.assertEqual(lin.confidence, Confidence.UNUSABLE.value)

    def test_low_coverage_cannot_enter_aggregate(self):
        d = self.svc.request_approval(
            "P-CHAODIAN", "2026-06", UsageScope.AGGREGATE_TREND,
            "审查员甲", Role.REVIEWER, "2026-07-15",
        )
        self.assertFalse(d.approved)

    def test_approval_bound_to_batch_is_invalidated_by_new_batch(self):
        # 潮电8月干净批次先获批内部参考
        d = self.svc.request_approval(
            "P-CHAODIAN", "2026-08", UsageScope.INTERNAL_REFERENCE,
            "审查员甲", Role.REVIEWER, "2026-09-15",
        )
        self.assertTrue(d.approved)

        # 之后出现改版重报新批次
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        old = next(b for b in data["batches"]
                   if b["batch_id"] == "B-CD-202608")
        rev = copy.deepcopy(old)
        rev.update({
            "batch_id": "B-CD-202608-REV",
            "kind": "api_revision",
            "received_at": "2026-09-20",
            "supersedes": "B-CD-202608",
            "note": "接口改版后重报",
        })
        data["batches"].append(rev)
        ds2 = Dataset.from_dict(data)

        active = self.svc.approvals.active_for(ds2, "P-CHAODIAN", "2026-08")
        self.assertEqual(active, [])  # 旧审批锚定旧批次，自动失效

    def test_public_observations_exclude_unapproved_and_quarantined(self):
        # 审批前：没有任何数据可公开
        self.assertEqual(self.svc.public_observations("2026-08"), [])
        # 云帆完成核查与公开发布审批
        self.svc.confirm_quarantine(
            "B-YF-202608", "YF-0801", "审查员甲", "真实增长", True)
        self.svc.request_approval(
            "P-YUNFAN", "2026-08", UsageScope.PUBLIC_RELEASE,
            "发布员乙", Role.PUBLISHER, "2026-09-16",
        )
        pub = self.svc.public_observations("2026-08")
        self.assertEqual({o["platform_id"] for o in pub}, {"P-YUNFAN"})
        self.assertEqual(len(pub), 6)
        # 每条公开观测都可回溯到批次与字典版本
        self.assertTrue(all(o["batch_id"] == "B-YF-202608" for o in pub))
        self.assertTrue(all(o["dictionary_version"] == "dict-v3" for o in pub))


class ServiceSummaryTest(unittest.TestCase):
    def test_summary_reports_batch_lifecycle(self):
        svc = QualityService(load_dataset(FIXTURE))
        summary = svc.summary()
        self.assertEqual(summary["platforms"], 3)
        self.assertIn("B-XL-202608-OLD", summary["batches_superseded"])
        self.assertIn("B-CD-202607", summary["batches_withdrawn"])
        self.assertGreater(summary["tickets"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
