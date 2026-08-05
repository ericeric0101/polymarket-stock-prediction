"""CLI parser and command-domain dispatcher."""

from __future__ import annotations

from . import above_x, calibration, data, market, operations, research
from .catalog import command_group
from .context import CommandContext
from .shared import _await_with_graceful_shutdown, _report_public_api_failure  # noqa: F401
from ..checkpoints import CHECKPOINTS
from ..config import Settings
from ..journal import ShadowJournal
import argparse

__all__ = ("_await_with_graceful_shutdown", "_report_public_api_failure", "build_parser", "main")


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
    equity_parser = subparsers.add_parser(
        "scan-equity-events", help="cursor-scan active tagged equity daily-direction events"
    )
    equity_parser.add_argument("--tag-slugs", default="stocks,equities")
    equity_parser.add_argument("--page-size", type=int, default=500)
    equity_parser.add_argument("--max-pages-per-tag", type=int, default=100)
    equity_parser.add_argument("--pause-seconds", type=float, default=0.2)
    equity_parser.add_argument(
        "--snapshot-books", action="store_true", help="also snapshot both outcome books for each candidate"
    )
    book_parser = subparsers.add_parser("snapshot-book", help="store one public CLOB order-book snapshot")
    book_parser.add_argument("--market-id", required=True)
    book_parser.add_argument("--token-id", required=True)
    market_book_parser = subparsers.add_parser(
        "snapshot-market", help="store both order books for one discovered market"
    )
    market_book_parser.add_argument("--market-id", required=True)
    baseline_parser = subparsers.add_parser(
        "evaluate-baseline", help="compare realized-vol baseline with saved Up/Down asks"
    )
    baseline_parser.add_argument("--market-id", required=True)
    baseline_parser.add_argument("--history-csv", required=True)
    baseline_parser.add_argument("--spot", required=True, type=float)
    baseline_parser.add_argument("--resolves-at", required=True, help="ISO-8601 timestamp, e.g. 2026-07-20T20:00:00Z")
    baseline_parser.add_argument("--lookback-days", type=int, default=20)
    baseline_parser.add_argument(
        "--volatility-estimator",
        choices=("CLOSE_TO_CLOSE", "EWMA", "GARMAN_KLASS", "YANG_ZHANG"),
        default="CLOSE_TO_CLOSE",
    )
    baseline_parser.add_argument("--volatility-decay", type=float, default=0.94)
    baseline_parser.add_argument("--ohlc-history-csv", help="optional Date,Open,High,Low,Close CSV for OHLC estimators")
    yahoo_parser = subparsers.add_parser(
        "download-yahoo-closes", help="download non-settlement Yahoo daily closes to Date,Close CSV"
    )
    yahoo_parser.add_argument("--symbol", required=True)
    yahoo_parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    yahoo_parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    yahoo_parser.add_argument("--output", required=True)
    settled_data_parser = subparsers.add_parser(
        "backfill-settled-market-data", help="download Pyth references and Yahoo intraday inputs for one settled market"
    )
    settled_data_parser.add_argument("--market-id", required=True)
    settled_data_parser.add_argument("--output-dir", default="data/historical")
    settled_data_parser.add_argument("--lookback-calendar-days", type=int, default=45)
    batch_backfill_parser = subparsers.add_parser(
        "batch-backfill-settled-markets", help="resumable Pyth, CLOB, and Gamma settlement backfill from discovery JSON"
    )
    batch_backfill_parser.add_argument("--discovery-json", required=True)
    batch_backfill_parser.add_argument("--output-dir", default="data/historical")
    batch_backfill_parser.add_argument("--start-offset", type=int, default=0)
    batch_backfill_parser.add_argument("--max-markets", type=int)
    batch_backfill_parser.add_argument("--pause-seconds", type=float, default=0.2)
    batch_backfill_parser.add_argument("--pyth-pause-seconds", type=float, default=2.0)
    pyth_intraday_parser = subparsers.add_parser(
        "backfill-pyth-intraday-spots", help="resumable Pyth Pro one-minute underlying spots for settled markets"
    )
    pyth_intraday_parser.add_argument("--discovery-json", required=True)
    pyth_intraday_parser.add_argument("--output-dir", default="data/historical/90d")
    pyth_intraday_parser.add_argument("--symbols", default="NVDA,TSLA")
    pyth_intraday_parser.add_argument("--pause-seconds", type=float, default=0.25)
    pyth_clob_parser = subparsers.add_parser(
        "backtest-pyth-clob", help="non-leaking Pyth minute-spot and CLOB-history batch replay"
    )
    pyth_clob_parser.add_argument("--data-dir", default="data/historical/90d")
    pyth_clob_parser.add_argument("--minimum-buffer", type=float, default=0.01)
    pyth_clob_parser.add_argument("--maximum-buffer", type=float, default=0.02)
    pyth_clob_parser.add_argument("--buffer-step", type=float, default=0.01)
    pyth_clob_parser.add_argument("--minimum-edge", type=float, default=0.02)
    pyth_clob_parser.add_argument("--lookback-days", type=int, default=20)
    pyth_clob_parser.add_argument("--training-days", type=int, default=20)
    pyth_clob_parser.add_argument("--validation-days", type=int, default=5)
    pyth_clob_parser.add_argument("--minimum-training-trades", type=int, default=10)
    pyth_clob_parser.add_argument(
        "--fee-rate", type=float, default=0.0, help="historical fee-rate assumption; 0 reports pre-fee PnL"
    )
    pyth_clob_parser.add_argument("--output", help="optional JSON report output path")
    above_x_discovery_parser = subparsers.add_parser(
        "discover-above-x-history",
        help="discover closed Pyth closes-above markets for isolated research",
    )
    above_x_discovery_parser.add_argument("--symbols", default="TSLA,NVDA")
    above_x_discovery_parser.add_argument("--date-start", help="inclusive New York market date, YYYY-MM-DD")
    above_x_discovery_parser.add_argument("--date-end", help="inclusive New York market date, YYYY-MM-DD")
    above_x_discovery_parser.add_argument("--page-size", type=int, default=500)
    above_x_discovery_parser.add_argument("--max-pages", type=int, default=100)
    above_x_discovery_parser.add_argument("--output", default="data/historical/above_x_discovery.json")
    above_x_backfill_parser = subparsers.add_parser(
        "backfill-above-x-history",
        help="download Above-X CLOB price proxies, Pyth final price, and settlement",
    )
    above_x_backfill_parser.add_argument("--discovery-json", default="data/historical/above_x_discovery.json")
    above_x_backfill_parser.add_argument("--output-dir", default="data/historical/above_x")
    above_x_backfill_parser.add_argument("--max-markets", type=int)
    above_x_backfill_parser.add_argument("--pause-seconds", type=float, default=0.2)
    above_x_backfill_parser.add_argument("--pyth-pause-seconds", type=float, default=2.0)
    above_x_backfill_parser.add_argument("--pyth-data-dir", default="data/historical/90d")
    above_x_coverage_parser = subparsers.add_parser(
        "above-x-coverage",
        help="report isolated Above-X historical data coverage",
    )
    above_x_coverage_parser.add_argument("--discovery-json", default="data/historical/above_x_discovery.json")
    above_x_coverage_parser.add_argument("--data-dir", default="data/historical/above_x")
    above_x_coverage_parser.add_argument("--spot-data-dir", default="data/historical/90d")
    above_x_coverage_parser.add_argument("--output", help="optional JSON report output path")
    above_x_backtest_parser = subparsers.add_parser(
        "backtest-above-x",
        help="replay isolated Above-X markets without changing core Up/Down policy",
    )
    above_x_backtest_parser.add_argument("--discovery-json", default="data/historical/above_x_discovery.json")
    above_x_backtest_parser.add_argument("--data-dir", default="data/historical/above_x")
    above_x_backtest_parser.add_argument("--spot-data-dir", default="data/historical/90d")
    above_x_backtest_parser.add_argument("--minimum-edge", type=float, default=0.02)
    above_x_backtest_parser.add_argument("--lookback-days", type=int, default=20)
    above_x_backtest_parser.add_argument("--output", help="optional JSON report output path")
    above_x_veto_parser = subparsers.add_parser(
        "walk-forward-above-x-veto", help="research-only Core 12:00 Above-X confirmation/veto walk-forward"
    )
    above_x_veto_parser.add_argument("--core-data-dir", default="data/historical/90d")
    above_x_veto_parser.add_argument("--above-x-data-dir", default="data/historical/above_x")
    above_x_veto_parser.add_argument("--discovery-json", default="data/historical/above_x_discovery.json")
    above_x_veto_parser.add_argument("--checkpoint", choices=("1200_EDT",), default="1200_EDT")
    above_x_veto_parser.add_argument("--buffer", type=float, default=0.02)
    above_x_veto_parser.add_argument("--minimum-edge", type=float, default=0.02)
    above_x_veto_parser.add_argument("--training-days", type=int, default=6)
    above_x_veto_parser.add_argument("--validation-days", type=int, default=2)
    above_x_veto_parser.add_argument("--minimum-training-trades", type=int, default=3)
    above_x_veto_parser.add_argument("--output", help="optional JSON report output path")
    above_x_veto_sync_parser = subparsers.add_parser(
        "sync-above-x-veto-shadow", help="persist read-only Above-X Core veto diagnostics"
    )
    above_x_veto_sync_parser.add_argument("--minimum-strikes", type=int, default=3)
    above_x_veto_sync_parser.add_argument("--maximum-width", type=float, default=0.30)
    nasdaq_baseline_parser = subparsers.add_parser(
        "evaluate-nasdaq-baseline", help="automatic free Nasdaq realized-vol baseline"
    )
    nasdaq_baseline_parser.add_argument("--market-id", required=True)
    nasdaq_baseline_parser.add_argument("--symbol", required=True)
    nasdaq_baseline_parser.add_argument("--resolves-at", required=True)
    nasdaq_baseline_parser.add_argument(
        "--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA"), default="CLOSE_TO_CLOSE"
    )
    nasdaq_baseline_parser.add_argument("--volatility-decay", type=float, default=0.94)
    stream_parser = subparsers.add_parser("stream-shadow", help="read-only Polymarket and stock-quote live streams")
    stream_parser.add_argument("--market-id", required=True)
    stream_parser.add_argument("--symbol", required=True)
    stream_parser.add_argument("--spot-provider", choices=("finnhub", "alpaca"), default="finnhub")
    stream_parser.add_argument("--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA"), default="CLOSE_TO_CLOSE")
    stream_parser.add_argument("--volatility-decay", type=float, default=0.94)
    stream_parser.add_argument(
        "--resolves-at", help="ISO-8601 resolution timestamp; defaults to the discovered market end date"
    )
    stream_parser.add_argument("--duration-seconds", type=float, default=0, help="0 runs until interrupted")
    supervisor_parser = subparsers.add_parser(
        "supervise-shadow", help="scheduled multi-market shadow observation and paper lifecycle"
    )
    supervisor_parser.add_argument("--spot-provider", choices=("finnhub", "alpaca"), default="finnhub")
    supervisor_parser.add_argument(
        "--spot-mode",
        choices=("PYTH_PRIMARY", "FINNHUB_ONLY"),
        default="FINNHUB_ONLY",
        help="FINNHUB_ONLY uses Finnhub spot plus exact or estimated prior-close thresholds; PYTH_PRIMARY requires a working Hermes feed",
    )
    supervisor_parser.add_argument(
        "--finnhub-threshold-safety-bps",
        type=float,
        default=35.0,
        help="FINNHUB_ONLY: label an estimated threshold as near when spot is within this many bps",
    )
    supervisor_parser.add_argument(
        "--volatility-estimator", choices=("CLOSE_TO_CLOSE", "EWMA"), default="CLOSE_TO_CLOSE"
    )
    supervisor_parser.add_argument("--volatility-decay", type=float, default=0.94)
    supervisor_parser.add_argument(
        "--comparison-estimators", default="EWMA", help="comma-separated shadow comparison estimators; default EWMA"
    )
    supervisor_parser.add_argument("--scan-interval-seconds", type=float, default=900)
    supervisor_parser.add_argument("--max-markets", type=int, default=18)
    supervisor_parser.add_argument("--minimum-seconds-to-resolution", type=float, default=900)
    supervisor_parser.add_argument(
        "--maker-minimum-edge", type=float, default=0.005, help="minimum unfilled maker edge, default 0.005"
    )
    supervisor_parser.add_argument(
        "--maker-reprice-minimum-price-change",
        type=float,
        default=0.02,
        help="minimum maker limit-price change before reprice, default 0.02",
    )
    supervisor_parser.add_argument(
        "--maker-minimum-quote-lifetime-seconds",
        type=float,
        default=30.0,
        help="minimum seconds an active maker quote remains before reprice, default 30",
    )
    supervisor_parser.add_argument("--paper-batch-seconds", type=float, default=30.0)
    supervisor_parser.add_argument(
        "--paper-entry-checkpoints",
        default="1200_EDT",
        help="comma-separated immutable checkpoints allowed to create paper entries; default 1200_EDT",
    )
    supervisor_parser.add_argument("--max-daily-paper-entries", type=int, default=5)
    supervisor_parser.add_argument("--max-per-risk-group", type=int, default=1)
    supervisor_parser.add_argument("--max-same-direction-paper-entries", type=int, default=2)
    supervisor_parser.add_argument("--duration-seconds", type=float, default=0, help="0 runs until interrupted")
    supervisor_parser.add_argument("--output-format", choices=("human", "json"), default="human")
    positions_parser = subparsers.add_parser(
        "paper-positions", help="list open or settled hold-to-resolution paper positions"
    )
    positions_parser.add_argument("--status", choices=("OPEN", "SETTLED"))
    maker_quotes_parser = subparsers.add_parser(
        "maker-shadow-quotes", help="list active or cancelled maker shadow quotes"
    )
    maker_quotes_parser.add_argument("--status", choices=("ACTIVE", "CANCELLED"), default="ACTIVE")
    portfolio_parser = subparsers.add_parser(
        "portfolio-decisions", help="list batched paper-entry selections and rejections"
    )
    portfolio_parser.add_argument("--limit", type=int, default=100)
    subparsers.add_parser("paper-performance", help="report realized paper PnL and calibration for settled positions")
    replay_parser = subparsers.add_parser(
        "replay-settled", help="replay immutable paper entries against official settled outcomes"
    )
    replay_parser.add_argument("--output", help="optional JSON report output path")
    historical_parser = subparsers.add_parser(
        "historical-backtest", help="offline replay of one daily Up/Down market from CLOB price history"
    )
    historical_parser.add_argument("--market-id", required=True)
    historical_parser.add_argument("--symbol", required=True)
    historical_parser.add_argument(
        "--history-csv", required=True, help="Date,Close CSV ending with prior close and final close"
    )
    historical_parser.add_argument(
        "--spot-csv", help="optional DateTime,Spot intraday CSV; required for simulated trades"
    )
    historical_parser.add_argument("--start-at", required=True, help="ISO-8601 history start timestamp")
    historical_parser.add_argument("--end-at", help="ISO-8601 history end timestamp; defaults to market resolution")
    historical_parser.add_argument("--minimum-edge", type=float, default=0.02)
    historical_parser.add_argument("--model-error-buffer", type=float, default=0.02)
    historical_parser.add_argument("--lookback-days", type=int, default=20)
    historical_parser.add_argument("--output", help="optional JSON report output path")
    subparsers.add_parser("replay-observations", help="replay all valid market observations against official outcomes")
    calibration_parser = subparsers.add_parser(
        "calibrate-paper", help="derive conservative settings from settled paper positions"
    )
    calibration_parser.add_argument(
        "--write", action="store_true", help="write a review-only recommendation to data/model_calibration.json"
    )
    subparsers.add_parser("calibrate-observations", help="calibrate from all settled market observations")
    first_signal_calibration_parser = subparsers.add_parser(
        "calibrate-first-signals", help="stratify selected-side first-signal calibration and sizing readiness"
    )
    first_signal_calibration_parser.add_argument("--output", help="optional JSON report output path")
    probability_walk_forward_parser = subparsers.add_parser(
        "walk-forward-probability-calibration",
        help="fit selected-side probability shrinkage only on earlier trading dates",
    )
    probability_walk_forward_parser.add_argument("--training-days", type=int, default=20)
    probability_walk_forward_parser.add_argument("--validation-days", type=int, default=5)
    probability_walk_forward_parser.add_argument("--minimum-training-samples", type=int, default=50)
    probability_walk_forward_parser.add_argument("--output", help="optional JSON report output path")
    subparsers.add_parser(
        "calibrate-checkpoints", help="report immutable checkpoint calibration against official settlements"
    )
    buffer_parser = subparsers.add_parser(
        "buffer-sweep", help="replay one-entry-per-market checkpoint policies across probability buffers"
    )
    buffer_parser.add_argument("--minimum-buffer", type=float, default=0.0)
    buffer_parser.add_argument("--maximum-buffer", type=float, default=0.20)
    buffer_parser.add_argument("--buffer-step", type=float, default=0.01)
    buffer_parser.add_argument("--minimum-edge", type=float, default=0.02)
    buffer_parser.add_argument("--checkpoint", choices=tuple(item[2] for item in CHECKPOINTS))
    buffer_parser.add_argument("--output", help="optional JSON report output path")
    walk_forward_parser = subparsers.add_parser(
        "walk-forward-buffer-sweep", help="select buffers on earlier trading days and evaluate only later days"
    )
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
        "walk-forward-top-five",
        help="select a capped daily Top-5 checkpoint policy on prior days only",
    )
    top_five_walk_forward_parser.add_argument(
        "--checkpoints",
        default="1200_EDT,1400_EDT,1530_EDT",
        help="chronological checkpoint names available to the policy search",
    )
    top_five_walk_forward_parser.add_argument(
        "--checkpoint-sets",
        default="",
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
    diagnostics_parser = subparsers.add_parser(
        "strategy-diagnostics", help="model, execution, source, volatility, and exit diagnostics"
    )
    diagnostics_parser.add_argument("--shares", type=float, default=10.0)
    diagnostics_parser.add_argument("--output", help="optional JSON report output path")
    close_calibration_parser = subparsers.add_parser(
        "close-source-calibration",
        help="compare official Pyth final-minute close with captured Finnhub close-window quotes",
    )
    close_calibration_parser.add_argument(
        "--market-date", required=True, help="completed New York trading date, YYYY-MM-DD"
    )
    close_calibration_parser.add_argument(
        "--symbols", help="comma-separated symbols; defaults to locally captured Finnhub symbols"
    )
    close_calibration_parser.add_argument("--output", help="optional JSON report output path")
    dashboard_parser = subparsers.add_parser("dashboard", help="open the continuously refreshing terminal dashboard")
    dashboard_parser.add_argument("--limit", type=int, default=18)
    dashboard_parser.add_argument("--refresh-seconds", type=float, default=3.0)
    dashboard_parser.add_argument("--daily-entry-limit", type=int, default=5)
    dashboard_parser.add_argument(
        "--once", action="store_true", help="print one plain-text snapshot instead of opening the live dashboard"
    )
    ladder_discovery_parser = subparsers.add_parser(
        "discover-price-ladders",
        help="discover strict Pyth closes-above contracts for isolated research",
    )
    ladder_discovery_parser.add_argument("--symbols", default="TSLA,NVDA")
    ladder_collection_parser = subparsers.add_parser(
        "collect-price-ladders",
        help="poll price-ladder books into isolated research tables",
    )
    ladder_collection_parser.add_argument("--symbols", default="TSLA,NVDA")
    ladder_collection_parser.add_argument("--interval-seconds", type=float, default=60.0)
    ladder_collection_parser.add_argument("--duration-seconds", type=float, default=0.0)
    subparsers.add_parser("settle-price-ladders", help="reconcile stored ladder contracts with official outcomes")
    ladder_report_parser = subparsers.add_parser(
        "price-ladder-report",
        help="compare core checkpoints with isolated ladder probabilities",
    )
    ladder_report_parser.add_argument("--date", help="New York market date, YYYY-MM-DD")
    ladder_report_parser.add_argument("--output", help="optional JSON report output path")
    research_dashboard_parser = subparsers.add_parser(
        "research-dashboard",
        help="serve a localhost-only core and price-ladder research dashboard",
    )
    research_dashboard_parser.add_argument("--host", default="127.0.0.1")
    research_dashboard_parser.add_argument("--port", type=int, default=8765)
    research_dashboard_parser.add_argument("--limit", type=int, default=18)
    research_dashboard_parser.add_argument("--daily-entry-limit", type=int, default=5)
    subparsers.add_parser(
        "settle-paper-positions",
        help="one-shot official reconciliation for open paper positions and model observations",
    )
    alpaca_parser = subparsers.add_parser("snapshot-alpaca-options", help="store free Alpaca indicative option quotes")
    alpaca_parser.add_argument("--symbols", required=True, help="comma-separated OCC option symbols, maximum 100")
    validation_parser = subparsers.add_parser(
        "validate-option-pricing", help="offline BSM/binomial option-pricing cross-check; never creates a signal"
    )
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


_HANDLERS = {
    "market": market.handle,
    "data": data.handle,
    "research": research.handle,
    "calibration": calibration.handle,
    "above_x": above_x.handle,
    "operations": operations.handle,
}


def main() -> None:
    arguments = build_parser().parse_args()
    settings = Settings.from_environment()
    journal = ShadowJournal(settings.journal_path)
    journal.initialize()
    _HANDLERS[command_group(arguments.command)](CommandContext(arguments, settings, journal))


if __name__ == "__main__":
    main()
