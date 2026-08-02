from __future__ import annotations

import unittest

from polymarket_stock.threshold_estimation import ThresholdSource, calibrated_threshold_estimate


class ThresholdEstimateTests(unittest.TestCase):
    def test_debiases_sources_and_uses_robust_median(self):
        calibrations = (
            {"status": "COMPLETE", "source_errors_bps": {"NASDAQ_DAILY_CLOSE": 10.0, "YAHOO_DAILY_CLOSE": -10.0}},
            {"status": "COMPLETE", "source_errors_bps": {"NASDAQ_DAILY_CLOSE": 10.0, "YAHOO_DAILY_CLOSE": -10.0}},
            {"status": "COMPLETE", "source_errors_bps": {"NASDAQ_DAILY_CLOSE": 10.0, "YAHOO_DAILY_CLOSE": -10.0}},
        )
        report = calibrated_threshold_estimate((
            ThresholdSource("NASDAQ_DAILY_CLOSE", 100.10),
            ThresholdSource("YAHOO_DAILY_CLOSE", 99.90),
        ), calibrations)
        self.assertAlmostEqual(report.price, 100.0, places=4)
        self.assertEqual(report.quality, "CALIBRATED_MULTI_SOURCE_MEDIUM")
        self.assertEqual(report.source_count, 2)
        self.assertEqual(report.calibration_samples, 6)

    def test_single_source_remains_a_labelled_estimate(self):
        report = calibrated_threshold_estimate((ThresholdSource("NASDAQ_DAILY_CLOSE", 100.0),), ())
        self.assertEqual(report.quality, "SINGLE_SOURCE_ESTIMATE")
        self.assertEqual(report.estimated_error_bps, 35.0)


if __name__ == "__main__":
    unittest.main()
