"""Scheduled multi-market shadow observation and hold-to-settlement lifecycle."""

from __future__ import annotations

import asyncio
from functools import partial
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
from typing import Callable, Mapping

from .domain import EventSink, NEW_YORK
from .baseline import DailyClose, daily_close_data_is_fresh
from .calibration import CalibrationRecommendation, load_calibration_recommendation
from .checkpoints import checkpoint_window
from .close_source_calibration import calibrate_close_sources, official_pyth_final_minute_close
from .equity_contracts import DailyEquityCloseContract, EquityContractParseError, parse_daily_equity_close_contract
from .event_risk import (
    EventCalendarError,
    EventCalendarUnavailable,
    FinnhubEarningsCalendarClient,
    combined_risk_events,
)
from .fees import PolymarketFeeRateClient
from .http import PublicApiError
from .journal import MakerShadowQuote, PaperBatchEntry, ShadowJournal
from .logging import log_event
from .market_discovery import GammaMarketClient, MarketCandidate
from .maker_shadow import MakerQuoteProposal, propose_maker_buy_quote
from .nasdaq_data import NasdaqBaselineClient, NasdaqPayloadError, NasdaqQuote, load_baseline_cache, save_baseline_cache
from .option_iv import OptionIvSurface, OptionSurfaceError, PolygonOptionIvClient, TradierOptionIvClient
from .portfolio_risk import PaperEntryCandidate, select_diversified_entries
from .pyth_benchmarks import PythBenchmarksClient
from .pyth_history import PythHistoryClient
from .quality import observable_equity_market_date
from .trading_calendar import previous_nyse_trading_day
from .threshold_estimation import ThresholdSource, calibrated_threshold_estimate
from .yahoo_data import YahooChartClient, YahooPayloadError
from .realtime import RealtimeBaselineEvaluator, RealtimeEvaluation
from .storage.writer import JournalWriter
from .streaming import (
    AlpacaIexStockStream,
    FinnhubStockStream,
    PolymarketMarketStream,
    PythHermesStockStream,
    ShadowStreamCoordinator,
    SpotQuote,
    run_with_reconnect,
)


MODEL_VERSION = "realized-vol-observation-v1-buffer-2pct"
IV_MODEL_VERSION = "iv-blend-v1-buffer-2pct"
MAX_OPTION_IV_AGE_SECONDS = 900.0


def symbol_from_candidate(candidate: MarketCandidate) -> str | None:
    """Daily equity templates publish the ticker in parentheses in the question."""

    match = re.search(r"\(([A-Z][A-Z.]{0,9})\)", candidate.question.upper())
    return match.group(1) if match else None


def select_active_candidates(
    candidates: tuple[MarketCandidate, ...], *, now: datetime, max_markets: int, minimum_seconds_to_resolution: float
) -> tuple[MarketCandidate, ...]:
    if now.tzinfo is None or max_markets < 1 or minimum_seconds_to_resolution < 0:
        raise ValueError("invalid active-universe selection inputs")
    selected: list[MarketCandidate] = []
    observation_date = observable_equity_market_date(now)
    for candidate in sorted(candidates, key=lambda item: (item.end_date, item.market_id)):
        try:
            resolves_at = datetime.fromisoformat(candidate.end_date.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Outside the regular session, observe the next tradable contract so the
        # Polymarket book remains visible without enabling a stock-model decision.
        if observation_date != resolves_at.astimezone(NEW_YORK).date():
            continue
        if not symbol_from_candidate(candidate) or (resolves_at - now).total_seconds() < minimum_seconds_to_resolution:
            continue
        selected.append(candidate)
        if len(selected) >= max_markets:
            break
    return tuple(selected)


@dataclass
class ActiveMarket:
    candidate: MarketCandidate
    contract: DailyEquityCloseContract
    symbol: str
    evaluator: RealtimeBaselineEvaluator
    daily_provider: str
    up_fee_rate: float | None
    down_fee_rate: float | None
    reference_spot: float
    reference_spot_observed_at: datetime
    price_to_beat: float
    pyth_reference: Mapping[str, object]
    option_surface: OptionIvSurface | None
    option_quality_flags: tuple[str, ...]
    risk_reasons: tuple[str, ...]
    additional_model_error_buffer: float
    coordinator: ShadowStreamCoordinator
    last_skip_reasons: tuple[str, ...] | None = None
    last_skip_logged_at: datetime | None = None
    last_evaluation_recorded_at: datetime | None = None
    recorded_checkpoint_keys: set[tuple[str, str]] = field(default_factory=set)

    @property
    def token_ids(self) -> tuple[str, str]:
        return (self.candidate.outcome_a_token_id, self.candidate.outcome_b_token_id)


@dataclass(frozen=True)
class PendingPaperEntry:
    candidate: PaperEntryCandidate
    runtime: ActiveMarket
    evaluation: RealtimeEvaluation
    payload: Mapping[str, object]
    created_at: datetime


from .stream_routing import MultiMarketRouter


class MultiMarketShadowSupervisor:
    """Refreshes the active universe and manages paper entries through settlement."""

    def __init__(
        self,
        *,
        journal: ShadowJournal,
        log_path: Path,
        spot_provider: str,
        volatility_estimator: str = "CLOSE_TO_CLOSE",
        volatility_decay: float = 0.94,
        comparison_estimators: tuple[str, ...] = ("EWMA",),
        finnhub_api_key: str = "",
        alpaca_api_key: str = "",
        alpaca_api_secret: str = "",
        max_markets: int = 18,
        minimum_seconds_to_resolution: float = 900,
        maker_minimum_edge: float = 0.005,
        maker_reprice_minimum_price_change: float = 0.02,
        maker_minimum_quote_lifetime_seconds: float = 30.0,
        paper_batch_seconds: float = 30.0,
        max_daily_paper_entries: int = 3,
        max_per_risk_group: int = 1,
        max_same_direction_paper_entries: int = 2,
        gamma_client: GammaMarketClient | None = None,
        daily_client: NasdaqBaselineClient | None = None,
        fee_client: PolymarketFeeRateClient | None = None,
        pyth_client: PythBenchmarksClient | None = None,
        pyth_api_key: str = "",
        pyth_pro_api_key: str = "",
        spot_mode: str = "FINNHUB_ONLY",
        finnhub_threshold_safety_bps: float = 35.0,
        tradier_api_token: str = "",
        polygon_api_key: str = "",
        event_calendar_path: Path = Path("data/event_calendar.json"),
        calibration_path: Path = Path("data/model_calibration.json"),
        event_sink: EventSink | None = None,
    ) -> None:
        if spot_provider not in {"finnhub", "alpaca"}:
            raise ValueError("spot_provider must be finnhub or alpaca")
        normalized_spot_mode = spot_mode.upper()
        if normalized_spot_mode not in {"PYTH_PRIMARY", "FINNHUB_ONLY"}:
            raise ValueError("spot_mode must be PYTH_PRIMARY or FINNHUB_ONLY")
        if normalized_spot_mode == "FINNHUB_ONLY" and spot_provider != "finnhub":
            raise ValueError("FINNHUB_ONLY mode requires --spot-provider finnhub")
        if finnhub_threshold_safety_bps < 0:
            raise ValueError("finnhub_threshold_safety_bps must be non-negative")
        if volatility_estimator.upper() not in {"CLOSE_TO_CLOSE", "EWMA"}:
            raise ValueError("supervisor volatility_estimator must be CLOSE_TO_CLOSE or EWMA")
        if not 0 < volatility_decay < 1:
            raise ValueError("volatility_decay must be between zero and one")
        normalized_comparisons = tuple(
            dict.fromkeys(
                item.upper() for item in comparison_estimators if item.upper() != volatility_estimator.upper()
            )
        )
        if any(item not in {"CLOSE_TO_CLOSE", "EWMA"} for item in normalized_comparisons):
            raise ValueError("supervisor comparison_estimators must be CLOSE_TO_CLOSE or EWMA")
        if max_markets < 1:
            raise ValueError("max_markets must be positive")
        if maker_minimum_edge < 0 or maker_minimum_edge >= 1:
            raise ValueError("maker_minimum_edge must be between 0 and 1")
        if maker_reprice_minimum_price_change < 0 or maker_minimum_quote_lifetime_seconds < 0:
            raise ValueError("maker reprice thresholds must be non-negative")
        if (
            paper_batch_seconds <= 0
            or min(max_daily_paper_entries, max_per_risk_group, max_same_direction_paper_entries) < 1
        ):
            raise ValueError("paper portfolio limits must be positive")
        self.journal = journal
        self.log_path = log_path
        self.spot_provider = spot_provider
        self.spot_mode = normalized_spot_mode
        self.finnhub_threshold_safety_bps = finnhub_threshold_safety_bps
        self.volatility_estimator = volatility_estimator.upper()
        self.volatility_decay = volatility_decay
        self.comparison_estimators = normalized_comparisons
        self.finnhub_api_key = finnhub_api_key
        self.alpaca_api_key = alpaca_api_key
        self.alpaca_api_secret = alpaca_api_secret
        self.max_markets = max_markets
        self.minimum_seconds_to_resolution = minimum_seconds_to_resolution
        self.maker_minimum_edge = maker_minimum_edge
        self.maker_reprice_minimum_price_change = maker_reprice_minimum_price_change
        self.maker_minimum_quote_lifetime_seconds = maker_minimum_quote_lifetime_seconds
        self.paper_batch_seconds = paper_batch_seconds
        self.max_daily_paper_entries = max_daily_paper_entries
        self.max_per_risk_group = max_per_risk_group
        self.max_same_direction_paper_entries = max_same_direction_paper_entries
        self.gamma = gamma_client or GammaMarketClient()
        self.daily_client = daily_client or NasdaqBaselineClient()
        self.fee_client = fee_client or PolymarketFeeRateClient()
        self.pyth_api_key = pyth_api_key.strip()
        self.pyth_pro_api_key = pyth_pro_api_key.strip()
        # Pyth Pro credentials authenticate Hermes too. Keep PYTH_API_KEY as an
        # explicit override for a distinct Core/Hermes credential.
        self.pyth_live_api_key = self.pyth_api_key or self.pyth_pro_api_key
        self.pyth_client = pyth_client or PythBenchmarksClient(api_key=self.pyth_live_api_key)
        self._pyth_feed_ids: dict[str, str] = {}
        self.earnings_client = FinnhubEarningsCalendarClient(finnhub_api_key)
        # Polygon/Massive is preferred when configured because it is a data-only
        # provider; its adapter rejects free/15-minute-delayed data for entries.
        self.option_client = (
            PolygonOptionIvClient(polygon_api_key)
            if polygon_api_key.strip()
            else TradierOptionIvClient(tradier_api_token)
        )
        self.event_calendar_path = event_calendar_path
        self.calibration_path = calibration_path
        try:
            self.calibration: CalibrationRecommendation | None = load_calibration_recommendation(calibration_path)
        except ValueError:
            self.calibration = None
        self.event_sink = event_sink or self._default_event_sink
        self._writer = JournalWriter(on_error=self._on_writer_error)
        self.runtimes: dict[str, ActiveMarket] = {}
        self._stream_tasks: list[asyncio.Task[None]] = []
        self._stream_runtimes: dict[str, ActiveMarket] = {}
        self._stream_signature: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self._close_calibration_attempted_dates: set[str] = set()
        self._pyth_close_cache_attempted_dates: set[str] = set()
        self._pending_paper_entries: dict[str, PendingPaperEntry] = {}
        self._paper_batch_task: asyncio.Task[None] | None = None
        self._markout_tasks: set[asyncio.Task[None]] = set()

    def _default_event_sink(self, event_type: str, payload: Mapping[str, object]) -> None:
        log_event(self.log_path, event_type, payload)
        print(json.dumps({"event_type": event_type, **payload}, sort_keys=True, default=str))

    async def refresh(self) -> None:
        """Discover current candidates, settle completed positions, and reconcile streams."""

        now = datetime.now(UTC)
        await self.reconcile_settlements()
        report = await asyncio.to_thread(
            self.gamma.discover_active_equity_candidates,
            tag_slugs=("stocks", "equities"),
            page_size=500,
            max_pages_per_tag=100,
            pause_seconds=0.2,
        )
        for candidate in report.candidates:
            self.journal.upsert_market_candidate(candidate)
        selected = select_active_candidates(
            report.candidates,
            now=now,
            max_markets=self.max_markets,
            minimum_seconds_to_resolution=self.minimum_seconds_to_resolution,
        )
        runtimes: dict[str, ActiveMarket] = {}
        for candidate in selected:
            try:
                contract = parse_daily_equity_close_contract(candidate)
            except EquityContractParseError as error:
                self.journal.record_contract_review(candidate.market_id, accepted=False, reason=str(error))
                self.event_sink("SUPERVISOR_MARKET_SKIPPED", {"market_id": candidate.market_id, "reason": str(error)})
                continue
            self.journal.record_contract_review(
                candidate.market_id, accepted=True, reason="PYTH_DAILY_CLOSE_TEMPLATE", contract=contract.as_payload()
            )
            symbol = contract.symbol
            existing = self.runtimes.get(candidate.market_id)
            if (
                existing
                and existing.symbol == symbol
                and existing.candidate.end_date == candidate.end_date
                and existing.token_ids == (candidate.outcome_a_token_id, candidate.outcome_b_token_id)
            ):
                existing.up_fee_rate, existing.down_fee_rate = await asyncio.to_thread(self._fee_rates, candidate)
                live_quote = existing.coordinator.latest_quote(
                    "PYTH_HERMES" if self.spot_mode == "PYTH_PRIMARY" else "FINNHUB",
                    symbol,
                )
                spot_for_options = live_quote.price if live_quote else existing.reference_spot
                existing.option_surface, existing.option_quality_flags = await asyncio.to_thread(
                    self._option_surface, symbol, spot_for_options, now, contract.resolves_at
                )
                existing.risk_reasons = await self._resolve_risk_reasons(
                    market_id=candidate.market_id,
                    symbol=symbol,
                    now=now,
                    resolves_at=contract.resolves_at,
                )
                runtimes[candidate.market_id] = existing
                continue
            try:
                closes, provider, reference_quote = await asyncio.to_thread(self._daily_closes, symbol, now)
            except (NasdaqPayloadError, PublicApiError, OSError) as error:
                self.event_sink(
                    "SUPERVISOR_MARKET_SKIPPED",
                    {"market_id": candidate.market_id, "symbol": symbol, "reason": str(error)},
                )
                continue
            yahoo_closes: tuple[DailyClose, ...] = ()
            if self.spot_mode == "FINNHUB_ONLY":
                try:
                    yahoo_closes = await asyncio.to_thread(self._yahoo_closes, symbol, contract)
                except (PublicApiError, YahooPayloadError, OSError, ValueError) as error:
                    self.event_sink(
                        "SUPERVISOR_THRESHOLD_SOURCE_UNAVAILABLE",
                        {
                            "market_id": candidate.market_id,
                            "symbol": symbol,
                            "source": "YAHOO_DAILY_CLOSE",
                            "error": str(error),
                        },
                    )
            try:
                price_to_beat, pyth_reference = await asyncio.to_thread(
                    self._pyth_price_to_beat,
                    contract,
                    closes,
                    yahoo_closes,
                )
            except (PublicApiError, YahooPayloadError, NasdaqPayloadError, OSError, ValueError) as error:
                self.event_sink(
                    "SUPERVISOR_MARKET_SKIPPED",
                    {
                        "market_id": candidate.market_id,
                        "symbol": symbol,
                        "reason": "PYTH_REFERENCE_UNAVAILABLE",
                        "error": str(error),
                    },
                )
                continue
            except Exception as error:
                self.event_sink(
                    "SUPERVISOR_INTERNAL_ERROR",
                    {
                        "market_id": candidate.market_id,
                        "symbol": symbol,
                        "stage": "price_to_beat",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                )
                continue
            up_fee_rate, down_fee_rate = await asyncio.to_thread(self._fee_rates, candidate)
            option_surface, option_flags = await asyncio.to_thread(
                self._option_surface, symbol, reference_quote.price, now, contract.resolves_at
            )
            risk_reasons = await self._resolve_risk_reasons(
                market_id=candidate.market_id,
                symbol=symbol,
                now=now,
                resolves_at=contract.resolves_at,
            )
            runtimes[candidate.market_id] = self._make_runtime(
                candidate,
                contract,
                closes,
                provider,
                reference_quote,
                price_to_beat,
                pyth_reference,
                up_fee_rate,
                down_fee_rate,
                option_surface,
                option_flags,
                risk_reasons,
            )
        self.runtimes = runtimes
        self.event_sink(
            "SUPERVISOR_UNIVERSE_REFRESHED",
            {
                "candidate_count": len(report.candidates),
                "active_market_count": len(runtimes),
                "events_scanned": report.events_scanned,
                "pages_scanned": report.pages_scanned,
            },
        )
        await self._reconcile_streams()

    def _on_writer_error(self, error: Exception) -> None:
        self.event_sink("JOURNAL_WRITE_FAILED", {"error_type": type(error).__name__, "error": str(error)})

    def _submit_write(self, kind: str, operation: Callable[[], None]) -> None:
        if not self._writer.submit(operation):
            self.event_sink("JOURNAL_WRITE_BACKPRESSURE", {"kind": kind, "queue": "full"})

    async def _resolve_risk_reasons(
        self,
        *,
        market_id: str,
        symbol: str,
        now: datetime,
        resolves_at: datetime,
    ) -> tuple[str, ...]:
        try:
            events = await asyncio.to_thread(
                combined_risk_events,
                self.event_calendar_path,
                symbol,
                now,
                resolves_at,
                self.finnhub_api_key,
                self.earnings_client,
            )
        except EventCalendarUnavailable as error:
            self.event_sink("SUPERVISOR_EVENT_CALENDAR_UNAVAILABLE", {"market_id": market_id, "error": str(error)})
            return ("EVENT_CALENDAR_UNAVAILABLE",)
        except EventCalendarError as error:
            self.event_sink("SUPERVISOR_EVENT_CALENDAR_ERROR", {"market_id": market_id, "error": str(error)})
            return ("EVENT_CALENDAR_INVALID",)
        return tuple(f"BLOCKING_EVENT:{event.kind.upper()}" for event in events if event.blocking)

    def _pyth_price_to_beat(
        self,
        contract: DailyEquityCloseContract,
        fallback_closes: list[DailyClose] | None = None,
        yahoo_closes: tuple[DailyClose, ...] = (),
    ) -> tuple[float, Mapping[str, object]]:
        market_day = contract.resolves_at.astimezone(NEW_YORK).date()
        prior_day = previous_nyse_trading_day(market_day)
        prior_day_key = prior_day.isoformat()
        cached = self.journal.get_pyth_daily_close(market_date=prior_day_key, symbol=contract.symbol)
        if cached is not None:
            return float(cached["price"]), {
                **cached,
                "source": "LOCAL_PYTH_FINAL_CANDLE",
                "threshold_quality": "EXACT_PYTH",
                "source_count": 1,
                "calibration_samples": 0,
                "estimated_error_bps": 0.0,
            }
        if self.spot_mode == "FINNHUB_ONLY":
            sources: list[ThresholdSource] = []
            nasdaq = _close_for_date(fallback_closes or (), prior_day_key)
            if nasdaq is not None:
                sources.append(ThresholdSource("NASDAQ_DAILY_CLOSE", nasdaq))
            yahoo = _close_for_date(yahoo_closes, prior_day_key)
            if yahoo is not None:
                sources.append(ThresholdSource("YAHOO_DAILY_CLOSE", yahoo))
            finnhub = self.journal.last_regular_spot_observation(
                source="FINNHUB",
                symbol=contract.symbol,
                market_date=prior_day_key,
            )
            if finnhub is not None:
                sources.append(
                    ThresholdSource(
                        "FINNHUB_LAST_REGULAR_TRADE",
                        float(finnhub["price"]),
                        "FINNHUB_CLOSE_WINDOW",
                    )
                )
            estimate = calibrated_threshold_estimate(sources, self.journal.list_close_source_calibrations())
            return estimate.price, {
                **estimate.as_payload(),
                "source": "CALIBRATED_NON_PYTH_THRESHOLD_ESTIMATE",
                "market_date": prior_day_key,
                "warning": "Pyth prior close unavailable; this is a calibrated non-settlement estimate",
            }
        requested_at = (
            datetime.combine(prior_day, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=16).astimezone(UTC)
        )
        feed_id = self._pyth_feed_ids.get(contract.symbol)
        if feed_id is None:
            feed_id = self.pyth_client.equity_feed_id(contract.symbol)
            self._pyth_feed_ids[contract.symbol] = feed_id
        reference = self.pyth_client.price_at(
            symbol=contract.symbol,
            feed_id=feed_id,
            observed_at=requested_at,
            maximum_delay_seconds=300,
        )
        return reference.price, reference.as_payload()

    def _yahoo_closes(self, symbol: str, contract: DailyEquityCloseContract) -> tuple[DailyClose, ...]:
        market_day = contract.resolves_at.astimezone(NEW_YORK).date()
        prior_day = previous_nyse_trading_day(market_day)
        return (
            YahooChartClient()
            .daily_closes(
                symbol,
                start_date=prior_day,
                end_date=prior_day,
            )
            .closes
        )

    def _daily_closes(self, symbol: str, now: datetime) -> tuple[list[DailyClose], str, NasdaqQuote]:
        cache_path = Path("data") / "baseline_cache" / f"{symbol}.json"
        try:
            quote, closes = load_baseline_cache(cache_path)
            if daily_close_data_is_fresh(closes, now):
                return closes, "NASDAQ_LOCAL_CACHE_NON_SETTLEMENT", quote
        except NasdaqPayloadError:
            pass
        quote = self.daily_client.latest_quote(symbol)
        closes = self.daily_client.daily_closes(symbol, now)
        save_baseline_cache(cache_path, quote, closes)
        return closes, "NASDAQ_PUBLIC_NON_SETTLEMENT", quote

    def _option_surface(
        self, symbol: str, spot: float, now: datetime, resolves_at: datetime
    ) -> tuple[OptionIvSurface | None, tuple[str, ...]]:
        if not self.option_client.configured:
            return None, ("OPTION_IV_PROVIDER_UNCONFIGURED",)
        try:
            surface = self.option_client.option_surface(symbol, spot, now, resolves_at)
        except (OptionSurfaceError, OSError) as error:
            reason = str(error).replace(" ", "_")[:120] or type(error).__name__
            return None, (f"OPTION_IV_UNAVAILABLE:{reason}",)
        return surface, surface.quality_flags

    def _fee_rates(self, candidate: MarketCandidate) -> tuple[float | None, float | None]:
        """Never substitute a guessed fee: unavailable rates simply remain unavailable."""

        try:
            up_rate = self.fee_client.get_fee_rate(candidate.outcome_a_token_id).fee_rate
            down_rate = self.fee_client.get_fee_rate(candidate.outcome_b_token_id).fee_rate
        except Exception as error:
            self.event_sink("SUPERVISOR_FEE_RATE_UNAVAILABLE", {"market_id": candidate.market_id, "error": str(error)})
            return None, None
        return up_rate, down_rate

    def _make_runtime(
        self,
        candidate: MarketCandidate,
        contract: DailyEquityCloseContract,
        closes: list[DailyClose],
        daily_provider: str,
        reference_quote: NasdaqQuote,
        price_to_beat: float,
        pyth_reference: Mapping[str, object],
        up_fee_rate: float | None,
        down_fee_rate: float | None,
        option_surface: OptionIvSurface | None,
        option_quality_flags: tuple[str, ...],
        risk_reasons: tuple[str, ...],
    ) -> ActiveMarket:
        calibrated_minimum_edge = (
            max(0.02, self.calibration.recommended_minimum_edge)
            if self.calibration and self.calibration.recommended_minimum_edge
            else 0.02
        )
        evaluator = RealtimeBaselineEvaluator(
            market_id=candidate.market_id,
            symbol=contract.symbol,
            resolves_at=contract.resolves_at,
            closes=closes,
            spot_provider=("PYTH_HERMES" if self.spot_mode == "PYTH_PRIMARY" else "FINNHUB"),
            up_fee_rate=up_fee_rate,
            volatility_estimator=self.volatility_estimator,
            volatility_decay=self.volatility_decay,
            comparison_estimators=self.comparison_estimators,
            down_fee_rate=down_fee_rate,
            base_model_error_buffer=0.02,
            fallback_buffer=0.0,
            minimum_edge=calibrated_minimum_edge,
            price_to_beat=price_to_beat,
        )
        runtime: ActiveMarket

        async def evaluate_callback(payload: Mapping[str, object]) -> None:
            await self._evaluate_runtime(runtime, payload)

        async def record_spot_observation(payload: Mapping[str, object]) -> None:
            self._submit_write("SPOT_OBSERVATION", partial(self.journal.record_spot_observation, payload))

        async def record_spot_comparison(payload: Mapping[str, object]) -> None:
            self._submit_write("SPOT_COMPARISON", partial(self.journal.record_spot_source_comparison, payload))

        async def record_source_gap(payload: Mapping[str, object]) -> None:
            self.event_sink("SOURCE_SPOT_GAP_DETECTED", payload)

        async def record_reevaluation_error(payload: Mapping[str, object]) -> None:
            self.event_sink(str(payload["event_type"]), payload)

        coordinator = ShadowStreamCoordinator(
            callback=evaluate_callback,
            primary_spot_source=("PYTH_HERMES" if self.spot_mode == "PYTH_PRIMARY" else "FINNHUB"),
            comparison_spot_source=(self.spot_provider if self.spot_mode == "PYTH_PRIMARY" else None),
            spot_observation_callback=record_spot_observation,
            spot_comparison_callback=record_spot_comparison,
            source_gap_callback=record_source_gap,
            reevaluation_error_callback=record_reevaluation_error,
        )
        runtime = ActiveMarket(
            candidate,
            contract,
            contract.symbol,
            evaluator,
            daily_provider,
            up_fee_rate,
            down_fee_rate,
            reference_quote.price,
            reference_quote.last_trade_at,
            price_to_beat,
            pyth_reference,
            option_surface,
            option_quality_flags,
            risk_reasons,
            0.0,
            coordinator,
        )
        return runtime

    async def _evaluate_runtime(self, runtime: ActiveMarket, trigger: Mapping[str, object]) -> None:
        now = datetime.now(UTC)
        coordinator = runtime.coordinator
        token_ids = runtime.token_ids
        pyth_quote = coordinator.latest_quote("PYTH_HERMES", runtime.symbol)
        finnhub_quote = coordinator.latest_quote("FINNHUB", runtime.symbol)
        primary_quote = pyth_quote if self.spot_mode == "PYTH_PRIMARY" else finnhub_quote
        comparison_quote = finnhub_quote if self.spot_mode == "PYTH_PRIMARY" else None
        pyth_primary_risk_reasons = (
            _pyth_primary_risk_reasons(now, pyth_quote, coordinator.max_age_seconds)
            if self.spot_mode == "PYTH_PRIMARY"
            else ()
        )
        source_uncertainty_buffer = (
            _cross_source_uncertainty_buffer(now, pyth_quote, comparison_quote, coordinator.max_age_seconds)
            if self.spot_mode == "PYTH_PRIMARY"
            else 0.0
        )
        fallback_threshold_warning = None
        threshold_is_estimated = bool(runtime.pyth_reference.get("estimated"))
        estimated_error_bps = float(runtime.pyth_reference.get("estimated_error_bps") or 0.0)
        if self.spot_mode == "FINNHUB_ONLY" and primary_quote is not None:
            distance_bps = abs(primary_quote.price - runtime.price_to_beat) / runtime.price_to_beat * 10_000
            if distance_bps <= max(self.finnhub_threshold_safety_bps, estimated_error_bps):
                fallback_threshold_warning = "NEAR_ESTIMATED_THRESHOLD"
        evaluation = runtime.evaluator.evaluate(
            now=now,
            spot=primary_quote.price if primary_quote else None,
            up_ask=coordinator.latest_best_asks.get(token_ids[0]),
            down_ask=coordinator.latest_best_asks.get(token_ids[1]),
            up_bid=coordinator.latest_best_bids.get(token_ids[0]),
            down_bid=coordinator.latest_best_bids.get(token_ids[1]),
            reference_spot=comparison_quote.price if comparison_quote else None,
            reference_spot_age_seconds=_quote_age_seconds(now, comparison_quote) if comparison_quote else None,
            option_iv=_usable_option_iv(runtime.option_surface, now),
            option_skew=runtime.option_surface.put_call_skew if runtime.option_surface else None,
            option_iv_provider=runtime.option_surface.provider if runtime.option_surface else None,
            option_iv_age_seconds=_option_iv_age_seconds(runtime.option_surface, now),
            option_quality_flags=_current_option_quality_flags(
                runtime.option_surface, runtime.option_quality_flags, now
            ),
            risk_reasons=runtime.risk_reasons + pyth_primary_risk_reasons,
            additional_model_error_buffer=(
                runtime.additional_model_error_buffer
                + source_uncertainty_buffer
                + (min(0.03, max(0.01, estimated_error_bps / 10_000)) if threshold_is_estimated else 0.0)
            ),
            spot_age_seconds=_age_seconds(now, coordinator.freshness.last_spot_at),
            book_age_seconds=_age_seconds(now, coordinator.freshness.last_book_at),
            stream_ready=coordinator.freshness.ready(now),
            trigger_reasons=tuple(str(reason) for reason in trigger.get("reasons", ())),
        )
        model_version = IV_MODEL_VERSION if evaluation.option_iv_status == "IV_VALID" else MODEL_VERSION
        result = {
            **evaluation.as_payload(),
            "daily_provider": runtime.daily_provider,
            "model_version": model_version,
            "price_to_beat": runtime.price_to_beat,
            "pyth_reference": runtime.pyth_reference,
            "spot_mode": self.spot_mode,
            "threshold_quality": runtime.pyth_reference.get("threshold_quality", "EXACT_PYTH"),
            "threshold_warning": fallback_threshold_warning,
            "primary_spot_source": "PYTH_HERMES" if self.spot_mode == "PYTH_PRIMARY" else "FINNHUB",
            "primary_spot": primary_quote.as_payload() if primary_quote else None,
            "pyth_live_spot": pyth_quote.as_payload() if pyth_quote else None,
            "cross_check_spot_source": self.spot_provider.upper(),
            "cross_check_spot": comparison_quote.as_payload() if comparison_quote else None,
            "dual_source_gate_reasons": list(pyth_primary_risk_reasons),
            "cross_source_model_error_buffer": source_uncertainty_buffer,
            "contract": runtime.contract.as_payload(),
            "option_surface": runtime.option_surface.as_payload() if runtime.option_surface else None,
            "up_book": _book_summary(coordinator, token_ids[0]),
            "down_book": _book_summary(coordinator, token_ids[1]),
        }
        maker_quotes = await asyncio.to_thread(self._sync_maker_shadow_quotes, runtime, evaluation, result, now)
        result["maker_shadow_quotes"] = maker_quotes
        checkpoint = (
            checkpoint_window(now)
            if evaluation.market_session == "REGULAR" and evaluation.fair_up_probability is not None
            else None
        )
        checkpoint_recorded = False
        if checkpoint:
            checkpoint_key = (checkpoint.checkpoint_date, checkpoint.checkpoint_name)
            if checkpoint_key not in runtime.recorded_checkpoint_keys:
                checkpoint_recorded = await asyncio.to_thread(
                    self.journal.record_checkpoint_observation,
                    checkpoint_date=checkpoint.checkpoint_date,
                    checkpoint_name=checkpoint.checkpoint_name,
                    payload=result,
                )
                runtime.recorded_checkpoint_keys.add(checkpoint_key)
                if checkpoint_recorded:
                    await asyncio.to_thread(
                        self._record_execution_books,
                        runtime,
                        spot=evaluation.spot,
                        fair_up_probability=evaluation.fair_up_probability,
                        payload=result,
                        observed_at=now,
                        observation_kind="CHECKPOINT",
                        signal_id=None,
                    )
                    self.event_sink(
                        "CHECKPOINT_OBSERVATION_RECORDED",
                        {
                            "market_id": runtime.candidate.market_id,
                            "symbol": runtime.symbol,
                            "checkpoint_date": checkpoint.checkpoint_date,
                            "checkpoint_name": checkpoint.checkpoint_name,
                            "checkpoint_delay_seconds": round(checkpoint.delay_seconds, 3),
                        },
                    )

        paper_signal = evaluation.paper_outcome is not None and evaluation.fair_up_probability is not None
        if evaluation.skip_reasons:
            should_record = (
                runtime.last_skip_reasons != evaluation.skip_reasons
                or runtime.last_skip_logged_at is None
                or (now - runtime.last_skip_logged_at).total_seconds() >= 60
            )
            if should_record:
                runtime.last_skip_reasons = evaluation.skip_reasons
                runtime.last_skip_logged_at = now
        else:
            runtime.last_skip_reasons = None
            runtime.last_skip_logged_at = None
            should_record = (
                checkpoint_recorded
                or paper_signal
                or runtime.last_evaluation_recorded_at is None
                or (now - runtime.last_evaluation_recorded_at).total_seconds() >= 60
            )
        if should_record:
            self._submit_write("REALTIME_EVALUATION", partial(self.journal.record_realtime_evaluation, result))
            runtime.last_evaluation_recorded_at = now
            self.event_sink("REALTIME_BASELINE_EVALUATED", result)
        if paper_signal:
            await self._queue_paper_entry(runtime, evaluation, result, now)

    def _record_execution_books(
        self,
        runtime: ActiveMarket,
        *,
        spot: float | None,
        fair_up_probability: float | None,
        payload: Mapping[str, object],
        observed_at: datetime,
        observation_kind: str,
        signal_id: str | None,
    ) -> None:
        fair_up = fair_up_probability
        for outcome, token_id, fee_rate, fair_probability in (
            ("UP", runtime.token_ids[0], runtime.up_fee_rate, fair_up),
            ("DOWN", runtime.token_ids[1], runtime.down_fee_rate, 1 - fair_up if fair_up is not None else None),
        ):
            book = runtime.coordinator.latest_books.get(token_id, {})
            self.journal.record_execution_observation(
                observed_at=observed_at,
                signal_id=signal_id,
                observation_kind=observation_kind,
                market_id=runtime.candidate.market_id,
                symbol=runtime.symbol,
                outcome=outcome,
                token_id=token_id,
                spot=spot,
                price_to_beat=runtime.price_to_beat,
                fair_probability=fair_probability,
                best_bid=runtime.coordinator.latest_best_bids.get(token_id),
                best_ask=runtime.coordinator.latest_best_asks.get(token_id),
                fee_rate=fee_rate,
                book_payload=book,
                evaluation_payload=payload,
            )

    def _schedule_markouts(
        self,
        runtime: ActiveMarket,
        evaluation: RealtimeEvaluation,
        payload: Mapping[str, object],
        signal_id: str,
    ) -> None:
        for delay_seconds in (60, 300, 900, 1800):
            task = asyncio.create_task(
                self._record_markout_after_delay(runtime, evaluation, payload, signal_id, delay_seconds)
            )
            self._markout_tasks.add(task)
            task.add_done_callback(self._markout_tasks.discard)

    async def _record_markout_after_delay(
        self,
        runtime: ActiveMarket,
        evaluation: RealtimeEvaluation,
        payload: Mapping[str, object],
        signal_id: str,
        delay_seconds: int,
    ) -> None:
        await asyncio.sleep(delay_seconds)
        await asyncio.to_thread(
            self._record_execution_books,
            runtime,
            spot=evaluation.spot,
            fair_up_probability=evaluation.fair_up_probability,
            payload=payload,
            observed_at=datetime.now(UTC),
            observation_kind=f"MARKOUT_{delay_seconds}S",
            signal_id=signal_id,
        )

    async def _queue_paper_entry(
        self, runtime: ActiveMarket, evaluation: RealtimeEvaluation, payload: Mapping[str, object], now: datetime
    ) -> None:
        outcome = evaluation.paper_outcome
        entry_ask = evaluation.up_ask if outcome == "UP" else evaluation.down_ask
        edge = evaluation.up_edge if outcome == "UP" else evaluation.down_edge
        fair_up = evaluation.fair_up_probability
        if outcome not in {"UP", "DOWN"} or entry_ask is None or edge is None or fair_up is None:
            return
        fair_probability = float(fair_up) if outcome == "UP" else 1 - float(fair_up)
        self._pending_paper_entries[runtime.candidate.market_id] = PendingPaperEntry(
            PaperEntryCandidate(
                runtime.candidate.market_id, runtime.symbol, outcome, float(entry_ask), fair_probability, float(edge)
            ),
            runtime,
            evaluation,
            dict(payload),
            now,
        )
        if self._paper_batch_task is None or self._paper_batch_task.done():
            self._paper_batch_task = asyncio.create_task(self._flush_paper_entries_after_delay())

    async def _flush_paper_entries_after_delay(self) -> None:
        await asyncio.sleep(self.paper_batch_seconds)
        pending = tuple(self._pending_paper_entries.values())
        self._pending_paper_entries.clear()
        self._paper_batch_task = None
        if not pending:
            return
        now = datetime.now(UTC)
        today = now.astimezone(NEW_YORK).date()
        positions = await asyncio.to_thread(self.journal.list_paper_positions)
        existing = tuple(
            position
            for position in positions
            if position.included_in_calibration and position.opened_at.astimezone(NEW_YORK).date() == today
        )
        existing_market_ids = {position.market_id for position in existing}
        batch_id = f"{today.isoformat()}-{int(now.timestamp() // self.paper_batch_seconds)}"
        candidates = tuple(item.candidate for item in pending if item.candidate.market_id not in existing_market_ids)
        rejected_existing = tuple(item for item in pending if item.candidate.market_id in existing_market_ids)
        decisions = select_diversified_entries(
            candidates,
            existing_symbols=((position.symbol, position.outcome) for position in existing),
            max_daily_entries=self.max_daily_paper_entries,
            max_per_risk_group=self.max_per_risk_group,
            max_same_direction=self.max_same_direction_paper_entries,
        )
        pending_by_market = {item.candidate.market_id: item for item in pending}
        batch_entries: list[PaperBatchEntry] = []
        for item in rejected_existing:
            batch_entries.append(
                PaperBatchEntry(
                    market_id=item.candidate.market_id,
                    symbol=item.candidate.symbol,
                    outcome=item.candidate.outcome,
                    risk_group=item.candidate.risk_group,
                    edge=item.candidate.edge,
                    selected=False,
                    reason="ALREADY_POSITIONED",
                    payload=item.payload,
                )
            )
        for decision in decisions:
            item = pending_by_market[decision.candidate.market_id]
            fee_rate = item.runtime.up_fee_rate if decision.candidate.outcome == "UP" else item.runtime.down_fee_rate
            batch_entries.append(
                PaperBatchEntry(
                    market_id=decision.candidate.market_id,
                    symbol=decision.candidate.symbol,
                    outcome=decision.candidate.outcome,
                    risk_group=decision.candidate.risk_group,
                    edge=decision.candidate.edge,
                    selected=decision.accepted,
                    reason=decision.reason,
                    payload=item.payload,
                    entry_ask=decision.candidate.entry_ask if decision.accepted else None,
                    fair_probability=decision.candidate.fair_probability if decision.accepted else None,
                    model_version=str(item.payload["model_version"])
                    if decision.accepted and fee_rate is not None
                    else None,
                    fee_rate=fee_rate if decision.accepted else None,
                )
            )
        results = await asyncio.to_thread(
            self.journal.commit_paper_batch, batch_id=batch_id, entries=tuple(batch_entries), created_at=now
        )
        results_by_market = {result.market_id: result for result in results}
        for decision in decisions:
            item = pending_by_market[decision.candidate.market_id]
            if not decision.accepted:
                self.event_sink(
                    "PAPER_ENTRY_REJECTED",
                    {"batch_id": batch_id, "market_id": decision.candidate.market_id, "reason": decision.reason},
                )
                continue
            result = results_by_market[decision.candidate.market_id]
            if not result.created or result.position is None:
                continue
            position = result.position
            await asyncio.to_thread(
                self._record_execution_books,
                item.runtime,
                spot=item.evaluation.spot,
                fair_up_probability=item.evaluation.fair_up_probability,
                payload=item.payload,
                observed_at=now,
                observation_kind="PAPER_ENTRY",
                signal_id=position.position_id,
            )
            self._schedule_markouts(item.runtime, item.evaluation, item.payload, position.position_id)
            self.event_sink(
                "PAPER_POSITION_OPENED",
                {
                    "batch_id": batch_id,
                    "position_id": position.position_id,
                    "market_id": position.market_id,
                    "symbol": position.symbol,
                    "outcome": position.outcome,
                    "entry_ask": position.entry_ask,
                },
            )

    def _sync_maker_shadow_quotes(
        self, runtime: ActiveMarket, evaluation: RealtimeEvaluation, payload: Mapping[str, object], now: datetime
    ) -> list[Mapping[str, object]]:
        """Maintain research-only passive quotes; touches never become assumed fills."""

        quotes: list[Mapping[str, object]] = []
        fair_up = evaluation.fair_up_probability
        proposals: tuple[MakerQuoteProposal | None, MakerQuoteProposal | None]
        if fair_up is None or evaluation.skip_reasons:
            proposals = (None, None)
        else:
            proposals = (
                propose_maker_buy_quote(
                    outcome="UP",
                    fair_probability=float(fair_up),
                    best_bid=evaluation.up_bid,
                    best_ask=evaluation.up_ask,
                    minimum_edge=self.maker_minimum_edge,
                ),
                propose_maker_buy_quote(
                    outcome="DOWN",
                    fair_probability=1 - float(fair_up),
                    best_bid=evaluation.down_bid,
                    best_ask=evaluation.down_ask,
                    minimum_edge=self.maker_minimum_edge,
                ),
            )
        for outcome, proposal, current_ask in (
            ("UP", proposals[0], evaluation.up_ask),
            ("DOWN", proposals[1], evaluation.down_ask),
        ):
            if current_ask is not None:
                touched = self.journal.record_maker_shadow_touch(
                    market_id=runtime.candidate.market_id,
                    outcome=outcome,
                    current_ask=float(current_ask),
                    observed_at=now,
                )
                if touched is not None:
                    self.event_sink("MAKER_SHADOW_QUOTE_TOUCHED", _maker_quote_payload(touched))
            quote, action = self.journal.sync_maker_shadow_quote(
                market_id=runtime.candidate.market_id,
                symbol=runtime.symbol,
                outcome=outcome,
                limit_price=proposal.limit_price if proposal else None,
                fair_probability=proposal.fair_probability if proposal else None,
                theoretical_edge=proposal.theoretical_edge if proposal else None,
                best_bid=proposal.best_bid if proposal else None,
                best_ask=proposal.best_ask if proposal else None,
                payload={
                    "evaluated_at": now.isoformat(),
                    "model_version": payload.get("model_version", MODEL_VERSION),
                    "source": "MAKER_SHADOW",
                },
                no_quote_reason="MODEL_OR_DATA_INVALID" if fair_up is None else "NO_MAKER_EDGE",
                observed_at=now,
                minimum_reprice_price_change=self.maker_reprice_minimum_price_change,
                minimum_quote_lifetime_seconds=self.maker_minimum_quote_lifetime_seconds,
            )
            if action:
                event_type = f"MAKER_SHADOW_QUOTE_{action}"
                self.event_sink(
                    event_type,
                    _maker_quote_payload(quote)
                    if quote
                    else {"market_id": runtime.candidate.market_id, "symbol": runtime.symbol, "outcome": outcome},
                )
            if quote is not None:
                quotes.append(_maker_quote_payload(quote))
        return quotes

    async def _settle_open_positions(self) -> None:
        from .supervisor_settlement import settle_open_positions

        await settle_open_positions(self)

    async def _reconcile_evaluation_settlements(self) -> None:
        from .supervisor_settlement import reconcile_evaluation_settlements

        await reconcile_evaluation_settlements(self)

    async def settle_open_positions(self) -> None:
        await self.reconcile_settlements()

    async def reconcile_settlements(self) -> None:
        await self._settle_open_positions()
        await self._reconcile_evaluation_settlements()

    async def _reconcile_streams(self) -> None:
        token_ids = tuple(sorted(token for runtime in self.runtimes.values() for token in runtime.token_ids))
        symbols = tuple(sorted({runtime.symbol for runtime in self.runtimes.values()}))
        signature = (token_ids, symbols)
        if signature == self._stream_signature:
            return
        await self._stop_streams()
        self._stream_signature = signature
        if not token_ids:
            return
        router = MultiMarketRouter(self.runtimes, self.spot_provider, self._pyth_feed_ids)

        async def status_callback(payload: Mapping[str, object]) -> None:
            self.event_sink(str(payload["event_type"]), payload)

        if self.spot_provider == "finnhub":
            if not self.finnhub_api_key:
                raise ValueError("FINNHUB_API_KEY is required for supervisor streaming")
            spot_stream = FinnhubStockStream(self.finnhub_api_key)
        else:
            spot_stream = AlpacaIexStockStream(self.alpaca_api_key, self.alpaca_api_secret)
        spot_callback = router.on_spot_message
        pyth_feed_ids = {feed_id: symbol for symbol, feed_id in self._pyth_feed_ids.items() if symbol in symbols}
        self._stream_tasks = [
            asyncio.create_task(
                run_with_reconnect(
                    "POLYMARKET_MARKET",
                    lambda: PolymarketMarketStream().run(token_ids, router.on_polymarket_message),
                    status_callback,
                )
            ),
            asyncio.create_task(
                run_with_reconnect(
                    f"{self.spot_provider.upper()}_STOCK",
                    lambda: spot_stream.run(symbols, spot_callback),
                    status_callback,
                )
            ),
        ]
        if self.spot_mode == "PYTH_PRIMARY":
            self._stream_tasks.append(
                asyncio.create_task(
                    run_with_reconnect(
                        "PYTH_HERMES",
                        lambda: PythHermesStockStream(self.pyth_live_api_key).run(
                            pyth_feed_ids, router.on_pyth_message
                        ),
                        status_callback,
                    )
                )
            )
        self._stream_runtimes = dict(self.runtimes)

    def _maybe_record_pyth_daily_close_cache(self) -> None:
        if not self.pyth_pro_api_key:
            return
        now = datetime.now(UTC)
        local_now = now.astimezone(NEW_YORK)
        if (local_now.hour, local_now.minute) < (16, 3):
            return
        market_date = local_now.date()
        date_key = market_date.isoformat()
        if date_key in self._pyth_close_cache_attempted_dates:
            return
        symbols = tuple(sorted({runtime.symbol for runtime in self.runtimes.values()}))
        if not symbols:
            symbols = tuple(
                sorted(
                    {
                        item.symbol
                        for item in self.journal.list_spot_observations(source="FINNHUB", market_date=market_date)
                    }
                )
            )
        if not symbols:
            return
        self._pyth_close_cache_attempted_dates.add(date_key)
        try:
            client = PythHistoryClient(self.pyth_pro_api_key)
            for symbol in symbols:
                close_price, candle_at = official_pyth_final_minute_close(client, symbol, market_date)
                self.journal.record_pyth_daily_close(
                    market_date=date_key,
                    symbol=symbol,
                    close_price=close_price,
                    candle_at=candle_at,
                    source="PYTH_PRO_HISTORY_FINAL_MINUTE",
                )
            self.event_sink("PYTH_DAILY_CLOSE_CACHE_RECORDED", {"market_date": date_key, "symbols": len(symbols)})
        except Exception as error:
            self.event_sink("PYTH_DAILY_CLOSE_CACHE_FAILED", {"market_date": date_key, "error": str(error)})

    def _supplemental_close_sources(
        self,
        symbols: tuple[str, ...],
        market_date: date,
    ) -> Mapping[str, Mapping[str, float]]:
        """Best-effort free daily closes to calibrate against Pyth during the trial."""
        result: dict[str, dict[str, float]] = {"NASDAQ_DAILY_CLOSE": {}, "YAHOO_DAILY_CLOSE": {}}
        request_now = datetime.combine(market_date, datetime.max.time(), tzinfo=NEW_YORK).astimezone(UTC)
        for symbol in symbols:
            try:
                value = _close_for_date(self.daily_client.daily_closes(symbol, request_now), market_date.isoformat())
                if value is not None:
                    result["NASDAQ_DAILY_CLOSE"][symbol] = value
            except (NasdaqPayloadError, PublicApiError, OSError, ValueError):
                pass
            try:
                value = _close_for_date(
                    YahooChartClient().daily_closes(symbol, start_date=market_date, end_date=market_date).closes,
                    market_date.isoformat(),
                )
                if value is not None:
                    result["YAHOO_DAILY_CLOSE"][symbol] = value
            except (YahooPayloadError, PublicApiError, OSError, ValueError):
                pass
        return result

    def _maybe_record_close_source_calibration(self) -> None:
        """Once after 16:03 ET, persist exact-close source diagnostics when Pro access exists."""

        if not self.pyth_pro_api_key:
            return
        now = datetime.now(UTC)
        local_now = now.astimezone(NEW_YORK)
        if (local_now.hour, local_now.minute) < (16, 3):
            return
        market_date = local_now.date()
        date_key = market_date.isoformat()
        if date_key in self._close_calibration_attempted_dates:
            return
        all_spots = self.journal.list_spot_observations(market_date=market_date)
        finnhub_spots = tuple(
            SpotQuote(item.source, item.symbol, item.price, item.observed_at, item.published_at)
            for item in all_spots
            if item.source == "FINNHUB"
        )
        symbols = tuple(
            sorted(
                {item.symbol for item in finnhub_spots if item.observed_at.astimezone(NEW_YORK).date() == market_date}
            )
        )
        if not symbols:
            return
        self._close_calibration_attempted_dates.add(date_key)
        try:
            pyth_spots = tuple(
                SpotQuote(item.source, item.symbol, item.price, item.observed_at, item.published_at)
                for item in all_spots
                if item.source == "PYTH_HERMES"
            )
            report = calibrate_close_sources(
                client=PythHistoryClient(self.pyth_pro_api_key),
                market_date=market_date,
                symbols=symbols,
                finnhub_spots=finnhub_spots,
                pyth_live_spots=pyth_spots,
                supplemental_closes=self._supplemental_close_sources(symbols, market_date),
            ).as_payload()
            for observation in report["observations"]:
                self.journal.record_close_source_calibration(observation)
            self.event_sink("CLOSE_SOURCE_CALIBRATION_RECORDED", report)
        except Exception as error:
            self.event_sink(
                "CLOSE_SOURCE_CALIBRATION_FAILED",
                {
                    "market_date": date_key,
                    "error": str(error),
                    "recorded_at": now.isoformat(),
                },
            )

    async def _stop_streams(self) -> None:
        for task in self._stream_tasks:
            task.cancel()
        if self._stream_tasks:
            await asyncio.gather(*self._stream_tasks, return_exceptions=True)
        for runtime in self._stream_runtimes.values():
            await runtime.coordinator.close()
        self._stream_tasks = []
        self._stream_runtimes = {}

    async def _stop_paper_batch(self) -> None:
        for task in tuple(self._markout_tasks):
            task.cancel()
        if self._markout_tasks:
            await asyncio.gather(*self._markout_tasks, return_exceptions=True)
        self._markout_tasks.clear()
        if self._paper_batch_task:
            self._paper_batch_task.cancel()
            await asyncio.gather(self._paper_batch_task, return_exceptions=True)
        self._paper_batch_task = None
        self._pending_paper_entries.clear()

    async def run(self, scan_interval_seconds: float, duration_seconds: float = 0) -> None:
        if scan_interval_seconds <= 0 or duration_seconds < 0:
            raise ValueError("invalid supervisor timing")
        started_at = datetime.now(UTC)
        await self._writer.start()
        try:
            while True:
                await self.refresh()
                await asyncio.to_thread(self._maybe_record_pyth_daily_close_cache)
                await asyncio.to_thread(self._maybe_record_close_source_calibration)
                if duration_seconds and (datetime.now(UTC) - started_at).total_seconds() >= duration_seconds:
                    return
                wait_seconds = scan_interval_seconds
                if duration_seconds:
                    remaining = duration_seconds - (datetime.now(UTC) - started_at).total_seconds()
                    wait_seconds = min(wait_seconds, max(0.0, remaining))
                if wait_seconds <= 0:
                    return
                await asyncio.sleep(wait_seconds)
        finally:
            await self._stop_paper_batch()
            await self._stop_streams()
            await self._writer.close()
            # Stop producers before final network reconciliation so a slow Gamma
            # response cannot leave streams writing while shutdown is in progress.
            try:
                await asyncio.wait_for(self.reconcile_settlements(), timeout=15.0)
            except TimeoutError:
                self.event_sink(
                    "SUPERVISOR_FINAL_RECONCILIATION_TIMED_OUT",
                    {"timeout_seconds": 15.0, "recorded_at": datetime.now(UTC).isoformat()},
                )


def _book_summary(coordinator: ShadowStreamCoordinator, token_id: str) -> Mapping[str, object]:
    levels = coordinator.latest_book_levels.get(token_id, {"bids": {}, "asks": {}})
    bids = levels.get("bids", {})
    asks = levels.get("asks", {})
    best_bid = coordinator.latest_best_bids.get(token_id)
    best_ask = coordinator.latest_best_asks.get(token_id)
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": bids.get(best_bid) if best_bid is not None else None,
        "best_ask_size": asks.get(best_ask) if best_ask is not None else None,
        "bid_depth": sum(bids.values()),
        "ask_depth": sum(asks.values()),
        "levels": coordinator.latest_books.get(token_id, {}),
    }


def _age_seconds(now: datetime, observed_at: datetime | None) -> float | None:
    return max(0.0, (now - observed_at).total_seconds()) if observed_at else None


def _option_iv_age_seconds(surface: OptionIvSurface | None, now: datetime) -> float | None:
    return _age_seconds(now, surface.observed_at) if surface else None


def _current_option_quality_flags(
    surface: OptionIvSurface | None, configured_flags: tuple[str, ...], now: datetime
) -> tuple[str, ...]:
    flags = list(configured_flags)
    age = _option_iv_age_seconds(surface, now)
    if surface and age is not None and age > MAX_OPTION_IV_AGE_SECONDS:
        flags.append("OPTION_IV_STALE")
    return tuple(sorted(set(flags)))


def _usable_option_iv(surface: OptionIvSurface | None, now: datetime) -> float | None:
    age = _option_iv_age_seconds(surface, now)
    if surface and surface.usable and age is not None and age <= MAX_OPTION_IV_AGE_SECONDS:
        return surface.atm_iv
    return None


def _maker_quote_payload(quote: MakerShadowQuote) -> Mapping[str, object]:
    return {
        "quote_id": quote.quote_id,
        "market_id": quote.market_id,
        "symbol": quote.symbol,
        "outcome": quote.outcome,
        "status": quote.status,
        "limit_price": quote.limit_price,
        "fair_probability": quote.fair_probability,
        "theoretical_edge": quote.theoretical_edge,
        "touch_count": quote.touch_count,
        "cancel_reason": quote.cancel_reason,
    }


def _close_for_date(closes: tuple[DailyClose, ...] | list[DailyClose], date_key: str) -> float | None:
    matching = [item.close for item in closes if item.date == date_key and item.close > 0]
    return matching[-1] if matching else None


def _cross_source_uncertainty_buffer(
    now: datetime,
    pyth_quote: SpotQuote | None,
    comparison_quote: SpotQuote | None,
    maximum_age_seconds: float,
) -> float:
    """Convert fresh sub-gate source disagreement into a bounded probability penalty."""

    if pyth_quote is None or comparison_quote is None:
        return 0.0
    if _quote_is_stale(now, pyth_quote, maximum_age_seconds) or _quote_is_stale(
        now, comparison_quote, maximum_age_seconds
    ):
        return 0.0
    pyth_price = pyth_quote.price
    comparison_price = comparison_quote.price
    relative_difference = abs(comparison_price - pyth_price) / pyth_price
    confidence = pyth_quote.confidence
    confidence_ratio = float(confidence) / pyth_price if confidence is not None else 0.0
    return min(0.02, relative_difference + min(0.005, 3 * confidence_ratio))


def _pyth_primary_risk_reasons(
    now: datetime, pyth_quote: SpotQuote | None, maximum_age_seconds: float
) -> tuple[str, ...]:
    """Pyth is the hard spot gate because it is the contract resolution source."""

    if pyth_quote is None:
        return ("PYTH_SPOT_UNAVAILABLE",)
    if _quote_is_stale(now, pyth_quote, maximum_age_seconds):
        return ("PYTH_SPOT_STALE",)
    return ()


def _quote_age_seconds(now: datetime, quote: SpotQuote) -> float | None:
    return _age_seconds(now, quote.published_at or quote.observed_at)


def _quote_is_stale(now: datetime, quote: SpotQuote, maximum_age_seconds: float) -> bool:
    age = _quote_age_seconds(now, quote)
    return age is None or age > maximum_age_seconds
