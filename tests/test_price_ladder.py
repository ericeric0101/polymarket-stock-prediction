from __future__ import annotations

import unittest

from polymarket_stock.market_discovery import MarketCandidate
from polymarket_stock.price_ladder import (
    LadderProbabilityPoint,
    PriceLadderContractError,
    diagnose_cross_market,
    fit_monotonic_curve,
    parse_price_ladder_contract,
    probability_point,
)


def candidate_payload(strike: float = 310.0) -> dict[str, object]:
    return {
        "id": f"tsla-{strike:g}",
        "event_id": "event-1",
        "event_slug": "tsla-closes-above-august-3",
        "question": f"Will Tesla (TSLA) close above ${strike:g} on August 3?",
        "title": "Tesla (TSLA) closes above ___ on August 3?",
        "groupItemThreshold": f"${strike:g}",
        "slug": f"tsla-above-{strike:g}",
        "description": "This resolves using the Pyth TSLA closing price.",
        "resolutionSource": "https://www.pyth.network/price-feeds/equity-us-tsla-usd?feed=Equity.US.TSLA%2FUSD",
        "endDate": "2026-08-03T20:00:00Z",
        "outcomes": "[\"Yes\", \"No\"]",
        "clobTokenIds": f"[\"yes-{strike:g}\", \"no-{strike:g}\"]",
    }


def ladder_point(strike: float, probability: float, market_id: str = "market") -> LadderProbabilityPoint:
    return LadderProbabilityPoint(
        strike=strike, probability=probability, lower_bound=max(0, probability - 0.02),
        upper_bound=min(1, probability + 0.02), spread=0.04, weight=100, market_id=market_id,
    )


class PriceLadderTests(unittest.TestCase):
    def test_contract_parser_requires_exact_pyth_close_above_template(self) -> None:
        contract = parse_price_ladder_contract(MarketCandidate.from_gamma_payload(candidate_payload()))
        self.assertEqual((contract.symbol, contract.strike), ("TSLA", 310.0))
        self.assertEqual(contract.pyth_feed, "Equity.US.TSLA/USD")
        self.assertEqual(contract.market_date, "2026-08-03")
        self.assertEqual((contract.yes_token_id, contract.no_token_id), ("yes-310", "no-310"))

    def test_contract_parser_rejects_wrong_source_and_outcomes(self) -> None:
        wrong_source = {**candidate_payload(), "resolutionSource": "https://example.com/nasdaq"}
        with self.assertRaises(PriceLadderContractError):
            parse_price_ladder_contract(MarketCandidate.from_gamma_payload(wrong_source))
        wrong_outcomes = {**candidate_payload(), "outcomes": "[\"Up\", \"Down\"]"}
        with self.assertRaises(PriceLadderContractError):
            parse_price_ladder_contract(MarketCandidate.from_gamma_payload(wrong_outcomes))

    def test_executable_probability_uses_both_complementary_books(self) -> None:
        point = probability_point(
            strike=310, market_id="market", yes_bid=0.44, yes_ask=0.50,
            no_bid=0.48, no_ask=0.54, yes_depth=50, no_depth=50,
        )
        assert point is not None
        self.assertAlmostEqual(point.lower_bound, 0.46)
        self.assertAlmostEqual(point.upper_bound, 0.50)
        self.assertAlmostEqual(point.probability, 0.48)

    def test_weighted_isotonic_curve_removes_increasing_strike_probability(self) -> None:
        points = tuple(ladder_point(strike, probability, str(strike)) for strike, probability in (
            (290, 0.80), (300, 0.60), (310, 0.70), (320, 0.20),
        ))
        curve = fit_monotonic_curve(points)
        self.assertEqual(curve.violations, 1)
        self.assertTrue(all(a >= b for a, b in zip(curve.adjusted_probabilities, curve.adjusted_probabilities[1:])))
        self.assertAlmostEqual(curve.interpolate(305) or 0, 0.65)

    def test_cross_market_diagnostic_confirms_or_rejects_only_as_research(self) -> None:
        points = tuple(ladder_point(strike, probability, str(strike)) for strike, probability in (
            (290, 0.80), (310, 0.50), (330, 0.20),
        ))
        confirmed = diagnose_cross_market(
            symbol="TSLA", market_date="2026-08-03", checkpoint_name="1200_EDT",
            price_to_beat=310, model_up_probability=0.54, up_down_market_probability=0.52, points=points,
        )
        disagreed = diagnose_cross_market(
            symbol="TSLA", market_date="2026-08-03", checkpoint_name="1200_EDT",
            price_to_beat=310, model_up_probability=0.80, up_down_market_probability=0.75, points=points,
        )
        self.assertEqual(confirmed.status, "CONFIRM")
        self.assertEqual(disagreed.status, "DISAGREE")
        self.assertEqual(confirmed.strikes, 3)


if __name__ == "__main__":
    unittest.main()
