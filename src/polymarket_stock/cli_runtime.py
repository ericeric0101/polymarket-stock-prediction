"""Runtime CLI handlers kept separate from parser construction and research commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
import ssl
from typing import Awaitable

from .config import Settings
from .journal import ShadowJournal
from .paper_reporting import paper_performance
from .price_ladder_collector import PriceLadderCollector
from .price_ladder_journal import PriceLadderJournal
from .probability_calibration import sizing_readiness
from .reporting import make_event_sink, render_dashboard, run_live_dashboard
from .research_web import ResearchDashboardServer
from .supervisor import MultiMarketShadowSupervisor


def supervise_shadow(
    arguments: argparse.Namespace, settings: Settings, journal: ShadowJournal,
    stream_credentials: Callable[[str], tuple[str, str, str]], run_async: Callable[[Awaitable[None]], None],
) -> None:
    api_key, api_secret, finnhub_api_key = stream_credentials(arguments.spot_provider)
    supervisor = MultiMarketShadowSupervisor(
        journal=journal, log_path=settings.log_path, spot_provider=arguments.spot_provider,
        volatility_estimator=arguments.volatility_estimator, volatility_decay=arguments.volatility_decay,
        comparison_estimators=tuple(item.strip() for item in arguments.comparison_estimators.split(",") if item.strip()),
        finnhub_api_key=finnhub_api_key, alpaca_api_key=api_key, alpaca_api_secret=api_secret,
        max_markets=arguments.max_markets, minimum_seconds_to_resolution=arguments.minimum_seconds_to_resolution,
        maker_minimum_edge=arguments.maker_minimum_edge,
        maker_reprice_minimum_price_change=arguments.maker_reprice_minimum_price_change,
        maker_minimum_quote_lifetime_seconds=arguments.maker_minimum_quote_lifetime_seconds,
        paper_batch_seconds=arguments.paper_batch_seconds, max_daily_paper_entries=arguments.max_daily_paper_entries,
        max_per_risk_group=arguments.max_per_risk_group,
        max_same_direction_paper_entries=arguments.max_same_direction_paper_entries,
        pyth_api_key=os.getenv("PYTH_API_KEY", ""), pyth_pro_api_key=os.getenv("PYTH_PRO_API_KEY", ""),
        spot_mode=arguments.spot_mode, finnhub_threshold_safety_bps=arguments.finnhub_threshold_safety_bps,
        tradier_api_token=os.getenv("TRADIER_API_TOKEN", ""), polygon_api_key=os.getenv("POLYGON_API_KEY", ""),
        event_sink=make_event_sink(settings.log_path, arguments.output_format),
    )
    try:
        run_async(supervisor.run(arguments.scan_interval_seconds, arguments.duration_seconds))
    except ssl.SSLCertVerificationError as error:
        raise SystemExit(
            "Supervisor TLS verification failed. Set SSL_CERT_FILE in .env to the PEM file for your "
            "VPN or proxy certificate authority; SSL verification remains enabled."
        ) from error


def dashboard(arguments: argparse.Namespace, journal: ShadowJournal) -> None:
    positions = journal.list_paper_positions()
    if arguments.once:
        print(render_dashboard(
            journal.dashboard_rows(arguments.limit),
            sum(item.status == "OPEN" for item in positions),
            sum(item.status == "SETTLED" for item in positions),
            positions=positions, signal_performance=journal.first_signal_performance(),
            sizing=sizing_readiness(journal.list_first_signal_calibration_observations()),
            daily_entry_limit=arguments.daily_entry_limit,
        ))
        return
    run_live_dashboard(
        journal, refresh_seconds=arguments.refresh_seconds, limit=arguments.limit,
        daily_entry_limit=arguments.daily_entry_limit,
    )


def price_ladders(
    arguments: argparse.Namespace, settings: Settings, report_public_api_failure: Callable[[Settings, str, Exception], None],
) -> None:
    symbols = tuple(symbol.strip().upper() for symbol in getattr(arguments, "symbols", "").split(",") if symbol.strip())
    journal = PriceLadderJournal(settings.journal_path)
    journal.initialize()
    collector = PriceLadderCollector(journal=journal)
    try:
        if arguments.command == "discover-price-ladders":
            print(json.dumps(collector.discover_and_store(symbols=symbols).as_payload(), sort_keys=True))
        elif arguments.command == "collect-price-ladders":
            collector.run(symbols=symbols, interval_seconds=arguments.interval_seconds, duration_seconds=arguments.duration_seconds)
        else:
            print(json.dumps(collector.settle_stored_contracts(), sort_keys=True))
    except Exception as error:
        from .http import PublicApiError
        if not isinstance(error, PublicApiError):
            raise
        report_public_api_failure(settings, "PRICE_LADDER_PUBLIC_API_FAILED", error)


def research_dashboard(arguments: argparse.Namespace, settings: Settings) -> None:
    ResearchDashboardServer(
        settings.journal_path, host=arguments.host, port=arguments.port,
        limit=arguments.limit, daily_entry_limit=arguments.daily_entry_limit,
    ).serve_forever()


def settle_paper_positions(
    settings: Settings, journal: ShadowJournal, run_async: Callable[[Awaitable[None]], None],
) -> None:
    supervisor = MultiMarketShadowSupervisor(
        journal=journal, log_path=settings.log_path, spot_provider="finnhub",
        tradier_api_token=os.getenv("TRADIER_API_TOKEN", ""),
    )
    run_async(supervisor.settle_open_positions())


def paper_performance_payload(journal: ShadowJournal) -> str:
    return json.dumps(paper_performance(journal.list_paper_positions()).as_payload(), sort_keys=True)
