"""Read-only historical CLOB price data for offline replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from .http import get_json


CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"


class PriceHistoryPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class PriceHistoryPoint:
    observed_at: datetime
    price: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "PriceHistoryPoint":
        try:
            timestamp = float(payload["t"])
            price = float(payload["p"])
        except (KeyError, TypeError, ValueError) as error:
            raise PriceHistoryPayloadError("price history point requires numeric t and p") from error
        if not 0 <= price <= 1:
            raise PriceHistoryPayloadError("price history price must be a probability")
        return cls(datetime.fromtimestamp(timestamp, tz=UTC), price)


class ClobPriceHistoryClient:
    """Public CLOB price-history client.

    Polymarket's parameter name is ``market`` but it expects a CLOB token id.
    """

    def __init__(self, get_json_fn: Callable[..., object] = get_json) -> None:
        self._get_json = get_json_fn

    def prices_history(
        self,
        token_id: str,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        interval: str | None = None,
        fidelity_minutes: int = 1,
    ) -> tuple[PriceHistoryPoint, ...]:
        if not token_id.strip():
            raise ValueError("token_id is required")
        if fidelity_minutes < 1:
            raise ValueError("fidelity_minutes must be positive")
        params: dict[str, object] = {"market": token_id, "fidelity": fidelity_minutes}
        if start_at is not None:
            if start_at.tzinfo is None:
                raise ValueError("start_at must be timezone-aware")
            params["startTs"] = int(start_at.timestamp())
        if end_at is not None:
            if end_at.tzinfo is None:
                raise ValueError("end_at must be timezone-aware")
            params["endTs"] = int(end_at.timestamp())
        if interval:
            params["interval"] = interval
        response = self._get_json(CLOB_PRICES_HISTORY_URL, params)
        if not isinstance(response, Mapping) or not isinstance(response.get("history"), list):
            raise PriceHistoryPayloadError("CLOB price history response must include a history array")
        points = [PriceHistoryPoint.from_payload(item) for item in response["history"] if isinstance(item, Mapping)]
        return tuple(sorted(points, key=lambda item: item.observed_at))
