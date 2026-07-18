"""Read-only discovery of daily equity-direction market candidates."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Callable, Iterable, Mapping

from .http import get_json


GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_EVENTS_KEYSET_URL = "https://gamma-api.polymarket.com/events/keyset"
REVIEW_REQUIRED = "REVIEW_REQUIRED"


class MarketPayloadError(ValueError):
    """Raised when a Gamma payload cannot safely identify a binary CLOB market."""


def _as_string_list(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise MarketPayloadError(f"{field_name} is not JSON") from error
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MarketPayloadError(f"{field_name} must be an array of strings")
    return tuple(value)


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class MarketCandidate:
    market_id: str
    question: str
    slug: str
    description: str
    resolution_source: str
    end_date: str
    outcome_a_label: str
    outcome_b_label: str
    outcome_a_token_id: str
    outcome_b_token_id: str
    review_status: str
    raw_payload: Mapping[str, object]

    @classmethod
    def from_gamma_payload(cls, payload: Mapping[str, object]) -> "MarketCandidate":
        market_id = _string_or_empty(payload.get("id"))
        question = _string_or_empty(payload.get("question"))
        if not market_id or not question:
            raise MarketPayloadError("market id and question are required")

        outcomes = _as_string_list(payload.get("outcomes"), "outcomes")
        token_ids = _as_string_list(payload.get("clobTokenIds"), "clobTokenIds")
        if len(outcomes) != 2 or len(token_ids) != 2 or not all(outcomes) or not all(token_ids):
            raise MarketPayloadError("market is not an eligible binary CLOB market")

        return cls(
            market_id=market_id,
            question=question,
            slug=_string_or_empty(payload.get("slug")),
            description=_string_or_empty(payload.get("description")),
            resolution_source=_string_or_empty(payload.get("resolutionSource")),
            end_date=_string_or_empty(payload.get("endDate")),
            outcome_a_label=outcomes[0],
            outcome_b_label=outcomes[1],
            outcome_a_token_id=token_ids[0],
            outcome_b_token_id=token_ids[1],
            review_status=REVIEW_REQUIRED,
            raw_payload=dict(payload),
        )


@dataclass(frozen=True)
class DiscoveryReport:
    candidates: tuple[MarketCandidate, ...]
    events_scanned: int
    pages_scanned: int
    tag_slugs: tuple[str, ...]


def is_daily_equity_direction_candidate(candidate: MarketCandidate, symbols: Iterable[str] | None) -> bool:
    """Use a narrow title filter; contract eligibility still requires human review."""

    question = candidate.question.upper()
    normalized_symbols = tuple(symbol.strip().upper() for symbol in (symbols or ()) if symbol.strip())
    contains_symbol = any(re.search(rf"(?<![A-Z]){re.escape(symbol)}(?![A-Z])", question) for symbol in normalized_symbols)
    direction_words = ("UP OR DOWN", "UP/DOWN", "CLOSE HIGHER", "CLOSE LOWER", "CLOSE UP", "CLOSE DOWN")
    directional_outcomes = {("UP", "DOWN"), ("YES", "NO"), ("HIGHER", "LOWER")}
    outcome_pair = (candidate.outcome_a_label.upper(), candidate.outcome_b_label.upper())
    raw_tags = candidate.raw_payload.get("tags")
    tags = raw_tags if isinstance(raw_tags, list) else ()
    tag_slugs = {
        str(tag.get("slug", "")).lower()
        for tag in tags if isinstance(tag, dict)
    }
    tagged_as_equity = bool({"stocks", "equities"}.intersection(tag_slugs))
    has_requested_symbol = contains_symbol if normalized_symbols else tagged_as_equity
    return has_requested_symbol and any(word in question for word in direction_words) and outcome_pair in directional_outcomes


class GammaMarketClient:
    """Public Gamma client. It only performs GET requests to the market endpoint."""

    def __init__(self, get_json_fn: Callable[..., object] = get_json, sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self._get_json = get_json_fn
        self._sleep = sleep_fn

    def list_active_markets(self, limit: int = 200) -> list[Mapping[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        response = self._get_json(
            GAMMA_MARKETS_URL,
            {"active": "true", "closed": "false", "limit": limit},
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise MarketPayloadError("Gamma markets response must be an array of objects")
        return response

    def get_event_by_slug(self, slug: str) -> Mapping[str, object]:
        if not slug.strip():
            raise ValueError("event slug is required")
        response = self._get_json(GAMMA_EVENTS_URL, {"slug": slug})
        if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
            raise MarketPayloadError(f"Gamma did not return exactly one event for slug {slug}")
        return response[0]

    def discover_event_candidates(self, slug: str, symbols: Iterable[str]) -> list[MarketCandidate]:
        event = self.get_event_by_slug(slug)
        markets = event.get("markets")
        if not isinstance(markets, list) or not all(isinstance(item, dict) for item in markets):
            raise MarketPayloadError("Gamma event response must include a markets array")
        candidates: list[MarketCandidate] = []
        for market in markets:
            merged_payload = {**event, **market}
            try:
                candidate = MarketCandidate.from_gamma_payload(merged_payload)
            except MarketPayloadError:
                continue
            if is_daily_equity_direction_candidate(candidate, symbols):
                candidates.append(candidate)
        return candidates

    def discover_active_equity_candidates(
        self,
        *,
        tag_slugs: Iterable[str] = ("stocks", "equities"),
        page_size: int = 500,
        max_pages_per_tag: int = 100,
        pause_seconds: float = 0.2,
    ) -> DiscoveryReport:
        """Cursor-scan tagged active events, retaining only daily direction markets.

        No symbol allowlist is used here. Tags scope the remote query, and local
        validation retains explicit Up/Down-style binary markets only.
        """

        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if max_pages_per_tag < 1:
            raise ValueError("max_pages_per_tag must be at least 1")
        if pause_seconds < 0:
            raise ValueError("pause_seconds cannot be negative")

        normalized_tags = tuple(tag.strip().lower() for tag in tag_slugs if tag.strip())
        if not normalized_tags:
            raise ValueError("at least one tag slug is required")

        candidates_by_market_id: dict[str, MarketCandidate] = {}
        events_scanned = 0
        pages_scanned = 0
        for tag_slug in normalized_tags:
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for page_number in range(max_pages_per_tag):
                params: dict[str, object] = {
                    "limit": page_size,
                    "closed": "false",
                    "tag_slug": tag_slug,
                    "related_tags": "true",
                }
                if cursor:
                    params["after_cursor"] = cursor
                response = self._get_json(GAMMA_EVENTS_KEYSET_URL, params)
                if not isinstance(response, dict):
                    raise MarketPayloadError("Gamma keyset events response must be an object")
                events = response.get("events")
                if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
                    raise MarketPayloadError("Gamma keyset events response must contain an events array")
                pages_scanned += 1
                events_scanned += len(events)
                for event in events:
                    if event.get("active") is not True or event.get("closed") is not False:
                        continue
                    markets = event.get("markets")
                    if not isinstance(markets, list):
                        continue
                    for market in markets:
                        if not isinstance(market, dict):
                            continue
                        try:
                            candidate = MarketCandidate.from_gamma_payload({**event, **market})
                        except MarketPayloadError:
                            continue
                        if is_daily_equity_direction_candidate(candidate, symbols=None):
                            candidates_by_market_id[candidate.market_id] = candidate

                next_cursor = response.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise MarketPayloadError("Gamma keyset pagination repeated a cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                if page_number + 1 < max_pages_per_tag and pause_seconds:
                    self._sleep(pause_seconds)

        return DiscoveryReport(
            candidates=tuple(candidates_by_market_id.values()),
            events_scanned=events_scanned,
            pages_scanned=pages_scanned,
            tag_slugs=normalized_tags,
        )

    def discover_daily_equity_candidates(self, symbols: Iterable[str], limit: int = 200) -> list[MarketCandidate]:
        candidates: list[MarketCandidate] = []
        for payload in self.list_active_markets(limit=limit):
            try:
                candidate = MarketCandidate.from_gamma_payload(payload)
            except MarketPayloadError:
                continue
            if is_daily_equity_direction_candidate(candidate, symbols):
                candidates.append(candidate)
        return candidates
