"""Deterministic batch selection for diversified shadow paper entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


RISK_GROUPS = {
    "AAPL": "MEGACAP_TECH",
    "AMZN": "MEGACAP_TECH",
    "GOOGL": "MEGACAP_TECH",
    "META": "MEGACAP_TECH",
    "MSFT": "MEGACAP_TECH",
    "MU": "SEMICONDUCTOR",
    "NVDA": "SEMICONDUCTOR",
    "COIN": "CRYPTO_BETA",
    "HOOD": "CRYPTO_BETA",
    "OPEN": "HIGH_BETA_GROWTH",
    "PLTR": "HIGH_BETA_GROWTH",
    "RKLB": "HIGH_BETA_GROWTH",
    "SPCX": "HIGH_BETA_GROWTH",
    "ABNB": "CONSUMER_TECH",
    "NFLX": "CONSUMER_TECH",
    "TSLA": "EV_AUTO",
}


@dataclass(frozen=True)
class PaperEntryCandidate:
    market_id: str
    symbol: str
    outcome: str
    entry_ask: float
    fair_probability: float
    edge: float

    @property
    def risk_group(self) -> str:
        return risk_group_for_symbol(self.symbol)


@dataclass(frozen=True)
class PortfolioDecision:
    candidate: PaperEntryCandidate
    accepted: bool
    reason: str


def risk_group_for_symbol(symbol: str) -> str:
    return RISK_GROUPS.get(symbol.upper(), f"SINGLE_NAME:{symbol.upper()}")


def select_diversified_entries(
    candidates: Iterable[PaperEntryCandidate],
    *,
    existing_symbols: Iterable[tuple[str, str]],
    max_daily_entries: int = 3,
    max_per_risk_group: int = 1,
    max_same_direction: int = 2,
) -> tuple[PortfolioDecision, ...]:
    """Choose the strongest independent candidates, returning auditable rejects."""

    if max_daily_entries < 1 or max_per_risk_group < 1 or max_same_direction < 1:
        raise ValueError("portfolio limits must be positive")
    accepted_groups: dict[str, int] = {}
    accepted_directions: dict[str, int] = {}
    selected_markets: set[str] = set()
    existing = tuple(existing_symbols)
    for symbol, outcome in existing:
        group = risk_group_for_symbol(symbol)
        accepted_groups[group] = accepted_groups.get(group, 0) + 1
        accepted_directions[outcome] = accepted_directions.get(outcome, 0) + 1
    decisions: list[PortfolioDecision] = []
    ordered = sorted(candidates, key=lambda item: (-item.edge, item.market_id))
    for candidate in ordered:
        if candidate.outcome not in {"UP", "DOWN"}:
            raise ValueError("paper entry outcome must be UP or DOWN")
        if candidate.market_id in selected_markets:
            decisions.append(PortfolioDecision(candidate, False, "DUPLICATE_MARKET"))
            continue
        total_entries = sum(accepted_groups.values())
        if total_entries >= max_daily_entries:
            decisions.append(PortfolioDecision(candidate, False, "DAILY_RISK_LIMIT"))
            continue
        group = candidate.risk_group
        if accepted_groups.get(group, 0) >= max_per_risk_group:
            decisions.append(PortfolioDecision(candidate, False, "CORRELATION_LIMIT"))
            continue
        if accepted_directions.get(candidate.outcome, 0) >= max_same_direction:
            decisions.append(PortfolioDecision(candidate, False, "DIRECTION_LIMIT"))
            continue
        selected_markets.add(candidate.market_id)
        accepted_groups[group] = accepted_groups.get(group, 0) + 1
        accepted_directions[candidate.outcome] = accepted_directions.get(candidate.outcome, 0) + 1
        decisions.append(PortfolioDecision(candidate, True, "SELECTED"))
    return tuple(decisions)
