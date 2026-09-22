import unittest
from pathlib import Path

from src.catalog import Catalog
from src.directory import Directory
from src.ledger import Ledger
from src.validation import Validator

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load(FIXTURES / "categories.json")
        cls.directory = Directory.load(FIXTURES / "platforms.json")
        cls.ledger = Ledger.load(FIXTURES / "submissions.json")
        cls.validator = Validator(
            cls.catalog,
            covered={pid: cls.directory.covered_categories(pid)
                     for pid in cls.directory.platforms},
        )
        cls.checks = {c.batch.batch_id: c for c in cls.validator.validate_all(cls.ledger)}

    def _codes(self, batch_id):
        return {f.code for f in self.checks[batch_id].findings}

    def test_late_submission_flagged(self):
        self.assertIn("LATE_SUBMISSION", self._codes("PL-MALLCO-20260720-LATE"))

    def test_withdrawal_carries_no_observations(self):
        wd = self.checks["PL-MALLCO-20260822-WD"]
        self.assertFalse(any(f.code == "WD_WITH_OBS" for f in wd.findings))

    def test_golive_api_break_only_for_eyewear(self):
        glasses = self.validator.comparability_breakpoints(self.ledger, "P-GOLIVE", "A0103")
        self.assertTrue(any(f.code == "API_CALIBER_BREAK" for f in glasses))
        camera = self.validator.comparability_breakpoints(self.ledger, "P-GOLIVE", "A0311")
        self.assertFalse(any(f.code == "API_CALIBER_BREAK" for f in camera))

    def test_mallco_dedup_drift_detected_once_at_backfill(self):
        ecg = self.validator.comparability_breakpoints(self.ledger, "P-MALLCO", "A0207")
        drift_batches = [f.batch_id for f in ecg if f.code == "METHODOLOGY_DRIFT"]
        self.assertEqual(drift_batches, ["PL-MALLCO-20260828-BF"])

    def test_missing_methodology_is_blocker(self):
        from src.ledger import Batch
        broken = Batch(
            "BX", "P-AIWEAR", "2026-09", "regular", "2026-10-15", None, "缺要素",
            {"sample_coverage": "", "category_dictionary": "x", "category_mappings": [],
             "dedup_method": "x", "refund_treatment": "x", "price_caliber": "x",
             "quality_statement": "x"},
            tuple(),
            {"declared_by": "a", "declared_at": "2026-10-15", "statement": "ok"},
        )
        check = self.validator.validate_batch(self.ledger, broken)
        self.assertTrue(check.has_blocker)
        self.assertIn("METHODOLOGY_MISSING", {f.code for f in check.findings})
        self.assertIn("MAPPING_TABLE_EMPTY", {f.code for f in check.findings})

    def test_unmapped_observation_is_blocker(self):
        from src.ledger import Batch, Observation
        method = {
            "sample_coverage": "约100%店铺", "category_dictionary": "d",
            "category_mappings": [{"platform_path": "未映射类目", "standard_code": "000000"}],
            "dedup_method": "按订单号去重", "refund_treatment": "当期冲减",
            "price_caliber": "含税含运费", "quality_statement": "ok",
        }
        obs = Observation("A0103", "未映射类目", 100, 10, 1, "2026-09-01", "2026-09-30")
        batch = Batch("BU", "P-AIWEAR", "2026-09", "regular", "2026-10-15", None,
                      "x", method, (obs,),
                      {"declared_by": "a", "declared_at": "2026-10-15", "statement": "ok"})
        check = self.validator.validate_batch(self.ledger, batch)
        codes = {f.code for f in check.findings}
        self.assertIn("UNMAPPED_CATEGORY", codes)
        self.assertIn("OBS_WITHOUT_MAPPING", codes)
        self.assertTrue(check.has_blocker)


if __name__ == "__main__":
    unittest.main()
