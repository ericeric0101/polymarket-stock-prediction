"""Phase 0 command-line entry point. It exposes no trading command."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
import ssl

from .alpaca_options import AlpacaCredentials, AlpacaIndicativeOptionsClient
from .baseline import DailyClose, daily_close_data_is_fresh, evaluate_realized_vol_baseline, load_daily_bars_csv, load_daily_closes_csv
from .batch_backfill import backfill_discovered_markets
from .buffer_sweep import buffer_values, run_buffer_sweep, walk_forward_buffer_sweep
from .checkpoints import CHECKPOINTS
from .calibration import calibrate_checkpoint_observations, calibrate_market_observations, calibrate_settled_positions, write_calibration_recommendation
from .clob_history import ClobPriceHistoryClient
from .config import Settings
from .cross_market import cross_market_report
from .equity_contracts import EquityContractParseError, parse_daily_equity_close_contract
from .fees import PolymarketFeeRateClient
from .historical_backtest import load_intraday_spots_csv, replay_daily_up_down_market
from .http import PublicApiError
from .intraday_spot_backfill import backfill_pyth_intraday_spots
from .journal import ShadowJournal
from .logging import log_event
from .market_discovery import GammaMarketClient, MarketCandidate
from .nasdaq_data import NasdaqBaselineClient, NasdaqPayloadError, load_baseline_cache, save_baseline_cache
from .option_pricing_validation import OptionPricingInputs, validate_option_quote
from .polymarket_data import ClobMarketDataClient
from .paper_reporting import paper_performance
from .probability_calibration import sizing_readiness, stratified_first_signal_calibration, walk_forward_probability_calibration
from .price_ladder_collector import PriceLadderCollector
from .price_ladder_journal import PriceLadderJournal
from .pyth_clob_backtest import run_pyth_clob_backtest
from .replay import replay_market_observations, replay_settled_positions
from .reporting import make_event_sink, render_dashboard, run_live_dashboard
from .research_web import ResearchDashboardServer
from .realtime import RealtimeBaselineEvaluator
from .settled_market_data import backfill_settled_market_data
from .streaming import AlpacaIexStockStream, FinnhubStockStream, PolymarketMarketStream, ShadowStreamCoordinator, run_with_reconnect
from .top5_walk_forward import (
    parse_checkpoint_sets, parse_probability_values, top_five_policies, walk_forward_top_five_policy,
)
from .strategy_diagnostics import strategy_diagnostics
from .supervisor import MultiMarketShadowSupervisor
from .yahoo_data import YahooChartClient, YahooPayloadError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket stock shadow research tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="initialize the local shadow journal")
    list_parser = subparsers.add_parser("list-markets", help="list locally discovered review-required markets and IDs")
    list_parser.add_argument("--symbol", help="optional ticker filter, for example TSLA")
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
    baseline_parser.add_argument("--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA", "GARMAN_KLASS", "YANG_ZHANG"), default="CLOSE_TO_CLOSE")
    baseline_parser.add_argument("--volatility-decay", type=float, default=0.94)
    baseline_parser.add_argument("--ohlc-history-csv", help="optional Date,Open,High,Low,Close CSV for OHLC estimators")
    yahoo_parser = subparsers.add_parser("download-yahoo-closes", help="download non-settlement Yahoo daily closes to Date,Close CSV")
    yahoo_parser.add_argument("--symbol", required=True)
    yahoo_parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    yahoo_parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    yahoo_parser.add_argument("--output", required=True)
    settled_data_parser = subparsers.add_parser("backfill-settled-market-data", help="download Pyth references and Yahoo intraday inputs for one settled market")
    settled_data_parser.add_argument("--market-id", required=True)
    settled_data_parser.add_argument("--output-dir", default="data/historical")
    settled_data_parser.add_argument("--lookback-calendar-days", type=int, default=45)
    batch_backfill_parser = subparsers.add_parser("batch-backfill-settled-markets", help="resumable Pyth, CLOB, and Gamma settlement backfill from discovery JSON")
    batch_backfill_parser.add_argument("--discovery-json", required=True)
    batch_backfill_parser.add_argument("--output-dir", default="data/historical")
    batch_backfill_parser.add_argument("--start-offset", type=int, default=0)
    batch_backfill_parser.add_argument("--max-markets", type=int)
    batch_backfill_parser.add_argument("--pause-seconds", type=float, default=0.2)
    batch_backfill_parser.add_argument("--pyth-pause-seconds", type=float, default=2.0)
    pyth_intraday_parser = subparsers.add_parser("backfill-pyth-intraday-spots", help="resumable Pyth Pro one-minute underlying spots for settled markets")
    pyth_intraday_parser.add_argument("--discovery-json", required=True)
    pyth_intraday_parser.add_argument("--output-dir", default="data/historical/90d")
    pyth_intraday_parser.add_argument("--symbols", default="NVDA,TSLA")
    pyth_intraday_parser.add_argument("--pause-seconds", type=float, default=0.25)
    pyth_clob_parser = subparsers.add_parser("backtest-pyth-clob", help="non-leaking Pyth minute-spot and CLOB-history batch replay")
    pyth_clob_parser.add_argument("--data-dir", default="data/historical/90d")
    pyth_clob_parser.add_argument("--minimum-buffer", type=float, default=0.01)
    pyth_clob_parser.add_argument("--maximum-buffer", type=float, default=0.02)
    pyth_clob_parser.add_argument("--buffer-step", type=float, default=0.01)
    pyth_clob_parser.add_argument("--minimum-edge", type=float, default=0.02)
    pyth_clob_parser.add_argument("--lookback-days", type=int, default=20)
    pyth_clob_parser.add_argument("--training-days", type=int, default=20)
    pyth_clob_parser.add_argument("--validation-days", type=int, default=5)
    pyth_clob_parser.add_argument("--minimum-training-trades", type=int, default=10)
    pyth_clob_parser.add_argument("--fee-rate", type=float, default=0.0, help="historical fee-rate assumption; 0 reports pre-fee PnL")
    pyth_clob_parser.add_argument("--output", help="optional JSON report output path")
    nasdaq_baseline_parser = subparsers.add_parser("evaluate-nasdaq-baseline", help="automatic free Nasdaq realized-vol baseline")
    nasdaq_baseline_parser.add_argument("--market-id", required=True)
    nasdaq_baseline_parser.add_argument("--symbol", required=True)
    nasdaq_baseline_parser.add_argument("--resolves-at", required=True)
    nasdaq_baseline_parser.add_argument("--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA"), default="CLOSE_TO_CLOSE")
    nasdaq_baseline_parser.add_argument("--volatility-decay", type=float, default=0.94)
    stream_parser = subparsers.add_parser("stream-shadow", help="read-only Polymarket and stock-quote live streams")
    stream_parser.add_argument("--market-id", required=True)
    stream_parser.add_argument("--symbol", required=True)
    stream_parser.add_argument("--spot-provider", choices=("finnhub", "alpaca"), default="finnhub")
    stream_parser.add_argument("--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA"), default="CLOSE_TO_CLOSE")
    stream_parser.add_argument("--volatility-decay", type=float, default=0.94)
    stream_parser.add_argument("--resolves-at", help="ISO-8601 resolution timestamp; defaults to the discovered market end date")
    stream_parser.add_argument("--duration-seconds", type=float, default=0, help="0 runs until interrupted")
    supervisor_parser = subparsers.add_parser("supervise-shadow", help="scheduled multi-market shadow observation and paper lifecycle")
    supervisor_parser.add_argument("--spot-provider", choices=("finnhub", "alpaca"), default="finnhub")
    supervisor_parser.add_argument("--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA"), default="CLOSE_TO_CLOSE")
    supervisor_parser.add_argument("--volatility-decay", type=float, default=0.94)
    supervisor_parser.add_argument("--comparison-estimators", default="EWMA", help="comma-separated shadow comparison estimators; default EWMA")
    supervisor_parser.add_argument("--scan-interval-seconds", type=float, default=900)
    supervisor_parser.add_argument("--max-markets", type=int, default=18)
    supervisor_parser.add_argument("--minimum-seconds-to-resolution", type=float, default=900)
    supervisor_parser.add_argument("--maker-minimum-edge", type=float, default=0.005, help="minimum unfilled maker edge, default 0.005")
    supervisor_parser.add_argument("--maker-reprice-minimum-price-change", type=float, default=0.02, help="minimum maker limit-price change before reprice, default 0.02")
    supervisor_parser.add_argument("--maker-minimum-quote-lifetime-seconds", type=float, default=30.0, help="minimum seconds an active maker quote remains before reprice, default 30")
    supervisor_parser.add_argument("--paper-batch-seconds", type=float, default=30.0)
    supervisor_parser.add_argument("--max-daily-paper-entries", type=int, default=5)
    supervisor_parser.add_argument("--max-per-risk-group", type=int, default=1)
    supervisor_parser.add_argument("--max-same-direction-paper-entries", type=int, default=2)
    supervisor_parser.add_argument("--duration-seconds", type=float, default=0, help="0 runs until interrupted")
    supervisor_parser.add_argument("--output-format", choices=("human", "json"), default="human")
    positions_parser = subparsers.add_parser("paper-positions", help="list open or settled hold-to-resolution paper positions")
    positions_parser.add_argument("--status", choices=("OPEN", "SETTLED"))
    maker_quotes_parser = subparsers.add_parser("maker-shadow-quotes", help="list active or cancelled maker shadow quotes")
    maker_quotes_parser.add_argument("--status", choices=("ACTIVE", "CANCELLED"), default="ACTIVE")
    portfolio_parser = subparsers.add_parser("portfolio-decisions", help="list batched paper-entry selections and rejections")
    portfolio_parser.add_argument("--limit", type=int, default=100)
    subparsers.add_parser("paper-performance", help="report realized paper PnL and calibration for settled positions")
    replay_parser = subparsers.add_parser("replay-settled", help="replay immutable paper entries against official settled outcomes")
    replay_parser.add_argument("--output", help="optional JSON report output path")
    historical_parser = subparsers.add_parser("historical-backtest", help="offline replay of one daily Up/Down market from CLOB price history")
    historical_parser.add_argument("--market-id", required=True)
    historical_parser.add_argument("--symbol", required=True)
    historical_parser.add_argument("--history-csv", required=True, help="Date,Close CSV ending with prior close and final close")
    historical_parser.add_argument("--spot-csv", help="optional DateTime,Spot intraday CSV; required for simulated trades")
    historical_parser.add_argument("--start-at", required=True, help="ISO-8601 history start timestamp")
    historical_parser.add_argument("--end-at", help="ISO-8601 history end timestamp; defaults to market resolution")
    historical_parser.add_argument("--minimum-edge", type=float, default=0.02)
    historical_parser.add_argument("--model-error-buffer", type=float, default=0.02)
    historical_parser.add_argument("--lookback-days", type=int, default=20)
    historical_parser.add_argument("--output", help="optional JSON report output path")
    subparsers.add_parser("replay-observations", help="replay all valid market observations against official outcomes")
    calibration_parser = subparsers.add_parser("calibrate-paper", help="derive conservative settings from settled paper positions")
    calibration_parser.add_argument("--write", action="store_true", help="write a review-only recommendation to data/model_calibration.json")
    subparsers.add_parser("calibrate-observations", help="calibrate from all settled market observations")
    first_signal_calibration_parser = subparsers.add_parser("calibrate-first-signals", help="stratify selected-side first-signal calibration and sizing readiness")
    first_signal_calibration_parser.add_argument("--output", help="optional JSON report output path")
    probability_walk_forward_parser = subparsers.add_parser("walk-forward-probability-calibration", help="fit selected-side probability shrinkage only on earlier trading dates")
    probability_walk_forward_parser.add_argument("--training-days", type=int, default=20)
    probability_walk_forward_parser.add_argument("--validation-days", type=int, default=5)
    probability_walk_forward_parser.add_argument("--minimum-training-samples", type=int, default=50)
    probability_walk_forward_parser.add_argument("--output", help="optional JSON report output path")
    subparsers.add_parser("calibrate-checkpoints", help="report immutable checkpoint calibration against official settlements")
    buffer_parser = subparsers.add_parser("buffer-sweep", help="replay one-entry-per-market checkpoint policies across probability buffers")
    buffer_parser.add_argument("--minimum-buffer", type=float, default=0.0)
    buffer_parser.add_argument("--maximum-buffer", type=float, default=0.20)
    buffer_parser.add_argument("--buffer-step", type=float, default=0.01)
    buffer_parser.add_argument("--minimum-edge", type=float, default=0.02)
    buffer_parser.add_argument("--checkpoint", choices=tuple(item[2] for item in CHECKPOINTS))
    buffer_parser.add_argument("--output", help="optional JSON report output path")
    walk_forward_parser = subparsers.add_parser("walk-forward-buffer-sweep", help="select buffers on earlier trading days and evaluate only later days")
    walk_forward_parser.add_argument("--minimum-buffer", type=float, default=0.0)
    walk_forward_parser.add_argument("--maximum-buffer", type=float, default=0.20)
    walk_forward_parser.add_argument("--buffer-step", type=float, default=0.01)
    walk_forward_parser.add_argument("--minimum-edge", type=float, default=0.02)
    walk_forward_parser.add_argument("--checkpoint", choices=tuple(item[2] for item in CHECKPOINTS))
    walk_forward_parser.add_argument("--training-days", type=int, default=20)
    walk_forward_parser.add_argument("--validation-days", type=int, default=5)
    walk_forward_parser.add_argument("--minimum-training-trades", type=int, default=10)
    walk_forward_parser.add_argument("--output", help="optional JSON report output path")
    top_five_walk_forward_parser = subparsers.add_parser(
        "walk-forward-top-five", help="select a capped daily Top-5 checkpoint policy on prior days only",
    )
    top_five_walk_forward_parser.add_argument(
        "--checkpoints", default="1200_EDT,1400_EDT,1530_EDT",
        help="chronological checkpoint names available to the policy search",
    )
    top_five_walk_forward_parser.add_argument(
        "--checkpoint-sets", default="",
        help="optional semicolon-separated policy sets, e.g. 1200_EDT;1200_EDT,1530_EDT",
    )
    top_five_walk_forward_parser.add_argument("--minimum-buffer", type=float, default=0.01)
    top_five_walk_forward_parser.add_argument("--maximum-buffer", type=float, default=0.03)
    top_five_walk_forward_parser.add_argument("--buffer-step", type=float, default=0.01)
    top_five_walk_forward_parser.add_argument("--minimum-edges", default="0.02,0.03,0.05")
    top_five_walk_forward_parser.add_argument("--max-daily-entries", type=int, default=5)
    top_five_walk_forward_parser.add_argument("--training-days", type=int, default=4)
    top_five_walk_forward_parser.add_argument("--validation-days", type=int, default=2)
    top_five_walk_forward_parser.add_argument("--minimum-training-trades", type=int, default=5)
    top_five_walk_forward_parser.add_argument("--raw-probabilities", action="store_true")
    top_five_walk_forward_parser.add_argument("--output", help="optional JSON report output path")
    diagnostics_parser = subparsers.add_parser("strategy-diagnostics", help="model, execution, source, volatility, and exit diagnostics")
    diagnostics_parser.add_argument("--shares", type=float, default=10.0)
    diagnostics_parser.add_argument("--output", help="optional JSON report output path")
    dashboard_parser = subparsers.add_parser("dashboard", help="open the continuously refreshing terminal dashboard")
    dashboard_parser.add_argument("--limit", type=int, default=18)
    dashboard_parser.add_argument("--refresh-seconds", type=float, default=3.0)
    dashboard_parser.add_argument("--daily-entry-limit", type=int, default=5)
    dashboard_parser.add_argument("--once", action="store_true", help="print one plain-text snapshot instead of opening the live dashboard")
    ladder_discovery_parser = subparsers.add_parser(
        "discover-price-ladders", help="discover strict Pyth closes-above contracts for isolated research",
    )
    ladder_discovery_parser.add_argument("--symbols", default="TSLA,NVDA")
    ladder_collection_parser = subparsers.add_parser(
        "collect-price-ladders", help="poll price-ladder books into isolated research tables",
    )
    ladder_collection_parser.add_argument("--symbols", default="TSLA,NVDA")
    ladder_collection_parser.add_argument("--interval-seconds", type=float, default=60.0)
    ladder_collection_parser.add_argument("--duration-seconds", type=float, default=0.0)
    subparsers.add_parser("settle-price-ladders", help="reconcile stored ladder contracts with official outcomes")
    ladder_report_parser = subparsers.add_parser(
        "price-ladder-report", help="compare core checkpoints with isolated ladder probabilities",
    )
    ladder_report_parser.add_argument("--date", help="New York market date, YYYY-MM-DD")
    ladder_report_parser.add_argument("--output", help="optional JSON report output path")
    research_dashboard_parser = subparsers.add_parser(
        "research-dashboard", help="serve a localhost-only core and price-ladder research dashboard",
    )
    research_dashboard_parser.add_argument("--host", default="127.0.0.1")
    research_dashboard_parser.add_argument("--port", type=int, default=8765)
    subparsers.add_parser(
        "settle-paper-positions",
        help="one-shot official reconciliation for open paper positions and model observations",
    )
    alpaca_parser = subparsers.add_parser("snapshot-alpaca-options", help="store free Alpaca indicative option quotes")
    alpaca_parser.add_argument("--symbols", required=True, help="comma-separated OCC option symbols, maximum 100")
    validation_parser = subparsers.add_parser("validate-option-pricing", help="offline BSM/binomial option-pricing cross-check; never creates a signal")
    validation_parser.add_argument("--spot", required=True, type=float)
    validation_parser.add_argument("--strike", required=True, type=float)
    validation_parser.add_argument("--bid", required=True, type=float)
    validation_parser.add_argument("--ask", required=True, type=float)
    validation_parser.add_argument("--annual-volatility", required=True, type=float)
    validation_parser.add_argument("--seconds-to-expiry", required=True, type=float)
    validation_parser.add_argument("--option-type", required=True, choices=("call", "put"))
    validation_parser.add_argument("--style", choices=("european", "american"), default="american")
    validation_parser.add_argument("--risk-free-rate", type=float, default=0.0)
    validation_parser.add_argument("--dividend-yield", type=float, default=0.0)
    validation_parser.add_argument("--binomial-steps", type=int, default=500)
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
    elif arguments.command == "validate-option-pricing":
        inputs = OptionPricingInputs(
            spot=arguments.spot, strike=arguments.strike, annual_volatility=arguments.annual_volatility,
            seconds_to_expiry=arguments.seconds_to_expiry, option_type=arguments.option_type,
            risk_free_rate=arguments.risk_free_rate, dividend_yield=arguments.dividend_yield,
        )
        result = validate_option_quote(
            inputs, bid=arguments.bid, ask=arguments.ask, style=arguments.style, binomial_steps=arguments.binomial_steps,
        )
        print(json.dumps(result.as_payload(), sort_keys=True))
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
    elif arguments.command == "download-yahoo-closes":
        try:
            series = YahooChartClient().daily_closes(
                arguments.symbol,
                start_date=date.fromisoformat(arguments.start_date),
                end_date=date.fromisoformat(arguments.end_date),
            )
        except (PublicApiError, YahooPayloadError, ValueError) as error:
            raise SystemExit(f"download-yahoo-closes failed: {error}") from error
        output = Path(arguments.output)
        series.write_csv(output)
        print(json.dumps({
            "symbol": series.symbol, "provider": series.provider, "rows": len(series.closes),
            "output": str(output), "settlement_source": False,
        }, sort_keys=True))
    elif arguments.command == "batch-backfill-settled-markets":
        try:
            report = backfill_discovered_markets(
                discovery_path=Path(arguments.discovery_json), output_dir=Path(arguments.output_dir),
                start_offset=arguments.start_offset, maximum_markets=arguments.max_markets,
                pause_seconds=arguments.pause_seconds, pyth_pause_seconds=arguments.pyth_pause_seconds,
            )
        except (OSError, ValueError, PublicApiError) as error:
            raise SystemExit(f"batch-backfill-settled-markets failed: {error}") from error
        print(json.dumps(report.as_payload(), sort_keys=True))
    elif arguments.command == "backfill-pyth-intraday-spots":
        api_key = os.getenv("PYTH_PRO_API_KEY", "")
        if not api_key:
            raise SystemExit("backfill-pyth-intraday-spots requires PYTH_PRO_API_KEY in .env")
        symbols = tuple(symbol.strip().upper() for symbol in arguments.symbols.split(",") if symbol.strip())
        try:
            report = backfill_pyth_intraday_spots(
                discovery_path=Path(arguments.discovery_json), output_dir=Path(arguments.output_dir), api_key=api_key,
                symbols=symbols, pause_seconds=arguments.pause_seconds,
            )
        except (OSError, ValueError, PublicApiError) as error:
            raise SystemExit(f"backfill-pyth-intraday-spots failed: {error}") from error
        print(json.dumps(report.as_payload(), sort_keys=True))
    elif arguments.command == "backtest-pyth-clob":
        try:
            report = run_pyth_clob_backtest(
                data_dir=Path(arguments.data_dir), buffers=buffer_values(
                    arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step,
                ), minimum_edge=arguments.minimum_edge, lookback_days=arguments.lookback_days,
                training_days=arguments.training_days, validation_days=arguments.validation_days,
                minimum_training_trades=arguments.minimum_training_trades, fee_rate=arguments.fee_rate,
            ).as_payload()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"backtest-pyth-clob failed: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "backfill-settled-market-data":
        try:
            candidate = MarketCandidate.from_gamma_payload(journal.get_market_candidate_raw_payload(arguments.market_id))
            contract = parse_daily_equity_close_contract(candidate)
            winning_outcome = journal.get_market_settlement_outcome(arguments.market_id)
            result = backfill_settled_market_data(
                candidate=candidate, contract=contract, output_dir=Path(arguments.output_dir),
                lookback_calendar_days=arguments.lookback_calendar_days,
            )
        except (KeyError, EquityContractParseError, PublicApiError, ValueError) as error:
            raise SystemExit(f"backfill-settled-market-data failed: {error}") from error
        print(json.dumps({**result.as_payload(), "winning_outcome": winning_outcome}, sort_keys=True))
    elif arguments.command == "evaluate-baseline":
        now = datetime.now(UTC)
        resolves_at = datetime.fromisoformat(arguments.resolves_at.replace("Z", "+00:00"))
        closes = load_daily_closes_csv(Path(arguments.history_csv))
        volatility_observations = None
        if arguments.ohlc_history_csv:
            bars = load_daily_bars_csv(Path(arguments.ohlc_history_csv))
            closes = [DailyClose(bar.date, bar.close) for bar in bars]
            volatility_observations = bars
        up_ask, down_ask = journal.get_latest_outcome_asks(arguments.market_id)
        outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        fee_client = PolymarketFeeRateClient()
        up_fee_rate = fee_client.get_fee_rate(outcomes[0].token_id).fee_rate
        down_fee_rate = fee_client.get_fee_rate(outcomes[1].token_id).fee_rate
        data_is_fresh = daily_close_data_is_fresh(closes, now)
        assessment = evaluate_realized_vol_baseline(
            spot=arguments.spot, closes=closes, seconds_to_resolution=(resolves_at - now).total_seconds(),
            up_ask=up_ask, down_ask=down_ask, up_fee_rate=up_fee_rate, down_fee_rate=down_fee_rate,
            base_model_error_buffer=0.02, fallback_buffer=0.0, minimum_edge=0.02,
            data_is_fresh=data_is_fresh, lookback_days=arguments.lookback_days,
            volatility_estimator=arguments.volatility_estimator,
            volatility_decay=arguments.volatility_decay,
            volatility_observations=volatility_observations,
        )
        result = {
            "market_id": arguments.market_id,
            "fair_up_probability": round(assessment.fair_up_probability, 6),
            "up_ask": up_ask,
            "down_ask": down_ask,
            "realized_volatility": round(assessment.annualized_realized_volatility, 6),
            "volatility_estimator": assessment.volatility_estimator,
            "prior_close": assessment.prior_close,
            "data_is_fresh": assessment.data_is_fresh,
            "model_error_buffer": assessment.model_error_buffer,
            "paper_outcome": assessment.paper_outcome,
            "up_fee_rate": up_fee_rate, "down_fee_rate": down_fee_rate,
            "up_taker_fee": assessment.up_edge.estimated_taker_fee,
            "down_taker_fee": assessment.down_edge.estimated_taker_fee,
            "signal_status": _signal_status(assessment.paper_outcome),
            "up_model_edge_before_costs": round(assessment.fair_up_probability - up_ask, 6),
            "down_model_edge_before_costs": round((1 - assessment.fair_up_probability) - down_ask, 6),
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
        outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        fee_client = PolymarketFeeRateClient()
        up_fee_rate = fee_client.get_fee_rate(outcomes[0].token_id).fee_rate
        down_fee_rate = fee_client.get_fee_rate(outcomes[1].token_id).fee_rate
        assessment = evaluate_realized_vol_baseline(
            spot=quote.price, closes=closes, seconds_to_resolution=(resolves_at - now).total_seconds(),
            up_ask=up_ask, down_ask=down_ask, up_fee_rate=up_fee_rate, down_fee_rate=down_fee_rate,
            base_model_error_buffer=0.02, fallback_buffer=0.0, minimum_edge=0.02,
            data_is_fresh=data_is_fresh, lookback_days=20,
            volatility_estimator=arguments.volatility_estimator,
            volatility_decay=arguments.volatility_decay,
        )
        result = {
            "market_id": arguments.market_id, "symbol": quote.symbol, "spot": quote.price,
            "spot_last_trade_at": quote.last_trade_at.isoformat(), "spot_is_real_time": quote.is_real_time,
            "fair_up_probability": round(assessment.fair_up_probability, 6), "up_ask": up_ask,
            "down_ask": down_ask, "prior_close": assessment.prior_close,
            "realized_volatility": round(assessment.annualized_realized_volatility, 6),
            "volatility_estimator": assessment.volatility_estimator,
            "data_is_fresh": assessment.data_is_fresh, "model_error_buffer": assessment.model_error_buffer,
            "paper_outcome": assessment.paper_outcome, "provider": provider,
            "up_fee_rate": up_fee_rate, "down_fee_rate": down_fee_rate,
            "up_taker_fee": assessment.up_edge.estimated_taker_fee,
            "down_taker_fee": assessment.down_edge.estimated_taker_fee,
            "signal_status": _signal_status(assessment.paper_outcome),
            "up_model_edge_before_costs": round(assessment.fair_up_probability - up_ask, 6),
            "down_model_edge_before_costs": round((1 - assessment.fair_up_probability) - down_ask, 6),
        }
        log_event(settings.log_path, "NASDAQ_BASELINE_EVALUATED", result)
        print(json.dumps(result, sort_keys=True))
    elif arguments.command == "stream-shadow":
        if arguments.spot_provider == "finnhub":
            finnhub_api_key = os.getenv("FINNHUB_API_KEY", "")
            if not finnhub_api_key:
                raise SystemExit("stream-shadow --spot-provider finnhub requires FINNHUB_API_KEY in .env")
            api_key = ""
            api_secret = ""
        else:
            api_key = os.getenv("ALPACA_API_KEY_ID", "")
            api_secret = os.getenv("ALPACA_API_SECRET_KEY", "")
            finnhub_api_key = ""
            if not api_key or not api_secret:
                raise SystemExit("stream-shadow --spot-provider alpaca requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env")
        try:
            candidate = MarketCandidate.from_gamma_payload(journal.get_market_candidate_raw_payload(arguments.market_id))
            outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        except KeyError as error:
            raise SystemExit(f"Unknown market id: {error}") from error
        try:
            contract = parse_daily_equity_close_contract(candidate)
        except EquityContractParseError as error:
            journal.record_contract_review(arguments.market_id, accepted=False, reason=str(error))
            raise SystemExit(f"stream-shadow rejected market contract: {error}") from error
        journal.record_contract_review(
            arguments.market_id, accepted=True, reason="PYTH_DAILY_CLOSE_TEMPLATE", contract=contract.as_payload()
        )
        if arguments.symbol.upper() != contract.symbol:
            raise SystemExit(f"stream-shadow symbol {arguments.symbol.upper()} does not match contract ticker {contract.symbol}")
        if arguments.resolves_at:
            requested_resolution = datetime.fromisoformat(arguments.resolves_at.replace("Z", "+00:00"))
            if requested_resolution != contract.resolves_at:
                raise SystemExit("stream-shadow --resolves-at does not match the discovered contract end time")
        resolves_at = contract.resolves_at
        fee_client = PolymarketFeeRateClient()
        try:
            up_fee_rate = fee_client.get_fee_rate(outcomes[0].token_id).fee_rate
            down_fee_rate = fee_client.get_fee_rate(outcomes[1].token_id).fee_rate
        except PublicApiError:
            up_fee_rate = None
            down_fee_rate = None
        now = datetime.now(UTC)
        cache_path = Path("data") / "baseline_cache" / f"{arguments.symbol.upper()}.json"
        daily_provider = "NASDAQ_PUBLIC_NON_SETTLEMENT"
        try:
            nasdaq_client = NasdaqBaselineClient()
            cached_quote = nasdaq_client.latest_quote(arguments.symbol)
            closes = nasdaq_client.daily_closes(arguments.symbol, now)
            save_baseline_cache(cache_path, cached_quote, closes)
        except (PublicApiError, NasdaqPayloadError):
            try:
                cached_quote, closes = load_baseline_cache(cache_path)
                daily_provider = "NASDAQ_LOCAL_CACHE_NON_SETTLEMENT"
            except NasdaqPayloadError as error:
                raise SystemExit("stream-shadow requires fresh daily baseline data or a usable local cache") from error
        try:
            _run_async(
                _run_shadow_stream(
                    settings, arguments.market_id, tuple(item.token_id for item in outcomes), arguments.symbol.upper(),
                    arguments.spot_provider, api_key, api_secret, finnhub_api_key, resolves_at, closes,
                    daily_provider, cached_quote.price, cached_quote.last_trade_at, contract.as_payload(),
                    up_fee_rate, down_fee_rate, arguments.duration_seconds, journal,
                )
            )
        except ssl.SSLCertVerificationError as error:
            raise SystemExit(
                "WebSocket TLS verification failed. Set SSL_CERT_FILE in .env to the PEM file for your "
                "VPN or proxy certificate authority; SSL verification remains enabled."
            ) from error
    elif arguments.command == "supervise-shadow":
        api_key, api_secret, finnhub_api_key = _stream_credentials(arguments.spot_provider)
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
            pyth_api_key=os.getenv("PYTH_API_KEY", ""),
            tradier_api_token=os.getenv("TRADIER_API_TOKEN", ""),
            polygon_api_key=os.getenv("POLYGON_API_KEY", ""),
            event_sink=make_event_sink(settings.log_path, arguments.output_format),
        )
        try:
            _run_async(supervisor.run(arguments.scan_interval_seconds, arguments.duration_seconds))
        except ssl.SSLCertVerificationError as error:
            raise SystemExit(
                "Supervisor TLS verification failed. Set SSL_CERT_FILE in .env to the PEM file for your "
                "VPN or proxy certificate authority; SSL verification remains enabled."
            ) from error
    elif arguments.command == "paper-positions":
        positions = journal.list_paper_positions(arguments.status)
        print(json.dumps([_paper_position_payload(position) for position in positions], sort_keys=True))
    elif arguments.command == "maker-shadow-quotes":
        print(json.dumps([_maker_quote_payload(quote) for quote in journal.list_maker_shadow_quotes(arguments.status)], sort_keys=True))
    elif arguments.command == "portfolio-decisions":
        print(json.dumps(journal.list_portfolio_decisions(arguments.limit), sort_keys=True))
    elif arguments.command == "paper-performance":
        print(json.dumps(paper_performance(journal.list_paper_positions()).as_payload(), sort_keys=True))
    elif arguments.command == "dashboard":
        positions = journal.list_paper_positions()
        if arguments.once:
            print(render_dashboard(
                journal.dashboard_rows(arguments.limit),
                sum(item.status == "OPEN" for item in positions),
                sum(item.status == "SETTLED" for item in positions),
                positions=positions,
                signal_performance=journal.first_signal_performance(),
                sizing=sizing_readiness(journal.list_first_signal_calibration_observations()),
                daily_entry_limit=arguments.daily_entry_limit,
            ))
        else:
            run_live_dashboard(
                journal, refresh_seconds=arguments.refresh_seconds, limit=arguments.limit,
                daily_entry_limit=arguments.daily_entry_limit,
            )
    elif arguments.command in {"discover-price-ladders", "collect-price-ladders", "settle-price-ladders"}:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in getattr(arguments, "symbols", "").split(",")
            if symbol.strip()
        )
        ladder_journal = PriceLadderJournal(settings.journal_path)
        ladder_journal.initialize()
        collector = PriceLadderCollector(journal=ladder_journal)
        try:
            if arguments.command == "discover-price-ladders":
                print(json.dumps(collector.discover_and_store(symbols=symbols).as_payload(), sort_keys=True))
            elif arguments.command == "collect-price-ladders":
                collector.run(
                    symbols=symbols, interval_seconds=arguments.interval_seconds,
                    duration_seconds=arguments.duration_seconds,
                )
            else:
                print(json.dumps(collector.settle_stored_contracts(), sort_keys=True))
        except PublicApiError as error:
            _report_public_api_failure(settings, "PRICE_LADDER_PUBLIC_API_FAILED", error)
    elif arguments.command == "price-ladder-report":
        report = cross_market_report(settings.journal_path, market_date=arguments.date)
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "research-dashboard":
        ResearchDashboardServer(
            settings.journal_path, host=arguments.host, port=arguments.port,
        ).serve_forever()
    elif arguments.command == "replay-settled":
        report = replay_settled_positions(journal.list_paper_positions()).as_payload()
        if arguments.output:
            Path(arguments.output).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "historical-backtest":
        try:
            candidate = MarketCandidate.from_gamma_payload(journal.get_market_candidate_raw_payload(arguments.market_id))
            outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        except KeyError as error:
            raise SystemExit(f"Unknown market id: {error}") from error
        try:
            contract = parse_daily_equity_close_contract(candidate)
        except EquityContractParseError as error:
            raise SystemExit(f"historical-backtest rejected market contract: {error}") from error
        if arguments.symbol.upper() != contract.symbol:
            raise SystemExit(f"historical-backtest symbol {arguments.symbol.upper()} does not match contract ticker {contract.symbol}")
        closes = load_daily_closes_csv(Path(arguments.history_csv))
        if len(closes) < arguments.lookback_days + 2:
            raise SystemExit("history CSV must contain lookback closes plus one final close row")
        final_close = closes[-1]
        closes_before_market = closes[:-1]
        spot_history = load_intraday_spots_csv(Path(arguments.spot_csv)) if arguments.spot_csv else ()
        start_at = datetime.fromisoformat(arguments.start_at.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(arguments.end_at.replace("Z", "+00:00")) if arguments.end_at else contract.resolves_at
        history_client = ClobPriceHistoryClient()
        try:
            up_history = history_client.prices_history(outcomes[0].token_id, start_at=start_at, end_at=end_at)
            down_history = history_client.prices_history(outcomes[1].token_id, start_at=start_at, end_at=end_at)
            settlement = GammaMarketClient().get_market_settlement(arguments.market_id)
        except PublicApiError as error:
            _report_public_api_failure(settings, "HISTORICAL_BACKTEST_DATA_FAILED", error)
        report = replay_daily_up_down_market(
            candidate=candidate, symbol=arguments.symbol, resolves_at=contract.resolves_at,
            closes_before_market=closes_before_market, final_close=final_close,
            up_history=up_history, down_history=down_history, settlement=settlement, spot_history=spot_history,
            minimum_edge=arguments.minimum_edge, model_error_buffer=arguments.model_error_buffer,
            lookback_days=arguments.lookback_days,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "calibrate-paper":
        recommendation = calibrate_settled_positions(journal.list_paper_positions())
        if arguments.write:
            write_calibration_recommendation(Path("data/model_calibration.json"), recommendation)
        print(json.dumps(recommendation.as_payload(), sort_keys=True))
    elif arguments.command == "replay-observations":
        print(json.dumps(replay_market_observations(journal.list_replay_observations()).as_payload(), sort_keys=True))
    elif arguments.command == "calibrate-observations":
        print(json.dumps(calibrate_market_observations(journal.list_replay_observations()).as_payload(), sort_keys=True))
    elif arguments.command == "calibrate-first-signals":
        observations = journal.list_first_signal_calibration_observations()
        report = {
            "calibration": stratified_first_signal_calibration(observations).as_payload(),
            "sizing_readiness": sizing_readiness(observations).as_payload(),
        }
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-probability-calibration":
        report = walk_forward_probability_calibration(
            journal.list_first_signal_calibration_observations(), training_days=arguments.training_days,
            validation_days=arguments.validation_days, minimum_training_samples=arguments.minimum_training_samples,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "calibrate-checkpoints":
        print(json.dumps(calibrate_checkpoint_observations(journal.list_checkpoint_observations()).as_payload(), sort_keys=True))
    elif arguments.command == "buffer-sweep":
        report = run_buffer_sweep(
            journal.list_buffer_sweep_observations(),
            buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
            minimum_edge=arguments.minimum_edge, checkpoint_name=arguments.checkpoint,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-buffer-sweep":
        report = walk_forward_buffer_sweep(
            journal.list_buffer_sweep_observations(),
            buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
            minimum_edge=arguments.minimum_edge, checkpoint_name=arguments.checkpoint,
            training_days=arguments.training_days, validation_days=arguments.validation_days,
            minimum_training_trades=arguments.minimum_training_trades,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-top-five":
        checkpoints = tuple(item.strip() for item in arguments.checkpoints.split(",") if item.strip())
        try:
            checkpoint_groups = parse_checkpoint_sets(arguments.checkpoint_sets, allowed=checkpoints)
            policies = top_five_policies(
                checkpoint_groups=checkpoint_groups,
                buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
                minimum_edges=parse_probability_values(arguments.minimum_edges),
                max_daily_entries=arguments.max_daily_entries,
                probability_calibration=("RAW" if arguments.raw_probabilities else "TRAINING_BINNED_SHRINKAGE"),
            )
            report = walk_forward_top_five_policy(
                journal.list_buffer_sweep_observations(), policies=policies,
                training_days=arguments.training_days, validation_days=arguments.validation_days,
                minimum_training_trades=arguments.minimum_training_trades,
            ).as_payload()
        except ValueError as error:
            raise SystemExit(f"walk-forward-top-five rejected arguments: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "strategy-diagnostics":
        report = strategy_diagnostics(
            journal.list_buffer_sweep_observations(), journal.list_execution_observations(),
            journal.list_spot_source_comparisons(sample_every_seconds=60),
            spots=journal.list_spot_observations(source="PYTH_HERMES", sample_every_seconds=60),
            requested_shares=arguments.shares,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "settle-paper-positions":
        supervisor = MultiMarketShadowSupervisor(
            journal=journal, log_path=settings.log_path, spot_provider="finnhub", tradier_api_token=os.getenv("TRADIER_API_TOKEN", "")
        )
        _run_async(supervisor.settle_open_positions())
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
        _print_market_candidates(report.candidates)
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


async def _await_with_graceful_shutdown(coroutine: object) -> bool:
    """Await a long-running coroutine and let its finalizers finish after Ctrl+C."""

    task = asyncio.ensure_future(coroutine)  # type: ignore[arg-type]
    try:
        await task
        return False
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True


def _run_async(coroutine: object) -> None:
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
        raise SystemExit("supervise-shadow --spot-provider alpaca requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env")
    return api_key, api_secret, ""


def _paper_position_payload(position: object) -> dict[str, object]:
    return {
        "position_id": getattr(position, "position_id"), "market_id": getattr(position, "market_id"),
        "symbol": getattr(position, "symbol"), "outcome": getattr(position, "outcome"),
        "status": getattr(position, "status"), "contracts": getattr(position, "contracts"),
        "entry_ask": getattr(position, "entry_ask"), "entry_fee": getattr(position, "entry_fee"),
        "entry_slippage": getattr(position, "entry_slippage"), "fair_probability": getattr(position, "fair_probability"),
        "opened_at": getattr(position, "opened_at").isoformat(),
        "settled_at": getattr(position, "settled_at").isoformat() if getattr(position, "settled_at") else None,
        "settlement_outcome": getattr(position, "settlement_outcome"), "payout": getattr(position, "payout"),
        "realized_pnl": getattr(position, "realized_pnl"),
        "included_in_calibration": getattr(position, "included_in_calibration"),
        "exclusion_reason": getattr(position, "exclusion_reason"),
    }


def _maker_quote_payload(quote: object) -> dict[str, object]:
    return {
        "quote_id": getattr(quote, "quote_id"), "market_id": getattr(quote, "market_id"),
        "symbol": getattr(quote, "symbol"), "outcome": getattr(quote, "outcome"),
        "status": getattr(quote, "status"), "limit_price": getattr(quote, "limit_price"),
        "fair_probability": getattr(quote, "fair_probability"), "theoretical_edge": getattr(quote, "theoretical_edge"),
        "best_bid": getattr(quote, "best_bid"), "best_ask": getattr(quote, "best_ask"),
        "touch_count": getattr(quote, "touch_count"),
        "last_touched_at": getattr(quote, "last_touched_at").isoformat() if getattr(quote, "last_touched_at") else None,
        "cancelled_at": getattr(quote, "cancelled_at").isoformat() if getattr(quote, "cancelled_at") else None,
        "cancel_reason": getattr(quote, "cancel_reason"),
    }


def _signal_status(paper_outcome: str | None) -> str:
    return f"PAPER_{paper_outcome}" if paper_outcome else "NO_PAPER_TRADE"


def _print_market_candidates(candidates: object) -> None:
    """Render market IDs in terminal output while withholding CLOB token IDs."""

    items = tuple(candidates)
    if not items:
        print("No locally discovered market candidates.")
        return
    print("\nReview-required markets:")
    for candidate in items:
        print(
            f"  market_id={getattr(candidate, 'market_id')} | "
            f"outcomes={getattr(candidate, 'outcome_a_label')}/{getattr(candidate, 'outcome_b_label')} | "
            f"end={getattr(candidate, 'end_date')} | "
            f"{getattr(candidate, 'question')}"
        )


def _snapshot_market_books(journal: ShadowJournal, market_id: str, outcomes: tuple[object, object]) -> int:
    """Fetch both published outcome books; this remains public read-only I/O."""

    client = ClobMarketDataClient()
    for outcome in outcomes:
        snapshot = client.get_order_book(getattr(outcome, "token_id"))
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
    closes: list[object],
    daily_provider: str,
    reference_spot: float,
    reference_spot_observed_at: datetime,
    contract: object,
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
                last_evaluation_recorded_at is None
                or (now - last_evaluation_recorded_at).total_seconds() >= 60
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


if __name__ == "__main__":
    main()
