"""Research command handlers."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from ..baseline import (
    DailyClose,
    daily_close_data_is_fresh,
    evaluate_realized_vol_baseline,
    load_daily_bars_csv,
    load_daily_closes_csv,
)
from ..clob_history import ClobPriceHistoryClient
from ..close_source_calibration import calibrate_close_sources
from ..equity_contracts import EquityContractParseError, parse_daily_equity_close_contract
from ..fees import PolymarketFeeRateClient
from ..historical_backtest import load_intraday_spots_csv, replay_daily_up_down_market
from ..http import PublicApiError
from ..logging import log_event
from ..market_discovery import GammaMarketClient, MarketCandidate
from ..nasdaq_data import NasdaqBaselineClient, load_baseline_cache, save_baseline_cache
from ..pyth_history import PythHistoryClient
from ..replay import replay_market_observations, replay_settled_positions
from ..strategy_diagnostics import strategy_diagnostics
from ..streaming import SpotQuote
from .context import CommandContext
from .shared import _report_public_api_failure, _signal_status, _snapshot_market_books, _write_optional_json


def handle(context: CommandContext) -> None:
    arguments = context.arguments
    settings = context.settings
    journal = context.journal
    if arguments.command == "evaluate-baseline":
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
            spot=arguments.spot,
            closes=closes,
            seconds_to_resolution=(resolves_at - now).total_seconds(),
            up_ask=up_ask,
            down_ask=down_ask,
            up_fee_rate=up_fee_rate,
            down_fee_rate=down_fee_rate,
            base_model_error_buffer=0.02,
            fallback_buffer=0.0,
            minimum_edge=0.02,
            data_is_fresh=data_is_fresh,
            lookback_days=arguments.lookback_days,
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
            "up_fee_rate": up_fee_rate,
            "down_fee_rate": down_fee_rate,
            "up_taker_fee": assessment.up_edge.estimated_taker_fee,
            "down_taker_fee": assessment.down_edge.estimated_taker_fee,
            "signal_status": _signal_status(assessment.paper_outcome),
            "up_model_edge_before_costs": round(assessment.fair_up_probability - up_ask, 6),
            "down_model_edge_before_costs": round(1 - assessment.fair_up_probability - down_ask, 6),
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
            spot=quote.price,
            closes=closes,
            seconds_to_resolution=(resolves_at - now).total_seconds(),
            up_ask=up_ask,
            down_ask=down_ask,
            up_fee_rate=up_fee_rate,
            down_fee_rate=down_fee_rate,
            base_model_error_buffer=0.02,
            fallback_buffer=0.0,
            minimum_edge=0.02,
            data_is_fresh=data_is_fresh,
            lookback_days=20,
            volatility_estimator=arguments.volatility_estimator,
            volatility_decay=arguments.volatility_decay,
        )
        result = {
            "market_id": arguments.market_id,
            "symbol": quote.symbol,
            "spot": quote.price,
            "spot_last_trade_at": quote.last_trade_at.isoformat(),
            "spot_is_real_time": quote.is_real_time,
            "fair_up_probability": round(assessment.fair_up_probability, 6),
            "up_ask": up_ask,
            "down_ask": down_ask,
            "prior_close": assessment.prior_close,
            "realized_volatility": round(assessment.annualized_realized_volatility, 6),
            "volatility_estimator": assessment.volatility_estimator,
            "data_is_fresh": assessment.data_is_fresh,
            "model_error_buffer": assessment.model_error_buffer,
            "paper_outcome": assessment.paper_outcome,
            "provider": provider,
            "up_fee_rate": up_fee_rate,
            "down_fee_rate": down_fee_rate,
            "up_taker_fee": assessment.up_edge.estimated_taker_fee,
            "down_taker_fee": assessment.down_edge.estimated_taker_fee,
            "signal_status": _signal_status(assessment.paper_outcome),
            "up_model_edge_before_costs": round(assessment.fair_up_probability - up_ask, 6),
            "down_model_edge_before_costs": round(1 - assessment.fair_up_probability - down_ask, 6),
        }
        log_event(settings.log_path, "NASDAQ_BASELINE_EVALUATED", result)
        print(json.dumps(result, sort_keys=True))
    elif arguments.command == "replay-settled":
        report = replay_settled_positions(journal.list_paper_positions()).as_payload()
        if arguments.output:
            Path(arguments.output).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "historical-backtest":
        try:
            candidate = MarketCandidate.from_gamma_payload(
                journal.get_market_candidate_raw_payload(arguments.market_id)
            )
            outcomes = journal.get_market_outcome_tokens(arguments.market_id)
        except KeyError as error:
            raise SystemExit(f"Unknown market id: {error}") from error
        try:
            contract = parse_daily_equity_close_contract(candidate)
        except EquityContractParseError as error:
            raise SystemExit(f"historical-backtest rejected market contract: {error}") from error
        if arguments.symbol.upper() != contract.symbol:
            raise SystemExit(
                f"historical-backtest symbol {arguments.symbol.upper()} does not match contract ticker "
                f"{contract.symbol}"
            )
        closes = load_daily_closes_csv(Path(arguments.history_csv))
        if len(closes) < arguments.lookback_days + 2:
            raise SystemExit("history CSV must contain lookback closes plus one final close row")
        final_close = closes[-1]
        closes_before_market = closes[:-1]
        spot_history = load_intraday_spots_csv(Path(arguments.spot_csv)) if arguments.spot_csv else ()
        start_at = datetime.fromisoformat(arguments.start_at.replace("Z", "+00:00"))
        end_at = (
            datetime.fromisoformat(arguments.end_at.replace("Z", "+00:00"))
            if arguments.end_at
            else contract.resolves_at
        )
        history_client = ClobPriceHistoryClient()
        try:
            up_history = history_client.prices_history(outcomes[0].token_id, start_at=start_at, end_at=end_at)
            down_history = history_client.prices_history(outcomes[1].token_id, start_at=start_at, end_at=end_at)
            settlement = GammaMarketClient().get_market_settlement(arguments.market_id)
        except PublicApiError as error:
            _report_public_api_failure(settings, "HISTORICAL_BACKTEST_DATA_FAILED", error)
        report = replay_daily_up_down_market(
            candidate=candidate,
            symbol=arguments.symbol,
            resolves_at=contract.resolves_at,
            closes_before_market=closes_before_market,
            final_close=final_close,
            up_history=up_history,
            down_history=down_history,
            settlement=settlement,
            spot_history=spot_history,
            minimum_edge=arguments.minimum_edge,
            model_error_buffer=arguments.model_error_buffer,
            lookback_days=arguments.lookback_days,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "replay-observations":
        print(json.dumps(replay_market_observations(journal.list_replay_observations()).as_payload(), sort_keys=True))
    elif arguments.command == "strategy-diagnostics":
        report = strategy_diagnostics(
            journal.list_buffer_sweep_observations(),
            journal.list_execution_observations(),
            journal.list_spot_source_comparisons(sample_every_seconds=60),
            spots=journal.list_spot_observations(source="PYTH_HERMES", sample_every_seconds=60),
            requested_shares=arguments.shares,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "close-source-calibration":
        api_key = os.getenv("PYTH_PRO_API_KEY", "")
        if not api_key:
            raise SystemExit("close-source-calibration requires PYTH_PRO_API_KEY in .env while the trial is active")
        try:
            market_date = date.fromisoformat(arguments.market_date)
        except ValueError as error:
            raise SystemExit("close-source-calibration --market-date must be YYYY-MM-DD") from error
        all_spots = journal.list_spot_observations()
        finnhub_spots = tuple(
            SpotQuote(item.source, item.symbol, item.price, item.observed_at, item.published_at)
            for item in all_spots
            if item.source == "FINNHUB"
        )
        pyth_spots = tuple(
            SpotQuote(item.source, item.symbol, item.price, item.observed_at, item.published_at)
            for item in all_spots
            if item.source == "PYTH_HERMES"
        )
        symbols = (
            tuple(item.strip().upper() for item in arguments.symbols.split(",") if item.strip())
            if arguments.symbols
            else tuple(sorted({item.symbol for item in finnhub_spots}))
        )
        report = calibrate_close_sources(
            client=PythHistoryClient(api_key),
            market_date=market_date,
            symbols=symbols,
            finnhub_spots=finnhub_spots,
            pyth_live_spots=pyth_spots,
        )
        payload = report.as_payload()
        for observation in payload["observations"]:
            journal.record_close_source_calibration(observation)
        _write_optional_json(arguments.output, payload)
        print(json.dumps(payload, sort_keys=True))
    else:
        raise AssertionError(f"Unexpected command for handler: {arguments.command}")
