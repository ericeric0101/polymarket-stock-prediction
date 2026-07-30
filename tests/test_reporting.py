from __future__ import annotations

from datetime import UTC, datetime
import unittest

from rich.console import Console

from polymarket_stock.journal import PaperPosition
from polymarket_stock.probability_calibration import sizing_readiness
from polymarket_stock.reporting import _rich_dashboard, render_dashboard


class ReportingTests(unittest.TestCase):
    def test_dashboard_renders_compact_market_row(self) -> None:
        text = render_dashboard(({
            "symbol": "TSLA", "market_id": "2958682", "market_session": "REGULAR", "spot": 380.12,
            "up_bid": 0.48, "up_ask": 0.50, "down_bid": 0.49, "down_ask": 0.51, "skip_reasons": [],
        },), 1, 2)
        self.assertIn("TSLA", text)
        self.assertIn("0.48/0.50", text)

    def test_plain_dashboard_shows_sizing_is_not_enabled(self) -> None:
        text = render_dashboard((), 0, 0, sizing=sizing_readiness(()))
        self.assertIn("FIXED_SMALL_POSITION_ONLY", text)
        self.assertIn("Kelly disabled", text)

    def test_rich_dashboard_renders_header_and_market_monitor(self) -> None:
        layout = _rich_dashboard(({
            "symbol": "TSLA", "market_id": "2958682", "market_session": "REGULAR", "spot": 380.12,
            "up_bid": 0.48, "up_ask": 0.50, "down_bid": 0.49, "down_ask": 0.51,
            "fair_up_probability": 0.53, "option_iv": 0.40, "skip_reasons": [],
        },), ())
        console = Console(width=150, record=True, color_system=None)
        console.print(layout)
        rendered = console.export_text()
        self.assertIn("Polymarket Stock Shadow", rendered)
        self.assertIn("Market Monitor", rendered)
        self.assertIn("TSLA", rendered)

    def test_rich_dashboard_shows_daily_portfolio_and_all_signal_metrics(self) -> None:
        position = PaperPosition(
            position_id="position-1", opened_at=datetime.now(UTC), market_id="market-1", symbol="TSLA",
            outcome="UP", status="SETTLED", contracts=1.0, entry_ask=0.20, entry_fee=0.01,
            entry_slippage=0.0, fair_probability=0.30, model_version="test", settled_at=datetime.now(UTC),
            settlement_outcome="UP", payout=1.0, realized_pnl=0.79,
        )
        layout = _rich_dashboard((), (position,), signal_performance={"settled_markets": 11, "wins": 9})
        console = Console(width=150, record=True, color_system=None)
        console.print(layout)
        rendered = console.export_text()
        self.assertIn("Daily Paper Portfolio", rendered)
        self.assertIn("selected: 1 / 3", rendered)
        self.assertIn("All first signals: 9/11", rendered)
        self.assertIn("TSLA", rendered)

    def test_rich_dashboard_shows_active_maker_quote(self) -> None:
        layout = _rich_dashboard(({
            "symbol": "TSLA", "market_id": "market-1", "market_session": "REGULAR", "spot": 380.0,
            "up_bid": 0.46, "up_ask": 0.48, "down_bid": 0.52, "down_ask": 0.54,
            "fair_up_probability": 0.472, "maker_shadow_quotes": [
                {"outcome": "DOWN", "limit_price": 0.52}, {"outcome": "UP", "limit_price": 0.46}
            ],
        },), ())
        console = Console(width=150, record=True, color_system=None)
        console.print(layout)
        rendered = console.export_text()
        self.assertIn("Maker: 2", rendered)
        self.assertIn("MAKER DOWN @ 0.52  UP @ 0.46", rendered)

    def test_rich_dashboard_shows_every_selected_position_up_to_eight(self) -> None:
        positions = tuple(
            PaperPosition(
                position_id=f"position-{index}", opened_at=datetime.now(UTC), market_id=f"market-{index}",
                symbol=f"SYM{index}", outcome="UP", status="OPEN", contracts=1.0, entry_ask=0.20,
                entry_fee=0.01, entry_slippage=0.0, fair_probability=0.30, model_version="test",
                settled_at=None, settlement_outcome=None, payout=None, realized_pnl=None,
            )
            for index in range(8)
        )
        layout = _rich_dashboard(
            (), positions, signal_performance={"settled_markets": 0, "wins": 0},
            sizing=sizing_readiness(()), daily_entry_limit=8,
        )
        console = Console(width=150, height=32, record=True, color_system=None)
        console.print(layout)
        rendered = console.export_text()
        for index in range(8):
            self.assertIn(f"SYM{index}", rendered)
