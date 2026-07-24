from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.journal import PaperPosition
from polymarket_stock.paper_reporting import paper_performance


def _position(*, status: str, outcome: str = "UP", settlement_outcome: str | None = None, pnl: float | None = None) -> PaperPosition:
    return PaperPosition(
        position_id="id", opened_at=datetime(2026, 7, 20, tzinfo=UTC), market_id="market", symbol="TSLA",
        outcome=outcome, status=status, contracts=1, entry_ask=0.5, entry_fee=0.005, entry_slippage=0.001,
        fair_probability=0.6, model_version="test", settled_at=datetime(2026, 7, 20, tzinfo=UTC) if status == "SETTLED" else None,
        settlement_outcome=settlement_outcome, payout=1 if outcome == settlement_outcome else 0 if status == "SETTLED" else None,
        realized_pnl=pnl,
    )


class PaperReportingTests(unittest.TestCase):
    def test_report_excludes_open_positions_from_calibration(self) -> None:
        report = paper_performance([
            _position(status="OPEN"),
            _position(status="SETTLED", outcome="UP", settlement_outcome="UP", pnl=0.494),
            _position(status="SETTLED", outcome="DOWN", settlement_outcome="UP", pnl=-0.506),
        ])
        self.assertEqual(report.open_positions, 1)
        self.assertEqual(report.settled_positions, 2)
        self.assertEqual(report.wins, 1)
        self.assertAlmostEqual(report.total_realized_pnl, -0.012)
        self.assertIsNotNone(report.brier_score)
