"""Official, public Polymarket taker-fee lookups and calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import time
from typing import Mapping

from .http import PublicApiError, get_json


CLOB_FEE_RATE_URL = "https://clob.polymarket.com/fee-rate"
FEE_PRECISION = Decimal("0.00001")


@dataclass(frozen=True)
class FeeRateQuote:
    token_id: str
    fee_rate: float
    fetched_at: float
    source: str = "POLYMARKET_CLOB_FEE_RATE"


def estimate_taker_fee_usdc(*, shares: float, price: float, fee_rate: float) -> float:
    """Return Polymarket's official taker fee, rounded to its published precision."""

    if shares <= 0 or not 0 <= price <= 1 or fee_rate < 0:
        raise ValueError("shares must be positive; price and fee_rate must be non-negative")
    fee = Decimal(str(shares)) * Decimal(str(fee_rate)) * Decimal(str(price)) * (Decimal("1") - Decimal(str(price)))
    return float(fee.quantize(FEE_PRECISION, rounding=ROUND_HALF_UP))


class PolymarketFeeRateClient:
    """Caches the protocol-published rate for each outcome token."""

    def __init__(self, *, ttl_seconds: float = 300, get_json_fn=get_json) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._get_json = get_json_fn
        self._cache: dict[str, FeeRateQuote] = {}

    def get_fee_rate(self, token_id: str) -> FeeRateQuote:
        if not token_id.strip():
            raise ValueError("token_id is required")
        now = time.monotonic()
        cached = self._cache.get(token_id)
        if cached and now - cached.fetched_at < self._ttl_seconds:
            return cached
        payload = self._get_json(f"{CLOB_FEE_RATE_URL}/{token_id}")
        if not isinstance(payload, Mapping):
            raise PublicApiError("Polymarket fee-rate response must be an object")
        raw_rate = payload.get("base_fee", payload.get("fee_rate"))
        try:
            rate = float(raw_rate) / 10_000 if "base_fee" in payload else float(raw_rate)
        except (TypeError, ValueError) as error:
            raise PublicApiError("Polymarket fee-rate response has no usable fee rate") from error
        if not 0 <= rate <= 1:
            raise PublicApiError("Polymarket fee-rate is outside valid bounds")
        quote = FeeRateQuote(token_id=token_id, fee_rate=rate, fetched_at=now)
        self._cache[token_id] = quote
        return quote
