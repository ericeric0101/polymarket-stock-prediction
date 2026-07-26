from pathlib import Path
import json

from polymarket_stock.intraday_spot_backfill import backfill_pyth_intraday_spots
from polymarket_stock.pyth_history import PythIntradaySpotSeries


def test_backfill_writes_one_resumable_file_per_nvda_tsla_market(monkeypatch, tmp_path: Path) -> None:
    discovery = tmp_path / "discovery.json"
    discovery.write_text(json.dumps([
        {"status": "FOUND", "market_id": "1", "symbol": "NVDA", "market_day": "2026-04-27"},
        {"status": "FOUND", "market_id": "2", "symbol": "TSLA", "market_day": "2026-04-27"},
        {"status": "FOUND", "market_id": "3", "symbol": "AAPL", "market_day": "2026-04-27"},
    ]), encoding="utf-8")

    class FakeClient:
        def __init__(self, _api_key):
            pass

        def intraday_spots(self, symbol, *, start_at, end_at):
            assert start_at.isoformat() == "2026-04-27T13:30:00+00:00"
            assert end_at.isoformat() == "2026-04-27T20:00:00+00:00"
            return PythIntradaySpotSeries(symbol, ((start_at, 100.0),))

    monkeypatch.setattr("polymarket_stock.intraday_spot_backfill.PythHistoryClient", FakeClient)
    output = tmp_path / "spots"
    report = backfill_pyth_intraday_spots(discovery_path=discovery, output_dir=output, api_key="secret", pause_seconds=0)

    assert (report.requested, report.completed, report.skipped, report.failed) == (2, 2, 0, 0)
    assert (output / "1_NVDA_2026-04-27_pyth_intraday.csv").read_text(encoding="utf-8").splitlines() == ["DateTime,Spot", "2026-04-27T13:30:00+00:00,100.0"]
    resumed = backfill_pyth_intraday_spots(discovery_path=discovery, output_dir=output, api_key="secret", pause_seconds=0)
    assert (resumed.completed, resumed.skipped, resumed.failed) == (0, 2, 0)
