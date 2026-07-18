"""Small dependency-free HTTP helpers for public, read-only market data."""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PublicApiError(RuntimeError):
    """Raised when a public market-data endpoint cannot provide valid JSON."""


def get_json(
    url: str,
    params: Mapping[str, object] | None = None,
    timeout_seconds: float = 15.0,
    headers: Mapping[str, str] | None = None,
) -> Any:
    query = urlencode(params or {}, doseq=True)
    request_url = f"{url}?{query}" if query else url
    request_headers = {"Accept": "application/json", "User-Agent": "polymarket-stock-shadow/0.1"}
    request_headers.update(headers or {})
    request = Request(request_url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise PublicApiError(f"GET {request_url} returned HTTP {response.status}")
            body = response.read().decode("utf-8")
    except HTTPError as error:
        raise PublicApiError(f"GET {request_url} returned HTTP {error.code}") from error
    except URLError as error:
        raise PublicApiError(f"GET {request_url} failed: {error.reason}") from error

    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise PublicApiError(f"GET {request_url} returned invalid JSON") from error
