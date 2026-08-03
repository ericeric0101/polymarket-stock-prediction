"""Operations command handlers."""

from __future__ import annotations

import json
import os
import ssl
from datetime import UTC, datetime
from pathlib import Path

from .. import cli_runtime
from ..alpaca_options import AlpacaCredentials, AlpacaIndicativeOptionsClient
from ..equity_contracts import EquityContractParseError, parse_daily_equity_close_contract
from ..fees import PolymarketFeeRateClient
from ..http import PublicApiError
from ..logging import log_event
from ..market_discovery import MarketCandidate
from ..nasdaq_data import NasdaqBaselineClient, NasdaqPayloadError, load_baseline_cache, save_baseline_cache
from ..option_pricing_validation import OptionPricingInputs, validate_option_quote
from .context import CommandContext
from .shared import _maker_quote_payload, _paper_position_payload, _run_async, _run_shadow_stream, _stream_credentials


def handle(context: CommandContext) -> None:
    arguments = context.arguments
    settings = context.settings
    journal = context.journal
    if arguments.command == "validate-option-pricing":
        inputs = OptionPricingInputs(
            spot=arguments.spot,
            strike=arguments.strike,
            annual_volatility=arguments.annual_volatility,
            seconds_to_expiry=arguments.seconds_to_expiry,
            option_type=arguments.option_type,
            risk_free_rate=arguments.risk_free_rate,
            dividend_yield=arguments.dividend_yield,
        )
        result = validate_option_quote(
            inputs, bid=arguments.bid, ask=arguments.ask, style=arguments.style, binomial_steps=arguments.binomial_steps
        )
        print(json.dumps(result.as_payload(), sort_keys=True))
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
                raise SystemExit(
                    "stream-shadow --spot-provider alpaca requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env"
                )
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
            journal.record_contract_review(arguments.market_id, accepted=False, reason=str(error))
            raise SystemExit(f"stream-shadow rejected market contract: {error}") from error
        journal.record_contract_review(
            arguments.market_id, accepted=True, reason="PYTH_DAILY_CLOSE_TEMPLATE", contract=contract.as_payload()
        )
        if arguments.symbol.upper() != contract.symbol:
            raise SystemExit(
                f"stream-shadow symbol {arguments.symbol.upper()} does not match contract ticker {contract.symbol}"
            )
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
                    settings,
                    arguments.market_id,
                    tuple(item.token_id for item in outcomes),
                    arguments.symbol.upper(),
                    arguments.spot_provider,
                    api_key,
                    api_secret,
                    finnhub_api_key,
                    resolves_at,
                    closes,
                    daily_provider,
                    cached_quote.price,
                    cached_quote.last_trade_at,
                    contract.as_payload(),
                    up_fee_rate,
                    down_fee_rate,
                    arguments.duration_seconds,
                    journal,
                )
            )
        except ssl.SSLCertVerificationError as error:
            raise SystemExit(
                "WebSocket TLS verification failed. Set SSL_CERT_FILE in .env to the PEM file for your "
                "VPN or proxy certificate authority; SSL verification remains enabled."
            ) from error
    elif arguments.command == "supervise-shadow":
        cli_runtime.supervise_shadow(arguments, settings, journal, _stream_credentials, _run_async)
    elif arguments.command == "paper-positions":
        positions = journal.list_paper_positions(arguments.status)
        print(json.dumps([_paper_position_payload(position) for position in positions], sort_keys=True))
    elif arguments.command == "maker-shadow-quotes":
        print(
            json.dumps(
                [_maker_quote_payload(quote) for quote in journal.list_maker_shadow_quotes(arguments.status)],
                sort_keys=True,
            )
        )
    elif arguments.command == "portfolio-decisions":
        print(json.dumps(journal.list_portfolio_decisions(arguments.limit), sort_keys=True))
    elif arguments.command == "paper-performance":
        print(cli_runtime.paper_performance_payload(journal))
    elif arguments.command == "dashboard":
        cli_runtime.dashboard(arguments, journal)
    elif arguments.command == "research-dashboard":
        cli_runtime.research_dashboard(arguments, settings)
    elif arguments.command == "settle-paper-positions":
        cli_runtime.settle_paper_positions(settings, journal, _run_async)
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
    else:
        raise AssertionError(f"Unexpected command for handler: {arguments.command}")
