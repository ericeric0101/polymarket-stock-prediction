"""JournalObservationRepository storage operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from zoneinfo import ZoneInfo

from ..checkpoints import DEFAULT_MAXIMUM_DELAY_SECONDS, checkpoint_target_at
from ..evaluation_payload import read_spot, read_threshold, validate_for_write
from .journal_helpers import _optional_float, _payload_execution_fee
from .journal_models import (
    BufferSweepObservation,
    CheckpointObservation,
    ExecutionObservation,
    SpotSourceComparison,
    StoredSpotObservation,
)
from .sqlite import database_connection


class JournalObservationRepository:
    def record_realtime_evaluation(self, payload: Mapping[str, object]) -> None:
        """Persist every fresh or rejected real-time shadow evaluation for calibration."""

        validate_for_write(payload)
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO realtime_evaluations (
                        evaluated_at, market_id, symbol, spot, up_ask, down_ask,
                        fair_up_probability, signal_status, skip_reasons_json, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(payload["evaluated_at"]),
                    str(payload["market_id"]),
                    str(payload["symbol"]),
                    payload.get("spot"),
                    payload.get("up_ask"),
                    payload.get("down_ask"),
                    payload.get("fair_up_probability"),
                    str(payload["signal_status"]),
                    json.dumps(payload["skip_reasons"], sort_keys=True),
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def record_checkpoint_observation(
        self,
        *,
        checkpoint_date: str,
        checkpoint_name: str,
        payload: Mapping[str, object],
        maximum_delay_seconds: float = DEFAULT_MAXIMUM_DELAY_SECONDS,
    ) -> bool:
        """Store the first valid observation after a fixed daily research checkpoint."""

        required = {"evaluated_at", "market_id", "symbol", "fair_up_probability", "model_version"}
        missing = required.difference(payload)
        if missing:
            raise ValueError(f"checkpoint observation is missing: {', '.join(sorted(missing))}")
        evaluated_at = datetime.fromisoformat(str(payload["evaluated_at"]))
        target_at = checkpoint_target_at(checkpoint_date, checkpoint_name)
        delay_seconds = max(0.0, (evaluated_at - target_at).total_seconds())
        eligible = delay_seconds <= maximum_delay_seconds
        with database_connection(self.path) as connection:
            return (
                connection.execute(
                    """INSERT OR IGNORE INTO checkpoint_observations (
                        market_id, symbol, checkpoint_date, checkpoint_name, evaluated_at,
                        fair_up_probability, up_ask, down_ask, model_version, option_iv, payload_json,
                        checkpoint_target_at, checkpoint_delay_seconds, eligible_for_calibration
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(payload["market_id"]),
                        str(payload["symbol"]),
                        checkpoint_date,
                        checkpoint_name,
                        str(payload["evaluated_at"]),
                        float(payload["fair_up_probability"]),
                        payload.get("up_ask"),
                        payload.get("down_ask"),
                        str(payload["model_version"]),
                        payload.get("option_iv"),
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                        target_at.isoformat(),
                        delay_seconds,
                        int(eligible),
                    ),
                ).rowcount
                == 1
            )

    def list_portfolio_decisions(self, limit: int = 100) -> tuple[Mapping[str, object], ...]:
        if limit < 1:
            raise ValueError("portfolio decision limit must be positive")
        with database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT created_at, batch_id, market_id, symbol, outcome, risk_group, edge, status, reason, payload_json
                    FROM portfolio_decisions ORDER BY id DESC LIMIT ?""",  # noqa: E501
                (limit,),
            ).fetchall()
        return tuple(
            {
                "created_at": str(row[0]),
                "batch_id": str(row[1]),
                "market_id": str(row[2]),
                "symbol": str(row[3]),
                "outcome": str(row[4]),
                "risk_group": str(row[5]),
                "edge": float(row[6]),
                "status": str(row[7]),
                "reason": str(row[8]),
                "payload": json.loads(str(row[9])),
            }
            for row in rows
        )

    def list_checkpoint_observations(self, *, eligible_only: bool = True) -> tuple[CheckpointObservation, ...]:
        query = """SELECT checkpoint.market_id, checkpoint.symbol, checkpoint.checkpoint_date,
                checkpoint.checkpoint_name, checkpoint.evaluated_at, checkpoint.fair_up_probability,
                checkpoint.up_ask, checkpoint.down_ask, checkpoint.model_version, checkpoint.option_iv,
                settlement.winning_outcome, checkpoint.checkpoint_target_at, checkpoint.checkpoint_delay_seconds,
                checkpoint.eligible_for_calibration
              FROM checkpoint_observations AS checkpoint
              JOIN market_settlements AS settlement ON settlement.market_id = checkpoint.market_id
              WHERE (? = 0 OR checkpoint.eligible_for_calibration = 1)
              ORDER BY checkpoint.checkpoint_date, checkpoint.checkpoint_name, checkpoint.market_id"""
        with database_connection(self.path) as connection:
            rows = connection.execute(query, (int(eligible_only),)).fetchall()
        return tuple(
            CheckpointObservation(
                market_id=str(row[0]),
                symbol=str(row[1]),
                checkpoint_date=str(row[2]),
                checkpoint_name=str(row[3]),
                evaluated_at=datetime.fromisoformat(str(row[4])),
                fair_up_probability=float(row[5]),
                up_ask=float(row[6]) if row[6] is not None else None,
                down_ask=float(row[7]) if row[7] is not None else None,
                model_version=str(row[8]),
                option_iv=float(row[9]) if row[9] is not None else None,
                winning_outcome=str(row[10]),
                checkpoint_target_at=datetime.fromisoformat(str(row[11])),
                checkpoint_delay_seconds=float(row[12]),
                eligible_for_calibration=bool(row[13]),
            )
            for row in rows
        )

    def list_buffer_sweep_observations(self) -> tuple[BufferSweepObservation, ...]:
        """Return immutable, on-time checkpoints with their original executable costs."""

        query = """SELECT checkpoint.market_id, checkpoint.symbol, checkpoint.checkpoint_date,
                checkpoint.checkpoint_name, checkpoint.evaluated_at, checkpoint.fair_up_probability,
                checkpoint.up_ask, checkpoint.down_ask, checkpoint.payload_json, settlement.winning_outcome
              FROM checkpoint_observations AS checkpoint
              JOIN market_settlements AS settlement ON settlement.market_id = checkpoint.market_id
              WHERE checkpoint.eligible_for_calibration = 1
              ORDER BY checkpoint.checkpoint_date, checkpoint.evaluated_at, checkpoint.market_id"""
        with database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        observations = []
        for row in rows:
            payload = json.loads(str(row[8]))
            comparison_models = payload.get("comparison_models")
            observations.append(
                BufferSweepObservation(
                    market_id=str(row[0]),
                    symbol=str(row[1]),
                    checkpoint_date=str(row[2]),
                    checkpoint_name=str(row[3]),
                    evaluated_at=datetime.fromisoformat(str(row[4])),
                    fair_up_probability=float(row[5]),
                    up_ask=float(row[6]) if row[6] is not None else None,
                    down_ask=float(row[7]) if row[7] is not None else None,
                    up_taker_fee=_payload_execution_fee(payload, "up"),
                    down_taker_fee=_payload_execution_fee(payload, "down"),
                    winning_outcome=str(row[9]),
                    spot=read_spot(payload),
                    price_to_beat=read_threshold(payload),
                    up_bid=_optional_float(payload.get("up_bid")),
                    down_bid=_optional_float(payload.get("down_bid")),
                    annualized_volatility=_optional_float(payload.get("annualized_realized_volatility")),
                    cross_source_difference=_optional_float(payload.get("cross_source_difference")),
                    comparison_models=tuple(dict(item) for item in comparison_models if isinstance(item, Mapping))
                    if isinstance(comparison_models, list)
                    else (),
                    payload=payload,
                )
            )
        return tuple(observations)

    def list_execution_observations(self) -> tuple[ExecutionObservation, ...]:
        query = """SELECT observed_at, signal_id, observation_kind, market_id, symbol, outcome, token_id,
                spot, price_to_beat, fair_probability, best_bid, best_ask, fee_rate,
                book_payload_json, evaluation_payload_json
              FROM execution_observations ORDER BY observed_at, id"""
        with database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        return tuple(
            ExecutionObservation(
                observed_at=datetime.fromisoformat(str(row[0])),
                signal_id=str(row[1]) if row[1] else None,
                observation_kind=str(row[2]),
                market_id=str(row[3]),
                symbol=str(row[4]),
                outcome=str(row[5]),
                token_id=str(row[6]),
                spot=_optional_float(row[7]),
                price_to_beat=_optional_float(row[8]),
                fair_probability=_optional_float(row[9]),
                best_bid=_optional_float(row[10]),
                best_ask=_optional_float(row[11]),
                fee_rate=_optional_float(row[12]),
                book_payload=json.loads(str(row[13])),
                evaluation_payload=json.loads(str(row[14])),
            )
            for row in rows
        )

    def list_spot_observations(
        self,
        *,
        source: str | None = None,
        market_date: date | None = None,
        sample_every_seconds: int = 1,
    ) -> tuple[StoredSpotObservation, ...]:
        if sample_every_seconds < 1:
            raise ValueError("sample_every_seconds must be positive")
        query = "SELECT observed_at, source, symbol, price, published_at FROM spot_observations"
        conditions = []
        parameters: list[object] = []
        if source:
            conditions.append("source = ?")
            parameters.append(source.upper())
        if market_date is not None:
            new_york = ZoneInfo("America/New_York")
            start = datetime.combine(market_date, wall_time.min, tzinfo=new_york).astimezone(UTC)
            end = datetime.combine(market_date + timedelta(days=1), wall_time.min, tzinfo=new_york).astimezone(UTC)
            conditions.extend(("observed_at >= ?", "observed_at < ?"))
            parameters.extend((start.isoformat(), end.isoformat()))
        if sample_every_seconds > 1:
            conditions.append("unixepoch(observed_at) % ? = 0")
            parameters.append(sample_every_seconds)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY observed_at, id"
        with database_connection(self.path) as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(
            StoredSpotObservation(
                observed_at=datetime.fromisoformat(str(row[0])),
                source=str(row[1]),
                symbol=str(row[2]),
                price=float(row[3]),
                published_at=datetime.fromisoformat(str(row[4])) if row[4] else None,
            )
            for row in rows
        )

    def list_spot_source_comparisons(self, *, sample_every_seconds: int = 1) -> tuple[SpotSourceComparison, ...]:
        if sample_every_seconds < 1:
            raise ValueError("sample_every_seconds must be positive")
        query = """SELECT observed_at, symbol, primary_source, primary_price, pyth_price,
                pyth_confidence, difference_bps, primary_published_at, pyth_published_at FROM spot_source_comparisons"""
        parameters: tuple[object, ...] = ()
        if sample_every_seconds > 1:
            query += " WHERE unixepoch(observed_at) % ? = 0"
            parameters = (sample_every_seconds,)
        query += " ORDER BY observed_at, id"
        with database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            SpotSourceComparison(
                observed_at=datetime.fromisoformat(str(row[0])),
                symbol=str(row[1]),
                primary_source=str(row[2]),
                primary_price=float(row[3]),
                pyth_price=float(row[4]),
                pyth_confidence=_optional_float(row[5]),
                difference_bps=float(row[6]),
                primary_published_at=datetime.fromisoformat(str(row[7])) if row[7] else None,
                pyth_published_at=datetime.fromisoformat(str(row[8])) if row[8] else None,
            )
            for row in rows
        )
