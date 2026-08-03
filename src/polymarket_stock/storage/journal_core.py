"""JournalCoreRepository storage operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from datetime import time as wall_time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..alpaca_options import IndicativeOptionQuote
from ..market_discovery import MarketCandidate
from ..polymarket_data import OrderBookSnapshot
from .journal_models import (
    StoredMarketCandidate,
    StoredOutcomeToken,
)
from .migrations import initialize_database
from .sqlite import database_connection


class JournalCoreRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        initialize_database(self.path)

    def record_decision(
        self,
        *,
        market_id: str,
        outcome: str,
        fair_yes_probability: float,
        executable_ask: float,
        edge: float,
        should_record_paper_trade: bool,
        payload: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> str:
        if outcome not in {"YES", "NO"}:
            raise ValueError("outcome must be YES or NO")
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest_source = f"{timestamp.isoformat()}|{market_id}|{outcome}|{payload_json}"
        decision_id = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

        with database_connection(self.path) as connection:
            connection.execute(
                """
                    INSERT INTO shadow_decisions (
                        id, created_at, market_id, outcome, fair_yes_probability,
                        executable_ask, edge, should_record_paper_trade, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    decision_id,
                    timestamp.isoformat(),
                    market_id,
                    outcome,
                    fair_yes_probability,
                    executable_ask,
                    edge,
                    int(should_record_paper_trade),
                    payload_json,
                ),
            )
        return decision_id

    def upsert_market_candidate(self, candidate: MarketCandidate) -> None:
        """Persist raw terms for human review; accepts the discovery dataclass lazily."""

        values = (
            candidate.market_id,
            datetime.now(UTC).isoformat(),
            candidate.question,
            candidate.slug,
            candidate.end_date,
            candidate.resolution_source,
            candidate.outcome_a_token_id,
            candidate.outcome_b_token_id,
            candidate.review_status,
            json.dumps(candidate.raw_payload, sort_keys=True, separators=(",", ":"), default=str),
        )
        with database_connection(self.path) as connection:
            connection.execute(
                """
                    INSERT INTO market_candidates (
                        market_id, discovered_at, question, slug, end_date, resolution_source,
                        yes_token_id, no_token_id, outcome_a_label, outcome_b_label,
                        outcome_a_token_id, outcome_b_token_id, review_status, raw_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        discovered_at=excluded.discovered_at,
                        question=excluded.question,
                        slug=excluded.slug,
                        end_date=excluded.end_date,
                        resolution_source=excluded.resolution_source,
                        yes_token_id=excluded.yes_token_id,
                        no_token_id=excluded.no_token_id,
                        outcome_a_label=excluded.outcome_a_label,
                        outcome_b_label=excluded.outcome_b_label,
                        outcome_a_token_id=excluded.outcome_a_token_id,
                        outcome_b_token_id=excluded.outcome_b_token_id,
                        review_status=excluded.review_status,
                        raw_payload_json=excluded.raw_payload_json
                    """,
                (
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    values[5],
                    values[6],
                    values[7],
                    candidate.outcome_a_label,
                    candidate.outcome_b_label,
                    candidate.outcome_a_token_id,
                    candidate.outcome_b_token_id,
                    values[8],
                    values[9],
                ),
            )

    def record_order_book_snapshot(self, market_id: str, snapshot: OrderBookSnapshot) -> None:
        with database_connection(self.path) as connection:
            connection.execute(
                """
                    INSERT INTO order_book_snapshots (
                        observed_at, market_id, token_id, best_bid, best_ask, midpoint, raw_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    snapshot.observed_at.isoformat(),
                    market_id,
                    snapshot.token_id,
                    snapshot.best_bid,
                    snapshot.best_ask,
                    snapshot.midpoint,
                    json.dumps(snapshot.raw_payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def record_spot_observation(self, payload: Mapping[str, object]) -> None:
        """Persist at most one received source quote per source/symbol/second."""

        try:
            observed_at = datetime.fromisoformat(str(payload["observed_at"]))
            source = str(payload["source"]).upper()
            symbol = str(payload["symbol"]).upper()
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("spot observation is invalid") from error
        if observed_at.tzinfo is None or not source or not symbol or price <= 0:
            raise ValueError("spot observation is invalid")
        published_at = payload.get("published_at")
        confidence = payload.get("confidence")
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO spot_observations (
                        observed_at, observed_second, source, symbol, price, published_at, confidence, feed_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(),
                    observed_at.replace(microsecond=0).isoformat(),
                    source,
                    symbol,
                    price,
                    str(published_at) if published_at else None,
                    float(confidence) if confidence is not None else None,
                    str(payload["feed_id"]) if payload.get("feed_id") else None,
                ),
            )

    def record_spot_source_comparison(self, payload: Mapping[str, object]) -> None:
        """Persist bounded Pyth-versus-primary source divergence diagnostics."""

        try:
            observed_at = datetime.fromisoformat(str(payload["observed_at"]))
            symbol = str(payload["symbol"]).upper()
            primary_source = str(payload["primary_source"]).upper()
            primary_price = float(payload["primary_price"])
            pyth_price = float(payload["pyth_price"])
            difference_bps = float(payload["difference_bps"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("spot source comparison is invalid") from error
        if observed_at.tzinfo is None or not symbol or not primary_source or min(primary_price, pyth_price) <= 0:
            raise ValueError("spot source comparison is invalid")
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO spot_source_comparisons (
                        observed_at, observed_second, symbol, primary_source, primary_price, primary_published_at,
                        pyth_price, pyth_published_at, pyth_confidence, pyth_feed_id, difference_bps
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(),
                    observed_at.replace(microsecond=0).isoformat(),
                    symbol,
                    primary_source,
                    primary_price,
                    payload.get("primary_published_at"),
                    pyth_price,
                    payload.get("pyth_published_at"),
                    float(payload["pyth_confidence"]) if payload.get("pyth_confidence") is not None else None,
                    payload.get("pyth_feed_id"),
                    difference_bps,
                ),
            )

    def record_pyth_daily_close(
        self, *, market_date: str, symbol: str, close_price: float, candle_at: datetime, source: str
    ) -> None:
        if not market_date or not symbol.strip() or close_price <= 0 or candle_at.tzinfo is None:
            raise ValueError("Pyth daily close is invalid")
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO pyth_daily_closes (market_date, symbol, close_price, candle_at, source, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_date, symbol) DO UPDATE SET close_price=excluded.close_price,
                        candle_at=excluded.candle_at, source=excluded.source, recorded_at=excluded.recorded_at""",
                (
                    market_date,
                    symbol.upper(),
                    close_price,
                    candle_at.isoformat(),
                    source,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_pyth_daily_close(self, *, market_date: str, symbol: str) -> Mapping[str, object] | None:
        with database_connection(self.path) as connection:
            row = connection.execute(
                "SELECT close_price, candle_at, source FROM pyth_daily_closes WHERE market_date = ? AND symbol = ?",
                (market_date, symbol.upper()),
            ).fetchone()
        if row is None:
            return None
        return {"price": float(row[0]), "candle_at": str(row[1]), "source": str(row[2])}

    def record_close_source_calibration(self, payload: Mapping[str, object]) -> None:
        """Upsert the one exact Pyth-close calibration record per symbol and session."""

        market_date = str(payload.get("market_date", ""))
        symbol = str(payload.get("symbol", "")).upper()
        status = str(payload.get("status", ""))
        if not market_date or not symbol or not status:
            raise ValueError("close source calibration is invalid")
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO source_close_calibrations (
                        market_date, symbol, recorded_at, status, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(market_date, symbol) DO UPDATE SET
                        recorded_at=excluded.recorded_at, status=excluded.status, payload_json=excluded.payload_json""",
                (
                    market_date,
                    symbol,
                    datetime.now(UTC).isoformat(),
                    status,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def list_close_source_calibrations(self, *, market_date: str | None = None) -> tuple[Mapping[str, object], ...]:
        query = "SELECT payload_json FROM source_close_calibrations"
        parameters: tuple[object, ...] = ()
        if market_date:
            query += " WHERE market_date = ?"
            parameters = (market_date,)
        query += " ORDER BY market_date, symbol"
        with database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(json.loads(str(row[0])) for row in rows)

    def last_regular_spot_observation(
        self,
        *,
        source: str,
        symbol: str,
        market_date: str,
    ) -> Mapping[str, object] | None:
        """Return the final locally recorded regular-session quote for one date."""
        local_date = datetime.fromisoformat(market_date).date()
        new_york = ZoneInfo("America/New_York")
        start = datetime.combine(local_date, wall_time(9, 30), tzinfo=new_york).astimezone(UTC)
        end = datetime.combine(local_date, wall_time(16), tzinfo=new_york).astimezone(UTC)
        with database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT price, observed_at, published_at FROM spot_observations
                    WHERE source = ? AND symbol = ? AND observed_at >= ? AND observed_at <= ?
                    ORDER BY observed_at DESC, id DESC LIMIT 1""",
                (source.upper(), symbol.upper(), start.isoformat(), end.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return {"price": float(row[0]), "observed_at": str(row[1]), "published_at": str(row[2]) if row[2] else None}

    def record_execution_observation(
        self,
        *,
        observed_at: datetime,
        signal_id: str | None,
        observation_kind: str,
        market_id: str,
        symbol: str,
        outcome: str,
        token_id: str,
        spot: float | None,
        price_to_beat: float | None,
        fair_probability: float | None,
        best_bid: float | None,
        best_ask: float | None,
        fee_rate: float | None,
        book_payload: Mapping[str, object],
        evaluation_payload: Mapping[str, object],
    ) -> None:
        if outcome not in {"UP", "DOWN"}:
            raise ValueError("execution observation outcome must be UP or DOWN")
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO execution_observations (
                        observed_at, signal_id, observation_kind, market_id, symbol, outcome, token_id,
                        spot, price_to_beat, fair_probability, best_bid, best_ask, fee_rate,
                        book_payload_json, evaluation_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observed_at.isoformat(),
                    signal_id,
                    observation_kind,
                    market_id,
                    symbol,
                    outcome,
                    token_id,
                    spot,
                    price_to_beat,
                    fair_probability,
                    best_bid,
                    best_ask,
                    fee_rate,
                    json.dumps(book_payload, sort_keys=True, separators=(",", ":"), default=str),
                    json.dumps(evaluation_payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def get_market_outcome_tokens(self, market_id: str) -> tuple[StoredOutcomeToken, StoredOutcomeToken]:
        """Return both outcome tokens for a discovered market."""

        with database_connection(self.path) as connection:
            row = connection.execute(
                """
                    SELECT outcome_a_label, outcome_a_token_id, outcome_b_label, outcome_b_token_id
                    FROM market_candidates WHERE market_id = ?
                    """,
                (market_id,),
            ).fetchone()
        if row is None or not all(row):
            raise KeyError(f"market {market_id} is not present in the local candidate journal")
        return (
            StoredOutcomeToken(label=row[0], token_id=row[1]),
            StoredOutcomeToken(label=row[2], token_id=row[3]),
        )

    def list_market_candidates(self, symbol: str | None = None) -> tuple[StoredMarketCandidate, ...]:
        """Return concise local candidate metadata without exposing CLOB token IDs."""

        normalized_symbol = symbol.strip().upper() if symbol else ""
        query = """
                SELECT market_id, question, slug, end_date, outcome_a_label, outcome_b_label, review_status
                FROM market_candidates
            """
        parameters: tuple[object, ...] = ()
        if normalized_symbol:
            query += " WHERE UPPER(question) LIKE ?"
            parameters = (f"%{normalized_symbol}%",)
        query += " ORDER BY end_date ASC, market_id ASC"
        with database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(StoredMarketCandidate(*row) for row in rows)

    def get_market_candidate(self, market_id: str) -> StoredMarketCandidate:
        with database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT market_id, question, slug, end_date, outcome_a_label, outcome_b_label, review_status
                    FROM market_candidates WHERE market_id = ?""",
                (market_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"market {market_id} is not present in the local candidate journal")
        return StoredMarketCandidate(*row)

    def get_market_candidate_raw_payload(self, market_id: str) -> Mapping[str, object]:
        """Return the persisted Gamma payload so contract terms can be revalidated."""

        with database_connection(self.path) as connection:
            row = connection.execute(
                "SELECT raw_payload_json FROM market_candidates WHERE market_id = ?", (market_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"market {market_id} is not present in the local candidate journal")
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError(f"market {market_id} has an invalid persisted raw payload")
        return payload

    def get_latest_outcome_asks(self, market_id: str) -> tuple[float, float]:
        outcomes = self.get_market_outcome_tokens(market_id)
        asks: list[float] = []
        with database_connection(self.path) as connection:
            for outcome in outcomes:
                row = connection.execute(
                    """SELECT best_ask FROM order_book_snapshots
                        WHERE market_id = ? AND token_id = ? ORDER BY id DESC LIMIT 1""",
                    (market_id, outcome.token_id),
                ).fetchone()
                if row is None or row[0] is None:
                    raise KeyError(f"market {market_id} has no stored ask for {outcome.label}")
                asks.append(float(row[0]))
        return asks[0], asks[1]

    def record_alpaca_indicative_option_quote(self, quote: IndicativeOptionQuote) -> None:
        with database_connection(self.path) as connection:
            connection.execute(
                """
                    INSERT INTO alpaca_indicative_option_quotes (
                        observed_at, option_symbol, bid_price, ask_price, feed, quality_label
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                (
                    quote.observed_at.isoformat(),
                    quote.symbol,
                    quote.bid_price,
                    quote.ask_price,
                    quote.feed,
                    quote.quality_label,
                ),
            )
