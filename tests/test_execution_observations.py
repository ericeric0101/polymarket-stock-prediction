from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from polymarket_stock.journal import ShadowJournal


def test_execution_observation_persists_book_fee_and_signal_link(tmp_path: Path) -> None:
    journal = ShadowJournal(tmp_path / "journal.db")
    journal.initialize()
    journal.record_execution_observation(
        observed_at=datetime(2026, 7, 27, 16, tzinfo=UTC),
        signal_id="position-1",
        observation_kind="MARKOUT_300S",
        market_id="market-1",
        symbol="TSLA",
        outcome="UP",
        token_id="token-up",
        spot=320.0,
        price_to_beat=318.5,
        fair_probability=0.72,
        best_bid=0.66,
        best_ask=0.68,
        fee_rate=0.04,
        book_payload={"bids": [{"price": "0.66", "size": "20"}], "asks": [{"price": "0.68", "size": "15"}]},
        evaluation_payload={"model_error_buffer": 0.02},
    )
    connection = sqlite3.connect(journal.path)
    row = connection.execute(
        "SELECT signal_id, observation_kind, spot, price_to_beat, best_bid, best_ask, fee_rate, book_payload_json FROM execution_observations"
    ).fetchone()
    connection.close()
    assert row[:7] == ("position-1", "MARKOUT_300S", 320.0, 318.5, 0.66, 0.68, 0.04)
    assert '"size":"15"' in row[7]
    observation = journal.list_execution_observations()[0]
    assert observation.signal_id == "position-1"
    assert observation.book_payload["asks"][0]["size"] == "15"
