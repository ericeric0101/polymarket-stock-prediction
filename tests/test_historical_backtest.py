from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from polymarket_stock.baseline import DailyClose
from polymarket_stock.clob_history import PriceHistoryPoint
from polymarket_stock.historical_backtest import (
    UnderlyingSpotPoint,
    close_risk_profile,
    price_gap_from_daily_closes,
    replay_daily_up_down_market,
)
from polymarket_stock.market_discovery import MarketCandidate, MarketSettlement


def _candidate() -> MarketCandidate:
    return MarketCandidate.from_gamma_payload(
        {
            "id": "market-1",
            "question": "Tesla (TSLA) Up or Down on July 27?",
            "slug": "tsla-up-or-down-on-july-27-2026",
            "description": "Pyth close terms",
            "resolutionSource": "https://pythdata.app/explore/Equity.US.TSLA%2FUSD",
            "endDate": "2026-07-27T20:00:00Z",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
        }
    )


class HistoricalBacktestTests(unittest.TestCase):
    def test_price_gap_uses_prior_close_as_price_to_beat(self) -> None:
        gap = price_gap_from_daily_closes(
            market_id="m",
            symbol="TSLA",
            prior_close=DailyClose("2026-07-24", 100),
            final_close=DailyClose("2026-07-27", 102),
        )
        self.assertEqual(gap.winning_outcome, "UP")
        self.assertAlmostEqual(gap.gap_bps, 200)

    def test_close_risk_profile_tracks_late_probability_moves(self) -> None:
        resolves_at = datetime(2026, 7, 27, 20, tzinfo=UTC)
        points = (
            PriceHistoryPoint(resolves_at - timedelta(minutes=60), 0.55),
            PriceHistoryPoint(resolves_at - timedelta(minutes=15), 0.80),
            PriceHistoryPoint(resolves_at - timedelta(minutes=1), 0.95),
        )
        profile = close_risk_profile(
            resolves_at=resolves_at,
            up_history=points,
            price_to_beat=100,
            final_price=101,
            windows_minutes=(60, 15, 1),
        )
        self.assertEqual([item.up_price for item in profile], [0.55, 0.80, 0.95])
        self.assertAlmostEqual(profile[1].absolute_move_from_previous_window or 0, 0.25)

    def test_replay_selects_positive_edge_historical_trades(self) -> None:
        resolves_at = datetime(2026, 7, 27, 20, tzinfo=UTC)
        closes = [DailyClose(f"2026-06-{day:02d}", 100 + day * 0.1) for day in range(1, 22)]
        up_history = [PriceHistoryPoint(resolves_at - timedelta(minutes=minute), 0.20) for minute in (60, 30)]
        down_history = [PriceHistoryPoint(point.observed_at, 0.80) for point in up_history]
        spot_history = [UnderlyingSpotPoint(point.observed_at, 109.0) for point in up_history]
        settlement = MarketSettlement("market-1", True, "UP", {"closed": True})
        report = replay_daily_up_down_market(
            candidate=_candidate(),
            symbol="TSLA",
            resolves_at=resolves_at,
            closes_before_market=closes,
            final_close=DailyClose("2026-07-27", 110),
            up_history=up_history,
            down_history=down_history,
            settlement=settlement,
            spot_history=spot_history,
            minimum_edge=0.0,
            model_error_buffer=0.0,
        )
        self.assertGreater(report.selected_trades, 0)
        self.assertEqual(report.wins, report.selected_trades)

    def test_replay_without_spot_history_only_reports_gap_and_close_risk(self) -> None:
        resolves_at = datetime(2026, 7, 27, 20, tzinfo=UTC)
        closes = [DailyClose(f"2026-06-{day:02d}", 100 + day * 0.1) for day in range(1, 22)]
        up_history = [PriceHistoryPoint(resolves_at - timedelta(minutes=60), 0.20)]
        down_history = [PriceHistoryPoint(up_history[0].observed_at, 0.80)]
        report = replay_daily_up_down_market(
            candidate=_candidate(),
            symbol="TSLA",
            resolves_at=resolves_at,
            closes_before_market=closes,
            final_close=DailyClose("2026-07-27", 110),
            up_history=up_history,
            down_history=down_history,
            settlement=MarketSettlement("market-1", True, "UP", {"closed": True}),
            minimum_edge=0.0,
            model_error_buffer=0.0,
        )
        self.assertEqual(report.selected_trades, 0)
        self.assertEqual(report.price_gap.winning_outcome, "UP")
