import unittest
from pathlib import Path

from src.service import QualityService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class TicketVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = QualityService.from_fixtures(FIXTURES, today="2026-09-22")

    def test_platform_sees_only_own_tickets(self):
        aiwear = self.service.tickets_for(role="platform", platform_id="P-AIWEAR")
        self.assertTrue(aiwear)
        self.assertTrue(all(t.platform_id == "P-AIWEAR" for t in aiwear))
        mallco = self.service.tickets_for(role="platform", platform_id="P-MALLCO")
        self.assertFalse(any(t.platform_id == "P-AIWEAR" for t in mallco))

    def test_cross_platform_access_denied(self):
        aiwear = self.service.tickets_for(role="platform", platform_id="P-AIWEAR")
        ticket = next(t for t in aiwear if t.platform_id == "P-AIWEAR")
        with self.assertRaises(PermissionError):
            self.service.tickets.get(ticket.ticket_id, role="platform",
                                     platform_id="P-GOLIVE")

    def test_statistician_sees_all_sources(self):
        all_tickets = self.service.tickets_for(role="statistician")
        owners = {t.platform_id for t in all_tickets}
        self.assertEqual(owners, {"P-AIWEAR", "P-GOLIVE", "P-MALLCO"})

    def test_platform_cannot_resolve_ticket(self):
        ticket = self.service.tickets_for(role="platform", platform_id="P-AIWEAR")[0]
        with self.assertRaises(PermissionError):
            self.service.close_ticket(
                ticket.ticket_id, actor="u_aiwear", role="platform",
                platform_id="P-AIWEAR", note="自行关闭",
            )

    def test_platform_can_investigate_own_ticket(self):
        ticket = self.service.tickets_for(role="platform", platform_id="P-AIWEAR")[0]
        moved = self.service.investigate_ticket(
            ticket.ticket_id, actor="u_aiwear", role="platform",
            platform_id="P-AIWEAR", note="平台开始核实订单凭证",
        )
        self.assertEqual(moved.status, "investigating")


if __name__ == "__main__":
    unittest.main()
