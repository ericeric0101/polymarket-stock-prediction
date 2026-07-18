"""Typed representation of a market contract after its terms are reviewed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ContractValidationError(ValueError):
    """Raised when a market cannot be safely represented as a daily direction contract."""


@dataclass(frozen=True)
class DailyDirectionContract:
    market_id: str
    title: str
    underlying: str
    reference_price: float
    reference_source: str
    resolves_at: datetime
    timezone: str

    def __post_init__(self) -> None:
        if not self.market_id.strip() or not self.title.strip():
            raise ContractValidationError("market_id and title are required")
        if not self.underlying.isalpha() or self.underlying != self.underlying.upper():
            raise ContractValidationError("underlying must be an uppercase ticker")
        if self.reference_price <= 0:
            raise ContractValidationError("reference_price must be positive")
        if not self.reference_source.strip():
            raise ContractValidationError("reference_source is required")
        if self.resolves_at.tzinfo is None:
            raise ContractValidationError("resolves_at must be timezone-aware")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ContractValidationError(f"unknown timezone: {self.timezone}") from error

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "DailyDirectionContract":
        required = {
            "market_id",
            "title",
            "underlying",
            "reference_price",
            "reference_source",
            "resolves_at",
            "timezone",
        }
        missing = sorted(required.difference(payload))
        if missing:
            raise ContractValidationError(f"missing contract fields: {', '.join(missing)}")

        resolves_at_raw = payload["resolves_at"]
        if not isinstance(resolves_at_raw, str):
            raise ContractValidationError("resolves_at must be an ISO-8601 string")
        try:
            resolves_at = datetime.fromisoformat(resolves_at_raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise ContractValidationError("resolves_at is not valid ISO-8601") from error

        try:
            return cls(
                market_id=str(payload["market_id"]),
                title=str(payload["title"]),
                underlying=str(payload["underlying"]),
                reference_price=float(payload["reference_price"]),
                reference_source=str(payload["reference_source"]),
                resolves_at=resolves_at,
                timezone=str(payload["timezone"]),
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, ContractValidationError):
                raise
            raise ContractValidationError("contract contains invalid values") from error
