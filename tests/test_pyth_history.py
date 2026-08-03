from datetime import UTC, datetime

import pytest

from polymarket_stock.pyth_history import PYTH_HISTORY_URL, PythHistoryClient, PythHistoryError


def test_pyth_history_requests_one_minute_equity_closes_with_bearer_auth() -> None:
    captured = {}

    def fake_get_json(url, params, **kwargs):
        captured.update({"url": url, "params": params, "headers": kwargs["headers"]})
        return {"s": "ok", "t": [1_777_296_600], "c": [300.25]}

    start = datetime(2026, 4, 27, 13, 30, tzinfo=UTC)
    series = PythHistoryClient("secret", fake_get_json).intraday_spots(
        "tsla", start_at=start, end_at=datetime(2026, 4, 27, 20, tzinfo=UTC)
    )

    assert series.symbol == "TSLA"
    assert series.points == ((datetime.fromtimestamp(1_777_296_600, tz=UTC), 300.25),)
    assert captured == {
        "url": PYTH_HISTORY_URL,
        "params": {"symbol": "Equity.US.TSLA/USD", "from": 1_777_296_600, "to": 1_777_320_000, "resolution": "1"},
        "headers": {"Authorization": "Bearer secret"},
    }


def test_pyth_history_rejects_empty_response() -> None:
    client = PythHistoryClient("secret", lambda *_args, **_kwargs: {"s": "ok", "t": [], "c": []})
    with pytest.raises(PythHistoryError, match="no usable"):
        client.intraday_spots(
            "NVDA", start_at=datetime(2026, 4, 27, 13, 30, tzinfo=UTC), end_at=datetime(2026, 4, 27, 20, tzinfo=UTC)
        )
