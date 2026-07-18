"""Phase 0 command-line entry point. It exposes no trading command."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path

from .alpaca_options import AlpacaCredentials, AlpacaIndicativeOptionsClient
from .baseline import daily_close_data_is_fresh, evaluate_realized_vol_baseline, load_daily_closes_csv
from .config import Settings
from .http import PublicApiError
from .journal import ShadowJournal
from .logging import log_event
from .market_discovery import GammaMarketClient
from .nasdaq_data import NasdaqBaselineClient, load_baseline_cache, save_baseline_cache
from .polymarket_data import ClobMarketDataClient
from .streaming import AlpacaIexStockStream, PolymarketMarketStream, ShadowStreamCoordinator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket stock shadow research tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="initialize the local shadow journal")
    scan_parser = subparsers.add_parser("scan-markets", help="discover review-required daily equity candidates")
    scan_parser.add_argument("--symbols", default="SPY,QQQ,AAPL,NVDA,TSLA")
    scan_parser.add_argument("--limit", type=int, default=200)
    event_parser = subparsers.add_parser("scan-event", help="discover candidates from one exact Gamma event slug")
    event_parser.add_argument("--slug", required=True)
    event_parser.add_argument("--symbols", default="SPY,QQQ,AAPL,NVDA,TSLA")
    equity_parser = subparsers.add_parser("scan-equity-events", help="cursor-scan active tagged equity daily-direction events")
    equity_parser.add_argument("--tag-slugs", default="stocks,equities")
    equity_parser.add_argument("--page-size", type=int, default=500)
    equity_parser.add_argument("--max-pages-per-tag", type=int, default=100)
    equity_parser.add_argument("--pause-seconds", type=float, default=0.2)
    equity_parser.add_argument("--snapshot-books", action="store_true", help="also snapshot both outcome books for each candidate")
    book_parser = subparsers.add_parser("snapshot-book", help="store one public CLOB order-book snapshot")
    book_parser.add_argument("--market-id", required=True)
    book_parser.add_argument("--token-id", required=True)
    market_book_parser = subparsers.add_parser("snapshot-market", help="store both order books for one discovered market")
    market_book_parser.add_argument("--market-id", required=True)
    baseline_parser = subparsers.add_parser("evaluate-baseline", help="compare realized-vol baseline with saved Up/Down asks")
    baseline_parser.add_argument("--market-id", required=True)
    baseline_parser.add_argument("--history-csv", required=True)
    baseline_parser.add_argument("--spot", required=True, type=float)
    baseline_parser.add_argument("--resolves-at", required=True, help="ISO-8601 timestamp, e.g. 2026-07-20T20:00:00Z")
    baseline_parser.add_argument("--lookback-days", type=int, default=20)
    nasdaq_baseline_parser = subparsers.add_parser("evaluate-nasdaq-baseline", help="automatic free Nasdaq realized-vol baseline")
    nasdaq_baseline_parser.add_argument("--market-id", required=True)
    nasdaq_baseline_parser.add_argument("--symbol", required=True)
    nasdaq_baseline_parser.add_argument("--resolves-at", required=True)
    stream_parser = subparsers.add_parser("stream-shadow", help="read-only Polymarket and Alpaca IEX live streams")
    stream_parser.add_argument("--market-id", required=True)
    stream_parser.add_argument("--symbol", required=True)
    stream_parser.add_argument("--duration-seconds", type=float, default=0, help="0 runs until interrupted")
    alpaca_parser = subparsers.add_parser("snapshot-alpaca-options", help="store free Alpaca indicative option quotes")
    alpaca_parser.add_argument("--symbols", required=True, help="comma-separated OCC option symbols, maximum 100")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    settings = Settings.from_environment()
    journal = ShadowJournal(settings.journal_path)
    journal.initialize()
    if arguments.command == "init-db":
        log_event(
            settings.log_path,
            "PHASE0_JOURNAL_INITIALIZED",
            {"journal_path": str(settings.journal_path), "shadow_mode": settings.shadow_mode},
        )
        print(f"Shadow journal initialized at {settings.journal_path}")
    elif arguments.command == "scan-markets":
        symbols = tuple(symbol.strip().upper() for symbol in arguments.symbols.split(",") if symbol.strip())
        try:
            candidates = GammaMarketClient().discover_daily_equity_candidates(symbols, limit=arguments.limit)
        except PublicApiError as error:
            _report_public_api_failure(settings, "MARKET_SCAN_FAILED", error)
        for candidate in candidates:
            journal.upsert_market_candidate(candidate)
        log_event(
            settings.log_path,
            "MARKET_SCAN_COMPLETED",
            {"candidate_count": len(candidates), "review_status": "REVIEW_REQUIRED", "symbols": symbols},
        )
        print(f"Stored {len(candidates)} review-required candidate(s)")
    elif arguments.command == "snapshot-book":
        try:
            snapshot = ClobMarketDataClient().get_order_book(arguments.token_id)
        except PublicApiError as error:
            _report_public_api_failure(settings, "ORDER_BOOK_SNAPSHOT_FAILED", error)
        journal.record_order_book_snapshot(arguments.market_id, snapshot)
        log_event(
            settings.log_path,
            "ORDER_BOOK_SNAPSHOT_RECORDED",
            {"market_id": arguments.market_id, "token_id": arguments.token_id, "best_ask": snapshot.best_ask},
        )
        print(f"Stored order-book snapshot for {arguments.token_id}")
    elif arguments.command == "snapshot-market":
        try:
            outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        except KeyError as error:
            raise SystemExit(f"Unknown market id: {error}") from error
        stored_count = _snapshot_market_books(journal, arguments.market_id, outcomes)
        print(f"Stored {stored_count} order-book snapshot(s) for market {arguments.market_id}")
    elif arguments.command == "evaluate-baseline":
        now = datetime.now(UTC)
        resolves_at = datetime.fromisoformat(arguments.resolves_at.replace("Z", "+00:00"))
        closes = load_daily_closes_csv(Path(arguments.history_csv))
        up_ask, down_ask = journal.get_latest_outcome_asks(arguments.market_id)
        data_is_fresh = daily_close_data_is_fresh(closes, now)
        assessment = evaluate_realized_vol_baseline(
            spot=arguments.spot, closes=closes, seconds_to_resolution=(resolves_at - now).total_seconds(),
            up_ask=up_ask, down_ask=down_ask, fee_rate=0.01, slippage=0.001,
            base_model_error_buffer=0.02, fallback_buffer=0.05, minimum_edge=0.02,
            data_is_fresh=data_is_fresh, lookback_days=arguments.lookback_days,
        )
        result = {
            "market_id": arguments.market_id,
            "fair_up_probability": round(assessment.fair_up_probability, 6),
            "up_ask": up_ask,
            "down_ask": down_ask,
            "realized_volatility": round(assessment.annualized_realized_volatility, 6),
            "prior_close": assessment.prior_close,
            "data_is_fresh": assessment.data_is_fresh,
            "model_error_buffer": assessment.model_error_buffer,
            "paper_outcome": assessment.paper_outcome,
        }
        log_event(settings.log_path, "BASELINE_EVALUATED", result)
        print(json.dumps(result, sort_keys=True))
    elif arguments.command == "evaluate-nasdaq-baseline":
        now = datetime.now(UTC)
        resolves_at = datetime.fromisoformat(arguments.resolves_at.replace("Z", "+00:00"))
        _snapshot_market_books(journal, arguments.market_id, journal.get_market_outcome_tokens(arguments.market_id))
        cache_path = Path("data") / "baseline_cache" / f"{arguments.symbol.upper()}.json"
        provider = "NASDAQ_PUBLIC_NON_SETTLEMENT"
        try:
            client = NasdaqBaselineClient()
            quote = client.latest_quote(arguments.symbol)
            closes = client.daily_closes(arguments.symbol, now)
            save_baseline_cache(cache_path, quote, closes)
        except PublicApiError:
            quote, closes = load_baseline_cache(cache_path)
            provider = "NASDAQ_LOCAL_CACHE_NON_SETTLEMENT"
        data_is_fresh = daily_close_data_is_fresh(closes, now) and daily_close_data_is_fresh(
            [type(closes[-1])(quote.last_trade_at.date().isoformat(), quote.price)], now
        )
        up_ask, down_ask = journal.get_latest_outcome_asks(arguments.market_id)
        assessment = evaluate_realized_vol_baseline(
            spot=quote.price, closes=closes, seconds_to_resolution=(resolves_at - now).total_seconds(),
            up_ask=up_ask, down_ask=down_ask, fee_rate=0.01, slippage=0.001,
            base_model_error_buffer=0.02, fallback_buffer=0.05, minimum_edge=0.02,
            data_is_fresh=data_is_fresh, lookback_days=20,
        )
        result = {
            "market_id": arguments.market_id, "symbol": quote.symbol, "spot": quote.price,
            "spot_last_trade_at": quote.last_trade_at.isoformat(), "spot_is_real_time": quote.is_real_time,
            "fair_up_probability": round(assessment.fair_up_probability, 6), "up_ask": up_ask,
            "down_ask": down_ask, "prior_close": assessment.prior_close,
            "realized_volatility": round(assessment.annualized_realized_volatility, 6),
            "data_is_fresh": assessment.data_is_fresh, "model_error_buffer": assessment.model_error_buffer,
            "paper_outcome": assessment.paper_outcome, "provider": provider,
        }
        log_event(settings.log_path, "NASDAQ_BASELINE_EVALUATED", result)
        print(json.dumps(result, sort_keys=True))
    elif arguments.command == "stream-shadow":
        api_key = os.getenv("ALPACA_API_KEY_ID", "")
        api_secret = os.getenv("ALPACA_API_SECRET_KEY", "")
        if not api_key or not api_secret:
            raise SystemExit("stream-shadow requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env")
        outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        asyncio.run(_run_shadow_stream(settings, arguments.market_id, tuple(item.token_id for item in outcomes), arguments.symbol.upper(), api_key, api_secret, arguments.duration_seconds))
    elif arguments.command == "scan-event":
        symbols = tuple(symbol.strip().upper() for symbol in arguments.symbols.split(",") if symbol.strip())
        try:
            candidates = GammaMarketClient().discover_event_candidates(arguments.slug, symbols)
        except PublicApiError as error:
            _report_public_api_failure(settings, "EVENT_SCAN_FAILED", error)
        for candidate in candidates:
            journal.upsert_market_candidate(candidate)
        log_event(
            settings.log_path,
            "EVENT_SCAN_COMPLETED",
            {"candidate_count": len(candidates), "review_status": "REVIEW_REQUIRED", "slug": arguments.slug},
        )
        print(f"Stored {len(candidates)} review-required candidate(s) from {arguments.slug}")
    elif arguments.command == "scan-equity-events":
        tag_slugs = tuple(tag.strip() for tag in arguments.tag_slugs.split(",") if tag.strip())
        try:
            report = GammaMarketClient().discover_active_equity_candidates(
                tag_slugs=tag_slugs,
                page_size=arguments.page_size,
                max_pages_per_tag=arguments.max_pages_per_tag,
                pause_seconds=arguments.pause_seconds,
            )
        except PublicApiError as error:
            _report_public_api_failure(settings, "EQUITY_EVENT_SCAN_FAILED", error)
        for candidate in report.candidates:
            journal.upsert_market_candidate(candidate)
        book_snapshots = 0
        if arguments.snapshot_books:
            for candidate in report.candidates:
                book_snapshots += _snapshot_market_books(
                    journal,
                    candidate.market_id,
                    journal.get_market_outcome_tokens(candidate.market_id),
                )
        log_event(
            settings.log_path,
            "EQUITY_EVENT_SCAN_COMPLETED",
            {
                "candidate_count": len(report.candidates),
                "events_scanned": report.events_scanned,
                "pages_scanned": report.pages_scanned,
                "review_status": "REVIEW_REQUIRED",
                "tag_slugs": report.tag_slugs,
                "order_book_snapshot_count": book_snapshots,
            },
        )
        print(
            f"Scanned {report.events_scanned} event(s) across {report.pages_scanned} page(s); "
            f"stored {len(report.candidates)} review-required candidate(s) and {book_snapshots} order-book snapshot(s)"
        )
    elif arguments.command == "snapshot-alpaca-options":
        symbols = tuple(symbol.strip() for symbol in arguments.symbols.split(",") if symbol.strip())
        quotes = AlpacaIndicativeOptionsClient(AlpacaCredentials.from_environment()).latest_quotes(symbols)
        for quote in quotes:
            journal.record_alpaca_indicative_option_quote(quote)
        log_event(
            settings.log_path,
            "ALPACA_INDICATIVE_QUOTES_RECORDED",
            {"requested_symbol_count": len(symbols), "returned_quote_count": len(quotes), "feed": "indicative"},
        )
        print(f"Stored {len(quotes)} Alpaca indicative option quote(s)")


def _report_public_api_failure(settings: Settings, event_type: str, error: PublicApiError) -> None:
    message = str(error)
    log_event(settings.log_path, event_type, {"error": message})
    if "CERTIFICATE_VERIFY_FAILED" in message:
        raise SystemExit(
            "Public API TLS verification failed. Configure this Python installation "
            "to trust your network's certificate authority; SSL verification remains enabled."
        )
    raise SystemExit(f"Public API request failed: {message}")


def _snapshot_market_books(journal: ShadowJournal, market_id: str, outcomes: tuple[object, object]) -> int:
    """Fetch both published outcome books; this remains public read-only I/O."""

    client = ClobMarketDataClient()
    for outcome in outcomes:
        snapshot = client.get_order_book(getattr(outcome, "token_id"))
        journal.record_order_book_snapshot(market_id, snapshot)
    return len(outcomes)


async def _run_shadow_stream(settings: Settings, market_id: str, token_ids: tuple[str, str], symbol: str, api_key: str, api_secret: str, duration_seconds: float) -> None:
    async def log_revaluation(payload: dict[str, object]) -> None:
        log_event(settings.log_path, str(payload["event_type"]), {**payload, "market_id": market_id, "symbol": symbol})

    coordinator = ShadowStreamCoordinator(callback=log_revaluation)
    tasks = [
        asyncio.create_task(PolymarketMarketStream().run(token_ids, coordinator.on_polymarket_message)),
        asyncio.create_task(AlpacaIexStockStream(api_key, api_secret).run((symbol,), coordinator.on_alpaca_message)),
    ]
    try:
        if duration_seconds > 0:
            await asyncio.wait(tasks, timeout=duration_seconds, return_when=asyncio.FIRST_EXCEPTION)
        else:
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.close()


if __name__ == "__main__":
    main()
