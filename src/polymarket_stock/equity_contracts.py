"""Strict parser for the currently observed Polymarket daily US-equity template."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Mapping

from .market_discovery import MarketCandidate


class EquityContractParseError(ValueError):
    pass


@dataclass(frozen=True)
class DailyEquityCloseContract:
    market_id: str
    symbol: str
    resolves_at: datetime
    pyth_feed: str
    up_label: str
    down_label: str
    prior_trading_day_rule: str
    tie_rule: str
    price_precision_rule: str

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["resolves_at"] = self.resolves_at.isoformat()
        return payload


def parse_daily_equity_close_contract(candidate: MarketCandidate) -> DailyEquityCloseContract:
    """Accept only the exact Pyth close-vs-prior-close daily equity template."""

    description = candidate.description
    question_match = re.search(r"\(([A-Z][A-Z.]{0,9})\)", candidate.question.upper())
    if not question_match:
        raise EquityContractParseError("question does not contain an uppercase ticker")
    symbol = question_match.group(1)
    if (candidate.outcome_a_label.upper(), candidate.outcome_b_label.upper()) != ("UP", "DOWN"):
        raise EquityContractParseError("outcomes must be published as Up/Down in that order")
    expected_feed = f"Equity.US.{symbol}/USD"
    normalized_source = candidate.resolution_source.replace("%2F", "/").upper()
    if expected_feed.upper() not in normalized_source:
        raise EquityContractParseError("resolution source is not the expected Pyth equity feed")
    normalized = " ".join(description.split()).upper()
    required_phrases = (
        "CLOSE PRICE",
        "MOST RECENT PRIOR TRADING DAY",
        "RESOLVE 50-50",
        "PUBLISHED BY PYTH",
        "WITHOUT ROUNDING",
    )
    missing = [phrase for phrase in required_phrases if phrase not in normalized]
    if missing:
        raise EquityContractParseError(f"description is missing required rule(s): {', '.join(missing)}")
    if not re.search(rf"\b{re.escape(symbol)}\b", normalized):
        raise EquityContractParseError("description ticker does not match question ticker")
    try:
        resolves_at = datetime.fromisoformat(candidate.end_date.replace("Z", "+00:00"))
    except ValueError as error:
        raise EquityContractParseError("market end date is not ISO-8601") from error
    if resolves_at.tzinfo is None:
        raise EquityContractParseError("market end date is timezone-naive")
    return DailyEquityCloseContract(
        market_id=candidate.market_id,
        symbol=symbol,
        resolves_at=resolves_at,
        pyth_feed=expected_feed,
        up_label=candidate.outcome_a_label,
        down_label=candidate.outcome_b_label,
        prior_trading_day_rule="MOST_RECENT_PRIOR_TRADING_DAY",
        tie_rule="SPLIT_50_50",
        price_precision_rule="PYTH_UNROUNDED_CLOSE",
    )
