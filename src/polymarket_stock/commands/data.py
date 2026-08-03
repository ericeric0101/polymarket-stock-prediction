"""Data command handlers."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from ..batch_backfill import backfill_discovered_markets
from ..buffer_sweep import buffer_values
from ..equity_contracts import EquityContractParseError, parse_daily_equity_close_contract
from ..http import PublicApiError
from ..intraday_spot_backfill import backfill_pyth_intraday_spots
from ..market_discovery import MarketCandidate
from ..pyth_clob_backtest import run_pyth_clob_backtest
from ..settled_market_data import backfill_settled_market_data
from ..yahoo_data import YahooChartClient, YahooPayloadError
from .context import CommandContext
from .shared import _write_optional_json


def handle(context: CommandContext) -> None:
    arguments = context.arguments
    journal = context.journal
    if arguments.command == "download-yahoo-closes":
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
        print(
            json.dumps(
                {
                    "symbol": series.symbol,
                    "provider": series.provider,
                    "rows": len(series.closes),
                    "output": str(output),
                    "settlement_source": False,
                },
                sort_keys=True,
            )
        )
    elif arguments.command == "batch-backfill-settled-markets":
        try:
            report = backfill_discovered_markets(
                discovery_path=Path(arguments.discovery_json),
                output_dir=Path(arguments.output_dir),
                start_offset=arguments.start_offset,
                maximum_markets=arguments.max_markets,
                pause_seconds=arguments.pause_seconds,
                pyth_pause_seconds=arguments.pyth_pause_seconds,
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
                discovery_path=Path(arguments.discovery_json),
                output_dir=Path(arguments.output_dir),
                api_key=api_key,
                symbols=symbols,
                pause_seconds=arguments.pause_seconds,
            )
        except (OSError, ValueError, PublicApiError) as error:
            raise SystemExit(f"backfill-pyth-intraday-spots failed: {error}") from error
        print(json.dumps(report.as_payload(), sort_keys=True))
    elif arguments.command == "backtest-pyth-clob":
        try:
            report = run_pyth_clob_backtest(
                data_dir=Path(arguments.data_dir),
                buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
                minimum_edge=arguments.minimum_edge,
                lookback_days=arguments.lookback_days,
                training_days=arguments.training_days,
                validation_days=arguments.validation_days,
                minimum_training_trades=arguments.minimum_training_trades,
                fee_rate=arguments.fee_rate,
            ).as_payload()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"backtest-pyth-clob failed: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "backfill-settled-market-data":
        try:
            candidate = MarketCandidate.from_gamma_payload(
                journal.get_market_candidate_raw_payload(arguments.market_id)
            )
            contract = parse_daily_equity_close_contract(candidate)
            winning_outcome = journal.get_market_settlement_outcome(arguments.market_id)
            result = backfill_settled_market_data(
                candidate=candidate,
                contract=contract,
                output_dir=Path(arguments.output_dir),
                lookback_calendar_days=arguments.lookback_calendar_days,
            )
        except (KeyError, EquityContractParseError, PublicApiError, ValueError) as error:
            raise SystemExit(f"backfill-settled-market-data failed: {error}") from error
        print(json.dumps({**result.as_payload(), "winning_outcome": winning_outcome}, sort_keys=True))
    else:
        raise AssertionError(f"Unexpected command for handler: {arguments.command}")
