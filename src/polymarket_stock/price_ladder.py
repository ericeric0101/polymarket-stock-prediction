"""Independent price-ladder research models and cross-market diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Iterable, Mapping

from .market_discovery import MarketCandidate


class PriceLadderContractError(ValueError):
    """Raised when a market cannot be safely treated as a Pyth equity close strike."""


@dataclass(frozen=True)
class PriceLadderContract:
    market_id: str
    event_id: str
    event_slug: str
    symbol: str
    strike: float
    market_date: str
    resolves_at: datetime
    pyth_feed: str
    yes_token_id: str
    no_token_id: str
    question: str
    rules_hash: str
    raw_payload: Mapping[str, object]

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["resolves_at"] = self.resolves_at.isoformat()
        return payload


@dataclass(frozen=True)
class LadderProbabilityPoint:
    strike: float
    probability: float
    lower_bound: float
    upper_bound: float
    spread: float
    weight: float
    market_id: str


@dataclass(frozen=True)
class MonotonicLadderCurve:
    points: tuple[LadderProbabilityPoint, ...]
    adjusted_probabilities: tuple[float, ...]
    violations: int

    def interpolate(self, strike: float) -> float | None:
        if len(self.points) < 2 or strike < self.points[0].strike or strike > self.points[-1].strike:
            return None
        for left_index, (left, right) in enumerate(zip(self.points, self.points[1:])):
            if left.strike <= strike <= right.strike:
                if right.strike == left.strike:
                    return self.adjusted_probabilities[left_index]
                fraction = (strike - left.strike) / (right.strike - left.strike)
                left_probability = self.adjusted_probabilities[left_index]
                right_probability = self.adjusted_probabilities[left_index + 1]
                return left_probability + fraction * (right_probability - left_probability)
        return None

    def bounds_at(self, strike: float) -> tuple[float, float] | None:
        if len(self.points) < 2 or strike < self.points[0].strike or strike > self.points[-1].strike:
            return None
        for point in self.points:
            if point.strike == strike:
                return point.lower_bound, point.upper_bound
        for left, right in zip(self.points, self.points[1:]):
            if left.strike < strike < right.strike:
                return min(left.lower_bound, right.lower_bound), max(left.upper_bound, right.upper_bound)
        return None


@dataclass(frozen=True)
class CrossMarketDiagnostic:
    symbol: str
    market_date: str
    checkpoint_name: str
    price_to_beat: float
    model_up_probability: float
    up_down_market_probability: float | None
    ladder_up_probability: float | None
    ladder_lower_bound: float | None
    ladder_upper_bound: float | None
    strikes: int
    monotonic_violations: int
    status: str
    reasons: tuple[str, ...]

    def as_payload(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def parse_price_ladder_contract(candidate: MarketCandidate) -> PriceLadderContract:
    """Parse only binary Yes/No equity-close-above markets using the expected Pyth feed."""

    payload = candidate.raw_payload
    searchable = " ".join(
        str(value) for value in (
            candidate.question, payload.get("title"), payload.get("groupItemTitle"), payload.get("groupItemThreshold"),
        ) if value
    )
    symbol_match = re.search(r"\(([A-Z][A-Z.]{0,9})\)", searchable.upper())
    if symbol_match is None:
        symbol_match = re.search(r"\b(TSLA|NVDA)\b", searchable.upper())
    if symbol_match is None:
        raise PriceLadderContractError("price-ladder title does not contain an equity ticker")
    symbol = symbol_match.group(1)
    if (candidate.outcome_a_label.upper(), candidate.outcome_b_label.upper()) != ("YES", "NO"):
        raise PriceLadderContractError("price-ladder outcomes must be Yes/No in that order")
    normalized_question = " ".join(candidate.question.upper().split())
    normalized_title = " ".join(str(payload.get("title") or "").upper().split())
    if "CLOSE" not in normalized_question + " " + normalized_title or "ABOVE" not in normalized_question + " " + normalized_title:
        raise PriceLadderContractError("market is not a closes-above strike")
    strike = _parse_strike(searchable)
    expected_feed = f"Equity.US.{symbol}/USD"
    source = candidate.resolution_source.replace("%2F", "/")
    if expected_feed.upper() not in source.upper():
        raise PriceLadderContractError("resolution source is not the expected Pyth equity feed")
    description = " ".join(candidate.description.upper().split())
    if "PYTH" not in description or "CLOS" not in description:
        raise PriceLadderContractError("description does not define a Pyth close")
    try:
        resolves_at = datetime.fromisoformat(candidate.end_date.replace("Z", "+00:00"))
    except ValueError as error:
        raise PriceLadderContractError("market end date is not ISO-8601") from error
    if resolves_at.tzinfo is None:
        raise PriceLadderContractError("market end date is timezone-naive")
    market_date = resolves_at.date().isoformat()
    import hashlib
    rules_hash = hashlib.sha256(
        f"{candidate.question}|{candidate.description}|{candidate.resolution_source}".encode("utf-8")
    ).hexdigest()
    return PriceLadderContract(
        market_id=candidate.market_id,
        event_id=str(payload.get("event_id") or payload.get("eventId") or payload.get("id") or ""),
        event_slug=str(payload.get("event_slug") or payload.get("slug") or ""),
        symbol=symbol, strike=strike, market_date=market_date, resolves_at=resolves_at,
        pyth_feed=expected_feed, yes_token_id=candidate.outcome_a_token_id,
        no_token_id=candidate.outcome_b_token_id, question=candidate.question,
        rules_hash=rules_hash, raw_payload=dict(payload),
    )


def probability_point(
    *, strike: float, market_id: str, yes_bid: float | None, yes_ask: float | None,
    no_bid: float | None, no_ask: float | None, yes_depth: float = 0.0, no_depth: float = 0.0,
) -> LadderProbabilityPoint | None:
    """Create executable probability bounds from both complementary books."""

    lower_candidates = [value for value in (yes_bid, 1 - no_ask if no_ask is not None else None) if value is not None]
    upper_candidates = [value for value in (yes_ask, 1 - no_bid if no_bid is not None else None) if value is not None]
    if not lower_candidates or not upper_candidates:
        return None
    lower = max(0.0, max(lower_candidates))
    upper = min(1.0, min(upper_candidates))
    if lower > upper:
        # Preserve an explicit wide uncertainty interval for incoherent crossed complements.
        lower, upper = min(lower, upper), max(lower, upper)
    midpoint = (lower + upper) / 2
    spread = upper - lower
    depth = max(yes_depth + no_depth, 1.0)
    weight = depth / max(spread, 0.01)
    return LadderProbabilityPoint(strike, midpoint, lower, upper, spread, weight, market_id)


def fit_monotonic_curve(points: Iterable[LadderProbabilityPoint]) -> MonotonicLadderCurve:
    """Weighted isotonic regression enforcing P(close > K) to decrease with K."""

    ordered = tuple(sorted(points, key=lambda item: (item.strike, item.market_id)))
    if not ordered:
        return MonotonicLadderCurve((), (), 0)
    violations = sum(left.probability < right.probability for left, right in zip(ordered, ordered[1:]))
    blocks: list[dict[str, float | int]] = []
    for index, point in enumerate(ordered):
        blocks.append({"start": index, "end": index, "weight": point.weight, "value": point.probability})
        while len(blocks) >= 2 and float(blocks[-2]["value"]) < float(blocks[-1]["value"]):
            right = blocks.pop()
            left = blocks.pop()
            weight = float(left["weight"]) + float(right["weight"])
            value = (
                float(left["value"]) * float(left["weight"])
                + float(right["value"]) * float(right["weight"])
            ) / weight
            blocks.append({"start": int(left["start"]), "end": int(right["end"]), "weight": weight, "value": value})
    adjusted = [0.0] * len(ordered)
    for block in blocks:
        for index in range(int(block["start"]), int(block["end"]) + 1):
            adjusted[index] = float(block["value"])
    return MonotonicLadderCurve(ordered, tuple(adjusted), violations)


def diagnose_cross_market(
    *, symbol: str, market_date: str, checkpoint_name: str, price_to_beat: float,
    model_up_probability: float, up_down_market_probability: float | None,
    points: Iterable[LadderProbabilityPoint], minimum_strikes: int = 3,
    maximum_bracket_width: float = 0.30, disagreement_threshold: float = 0.10,
    confirmation_threshold: float = 0.07,
) -> CrossMarketDiagnostic:
    curve = fit_monotonic_curve(points)
    ladder_probability = curve.interpolate(price_to_beat)
    bounds = curve.bounds_at(price_to_beat)
    reasons: list[str] = []
    if len(curve.points) < minimum_strikes:
        reasons.append("INSUFFICIENT_STRIKES")
    if ladder_probability is None or bounds is None:
        reasons.append("PRICE_TO_BEAT_NOT_BRACKETED")
    elif bounds[1] - bounds[0] > maximum_bracket_width:
        reasons.append("LADDER_BOOK_TOO_WIDE")
    if curve.violations:
        reasons.append("NON_MONOTONIC_RAW_LADDER")
    if reasons and any(reason in {"INSUFFICIENT_STRIKES", "PRICE_TO_BEAT_NOT_BRACKETED", "LADDER_BOOK_TOO_WIDE"} for reason in reasons):
        status = "UNRELIABLE"
    elif ladder_probability is None:
        status = "UNRELIABLE"
    else:
        comparisons = [abs(ladder_probability - model_up_probability)]
        if up_down_market_probability is not None:
            comparisons.append(abs(ladder_probability - up_down_market_probability))
        if max(comparisons) >= disagreement_threshold:
            status = "DISAGREE"
        elif max(comparisons) <= confirmation_threshold:
            status = "CONFIRM"
        else:
            status = "MIXED"
    return CrossMarketDiagnostic(
        symbol=symbol, market_date=market_date, checkpoint_name=checkpoint_name,
        price_to_beat=price_to_beat, model_up_probability=model_up_probability,
        up_down_market_probability=up_down_market_probability,
        ladder_up_probability=ladder_probability,
        ladder_lower_bound=bounds[0] if bounds else None,
        ladder_upper_bound=bounds[1] if bounds else None,
        strikes=len(curve.points), monotonic_violations=curve.violations,
        status=status, reasons=tuple(reasons),
    )


def _parse_strike(value: str) -> float:
    patterns = (
        r"(?:ABOVE|THRESHOLD)\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"\$([0-9][0-9,]*(?:\.[0-9]+)?)",
    )
    normalized = value.upper()
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            strike = float(match.group(1).replace(",", ""))
            if strike > 0:
                return strike
    raise PriceLadderContractError("price-ladder strike is missing")
