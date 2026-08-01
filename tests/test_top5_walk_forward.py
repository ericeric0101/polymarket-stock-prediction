from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.journal import BufferSweepObservation
from polymarket_stock.top5_walk_forward import (
    TopFivePolicy, run_top_five_policy, top_five_policies, walk_forward_top_five_policy,
)


def _observation(
    market_id: str, checkpoint_date: str, checkpoint_name: str, fair_up: float, outcome: str,
) -> BufferSweepObservation:
    return BufferSweepObservation(
        market_id=market_id, symbol=market_id, checkpoint_date=checkpoint_date,
        checkpoint_name=checkpoint_name,
        evaluated_at=datetime.fromisoformat(f"{checkpoint_date}T16:00:00+00:00").replace(tzinfo=UTC),
        fair_up_probability=fair_up, up_ask=0.60, down_ask=0.40,
        up_taker_fee=0.01, down_taker_fee=0.01, winning_outcome=outcome,
    )


class TopFiveWalkForwardTests(unittest.TestCase):
    def test_daily_cap_and_one_entry_per_market_are_enforced(self) -> None:
        observations = [
            _observation(str(index), "2026-07-20", "1200_EDT", 0.90, "UP")
            for index in range(7)
        ] + [
            _observation("0", "2026-07-20", "1400_EDT", 0.95, "UP"),
        ]
        result = run_top_five_policy(
            observations,
            policy=TopFivePolicy(("1200_EDT", "1400_EDT"), buffer=0.01, minimum_edge=0.02,
                                  max_daily_entries=5),
        )
        self.assertEqual(result.selected_trades, 5)
        self.assertEqual(result.wins, 5)

    def test_validation_day_cannot_change_selected_policy(self) -> None:
        observations = [
            _observation("one", "2026-07-20", "1200_EDT", 0.67, "UP"),
            _observation("two", "2026-07-21", "1200_EDT", 0.67, "UP"),
            _observation("three", "2026-07-22", "1200_EDT", 0.67, "UP"),
        ]
        policies = top_five_policies(
            checkpoint_groups=(("1200_EDT",),), buffers=(0.01, 0.05), minimum_edges=(0.02,),
            max_daily_entries=5,
        )
        report = walk_forward_top_five_policy(
            observations, policies=policies, training_days=2, validation_days=1, minimum_training_trades=1,
        )
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.windows[0].training_dates, ("2026-07-20", "2026-07-21"))
        self.assertEqual(report.windows[0].validation_dates, ("2026-07-22",))
        self.assertEqual(report.windows[0].selected_policy.buffer, 0.01)

