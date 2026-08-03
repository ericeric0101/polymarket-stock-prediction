"""Historical discovery and backfill for isolated Above-X markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
import csv
import json
from pathlib import Path
import time as time_module
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .clob_history import ClobPriceHistoryClient, PriceHistoryPoint
from .http import get_json
from .market_discovery import GAMMA_EVENTS_KEYSET_URL, GammaMarketClient, MarketCandidate, MarketPayloadError
from .price_ladder import PriceLadderContract, PriceLadderContractError, parse_price_ladder_contract
from .pyth_benchmarks import PythBenchmarksClient

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AboveXDiscoveryReport:
    contracts: tuple[PriceLadderContract, ...]
    pages_scanned: int
    markets_scanned: int
    rejected_markets: int
    date_start: str | None
    date_end: str | None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "contracts": [item.as_payload() for item in self.contracts],
            "pages_scanned": self.pages_scanned,
            "markets_scanned": self.markets_scanned,
            "rejected_markets": self.rejected_markets,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "market_type": "ABOVE_X",
        }


@dataclass(frozen=True)
class AboveXCoverageReport:
    discovery_contracts: int
    complete_markets: int
    missing_yes_clob: int
    missing_no_clob: int
    missing_pyth_final: int
    missing_settlement: int
    matching_intraday_spot: int
    missing_intraday_spot: int
    market_date_start: str | None
    market_date_end: str | None

    def as_payload(self) -> Mapping[str, object]:
        return {
            "market_type": "ABOVE_X",
            "discovery_contracts": self.discovery_contracts,
            "complete_markets": self.complete_markets,
            "missing_yes_clob": self.missing_yes_clob,
            "missing_no_clob": self.missing_no_clob,
            "missing_pyth_final": self.missing_pyth_final,
            "missing_settlement": self.missing_settlement,
            "matching_intraday_spot": self.matching_intraday_spot,
            "missing_intraday_spot": self.missing_intraday_spot,
            "market_date_start": self.market_date_start,
            "market_date_end": self.market_date_end,
        }


def above_x_coverage_report(
    *,
    discovery_path: Path,
    output_dir: Path,
    spot_data_dir: Path | None = None,
) -> AboveXCoverageReport:
    payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Above-X discovery JSON must be a list")
    contracts = [_contract_from_payload(item) for item in payload if isinstance(item, Mapping)]
    complete = missing_yes = missing_no = missing_pyth = missing_settlement = 0
    matching_spot = 0
    for contract in contracts:
        stem = f"{contract.market_id}_{contract.symbol}_{contract.market_date}_above_x"
        paths = {
            "yes": output_dir / f"{stem}_yes_clob.csv",
            "no": output_dir / f"{stem}_no_clob.csv",
            "pyth": output_dir / f"{stem}_pyth_final.json",
            "settlement": output_dir / f"{stem}_settlement.json",
        }
        missing_yes += not paths["yes"].is_file()
        missing_no += not paths["no"].is_file()
        missing_pyth += not paths["pyth"].is_file()
        missing_settlement += not paths["settlement"].is_file()
        complete += all(path.is_file() for path in paths.values())
        if spot_data_dir is not None:
            matches = list(spot_data_dir.glob(f"*_{contract.symbol}_{contract.market_date}_pyth_intraday.csv"))
            matching_spot += bool(matches)
    dates = sorted(contract.market_date for contract in contracts)
    return AboveXCoverageReport(
        discovery_contracts=len(contracts),
        complete_markets=complete,
        missing_yes_clob=missing_yes,
        missing_no_clob=missing_no,
        missing_pyth_final=missing_pyth,
        missing_settlement=missing_settlement,
        matching_intraday_spot=matching_spot,
        missing_intraday_spot=len(contracts) - matching_spot,
        market_date_start=dates[0] if dates else None,
        market_date_end=dates[-1] if dates else None,
    )


@dataclass(frozen=True)
class AboveXBackfillReport:
    requested: int
    completed: int
    skipped: int
    failed: int
    failures: tuple[Mapping[str, object], ...]

    def as_payload(self) -> Mapping[str, object]:
        return {
            "requested": self.requested,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": [dict(item) for item in self.failures],
            "execution_price_assumption": "HISTORICAL_PRICE_PROXY",
        }


class AboveXHistoricalDiscovery:
    """Discover closed Pyth-resolved closes-above markets through Gamma."""

    def __init__(self, get_json_fn: Callable[..., object] = get_json) -> None:
        self._get_json = get_json_fn

    def discover(
        self,
        *,
        symbols: Iterable[str] = ("TSLA", "NVDA"),
        date_start: str | None = None,
        date_end: str | None = None,
        page_size: int = 500,
        max_pages: int = 100,
    ) -> AboveXDiscoveryReport:
        wanted = {item.strip().upper() for item in symbols if item.strip()}
        if not wanted:
            raise ValueError("at least one Above-X symbol is required")
        if not 1 <= page_size <= 500 or max_pages < 1:
            raise ValueError("invalid historical discovery pagination")
        start = _date_or_none(date_start)
        end = _date_or_none(date_end)
        if start and end and start > end:
            raise ValueError("date_start must not be after date_end")

        contracts: dict[str, PriceLadderContract] = {}
        pages = scanned = rejected = 0
        for symbol in sorted(wanted):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(max_pages):
                params: dict[str, object] = {
                    "closed": "true",
                    "limit": page_size,
                    "tag_slug": "stocks",
                    "related_tags": "true",
                    "title_search": symbol,
                }
                if start:
                    params["end_date_min"] = f"{start.isoformat()}T00:00:00Z"
                if end:
                    params["end_date_max"] = f"{end.isoformat()}T23:59:59Z"
                if cursor:
                    params["after_cursor"] = cursor
                payload = self._get_json(GAMMA_EVENTS_KEYSET_URL, params)
                if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
                    raise MarketPayloadError("Gamma keyset events response must contain an events array")
                events = payload["events"]
                if not all(isinstance(item, dict) for item in events):
                    raise MarketPayloadError("Gamma keyset events must contain objects")
                pages += 1
                for event in events:
                    markets = event.get("markets")
                    if not isinstance(markets, list) or not all(isinstance(item, dict) for item in markets):
                        continue
                    scanned += len(markets)
                    for market in markets:
                        try:
                            merged = {**event, **market, "event_id": event.get("id"), "event_slug": event.get("slug")}
                            contract = parse_price_ladder_contract(MarketCandidate.from_gamma_payload(merged))
                            if contract.symbol != symbol:
                                continue
                            if start and contract.market_date < start.isoformat():
                                continue
                            if end and contract.market_date > end.isoformat():
                                continue
                            contracts[contract.market_id] = contract
                        except (MarketPayloadError, PriceLadderContractError, ValueError):
                            rejected += 1
                next_cursor = payload.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise MarketPayloadError("Gamma keyset pagination repeated a cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        ordered = tuple(
            sorted(
                contracts.values(),
                key=lambda item: (item.market_date, item.symbol, item.strike, item.market_id),
            )
        )
        return AboveXDiscoveryReport(ordered, pages, scanned, rejected, date_start, date_end)


def write_above_x_discovery(path: Path, report: AboveXDiscoveryReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([item.as_payload() for item in report.contracts], sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def backfill_above_x_markets(
    *,
    discovery_path: Path,
    output_dir: Path,
    pyth_api_key: str = "",
    pause_seconds: float = 0.2,
    pyth_pause_seconds: float = 2.0,
    maximum_markets: int | None = None,
    pyth_data_dir: Path | None = Path("data/historical/90d"),
) -> AboveXBackfillReport:
    """Download CLOB price proxies, final Pyth price, and Gamma settlement."""
    if pause_seconds < 0 or pyth_pause_seconds < 0:
        raise ValueError("pause values must be non-negative")
    payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Above-X discovery JSON must be a list")
    items = [item for item in payload if isinstance(item, Mapping)]
    if maximum_markets is not None:
        if maximum_markets < 1:
            raise ValueError("maximum_markets must be positive")
        items = [item for item in items if not _above_x_files_complete(_contract_from_payload(item), output_dir)]
        items = items[:maximum_markets]
    output_dir.mkdir(parents=True, exist_ok=True)
    clob = ClobPriceHistoryClient()
    gamma = GammaMarketClient()
    pyth = PythBenchmarksClient(api_key=pyth_api_key)
    feed_ids: dict[str, str] = {}
    final_cache: dict[tuple[str, str], Mapping[str, object]] = {}
    completed = skipped = 0
    failures: list[Mapping[str, object]] = []
    for index, item in enumerate(items):
        contract = _contract_from_payload(item)
        stem = f"{contract.market_id}_{contract.symbol}_{contract.market_date}_above_x"
        settlement_path = output_dir / f"{stem}_settlement.json"
        if _above_x_files_complete(contract, output_dir):
            skipped += 1
            continue
        try:
            settlement = gamma.get_market_settlement(contract.market_id)
            winner = settlement.winning_outcome.upper() if settlement.winning_outcome else ""
            if not settlement.closed or winner not in {"YES", "NO"}:
                raise ValueError("Gamma does not provide a closed Yes/No settlement")
            start_at = datetime.combine(
                datetime.fromisoformat(contract.market_date).date(),
                time(9, 30),
                tzinfo=NEW_YORK,
            ).astimezone(UTC)
            yes_history = clob.prices_history(
                contract.yes_token_id,
                start_at=start_at,
                end_at=contract.resolves_at,
            )
            no_history = clob.prices_history(
                contract.no_token_id,
                start_at=start_at,
                end_at=contract.resolves_at,
            )
            cache_key = (contract.symbol, contract.market_date)
            final_payload = final_cache.get(cache_key)
            if final_payload is None:
                final_payload = _local_pyth_final(contract, pyth_data_dir) if pyth_data_dir else None
                if final_payload is None:
                    feed_id = feed_ids.get(contract.symbol)
                    if feed_id is None:
                        feed_id = pyth.equity_feed_id(contract.symbol)
                        feed_ids[contract.symbol] = feed_id
                    final_payload = pyth.price_at(
                        symbol=contract.symbol,
                        feed_id=feed_id,
                        observed_at=contract.resolves_at,
                    ).as_payload()
                final_cache[cache_key] = final_payload
            _write_price_history(output_dir / f"{stem}_yes_clob.csv", yes_history)
            _write_price_history(output_dir / f"{stem}_no_clob.csv", no_history)
            (output_dir / f"{stem}_market.json").write_text(
                json.dumps(contract.as_payload(), sort_keys=True, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            (output_dir / f"{stem}_pyth_final.json").write_text(
                json.dumps(final_payload, sort_keys=True, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            settlement_path.write_text(
                json.dumps(
                    {
                        "market_id": contract.market_id,
                        "winning_outcome": winner,
                        "provider": "POLYMARKET_GAMMA",
                        "payload": settlement.raw_payload,
                    },
                    sort_keys=True,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            completed += 1
        except Exception as error:
            failures.append(
                {
                    "market_id": contract.market_id,
                    "symbol": contract.symbol,
                    "market_date": contract.market_date,
                    "error": str(error),
                }
            )
        if pause_seconds and index + 1 < len(items):
            time_module.sleep(pause_seconds)
    return AboveXBackfillReport(len(items), completed, skipped, len(failures), tuple(failures))


def _above_x_files_complete(contract: PriceLadderContract, output_dir: Path) -> bool:
    stem = f"{contract.market_id}_{contract.symbol}_{contract.market_date}_above_x"
    return all(
        (output_dir / f"{stem}{suffix}").is_file()
        for suffix in (
            "_yes_clob.csv",
            "_no_clob.csv",
            "_market.json",
            "_pyth_final.json",
            "_settlement.json",
        )
    )


def _contract_from_payload(payload: Mapping[str, object]) -> PriceLadderContract:
    resolves_at = datetime.fromisoformat(str(payload["resolves_at"]).replace("Z", "+00:00"))
    return PriceLadderContract(
        market_id=str(payload["market_id"]),
        event_id=str(payload.get("event_id", "")),
        event_slug=str(payload.get("event_slug", "")),
        symbol=str(payload["symbol"]).upper(),
        strike=float(payload["strike"]),
        market_date=str(payload["market_date"]),
        resolves_at=resolves_at,
        pyth_feed=str(payload["pyth_feed"]),
        yes_token_id=str(payload["yes_token_id"]),
        no_token_id=str(payload["no_token_id"]),
        question=str(payload["question"]),
        rules_hash=str(payload["rules_hash"]),
        raw_payload=payload.get("raw_payload", {}) if isinstance(payload.get("raw_payload"), Mapping) else {},
    )


def _write_price_history(path: Path, points: Iterable[PriceHistoryPoint]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("DateTime", "Price"))
        writer.writerows((point.observed_at.isoformat(), point.price) for point in points)


def _date_or_none(value: str | None):
    if value is None or not value.strip():
        return None
    return datetime.fromisoformat(value).date()


def _local_pyth_final(contract: PriceLadderContract, data_dir: Path | None) -> Mapping[str, object] | None:
    if data_dir is None:
        return None
    for path in sorted(data_dir.glob(f"*_{contract.symbol}_{contract.market_date}_pyth_references.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            final = payload.get("final_price")
            if isinstance(final, Mapping) and "price" in final:
                return {
                    "provider": "LOCAL_PYTH_REFERENCE",
                    "symbol": contract.symbol,
                    "market_date": contract.market_date,
                    "price": final["price"],
                    "source": str(path),
                }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None
