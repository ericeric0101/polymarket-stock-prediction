"""Shared CLI runtime helpers with no command dispatch."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from ..baseline import DailyClose
from ..config import Settings
from ..http import PublicApiError
from ..journal import MakerShadowQuote, PaperPosition, ShadowJournal, StoredMarketCandidate, StoredOutcomeToken
from ..logging import log_event
from ..market_discovery import MarketCandidate
from ..polymarket_data import ClobMarketDataClient
from ..realtime import RealtimeBaselineEvaluator
from ..streaming import (
    AlpacaIexStockStream,
    FinnhubStockStream,
    PolymarketMarketStream,
    ShadowStreamCoordinator,
    run_with_reconnect,
)


def _write_optional_json(output: str | None, payload: object) -> None:
    if output:
        Path(output).write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _report_public_api_failure(settings: Settings, event_type: str, error: PublicApiError) -> None:
    message = str(error)
    log_event(settings.log_path, event_type, {"error": message})
    if "CERTIFICATE_VERIFY_FAILED" in message:
        raise SystemExit(
            "Public API TLS verification failed. Configure this Python installation "
            "to trust your network's certificate authority; SSL verification remains enabled."
        )
    raise SystemExit(f"Public API request failed: {message}")


async def _await_with_graceful_shutdown(coroutine: Awaitable[object]) -> bool:
    """Await a long-running coroutine and let its finalizers finish after Ctrl+C."""

    task = asyncio.ensure_future(coroutine)
    try:
        await task
        return False
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True


def _run_async(coroutine: Awaitable[object]) -> None:
    """Let coroutine finalizers close streams before returning a clean Ctrl+C result."""

    try:
        interrupted = asyncio.run(_await_with_graceful_shutdown(coroutine))
    except KeyboardInterrupt:
        # A second Ctrl+C can still interrupt Python's signal runner. The first
        # interrupt is handled by the wrapper above and drains child tasks.
        print("\nStopped cleanly.")
        return
    if interrupted:
        print("\nStopped cleanly.")


def _stream_credentials(spot_provider: str) -> tuple[str, str, str]:
    if spot_provider == "finnhub":
        finnhub_api_key = os.getenv("FINNHUB_API_KEY", "")
        if not finnhub_api_key:
            raise SystemExit("supervise-shadow --spot-provider finnhub requires FINNHUB_API_KEY in .env")
        return "", "", finnhub_api_key
    api_key = os.getenv("ALPACA_API_KEY_ID", "")
    api_secret = os.getenv("ALPACA_API_SECRET_KEY", "")
    if not api_key or not api_secret:
        raise SystemExit(
            "supervise-shadow --spot-provider alpaca requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env"
        )
    return api_key, api_secret, ""


def _paper_position_payload(position: PaperPosition) -> dict[str, object]:
    return {
        "position_id": position.position_id,
        "market_id": position.market_id,
        "symbol": position.symbol,
        "outcome": position.outcome,
        "status": position.status,
        "contracts": position.contracts,
        "entry_ask": position.entry_ask,
        "entry_fee": position.entry_fee,
        "entry_slippage": position.entry_slippage,
        "fair_probability": position.fair_probability,
        "opened_at": position.opened_at.isoformat(),
        "settled_at": position.settled_at.isoformat() if position.settled_at else None,
        "settlement_outcome": position.settlement_outcome,
        "payout": position.payout,
        "realized_pnl": position.realized_pnl,
        "included_in_calibration": position.included_in_calibration,
        "exclusion_reason": position.exclusion_reason,
    }


def _maker_quote_payload(quote: MakerShadowQuote) -> dict[str, object]:
    return {
        "quote_id": quote.quote_id,
        "market_id": quote.market_id,
        "symbol": quote.symbol,
        "outcome": quote.outcome,
        "status": quote.status,
        "limit_price": quote.limit_price,
        "fair_probability": quote.fair_probability,
        "theoretical_edge": quote.theoretical_edge,
        "best_bid": quote.best_bid,
        "best_ask": quote.best_ask,
        "touch_count": quote.touch_count,
        "last_touched_at": quote.last_touched_at.isoformat() if quote.last_touched_at else None,
        "cancelled_at": quote.cancelled_at.isoformat() if quote.cancelled_at else None,
        "cancel_reason": quote.cancel_reason,
    }


def _signal_status(paper_outcome: str | None) -> str:
    return f"PAPER_{paper_outcome}" if paper_outcome else "NO_PAPER_TRADE"


def _print_market_candidates(candidates: Iterable[StoredMarketCandidate | MarketCandidate]) -> None:
    """Render market IDs in terminal output while withholding CLOB token IDs."""

    items = tuple(candidates)
    if not items:
        print("No locally discovered market candidates.")
        return
    print("\nReview-required markets:")
    for candidate in items:
        print(
            f"  market_id={candidate.market_id} | "
            f"outcomes={candidate.outcome_a_label}/{candidate.outcome_b_label} | "
            f"end={candidate.end_date} | "
            f"{candidate.question}"
        )


def _snapshot_market_books(
    journal: ShadowJournal,
    market_id: str,
    outcomes: tuple[StoredOutcomeToken, StoredOutcomeToken],
) -> int:
    """Fetch both published outcome books; this remains public read-only I/O."""

    client = ClobMarketDataClient()
    for outcome in outcomes:
        snapshot = client.get_order_book(outcome.token_id)
        journal.record_order_book_snapshot(market_id, snapshot)
    return len(outcomes)


async def _run_shadow_stream(
    settings: Settings,
    market_id: str,
    token_ids: tuple[str, str],
    symbol: str,
    spot_provider: str,
    api_key: str,
    api_secret: str,
    finnhub_api_key: str,
    resolves_at: datetime,
    closes: list[DailyClose],
    daily_provider: str,
    reference_spot: float,
    reference_spot_observed_at: datetime,
    contract: Mapping[str, object],
    up_fee_rate: float | None,
    down_fee_rate: float | None,
    duration_seconds: float,
    journal: ShadowJournal,
) -> None:
    evaluator = RealtimeBaselineEvaluator(
        market_id=market_id,
        symbol=symbol,
        resolves_at=resolves_at,
        closes=closes,
        spot_provider=spot_provider.upper(),
        up_fee_rate=up_fee_rate,
        down_fee_rate=down_fee_rate,
    )
    coordinator: ShadowStreamCoordinator
    last_skip_reasons: tuple[str, ...] | None = None
    last_skip_logged_at: datetime | None = None
    last_evaluation_recorded_at: datetime | None = None

    async def evaluate_realtime(payload: dict[str, object]) -> None:
        nonlocal last_skip_reasons, last_skip_logged_at, last_evaluation_recorded_at
        now = datetime.now(UTC)
        spot_updated_at = coordinator.freshness.last_spot_at
        book_updated_at = coordinator.freshness.last_book_at
        spot_age = _age_seconds(now, spot_updated_at)
        book_age = _age_seconds(now, book_updated_at)
        evaluation = evaluator.evaluate(
            now=now,
            spot=coordinator.latest_spots.get(symbol),
            up_ask=coordinator.latest_best_asks.get(token_ids[0]),
            down_ask=coordinator.latest_best_asks.get(token_ids[1]),
            up_bid=coordinator.latest_best_bids.get(token_ids[0]),
            down_bid=coordinator.latest_best_bids.get(token_ids[1]),
            reference_spot=reference_spot,
            reference_spot_age_seconds=_age_seconds(now, reference_spot_observed_at),
            spot_age_seconds=spot_age,
            book_age_seconds=book_age,
            stream_ready=coordinator.freshness.ready(now),
            trigger_reasons=tuple(str(reason) for reason in payload.get("reasons", ())),
        )
        result = {**evaluation.as_payload(), "daily_provider": daily_provider, "contract": contract}
        if evaluation.skip_reasons:
            skip_reasons = evaluation.skip_reasons
            should_record = (
                skip_reasons != last_skip_reasons
                or last_skip_logged_at is None
                or (now - last_skip_logged_at).total_seconds() >= 60
            )
            if should_record:
                last_skip_reasons = skip_reasons
                last_skip_logged_at = now
        else:
            last_skip_reasons = None
            last_skip_logged_at = None
            should_record = (
                last_evaluation_recorded_at is None or (now - last_evaluation_recorded_at).total_seconds() >= 60
            )
        if not should_record:
            return
        journal.record_realtime_evaluation(result)
        last_evaluation_recorded_at = now
        log_event(settings.log_path, "REALTIME_BASELINE_EVALUATED", result)
        print(json.dumps(result, sort_keys=True))

    coordinator = ShadowStreamCoordinator(callback=evaluate_realtime)
    if spot_provider == "finnhub":
        spot_stream = FinnhubStockStream(finnhub_api_key)
        spot_callback = coordinator.on_finnhub_message
    else:
        spot_stream = AlpacaIexStockStream(api_key, api_secret)
        spot_callback = coordinator.on_alpaca_message

    async def log_stream_status(payload: dict[str, object]) -> None:
        log_event(settings.log_path, str(payload["event_type"]), {**payload, "market_id": market_id, "symbol": symbol})
        print(json.dumps(payload, sort_keys=True))

    tasks = [
        asyncio.create_task(
            run_with_reconnect(
                "POLYMARKET_MARKET",
                lambda: PolymarketMarketStream().run(token_ids, coordinator.on_polymarket_message),
                log_stream_status,
            )
        ),
        asyncio.create_task(
            run_with_reconnect(
                f"{spot_provider.upper()}_STOCK",
                lambda: spot_stream.run((symbol,), spot_callback),
                log_stream_status,
            )
        ),
    ]
    try:
        if duration_seconds > 0:
            done, _ = await asyncio.wait(tasks, timeout=duration_seconds, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        else:
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.close()


def _age_seconds(now: datetime, observed_at: datetime | None) -> float | None:
    if observed_at is None:
        return None
    return max(0.0, (now - observed_at).total_seconds())
