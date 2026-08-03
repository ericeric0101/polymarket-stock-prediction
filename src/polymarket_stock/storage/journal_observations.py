"""JournalObservationRepository storage operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from zoneinfo import ZoneInfo

from ..checkpoints import DEFAULT_MAXIMUM_DELAY_SECONDS, checkpoint_target_at
from ..evaluation_payload import read_spot, read_threshold, validate_for_write
from .journal_helpers import _optional_float, _payload_execution_fee, _required_float
from .journal_models import (
    BufferSweepObservation,
    CheckpointObservation,
    ExecutionObservation,
    SpotSourceComparison,
    StoredSpotObservation,
)
from .journal_repository import JournalRepository
from .sqlite import database_connection


class JournalObservationRepository(JournalRepository):
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
                        _required_float(payload["fair_up_probability"], "fair_up_probability"),
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
                "created_at": str(row["created_at"]),
                "batch_id": str(row["batch_id"]),
                "market_id": str(row["market_id"]),
                "symbol": str(row["symbol"]),
                "outcome": str(row["outcome"]),
                "risk_group": str(row["risk_group"]),
                "edge": float(row["edge"]),
                "status": str(row["status"]),
                "reason": str(row["reason"]),
                "payload": json.loads(str(row["payload_json"])),
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
                market_id=str(row["market_id"]),
                symbol=str(row["symbol"]),
                checkpoint_date=str(row["checkpoint_date"]),
                checkpoint_name=str(row["checkpoint_name"]),
                evaluated_at=datetime.fromisoformat(str(row["evaluated_at"])),
                fair_up_probability=float(row["fair_up_probability"]),
                up_ask=float(row["up_ask"]) if row["up_ask"] is not None else None,
                down_ask=float(row["down_ask"]) if row["down_ask"] is not None else None,
                model_version=str(row["model_version"]),
                option_iv=float(row["option_iv"]) if row["option_iv"] is not None else None,
                winning_outcome=str(row["winning_outcome"]),
                checkpoint_target_at=datetime.fromisoformat(str(row["checkpoint_target_at"])),
                checkpoint_delay_seconds=float(row["checkpoint_delay_seconds"]),
                eligible_for_calibration=bool(row["eligible_for_calibration"]),
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
            payload = json.loads(str(row["payload_json"]))
            comparison_models = payload.get("comparison_models")
            observations.append(
                BufferSweepObservation(
                    market_id=str(row["market_id"]),
                    symbol=str(row["symbol"]),
                    checkpoint_date=str(row["checkpoint_date"]),
                    checkpoint_name=str(row["checkpoint_name"]),
                    evaluated_at=datetime.fromisoformat(str(row["evaluated_at"])),
                    fair_up_probability=float(row["fair_up_probability"]),
                    up_ask=float(row["up_ask"]) if row["up_ask"] is not None else None,
                    down_ask=float(row["down_ask"]) if row["down_ask"] is not None else None,
                    up_taker_fee=_payload_execution_fee(payload, "up"),
                    down_taker_fee=_payload_execution_fee(payload, "down"),
                    winning_outcome=str(row["winning_outcome"]),
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
                observed_at=datetime.fromisoformat(str(row["observed_at"])),
                signal_id=str(row["signal_id"]) if row["signal_id"] else None,
                observation_kind=str(row["observation_kind"]),
                market_id=str(row["market_id"]),
                symbol=str(row["symbol"]),
                outcome=str(row["outcome"]),
                token_id=str(row["token_id"]),
                spot=_optional_float(row["spot"]),
                price_to_beat=_optional_float(row["price_to_beat"]),
                fair_probability=_optional_float(row["fair_probability"]),
                best_bid=_optional_float(row["best_bid"]),
                best_ask=_optional_float(row["best_ask"]),
                fee_rate=_optional_float(row["fee_rate"]),
                book_payload=json.loads(str(row["book_payload_json"])),
                evaluation_payload=json.loads(str(row["evaluation_payload_json"])),
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
                observed_at=datetime.fromisoformat(str(row["observed_at"])),
                source=str(row["source"]),
                symbol=str(row["symbol"]),
                price=float(row["price"]),
                published_at=datetime.fromisoformat(str(row["published_at"])) if row["published_at"] else None,
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
                observed_at=datetime.fromisoformat(str(row["observed_at"])),
                symbol=str(row["symbol"]),
                primary_source=str(row["primary_source"]),
                primary_price=float(row["primary_price"]),
                pyth_price=float(row["pyth_price"]),
                pyth_confidence=_optional_float(row["pyth_confidence"]),
                difference_bps=float(row["difference_bps"]),
                primary_published_at=datetime.fromisoformat(str(row["primary_published_at"]))
                if row["primary_published_at"]
                else None,
                pyth_published_at=datetime.fromisoformat(str(row["pyth_published_at"]))
                if row["pyth_published_at"]
                else None,
            )
            for row in rows
        )
