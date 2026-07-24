from __future__ import annotations

import unittest

from rich.console import Console

from polymarket_stock.reporting import _rich_dashboard, render_dashboard


class ReportingTests(unittest.TestCase):
    def test_dashboard_renders_compact_market_row(self) -> None:
        text = render_dashboard(({
            "symbol": "TSLA", "market_id": "2958682", "market_session": "REGULAR", "spot": 380.12,
            "up_bid": 0.48, "up_ask": 0.50, "down_bid": 0.49, "down_ask": 0.51, "skip_reasons": [],
        },), 1, 2)
        self.assertIn("TSLA", text)
        self.assertIn("0.48/0.50", text)

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
