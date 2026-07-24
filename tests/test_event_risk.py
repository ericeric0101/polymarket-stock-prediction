from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.event_risk import EventCalendarUnavailable, FinnhubEarningsCalendarClient, load_risk_events
from polymarket_stock.http import PublicApiError
from polymarket_stock.quality import us_equity_session


class EventRiskTests(unittest.TestCase):
    def test_calendar_loads_symbol_and_macro_events_in_resolution_window(self) -> None:
        now = datetime(2026, 7, 20, 15, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text('[{"kind":"earnings","starts_at":"2026-07-20T16:00:00Z","symbols":["TSLA"]},{"kind":"FOMC","starts_at":"2026-07-20T17:00:00Z","symbols":["*"]}]', encoding="utf-8")
            events = load_risk_events(path, "TSLA", now, now + timedelta(hours=4))
        self.assertEqual([event.kind for event in events], ["earnings", "FOMC"])

    def test_nyse_holiday_blocks_regular_session(self) -> None:
        self.assertEqual(us_equity_session(datetime(2026, 12, 25, 16, tzinfo=UTC)), "HOLIDAY:CHRISTMAS")

    def test_finnhub_earnings_calendar_blocks_bmo_in_resolution_window(self) -> None:
        client = FinnhubEarningsCalendarClient("key", get_json_fn=lambda *_args, **_kwargs: {
            "earningsCalendar": [{"symbol": "TSLA", "date": "2026-07-20", "hour": "bmo"}]
        })
        events = client.events("TSLA", datetime(2026, 7, 20, 12, tzinfo=UTC), datetime(2026, 7, 20, 20, tzinfo=UTC))
        self.assertEqual([event.kind for event in events], ["EARNINGS"])

    def test_finnhub_unavailable_calendar_is_a_distinct_hard_gate(self) -> None:
        def unavailable(*_args, **_kwargs):
            raise PublicApiError("GET request timed out")

        client = FinnhubEarningsCalendarClient("key", get_json_fn=unavailable)
        now = datetime(2026, 7, 20, 12, tzinfo=UTC)
        with self.assertRaises(EventCalendarUnavailable):
            client.events("TSLA", now, now + timedelta(hours=8))
        # The cooldown prevents each remaining market from waiting for another timeout.
        with self.assertRaises(EventCalendarUnavailable):
            client.events("AAPL", now, now + timedelta(hours=8))
