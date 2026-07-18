from __future__ import annotations

import unittest

from polymarket_stock.metrics import calibration_metrics, paper_pnl


class MetricsTests(unittest.TestCase):
    def test_calibration_metrics(self) -> None:
        metrics = calibration_metrics([(0.8, True), (0.2, False)])
        self.assertEqual(metrics.sample_size, 2)
        self.assertAlmostEqual(metrics.brier_score, 0.04)
        self.assertGreater(metrics.log_loss, 0)

    def test_paper_pnl_includes_costs(self) -> None:
        self.assertAlmostEqual(paper_pnl(0.50, True, 0.02, 0.01), 0.48)
        self.assertAlmostEqual(paper_pnl(0.50, False, 0.02, 0.01), -0.52)
