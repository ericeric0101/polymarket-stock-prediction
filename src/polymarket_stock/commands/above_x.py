"""Above X command handlers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import cli_runtime
from ..above_x_backtest import run_above_x_backtest
from ..above_x_research import (
    AboveXHistoricalDiscovery,
    above_x_coverage_report,
    backfill_above_x_markets,
    write_above_x_discovery,
)
from ..above_x_veto import historical_observations, sync_live_veto_shadow, walk_forward
from ..cross_market import cross_market_report
from ..http import PublicApiError
from .context import CommandContext
from .shared import _report_public_api_failure, _write_optional_json


def handle(context: CommandContext) -> None:
    arguments = context.arguments
    settings = context.settings
    if arguments.command == "discover-above-x-history":
        symbols = tuple(symbol.strip().upper() for symbol in arguments.symbols.split(",") if symbol.strip())
        try:
            report = AboveXHistoricalDiscovery().discover(
                symbols=symbols,
                date_start=arguments.date_start,
                date_end=arguments.date_end,
                page_size=arguments.page_size,
                max_pages=arguments.max_pages,
            )
            write_above_x_discovery(Path(arguments.output), report)
        except (OSError, ValueError, PublicApiError) as error:
            raise SystemExit(f"discover-above-x-history failed: {error}") from error
        print(
            json.dumps(
                {
                    "market_type": "ABOVE_X",
                    "contracts": len(report.contracts),
                    "pages_scanned": report.pages_scanned,
                    "markets_scanned": report.markets_scanned,
                    "rejected_markets": report.rejected_markets,
                    "output": arguments.output,
                },
                sort_keys=True,
            )
        )
    elif arguments.command == "backfill-above-x-history":
        try:
            report = backfill_above_x_markets(
                discovery_path=Path(arguments.discovery_json),
                output_dir=Path(arguments.output_dir),
                pyth_api_key=os.getenv("PYTH_PRO_API_KEY", ""),
                maximum_markets=arguments.max_markets,
                pause_seconds=arguments.pause_seconds,
                pyth_pause_seconds=arguments.pyth_pause_seconds,
                pyth_data_dir=Path(arguments.pyth_data_dir),
            )
        except (OSError, ValueError, PublicApiError) as error:
            raise SystemExit(f"backfill-above-x-history failed: {error}") from error
        print(json.dumps(report.as_payload(), sort_keys=True))
    elif arguments.command == "above-x-coverage":
        try:
            report = above_x_coverage_report(
                discovery_path=Path(arguments.discovery_json),
                output_dir=Path(arguments.data_dir),
                spot_data_dir=Path(arguments.spot_data_dir),
            ).as_payload()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"above-x-coverage failed: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "backtest-above-x":
        try:
            report = run_above_x_backtest(
                discovery_path=Path(arguments.discovery_json),
                data_dir=Path(arguments.data_dir),
                spot_data_dir=Path(arguments.spot_data_dir),
                minimum_edge=arguments.minimum_edge,
                lookback_days=arguments.lookback_days,
            ).as_payload()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"backtest-above-x failed: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-above-x-veto":
        try:
            observations = historical_observations(
                core_dir=Path(arguments.core_data_dir),
                above_x_dir=Path(arguments.above_x_data_dir),
                discovery_path=Path(arguments.discovery_json),
                checkpoint_name=arguments.checkpoint,
                buffer=arguments.buffer,
                minimum_edge=arguments.minimum_edge,
            )
            report = walk_forward(
                observations=observations,
                training_days=arguments.training_days,
                validation_days=arguments.validation_days,
                minimum_training_trades=arguments.minimum_training_trades,
            ).as_payload()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"walk-forward-above-x-veto failed: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps({**report, "observation_count": len(observations)}, sort_keys=True))
    elif arguments.command == "sync-above-x-veto-shadow":
        from ..above_x_veto import AboveXVetoPolicy

        try:
            rows = sync_live_veto_shadow(
                journal_path=settings.journal_path,
                policy=AboveXVetoPolicy("VETO_DISAGREEMENT", arguments.minimum_strikes, arguments.maximum_width),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"sync-above-x-veto-shadow failed: {error}") from error
        print(json.dumps({"written": len(rows), "rows": list(rows)}, sort_keys=True, default=str))
    elif arguments.command in {"discover-price-ladders", "collect-price-ladders", "settle-price-ladders"}:
        cli_runtime.price_ladders(arguments, settings, _report_public_api_failure)
    elif arguments.command == "price-ladder-report":
        report = cross_market_report(settings.journal_path, market_date=arguments.date)
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    else:
        raise AssertionError(f"Unexpected command for handler: {arguments.command}")
