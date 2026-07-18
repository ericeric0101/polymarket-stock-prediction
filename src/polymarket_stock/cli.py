"""Phase 0 command-line entry point. It exposes no trading command."""

from __future__ import annotations

import argparse

from .alpaca_options import AlpacaCredentials, AlpacaIndicativeOptionsClient
from .config import Settings
from .http import PublicApiError
from .journal import ShadowJournal
from .logging import log_event
from .market_discovery import GammaMarketClient
from .polymarket_data import ClobMarketDataClient


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
    book_parser = subparsers.add_parser("snapshot-book", help="store one public CLOB order-book snapshot")
    book_parser.add_argument("--market-id", required=True)
    book_parser.add_argument("--token-id", required=True)
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
        log_event(
            settings.log_path,
            "EQUITY_EVENT_SCAN_COMPLETED",
            {
                "candidate_count": len(report.candidates),
                "events_scanned": report.events_scanned,
                "pages_scanned": report.pages_scanned,
                "review_status": "REVIEW_REQUIRED",
                "tag_slugs": report.tag_slugs,
            },
        )
        print(
            f"Scanned {report.events_scanned} event(s) across {report.pages_scanned} page(s); "
            f"stored {len(report.candidates)} review-required candidate(s)"
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


if __name__ == "__main__":
    main()
