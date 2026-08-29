from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

from titan_brain.eod import EODReviewItem, build_eod_learning_packet, write_eod_learning_packet
from titan_brain.research import ArtifactExistsError


class EODTests(unittest.TestCase):
    def test_profitable_rule_violation_is_not_promotable(self):
        packet = build_eod_learning_packet(
            trading_date=date(2026, 8, 28),
            created_at=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
            reviews=[
                EODReviewItem(
                    record_id="x",
                    setup_id="FIRST_PULLBACK",
                    instrument="stock",
                    process_adherent=False,
                    realized_r=1.2,
                    mae_r=-0.3,
                    mfe_r=1.5,
                    exit_efficiency=0.8,
                    spread_cost=0.02,
                    slippage_cost=0.01,
                    route_comparison={"stock_actual": 1.2, "call_shadow": 0.8},
                    lesson="Profitable but invalid process",
                )
            ],
        )
        review = packet["reviews"][0]
        self.assertEqual(review["quadrant"], "BAD_PROCESS_GOOD_OUTCOME")
        self.assertFalse(review["promotion_eligible"])
        self.assertFalse(packet["automatic_promoted_edge_mutation"])

    def test_packet_is_create_only(self):
        packet = build_eod_learning_packet(
            trading_date=date(2026, 8, 28),
            created_at=datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
            reviews=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packet.json"
            write_eod_learning_packet(path, packet)
            with self.assertRaises(ArtifactExistsError):
                write_eod_learning_packet(path, packet)


if __name__ == "__main__":
    unittest.main()

