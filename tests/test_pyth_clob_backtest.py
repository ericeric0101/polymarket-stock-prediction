from datetime import UTC, datetime, timedelta
from pathlib import Path
import json

from polymarket_stock.pyth_clob_backtest import run_pyth_clob_backtest


def test_batch_replay_uses_only_prior_days_and_reports_walk_forward_shortage(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, 20, tzinfo=UTC)
    for index in range(22):
        day = (start + timedelta(days=index)).date().isoformat()
        stem = f"{index}_TSLA_{day}"
        reference = {
            "market_id": str(index),
            "symbol": "TSLA",
            "market_day": day,
            "price_to_beat": {"price": 100 + index * 0.1},
            "final_price": {"price": 100.2 + index * 0.1, "requested_at": (start + timedelta(days=index)).isoformat()},
        }
        (tmp_path / f"{stem}_pyth_references.json").write_text(json.dumps(reference), encoding="utf-8")
        (tmp_path / f"{stem}_settlement.json").write_text(json.dumps({"winning_outcome": "UP"}), encoding="utf-8")
        (tmp_path / f"{stem}_up_clob.csv").write_text(
            "DateTime,Price\n" + f"{day}T17:00:00+00:00,0.40\n", encoding="utf-8"
        )
        (tmp_path / f"{stem}_down_clob.csv").write_text(
            "DateTime,Price\n" + f"{day}T17:00:00+00:00,0.60\n", encoding="utf-8"
        )
        (tmp_path / f"{stem}_pyth_intraday.csv").write_text(
            "DateTime,Spot\n" + f"{day}T17:00:00+00:00,{101 + index * 0.1}\n", encoding="utf-8"
        )

    report = run_pyth_clob_backtest(
        data_dir=tmp_path,
        buffers=(0.01, 0.02),
        minimum_edge=0.0,
        training_days=20,
        validation_days=5,
        minimum_training_trades=1,
    )

    assert report.discovered_market_days == 22
    assert report.eligible_market_days == 1
    assert report.observation_count == 2
    assert report.sweep.results[0].selected_trades == 1
    assert report.walk_forward.status == "INSUFFICIENT_DISTINCT_DAYS"
