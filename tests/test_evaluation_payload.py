from __future__ import annotations
from datetime import UTC, datetime
import unittest
from polymarket_stock.evaluation_payload import PAYLOAD_VERSION, validate
from polymarket_stock.realtime import RealtimeEvaluation

class EvaluationPayloadTests(unittest.TestCase):
    def test_realtime_payload_is_versioned_and_complete(self):
        evaluation = RealtimeEvaluation(datetime(2026, 8, 3, 16, tzinfo=UTC), "m", "TSLA", "FINNHUB", 100, None, None, None, None, None, None, None, "IV_UNAVAILABLE", .5, .5, .49, .49, None, None, None, None, 1, 1, True, "REGULAR", True, .5, .2, "EWMA", (), 99, .02, .02, 0, 0, "UP", "UP", True, (), (), (), ())
        payload = evaluation.as_payload()
        self.assertEqual(payload["payload_version"], PAYLOAD_VERSION)
        validate(payload)

    def test_legacy_payload_remains_readable(self):
        validate({"evaluated_at": "2026-08-03T16:00:00+00:00", "market_id": "m", "symbol": "TSLA", "signal_status": "NO_PAPER_TRADE", "skip_reasons": []})
