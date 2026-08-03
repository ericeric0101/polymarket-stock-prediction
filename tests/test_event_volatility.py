from __future__ import annotations

import unittest

from polymarket_stock.event_volatility import EventReturn, event_conditioned_volatility


class EventVolatilityTests(unittest.TestCase):
    def test_event_volatility_requires_a_minimum_sample(self) -> None:
        events = [EventReturn("2026-01-01", "EARNINGS", 0.08)] * 3
        result = event_conditioned_volatility(events, event_type="EARNINGS", minimum_samples=5)
        self.assertEqual(result.status, "INSUFFICIENT_EVENT_SAMPLES")
        self.assertIsNone(result.annualized_volatility)

    def test_event_volatility_is_available_for_a_sufficient_cohort(self) -> None:
        events = [
            EventReturn("2026-01-01", "EARNINGS", 0.08),
            EventReturn("2026-02-01", "EARNINGS", -0.06),
            EventReturn("2026-03-01", "EARNINGS", 0.04),
            EventReturn("2026-04-01", "EARNINGS", -0.05),
            EventReturn("2026-05-01", "EARNINGS", 0.07),
        ]
        result = event_conditioned_volatility(events, event_type="EARNINGS", minimum_samples=5)
        self.assertEqual(result.status, "READY_FOR_RESEARCH")
        self.assertIsNotNone(result.annualized_volatility)
