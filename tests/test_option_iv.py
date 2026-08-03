from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from polymarket_stock.http import PublicApiError
from polymarket_stock.option_iv import OptionIvPoint, OptionSurfaceError, PolygonOptionIvClient, build_option_iv_surface


class OptionIvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 20, 15, tzinfo=UTC)

    def test_surface_selects_near_atm_put_call_iv_and_skew(self) -> None:
        expiry = self.now + timedelta(days=1)
        points = [
            OptionIvPoint("CALL", "call", 100, 2, 2.1, 0.30, self.now, expiry),
            OptionIvPoint("PUT", "put", 100, 2, 2.1, 0.34, self.now, expiry),
            OptionIvPoint("WIDE", "call", 101, 1, 2, 0.50, self.now, expiry),
        ]
        surface = build_option_iv_surface("TSLA", 100, "2026-07-21", self.now, points)
        self.assertAlmostEqual(surface.atm_iv, 0.32)
        self.assertAlmostEqual(surface.put_call_skew, 0.04)
        self.assertTrue(surface.usable)

    def test_surface_rejects_stale_points(self) -> None:
        point = OptionIvPoint(
            "CALL", "call", 100, 2, 2.1, 0.30, self.now - timedelta(minutes=20), self.now + timedelta(days=1)
        )
        with self.assertRaises(OptionSurfaceError):
            build_option_iv_surface("TSLA", 100, "2026-07-21", self.now, [point])

    def test_polygon_rejects_delayed_surface_for_entry(self) -> None:
        client = PolygonOptionIvClient(
            "test-key",
            get_json_fn=lambda *_args, **_kwargs: {
                "results": [
                    _polygon_row("call", 100, 0.30, "DELAYED", self.now),
                    _polygon_row("put", 100, 0.34, "DELAYED", self.now),
                ]
            },
        )
        surface = client.option_surface("TSLA", 100, self.now, self.now + timedelta(hours=4))
        self.assertEqual(surface.provider, "MASSIVE_OPTIONS")
        self.assertIn("POLYGON_OPTION_QUOTES_DELAYED", surface.quality_flags)
        self.assertFalse(surface.usable)

    def test_polygon_uses_realtime_surface_and_free_tier_interval(self) -> None:
        calls = []

        def response(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "results": [
                    _polygon_row("call", 100, 0.30, "REAL-TIME", self.now),
                    _polygon_row("put", 100, 0.34, "REAL-TIME", self.now),
                ]
            }

        client = PolygonOptionIvClient("test-key", get_json_fn=response)
        surface = client.option_surface("TSLA", 100, self.now, self.now + timedelta(hours=4))
        self.assertTrue(surface.usable)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(OptionSurfaceError, "POLYGON_FREE_TIER_RATE_LIMITED"):
            client.option_surface("TSLA", 100, self.now + timedelta(seconds=11), self.now + timedelta(hours=4))
        self.assertEqual(len(calls), 1)

    def test_polygon_stops_after_entitlement_failure(self) -> None:
        calls = 0

        def denied(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise PublicApiError("GET request returned HTTP 403")

        client = PolygonOptionIvClient("test-key", get_json_fn=denied)
        with self.assertRaisesRegex(OptionSurfaceError, "POLYGON_OPTIONS_NOT_ENTITLED"):
            client.option_surface("TSLA", 100, self.now, self.now + timedelta(hours=4))
        with self.assertRaisesRegex(OptionSurfaceError, "POLYGON_OPTIONS_NOT_ENTITLED"):
            client.option_surface("AAPL", 100, self.now + timedelta(minutes=1), self.now + timedelta(hours=4))
        self.assertEqual(calls, 1)


def _polygon_row(option_type: str, strike: float, iv: float, timeframe: str, now: datetime) -> dict[str, object]:
    return {
        "details": {
            "ticker": f"O:TSLA260722{option_type[0].upper()}00100000",
            "contract_type": option_type,
            "expiration_date": "2026-07-22",
            "strike_price": strike,
        },
        "implied_volatility": iv,
        "last_quote": {
            "bid": 2.0,
            "ask": 2.1,
            "last_updated": int(now.timestamp() * 1_000_000_000),
            "timeframe": timeframe,
        },
    }
