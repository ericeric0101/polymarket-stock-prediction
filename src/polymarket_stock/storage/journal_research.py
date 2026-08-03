"""JournalResearchRepository storage operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from ..evaluation_payload import read_spot, read_threshold
from ..quality import observable_equity_market_date
from .journal_models import (
    FirstSignalCalibrationObservation,
    ReplayObservation,
)
from .sqlite import database_connection


class JournalResearchRepository:
    def record_contract_review(
        self, market_id: str, *, accepted: bool, reason: str, contract: Mapping[str, object] | None = None
    ) -> None:
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO market_contract_reviews (market_id, reviewed_at, status, reason, contract_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET reviewed_at=excluded.reviewed_at, status=excluded.status,
                        reason=excluded.reason, contract_json=excluded.contract_json""",
                (
                    market_id,
                    datetime.now(UTC).isoformat(),
                    "ACCEPTED" if accepted else "REJECTED",
                    reason,
                    json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str) if contract else None,
                ),
            )

    def get_market_settlement_outcome(self, market_id: str) -> str:
        """Return the previously reconciled official market outcome."""

        with database_connection(self.path) as connection:
            row = connection.execute(
                "SELECT winning_outcome FROM market_settlements WHERE market_id = ?", (market_id,)
            ).fetchone()
        if row is None:
            raise KeyError(market_id)
        return str(row[0])

    def record_market_settlement(self, market_id: str, winning_outcome: str, payload: Mapping[str, object]) -> None:
        if winning_outcome not in {"UP", "DOWN"}:
            raise ValueError("winning_outcome must be UP or DOWN")
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO market_settlements (market_id, settled_at, winning_outcome, payload_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET settled_at=excluded.settled_at,
                        winning_outcome=excluded.winning_outcome, payload_json=excluded.payload_json""",
                (
                    market_id,
                    datetime.now(UTC).isoformat(),
                    winning_outcome,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def pending_evaluation_market_ids(self, limit: int = 100) -> tuple[str, ...]:
        with database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT DISTINCT market_id FROM realtime_evaluations
                    WHERE fair_up_probability IS NOT NULL
                      AND market_id NOT IN (SELECT market_id FROM market_settlements)
                    ORDER BY market_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_replay_observations(self) -> tuple[ReplayObservation, ...]:
        """Return one latest valid model observation per officially settled market."""

        query = """WITH latest AS (
                SELECT market_id, MAX(evaluated_at) AS evaluated_at FROM realtime_evaluations
                WHERE fair_up_probability IS NOT NULL GROUP BY market_id
            ) SELECT evaluation.market_id, evaluation.symbol, evaluation.evaluated_at,
                evaluation.fair_up_probability, evaluation.up_ask, evaluation.down_ask, settlement.winning_outcome
              FROM latest
              JOIN realtime_evaluations AS evaluation
                ON evaluation.market_id = latest.market_id AND evaluation.evaluated_at = latest.evaluated_at
              JOIN market_settlements AS settlement ON settlement.market_id = evaluation.market_id
              ORDER BY evaluation.evaluated_at ASC"""
        with database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        return tuple(
            ReplayObservation(
                market_id=str(row[0]),
                symbol=str(row[1]),
                evaluated_at=datetime.fromisoformat(str(row[2])),
                fair_up_probability=float(row[3]),
                up_ask=float(row[4]) if row[4] is not None else None,
                down_ask=float(row[5]) if row[5] is not None else None,
                winning_outcome=str(row[6]),
            )
            for row in rows
        )

    def list_first_signal_calibration_observations(self) -> tuple[FirstSignalCalibrationObservation, ...]:
        """Return exactly one pre-settlement model signal per officially settled market.

        These are selected-side probabilities, rather than raw Fair-Up probabilities,
        so calibration measures the probability the policy actually acted on.
        """

        query = """SELECT evaluation.evaluated_at, evaluation.market_id, evaluation.symbol,
                    evaluation.fair_up_probability, evaluation.up_ask, evaluation.down_ask,
                    evaluation.signal_status, evaluation.payload_json, settlement.winning_outcome
                FROM market_settlements AS settlement
                JOIN realtime_evaluations AS evaluation ON evaluation.id = (
                    SELECT first_signal.id FROM realtime_evaluations AS first_signal
                    WHERE first_signal.market_id = settlement.market_id
                      AND first_signal.fair_up_probability IS NOT NULL
                      AND first_signal.signal_status IN (
                          'PAPER_UP', 'PAPER_DOWN', 'OBSERVATION_ONLY_UP', 'OBSERVATION_ONLY_DOWN'
                      )
                    ORDER BY first_signal.evaluated_at, first_signal.id LIMIT 1
                )
                ORDER BY evaluation.evaluated_at, evaluation.market_id"""
        with database_connection(self.path) as connection:
            rows = connection.execute(query).fetchall()
        observations = []
        for row in rows:
            payload = json.loads(str(row[7]))
            outcome = "UP" if str(row[6]) in {"PAPER_UP", "OBSERVATION_ONLY_UP"} else "DOWN"
            fair_up = float(row[3])
            entry_ask = float(row[4] if outcome == "UP" else row[5])
            entry_fee_value = payload.get("up_taker_fee") if outcome == "UP" else payload.get("down_taker_fee")
            spot = read_spot(payload)
            threshold = read_threshold(payload)
            threshold_distance_bps = None
            if spot is not None and threshold is not None and float(threshold) > 0:
                threshold_distance_bps = (float(spot) / float(threshold) - 1.0) * 10_000
            option_iv_status = str(payload.get("option_iv_status") or "IV_UNAVAILABLE")
            observations.append(
                FirstSignalCalibrationObservation(
                    market_id=str(row[1]),
                    symbol=str(row[2]),
                    evaluated_at=datetime.fromisoformat(str(row[0])),
                    model_outcome=outcome,
                    selected_fair_probability=fair_up if outcome == "UP" else 1.0 - fair_up,
                    entry_ask=entry_ask,
                    entry_fee=float(entry_fee_value) if entry_fee_value is not None else None,
                    winning_outcome=str(row[8]),
                    model_version=str(payload.get("model_version") or "unknown"),
                    option_iv_status=option_iv_status,
                    iv_regime="IV_VALID" if option_iv_status == "IV_VALID" else "REALIZED_VOL_FALLBACK",
                    spot_provider=str(payload.get("spot_provider") or "unknown"),
                    threshold_distance_bps=threshold_distance_bps,
                    volatility_estimator=str(payload.get("volatility_estimator") or "CLOSE_TO_CLOSE"),
                )
            )
        return tuple(observations)

    def first_signal_performance(self) -> Mapping[str, object]:
        """Summarize one first model signal per officially settled market."""

        query = """SELECT COUNT(*), SUM(
                    CASE
                        WHEN evaluation.signal_status IN ('PAPER_UP', 'OBSERVATION_ONLY_UP') THEN 'UP'
                        ELSE 'DOWN'
                    END = settlement.winning_outcome
                ) FROM market_settlements AS settlement
                JOIN realtime_evaluations AS evaluation ON evaluation.id = (
                    SELECT first_signal.id FROM realtime_evaluations AS first_signal
                    WHERE first_signal.market_id = settlement.market_id
                      AND first_signal.fair_up_probability IS NOT NULL
                      AND first_signal.signal_status IN (
                          'PAPER_UP', 'PAPER_DOWN', 'OBSERVATION_ONLY_UP', 'OBSERVATION_ONLY_DOWN'
                      )
                    ORDER BY first_signal.evaluated_at, first_signal.id LIMIT 1
                )"""
        with database_connection(self.path) as connection:
            count, wins = connection.execute(query).fetchone()
        settled_markets = int(count or 0)
        correct = int(wins or 0)
        return {
            "settled_markets": settled_markets,
            "wins": correct,
            "losses": settled_markets - correct,
            "win_rate": correct / settled_markets if settled_markets else None,
        }

    def dashboard_rows(
        self,
        limit: int = 18,
        *,
        now: datetime | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Return stable symbol rows with immutable same-day checkpoint decisions."""
        if limit < 1:
            raise ValueError("dashboard limit must be positive")
        timestamp = now or datetime.now(UTC)
        ny_date = observable_equity_market_date(timestamp).isoformat()
        with database_connection(self.path) as connection:
            rows = connection.execute(
                """SELECT evaluation.payload_json FROM realtime_evaluations AS evaluation
                    JOIN market_candidates AS candidate ON candidate.market_id = evaluation.market_id
                    WHERE evaluation.id IN (SELECT MAX(id) FROM realtime_evaluations GROUP BY market_id)
                      AND date(candidate.end_date) = ?
                    ORDER BY evaluation.evaluated_at DESC""",
                (ny_date,),
            ).fetchall()
            checkpoint_rows = connection.execute(
                """SELECT market_id, checkpoint_name, payload_json FROM checkpoint_observations
                    WHERE checkpoint_date = ? AND eligible_for_calibration = 1
                      AND checkpoint_name IN ('1200_EDT', '1400_EDT', '1530_EDT')
                    ORDER BY evaluated_at, id""",
                (ny_date,),
            ).fetchall()

        # Old journals can contain overlapping regular and after-hours contracts.
        # Keep one stable row per symbol and prefer the regular-session contract.
        by_symbol: dict[str, dict[str, object]] = {}
        for row in rows:
            payload = json.loads(str(row[0]))
            symbol = str(payload.get("symbol") or "").upper()
            if not symbol:
                continue
            current = by_symbol.get(symbol)
            candidate_rank = (payload.get("market_session") == "REGULAR", str(payload.get("evaluated_at") or ""))
            current_rank = (
                (current.get("market_session") == "REGULAR", str(current.get("evaluated_at") or ""))
                if current
                else (False, "")
            )
            if current is None or candidate_rank > current_rank:
                by_symbol[symbol] = payload

        checkpoints_by_market: dict[str, dict[str, Mapping[str, object]]] = {}
        for market_id, checkpoint_name, payload_json in checkpoint_rows:
            checkpoints_by_market.setdefault(str(market_id), {})[str(checkpoint_name)] = json.loads(str(payload_json))
        selected = []
        for symbol in sorted(by_symbol)[:limit]:
            payload = dict(by_symbol[symbol])
            payload["checkpoints"] = checkpoints_by_market.get(str(payload.get("market_id")), {})
            selected.append(payload)
        return tuple(selected)
