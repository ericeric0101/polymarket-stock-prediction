from __future__ import annotations

from datetime import UTC, datetime
import unittest

from polymarket_stock.journal import (
    BufferSweepObservation, ExecutionObservation, SpotSourceComparison, StoredSpotObservation,
)
from polymarket_stock.strategy_diagnostics import (
    book_vwap, direction_benchmarks, execution_quality, exit_horizon_replay,
    intraday_volatility_summary, spot_divergence_summary, volatility_comparison_summary,
)


NOW = datetime(2026, 7, 30, 16, tzinfo=UTC)


def checkpoint(
    market_id: str = "one", *, fair_up: float = 0.7, outcome: str = "UP",
    spot: float = 101, threshold: float = 100, evaluated_at: datetime = NOW,
    checkpoint_date: str = "2026-07-30", annualized_volatility: float | None = None,
) -> BufferSweepObservation:
    return BufferSweepObservation(
        market_id=market_id, symbol=market_id, checkpoint_date=checkpoint_date, checkpoint_name="1200_EDT",
        evaluated_at=evaluated_at, fair_up_probability=fair_up, up_ask=0.6, down_ask=0.4,
        up_taker_fee=0.01, down_taker_fee=0.01, winning_outcome=outcome,
        spot=spot, price_to_beat=threshold, annualized_volatility=annualized_volatility,
        comparison_models=({"volatility_estimator": "EWMA", "fair_up_probability": 0.4},),
    )


def execution(kind: str, *, bid: float, ask: float) -> ExecutionObservation:
    return ExecutionObservation(
        observed_at=NOW, signal_id="signal", observation_kind=kind, market_id="one", symbol="ONE",
        outcome="UP", token_id="up", spot=101, price_to_beat=100, fair_probability=0.7,
        best_bid=bid, best_ask=ask, fee_rate=0.04,
        book_payload={
            "asks": [{"price": ask, "size": 5}, {"price": ask + 0.02, "size": 10}],
            "bids": [{"price": bid, "size": 5}, {"price": bid - 0.02, "size": 10}],
        },
        evaluation_payload={"paper_outcome": "UP"},
    )


class StrategyDiagnosticsTests(unittest.TestCase):
    def test_benchmarks_compare_model_market_spot_and_majority(self) -> None:
        results = {item.name: item for item in direction_benchmarks((checkpoint(),))}
        assert results["MODEL_DIRECTION"].wins == 1
        assert results["MARKET_FAVORITE"].wins == 1
        assert results["SPOT_VS_THRESHOLD"].wins == 1
        assert results["MARKET_MAJORITY"].wins == 1


    def test_depth_vwap_requires_enough_size_and_reports_slippage(self) -> None:
        entry = execution("PAPER_ENTRY", bid=0.58, ask=0.60)
        assert book_vwap(entry.book_payload, side="asks", shares=10) == 0.61
        assert book_vwap(entry.book_payload, side="asks", shares=20) is None
        summary = execution_quality((entry, execution("MARKOUT_60S", bid=0.61, ask=0.63)), requested_shares=10)
        assert summary.depth_fillable_signals == 1
        assert abs(summary.average_depth_slippage - 0.01) < 1e-12
        assert abs(summary.delayed_entry_slippage["MARKOUT_60S"] - 0.04) < 1e-12


    def test_spot_and_volatility_disagreement_are_summarized(self) -> None:
        comparisons = (
            SpotSourceComparison(NOW, "ONE", "FINNHUB", 100.1, 100, 0.01, 10, NOW, NOW),
            SpotSourceComparison(NOW, "ONE", "FINNHUB", 100.6, 100, 0.01, 60, NOW, NOW),
            SpotSourceComparison(NOW, "ONE", "FINNHUB", 120, 100, 0.01, 2000, None, None),
        )
        spot = spot_divergence_summary(comparisons)
        volatility = volatility_comparison_summary((checkpoint(),))
        assert spot.above_50_bps == 1
        assert spot.total_observations == 3
        assert spot.observations == 2
        assert spot.excluded_stale_or_unstamped == 1
        assert volatility.direction_disagreements == 1
        assert volatility.large_probability_disagreements == 1


    def test_exit_replay_uses_executable_bid_and_hold_settlement(self) -> None:
        entry = execution("PAPER_ENTRY", bid=0.58, ask=0.60)
        markout = execution("MARKOUT_60S", bid=0.70, ask=0.72)
        report = exit_horizon_replay((entry, markout), (checkpoint(),), requested_shares=10)[0]
        assert report.liquid_positions == 1
        assert report.total_exit_pnl < report.total_hold_pnl

    def test_intraday_volatility_uses_only_prior_matching_checkpoint_history(self) -> None:
        day_one = datetime(2026, 7, 29, 16, tzinfo=UTC)
        spots = (
            StoredSpotObservation(day_one.replace(minute=0), "PYTH_HERMES", "ONE", 100.00),
            StoredSpotObservation(day_one.replace(minute=1), "PYTH_HERMES", "ONE", 100.01),
            StoredSpotObservation(day_one.replace(minute=2), "PYTH_HERMES", "ONE", 100.02),
            StoredSpotObservation(NOW.replace(minute=0), "PYTH_HERMES", "ONE", 100.00),
            StoredSpotObservation(NOW.replace(minute=1), "PYTH_HERMES", "ONE", 101.00),
            StoredSpotObservation(NOW.replace(minute=2), "PYTH_HERMES", "ONE", 102.00),
        )
        checkpoints = (
            checkpoint(evaluated_at=day_one.replace(minute=3), checkpoint_date="2026-07-29", annualized_volatility=0.2),
            checkpoint(evaluated_at=NOW.replace(minute=3), annualized_volatility=0.2),
        )
        summary = intraday_volatility_summary(spots, checkpoints)
        assert summary.checkpoint_paths == 2
        assert summary.history_comparisons == 1
        assert summary.high_regime_count == 1
        assert summary.mean_intraday_to_daily_model_ratio is not None
        assert summary.by_checkpoint["1200_EDT"]["high_regime_count"] == 1
