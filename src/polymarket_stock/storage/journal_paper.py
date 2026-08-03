"""JournalPaperRepository storage operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

from ..fees import estimate_taker_fee_usdc
from .journal_helpers import _maker_shadow_quote_from_row, _paper_position_from_row
from .journal_models import (
    MakerShadowQuote,
    PaperBatchEntry,
    PaperBatchResult,
    PaperPosition,
)
from .journal_repository import JournalRepository
from .sqlite import database_connection


class JournalPaperRepository(JournalRepository):
    def record_portfolio_decision(
        self,
        *,
        batch_id: str,
        market_id: str,
        symbol: str,
        outcome: str,
        risk_group: str,
        edge: float,
        selected: bool,
        reason: str,
        payload: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> None:
        if outcome not in {"UP", "DOWN"}:
            raise ValueError("portfolio decision outcome must be UP or DOWN")
        timestamp = created_at or datetime.now(UTC)
        with database_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO portfolio_decisions (
                        created_at, batch_id, market_id, symbol, outcome, risk_group, edge, status, reason, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp.isoformat(),
                    batch_id,
                    market_id,
                    symbol.upper(),
                    outcome,
                    risk_group,
                    edge,
                    "SELECTED" if selected else "REJECTED",
                    reason,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )

    def commit_paper_batch(
        self, *, batch_id: str, entries: tuple[PaperBatchEntry, ...], created_at: datetime
    ) -> tuple[PaperBatchResult, ...]:
        """Atomically record portfolio decisions and create selected paper positions."""
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        results: list[PaperBatchResult] = []
        with database_connection(self.path) as connection:
            for entry in entries:
                if entry.outcome not in {"UP", "DOWN"}:
                    raise ValueError("paper batch outcome must be UP or DOWN")
                connection.execute(
                    """INSERT INTO portfolio_decisions (
                            created_at, batch_id, market_id, symbol, outcome, risk_group, edge, status, reason, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",  # noqa: E501
                    (
                        created_at.isoformat(),
                        batch_id,
                        entry.market_id,
                        entry.symbol.upper(),
                        entry.outcome,
                        entry.risk_group,
                        entry.edge,
                        "SELECTED" if entry.selected else "REJECTED",
                        entry.reason,
                        json.dumps(entry.payload, sort_keys=True, separators=(",", ":"), default=str),
                    ),
                )
                entry_ask = entry.entry_ask
                fair_probability = entry.fair_probability
                model_version = entry.model_version
                fee_rate = entry.fee_rate
                if (
                    not entry.selected
                    or entry_ask is None
                    or fair_probability is None
                    or model_version is None
                    or fee_rate is None
                ):
                    results.append(PaperBatchResult(entry.market_id, None, False))
                    continue
                open_row = connection.execute(
                    "SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts, entry_ask, entry_fee, entry_slippage, fair_probability, model_version, settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason FROM paper_positions WHERE market_id = ? AND status = 'OPEN'",  # noqa: E501
                    (entry.market_id,),
                ).fetchone()
                if open_row is not None:
                    results.append(PaperBatchResult(entry.market_id, _paper_position_from_row(open_row), False))
                    continue
                position_id = hashlib.sha256(f"{entry.market_id}|{entry.outcome}".encode()).hexdigest()
                entry_fee = estimate_taker_fee_usdc(shares=1.0, price=entry_ask, fee_rate=fee_rate)
                inserted = (
                    connection.execute(
                        """INSERT OR IGNORE INTO paper_positions (position_id, opened_at, market_id, symbol, outcome, status, contracts, entry_ask, entry_fee, entry_slippage, fair_probability, model_version, entry_payload_json) VALUES (?, ?, ?, ?, ?, 'OPEN', 1.0, ?, ?, 0.0, ?, ?, ?)""",  # noqa: E501
                        (
                            position_id,
                            created_at.isoformat(),
                            entry.market_id,
                            entry.symbol.upper(),
                            entry.outcome,
                            entry_ask,
                            entry_fee,
                            fair_probability,
                            model_version,
                            json.dumps(entry.payload, sort_keys=True, separators=(",", ":"), default=str),
                        ),
                    ).rowcount
                    == 1
                )
                row = connection.execute(
                    "SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts, entry_ask, entry_fee, entry_slippage, fair_probability, model_version, settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason FROM paper_positions WHERE position_id = ?",  # noqa: E501
                    (position_id,),
                ).fetchone()
                results.append(
                    PaperBatchResult(entry.market_id, _paper_position_from_row(row) if row else None, inserted)
                )
        return tuple(results)

    def open_paper_position(
        self,
        *,
        market_id: str,
        symbol: str,
        outcome: str,
        entry_ask: float,
        fair_probability: float,
        model_version: str,
        payload: Mapping[str, object],
        contracts: float = 1.0,
        fee_rate: float,
        opened_at: datetime | None = None,
    ) -> tuple[PaperPosition, bool]:
        """Create one hold-to-settlement paper entry, idempotently per market/outcome."""

        if outcome not in {"UP", "DOWN"}:
            raise ValueError("paper position outcome must be UP or DOWN")
        if not 0 <= entry_ask <= 1 or not 0 <= fair_probability <= 1:
            raise ValueError("entry_ask and fair_probability must be between 0 and 1")
        if contracts <= 0 or fee_rate < 0 or not model_version:
            raise ValueError("invalid paper position inputs")
        timestamp = opened_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        position_id = hashlib.sha256(f"{market_id}|{outcome}".encode()).hexdigest()
        entry_fee = estimate_taker_fee_usdc(shares=contracts, price=entry_ask, fee_rate=fee_rate)
        entry_slippage = 0.0
        with database_connection(self.path) as connection:
            open_row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                        entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                        settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                    FROM paper_positions WHERE market_id = ? AND status = 'OPEN'""",
                (market_id,),
            ).fetchone()
            if open_row is not None:
                return _paper_position_from_row(open_row), False
            inserted = (
                connection.execute(
                    """INSERT OR IGNORE INTO paper_positions (
                        position_id, opened_at, market_id, symbol, outcome, status, contracts,
                        entry_ask, entry_fee, entry_slippage, fair_probability, model_version, entry_payload_json
                    ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        position_id,
                        timestamp.isoformat(),
                        market_id,
                        symbol.upper(),
                        outcome,
                        contracts,
                        entry_ask,
                        entry_fee,
                        entry_slippage,
                        fair_probability,
                        model_version,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                    ),
                ).rowcount
                == 1
            )
            row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                        entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                        settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                    FROM paper_positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("paper position insert did not return a row")
        return _paper_position_from_row(row), inserted

    def sync_maker_shadow_quote(
        self,
        *,
        market_id: str,
        symbol: str,
        outcome: str,
        limit_price: float | None,
        fair_probability: float | None,
        theoretical_edge: float | None,
        best_bid: float | None,
        best_ask: float | None,
        payload: Mapping[str, object],
        no_quote_reason: str = "NO_MAKER_EDGE",
        minimum_reprice_price_change: float = 0.0,
        minimum_quote_lifetime_seconds: float = 0.0,
        observed_at: datetime | None = None,
    ) -> tuple[MakerShadowQuote | None, str | None]:
        """Create, reprice, or cancel one passive quote without assuming a fill."""

        if outcome not in {"UP", "DOWN"}:
            raise ValueError("maker quote outcome must be UP or DOWN")
        if minimum_reprice_price_change < 0 or minimum_quote_lifetime_seconds < 0:
            raise ValueError("maker reprice thresholds must be non-negative")
        timestamp = observed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        has_proposal = None not in (limit_price, fair_probability, theoretical_edge, best_bid, best_ask)
        if (
            has_proposal
            and limit_price is not None
            and fair_probability is not None
            and best_bid is not None
            and best_ask is not None
            and not (0 < limit_price < 1 and 0 <= fair_probability <= 1 and 0 <= best_bid <= best_ask <= 1)
        ):
            raise ValueError("invalid maker quote proposal")
        with database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                        limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                        last_touched_at, cancelled_at, cancel_reason
                    FROM maker_shadow_quotes WHERE market_id = ? AND outcome = ? AND status = 'ACTIVE'""",
                (market_id, outcome),
            ).fetchone()
            active = _maker_shadow_quote_from_row(row) if row is not None else None
            if (
                limit_price is None
                or fair_probability is None
                or theoretical_edge is None
                or best_bid is None
                or best_ask is None
            ):
                if active is None:
                    return None, None
                connection.execute(
                    """UPDATE maker_shadow_quotes SET status = 'CANCELLED', cancelled_at = ?,
                        cancel_reason = ?, last_observed_at = ? WHERE quote_id = ?""",
                    (timestamp.isoformat(), no_quote_reason, timestamp.isoformat(), active.quote_id),
                )
                return None, "CANCELLED"
            if active is not None and active.limit_price == limit_price:
                connection.execute(
                    """UPDATE maker_shadow_quotes SET last_observed_at = ?, fair_probability = ?,
                        theoretical_edge = ?, best_bid = ?, best_ask = ?, payload_json = ? WHERE quote_id = ?""",
                    (
                        timestamp.isoformat(),
                        fair_probability,
                        theoretical_edge,
                        best_bid,
                        best_ask,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                        active.quote_id,
                    ),
                )
                return replace(
                    active,
                    last_observed_at=timestamp,
                    fair_probability=fair_probability,
                    theoretical_edge=theoretical_edge,
                    best_bid=best_bid,
                    best_ask=best_ask,
                ), None
            if active is not None:
                price_change = abs(active.limit_price - limit_price)
                quote_age_seconds = max(0.0, (timestamp - active.created_at).total_seconds())
                should_hold = (
                    price_change < minimum_reprice_price_change or quote_age_seconds < minimum_quote_lifetime_seconds
                )
                if should_hold:
                    connection.execute(
                        """UPDATE maker_shadow_quotes SET last_observed_at = ?, fair_probability = ?,
                            theoretical_edge = ?, best_bid = ?, best_ask = ?, payload_json = ? WHERE quote_id = ?""",
                        (
                            timestamp.isoformat(),
                            fair_probability,
                            theoretical_edge,
                            best_bid,
                            best_ask,
                            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                            active.quote_id,
                        ),
                    )
                    return replace(
                        active,
                        last_observed_at=timestamp,
                        fair_probability=float(fair_probability),
                        theoretical_edge=float(theoretical_edge),
                        best_bid=float(best_bid),
                        best_ask=float(best_ask),
                    ), None
            action = "OPENED" if active is None else "REPRICED"
            if active is not None:
                connection.execute(
                    """UPDATE maker_shadow_quotes SET status = 'CANCELLED', cancelled_at = ?,
                        cancel_reason = 'REPRICE_LIMIT_CHANGED', last_observed_at = ? WHERE quote_id = ?""",
                    (timestamp.isoformat(), timestamp.isoformat(), active.quote_id),
                )
            quote_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO maker_shadow_quotes (
                        quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                        limit_price, fair_probability, theoretical_edge, best_bid, best_ask, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)""",
                (
                    quote_id,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                    market_id,
                    symbol.upper(),
                    outcome,
                    limit_price,
                    fair_probability,
                    theoretical_edge,
                    best_bid,
                    best_ask,
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )
            row = connection.execute(
                """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                        limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                        last_touched_at, cancelled_at, cancel_reason FROM maker_shadow_quotes WHERE quote_id = ?""",
                (quote_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("maker quote insert did not return a row")
        return _maker_shadow_quote_from_row(row), action

    def record_maker_shadow_touch(
        self, *, market_id: str, outcome: str, current_ask: float, observed_at: datetime | None = None
    ) -> MakerShadowQuote | None:
        """Record an ask touching an active quote; a touch is explicitly not a fill."""

        if outcome not in {"UP", "DOWN"} or not 0 <= current_ask <= 1:
            raise ValueError("invalid maker touch inputs")
        timestamp = observed_at or datetime.now(UTC)
        with database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT quote_id, limit_price FROM maker_shadow_quotes
                    WHERE market_id = ? AND outcome = ? AND status = 'ACTIVE'""",
                (market_id, outcome),
            ).fetchone()
            if row is None or current_ask > float(row["limit_price"]):
                return None
            connection.execute(
                """UPDATE maker_shadow_quotes SET touch_count = touch_count + 1,
                        last_touched_at = ?, last_observed_at = ? WHERE quote_id = ?""",
                (timestamp.isoformat(), timestamp.isoformat(), str(row["quote_id"])),
            )
            updated = connection.execute(
                """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                        limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                        last_touched_at, cancelled_at, cancel_reason FROM maker_shadow_quotes WHERE quote_id = ?""",
                (str(row["quote_id"]),),
            ).fetchone()
        return _maker_shadow_quote_from_row(updated) if updated is not None else None  # noqa: F821

    def cancel_maker_shadow_quotes(self, market_id: str, reason: str, cancelled_at: datetime | None = None) -> int:
        timestamp = cancelled_at or datetime.now(UTC)
        with database_connection(self.path) as connection:
            return connection.execute(
                """UPDATE maker_shadow_quotes SET status = 'CANCELLED', cancelled_at = ?, cancel_reason = ?,
                    last_observed_at = ? WHERE market_id = ? AND status = 'ACTIVE'""",
                (timestamp.isoformat(), reason, timestamp.isoformat(), market_id),
            ).rowcount

    def list_maker_shadow_quotes(self, status: str | None = None) -> tuple[MakerShadowQuote, ...]:
        if status is not None and status not in {"ACTIVE", "CANCELLED"}:
            raise ValueError("maker quote status must be ACTIVE or CANCELLED")
        query = """SELECT quote_id, created_at, last_observed_at, market_id, symbol, outcome, status,
                limit_price, fair_probability, theoretical_edge, best_bid, best_ask, touch_count,
                last_touched_at, cancelled_at, cancel_reason FROM maker_shadow_quotes"""
        parameters: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY created_at DESC"
        with database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_maker_shadow_quote_from_row(row) for row in rows)  # noqa: F821

    def list_paper_positions(self, status: str | None = None) -> tuple[PaperPosition, ...]:
        if status is not None and status not in {"OPEN", "SETTLED"}:
            raise ValueError("status must be OPEN or SETTLED")
        query = """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                FROM paper_positions"""
        parameters: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY opened_at ASC"
        with database_connection(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_paper_position_from_row(row) for row in rows)

    def settle_paper_position(
        self,
        position_id: str,
        *,
        settlement_outcome: str,
        settlement_payload: Mapping[str, object],
        settled_at: datetime | None = None,
    ) -> PaperPosition:
        if settlement_outcome not in {"UP", "DOWN"}:
            raise ValueError("settlement_outcome must be UP or DOWN")
        timestamp = settled_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware")
        with database_connection(self.path) as connection:
            row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                        entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                        settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                    FROM paper_positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown paper position {position_id}")
            position = _paper_position_from_row(row)
            if position.status == "SETTLED":
                return position
            payout = position.contracts if position.outcome == settlement_outcome else 0.0
            realized_pnl = payout - (
                position.entry_ask * position.contracts + position.entry_fee + position.entry_slippage
            )
            connection.execute(
                """UPDATE paper_positions SET status = 'SETTLED', settled_at = ?, settlement_outcome = ?,
                        payout = ?, realized_pnl = ?, settlement_payload_json = ? WHERE position_id = ?""",
                (
                    timestamp.isoformat(),
                    settlement_outcome,
                    payout,
                    realized_pnl,
                    json.dumps(settlement_payload, sort_keys=True, separators=(",", ":"), default=str),
                    position_id,
                ),
            )
            row = connection.execute(
                """SELECT position_id, opened_at, market_id, symbol, outcome, status, contracts,
                        entry_ask, entry_fee, entry_slippage, fair_probability, model_version,
                        settled_at, settlement_outcome, payout, realized_pnl, included_in_calibration, exclusion_reason
                    FROM paper_positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("paper position settlement did not return a row")
        return _paper_position_from_row(row)
