from __future__ import annotations

import unittest

from polymarket_stock.market_discovery import GammaMarketClient, MarketCandidate, MarketPayloadError, is_daily_equity_direction_candidate


PAYLOAD = {
    "id": "123",
    "question": "Will SPY be up or down on July 18?",
    "slug": "spy-updown-july-18",
    "description": "Resolution details.",
    "resolutionSource": "Official source.",
    "endDate": "2026-07-18T20:00:00Z",
    "outcomes": '["Up", "Down"]',
    "clobTokenIds": '["yes-token", "no-token"]',
}


class MarketDiscoveryTests(unittest.TestCase):
    def test_candidate_preserves_outcome_token_order_and_requires_review(self) -> None:
        candidate = MarketCandidate.from_gamma_payload(PAYLOAD)
        self.assertEqual(candidate.outcome_a_label, "Up")
        self.assertEqual(candidate.outcome_a_token_id, "yes-token")
        self.assertEqual(candidate.outcome_b_label, "Down")
        self.assertEqual(candidate.outcome_b_token_id, "no-token")
        self.assertEqual(candidate.review_status, "REVIEW_REQUIRED")
        self.assertTrue(is_daily_equity_direction_candidate(candidate, ("SPY",)))

    def test_non_binary_market_is_rejected(self) -> None:
        payload = {**PAYLOAD, "outcomes": '["Yes", "No", "Maybe"]'}
        with self.assertRaises(MarketPayloadError):
            MarketCandidate.from_gamma_payload(payload)

    def test_client_filters_candidates_without_network(self) -> None:
        client = GammaMarketClient(get_json_fn=lambda _url, _params: [PAYLOAD])
        candidates = client.discover_daily_equity_candidates(("SPY",), limit=20)
        self.assertEqual([candidate.market_id for candidate in candidates], ["123"])

    def test_client_discovers_nested_event_market_by_exact_slug(self) -> None:
        event_payload = {"slug": "spy-updown-july-18", "markets": [PAYLOAD]}
        client = GammaMarketClient(get_json_fn=lambda _url, _params: [event_payload])
        candidates = client.discover_event_candidates("spy-updown-july-18", ("SPY",))
        self.assertEqual([candidate.market_id for candidate in candidates], ["123"])

    def test_cursor_scan_deduplicates_tagged_daily_equity_events(self) -> None:
        tagged_payload = {**PAYLOAD, "tags": [{"slug": "stocks"}]}
        responses = [
            {"events": [{"active": True, "closed": False, "markets": [tagged_payload]}], "next_cursor": "next"},
            {"events": [{"active": True, "closed": False, "markets": [tagged_payload]}], "next_cursor": ""},
            {"events": [], "next_cursor": ""},
        ]
        observed_params = []

        def fake_get_json(_url, params):
            observed_params.append(params)
            return responses.pop(0)

        client = GammaMarketClient(get_json_fn=fake_get_json, sleep_fn=lambda _seconds: None)
        report = client.discover_active_equity_candidates(
            tag_slugs=("stocks", "equities"), page_size=10, max_pages_per_tag=5, pause_seconds=0
        )
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.events_scanned, 2)
        self.assertEqual(report.pages_scanned, 3)
        self.assertEqual(observed_params[1]["after_cursor"], "next")
