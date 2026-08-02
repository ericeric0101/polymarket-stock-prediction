from datetime import UTC, datetime
from unittest import TestCase

from polymarket_stock.above_x_veto import AboveXVetoPolicy, VetoObservation, apply_policy, walk_forward


def observation(day, core, ladder, winner):
    return VetoObservation("m" + day, "TSLA", day, "1200_EDT", datetime(2026, 7, 1, tzinfo=UTC), core, .05, winner, 100., .6, .55, 5, True, ladder, ())


class AboveXVetoTests(TestCase):
    def test_veto_removes_only_reliable_disagreement(self):
        rows = (observation("2026-07-01", "UP", "UP", "UP"), observation("2026-07-02", "DOWN", "UP", "UP"))
        result = apply_policy(rows, AboveXVetoPolicy("VETO_DISAGREEMENT", 3))
        self.assertEqual((result.trades, result.wins, result.vetoed), (1, 1, 1))

    def test_walk_forward_does_not_select_from_validation(self):
        rows = tuple(observation(f"2026-07-{day:02d}", "UP", "DOWN" if day < 4 else "UP", "UP") for day in range(1, 7))
        report = walk_forward(observations=rows, training_days=3, validation_days=2, minimum_training_trades=2)
        self.assertEqual(report.status, "READY")
        window = report.windows[0]
        self.assertEqual(window["training_dates"], ["2026-07-01", "2026-07-02", "2026-07-03"])
        self.assertEqual(window["validation_dates"], ["2026-07-04", "2026-07-05"])
