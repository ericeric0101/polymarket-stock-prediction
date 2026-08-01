"""Read-only price-ladder discovery and polling sidecar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import time
from typing import Callable, Iterable, Mapping

from .checkpoints import checkpoint_window
from .http import get_json
from .market_discovery import GAMMA_EVENTS_KEYSET_URL, GammaMarketClient, MarketCandidate, MarketPayloadError
from .polymarket_data import ClobMarketDataClient, OrderBookSnapshot
from .price_ladder import PriceLadderContract, PriceLadderContractError, parse_price_ladder_contract
from .price_ladder_journal import PriceLadderJournal


@dataclass(frozen=True)
class LadderDiscoveryReport:
    contracts: tuple[PriceLadderContract, ...]
    events_scanned: int
    markets_scanned: int
    rejected_markets: int

    def as_payload(self) -> Mapping[str, object]:
        return {
            "contracts": [item.as_payload() for item in self.contracts],
            "events_scanned": self.events_scanned,
            "markets_scanned": self.markets_scanned,
            "rejected_markets": self.rejected_markets,
        }


@dataclass(frozen=True)
class LadderCollectionReport:
    observed_at: datetime
    contracts: int
    snapshots_written: int
    failures: tuple[Mapping[str, str], ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "observed_at": self.observed_at.isoformat(), "contracts": self.contracts,
            "snapshots_written": self.snapshots_written, "failures": [dict(item) for item in self.failures],
        }


class PriceLadderGammaClient:
    def __init__(self, get_json_fn: Callable[..., object] = get_json) -> None:
        self._get_json = get_json_fn

    def discover(
        self, *, symbols: Iterable[str] = ("TSLA", "NVDA"), tag_slugs: Iterable[str] = ("stocks", "equities"),
        page_size: int = 500, max_pages_per_tag: int = 20,
    ) -> LadderDiscoveryReport:
        normalized_symbols = {item.strip().upper() for item in symbols if item.strip()}
        if not normalized_symbols:
            raise ValueError("at least one ladder symbol is required")
        contracts: dict[str, PriceLadderContract] = {}
        events_scanned = markets_scanned = rejected = 0
        for tag_slug in tuple(dict.fromkeys(item.strip().lower() for item in tag_slugs if item.strip())):
            cursor: str | None = None
            seen: set[str] = set()
            for _ in range(max_pages_per_tag):
                parameters: dict[str, object] = {
                    "limit": page_size, "closed": "false", "tag_slug": tag_slug, "related_tags": "true",
                }
                if cursor:
                    parameters["after_cursor"] = cursor
                response = self._get_json(GAMMA_EVENTS_KEYSET_URL, parameters)
                if not isinstance(response, dict) or not isinstance(response.get("events"), list):
                    raise MarketPayloadError("Gamma keyset ladder response is invalid")
                events = response["events"]
                events_scanned += len(events)
                for event in events:
                    if not isinstance(event, dict) or event.get("active") is not True or event.get("closed") is not False:
                        continue
                    markets = event.get("markets")
                    if not isinstance(markets, list):
                        continue
                    for market in markets:
                        if not isinstance(market, dict):
                            continue
                        markets_scanned += 1
                        merged = {
                            **event, **market,
                            "event_id": event.get("id"), "event_slug": event.get("slug"),
                            "title": event.get("title") or market.get("question"),
                        }
                        try:
                            candidate = MarketCandidate.from_gamma_payload(merged)
                            contract = parse_price_ladder_contract(candidate)
                        except (MarketPayloadError, PriceLadderContractError):
                            rejected += 1
                            continue
                        if contract.symbol in normalized_symbols:
                            contracts[contract.market_id] = contract
                next_cursor = response.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                if next_cursor in seen:
                    raise MarketPayloadError("Gamma keyset ladder pagination repeated a cursor")
                seen.add(next_cursor)
                cursor = next_cursor
        return LadderDiscoveryReport(
            tuple(sorted(contracts.values(), key=lambda item: (item.market_date, item.symbol, item.strike))),
            events_scanned, markets_scanned, rejected,
        )


class PriceLadderCollector:
    """Polls public books and writes only price_ladder_* tables."""

    def __init__(
        self, *, journal: PriceLadderJournal, gamma: PriceLadderGammaClient | None = None,
        clob: ClobMarketDataClient | None = None, sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.journal = journal
        self.gamma = gamma or PriceLadderGammaClient()
        self.clob = clob or ClobMarketDataClient()
        self.sleep = sleep_fn

    def discover_and_store(self, *, symbols: Iterable[str]) -> LadderDiscoveryReport:
        report = self.gamma.discover(symbols=symbols)
        for contract in report.contracts:
            self.journal.upsert_contract(contract)
        return report

    def collect_once(
        self, *, contracts: Iterable[PriceLadderContract], observed_at: datetime | None = None,
    ) -> LadderCollectionReport:
        now = observed_at or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("collection timestamp must be timezone-aware")
        checkpoint = checkpoint_window(now)
        checkpoint_name = (
            checkpoint.checkpoint_name
            if checkpoint and checkpoint.checkpoint_name in {"1200_EDT", "1400_EDT", "1530_EDT"} else None
        )
        items = tuple(contracts)
        failures: list[Mapping[str, str]] = []
        written = 0
        for contract in items:
            try:
                yes_book = self.clob.get_order_book(contract.yes_token_id)
                no_book = self.clob.get_order_book(contract.no_token_id)
                written += int(self.journal.record_snapshot(
                    contract, observed_at=now, checkpoint_name=checkpoint_name,
                    yes_bid=yes_book.best_bid, yes_ask=yes_book.best_ask,
                    no_bid=no_book.best_bid, no_ask=no_book.best_ask,
                    yes_bid_depth=_depth(yes_book, "bids"), yes_ask_depth=_depth(yes_book, "asks"),
                    no_bid_depth=_depth(no_book, "bids"), no_ask_depth=_depth(no_book, "asks"),
                    yes_book=yes_book.raw_payload, no_book=no_book.raw_payload,
                ))
            except Exception as error:  # Keep one bad strike from stopping the isolated sidecar.
                failures.append({"market_id": contract.market_id, "error": f"{type(error).__name__}: {error}"})
        return LadderCollectionReport(now, len(items), written, tuple(failures))

    def run(
        self, *, symbols: Iterable[str], interval_seconds: float = 60.0, duration_seconds: float = 0,
    ) -> None:
        if interval_seconds <= 0 or duration_seconds < 0:
            raise ValueError("invalid ladder collection interval or duration")
        discovery = self.discover_and_store(symbols=symbols)
        contracts = discovery.contracts
        started = time.monotonic()
        while True:
            report = self.collect_once(contracts=contracts)
            print(json.dumps(report.as_payload(), sort_keys=True), flush=True)
            if duration_seconds and time.monotonic() - started >= duration_seconds:
                return
            self.sleep(interval_seconds)

    def settle_stored_contracts(self) -> tuple[Mapping[str, object], ...]:
        gamma = GammaMarketClient()
        results = []
        for contract in self.journal.list_contracts():
            settlement = gamma.get_market_settlement(contract.market_id)
            if settlement.closed and settlement.winning_outcome in {"Yes", "No", "YES", "NO"}:
                self.journal.record_settlement(
                    contract.market_id, settlement.winning_outcome, settlement.raw_payload, settled_at=datetime.now(UTC),
                )
                results.append({"market_id": contract.market_id, "outcome": settlement.winning_outcome.upper()})
        return tuple(results)


def _depth(book: OrderBookSnapshot, side: str) -> float:
    levels = book.bids if side == "bids" else book.asks
    return sum(level.size for level in levels[:5])
