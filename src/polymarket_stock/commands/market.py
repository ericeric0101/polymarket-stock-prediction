"""Market command handlers."""

from __future__ import annotations

from ..http import PublicApiError
from ..logging import log_event
from ..market_discovery import GammaMarketClient
from ..polymarket_data import ClobMarketDataClient
from .context import CommandContext
from .shared import _print_market_candidates, _report_public_api_failure, _snapshot_market_books


def handle(context: CommandContext) -> None:
    arguments = context.arguments
    settings = context.settings
    journal = context.journal
    if arguments.command == "init-db":
        log_event(
            settings.log_path,
            "PHASE0_JOURNAL_INITIALIZED",
            {"journal_path": str(settings.journal_path), "shadow_mode": settings.shadow_mode},
        )
        print(f"Shadow journal initialized at {settings.journal_path}")
    elif arguments.command == "list-markets":
        candidates = journal.list_market_candidates(arguments.symbol)
        _print_market_candidates(candidates)
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
                    journal, candidate.market_id, journal.get_market_outcome_tokens(candidate.market_id)
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
            f"stored {len(report.candidates)} review-required candidate(s) and "
            f"{book_snapshots} order-book snapshot(s)"
        )
        _print_market_candidates(report.candidates)
    else:
        raise AssertionError(f"Unexpected command for handler: {arguments.command}")
