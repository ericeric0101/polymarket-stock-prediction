"""SQLite schema definitions for persistent research journals."""

from __future__ import annotations

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_decisions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    fair_yes_probability REAL NOT NULL CHECK (fair_yes_probability >= 0 AND fair_yes_probability <= 1),
    executable_ask REAL NOT NULL CHECK (executable_ask >= 0 AND executable_ask <= 1),
    edge REAL NOT NULL,
    should_record_paper_trade INTEGER NOT NULL CHECK (should_record_paper_trade IN (0, 1)),
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_decisions_market_created
    ON shadow_decisions (market_id, created_at);
CREATE TABLE IF NOT EXISTS market_candidates (
    market_id TEXT PRIMARY KEY,
    discovered_at TEXT NOT NULL,
    question TEXT NOT NULL,
    slug TEXT NOT NULL,
    end_date TEXT NOT NULL,
    resolution_source TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    outcome_a_label TEXT NOT NULL DEFAULT '',
    outcome_b_label TEXT NOT NULL DEFAULT '',
    outcome_a_token_id TEXT NOT NULL DEFAULT '',
    outcome_b_token_id TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    midpoint REAL,
    raw_payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_market_observed
    ON order_book_snapshots (market_id, observed_at);
CREATE TABLE IF NOT EXISTS execution_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    signal_id TEXT,
    observation_kind TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    token_id TEXT NOT NULL,
    spot REAL,
    price_to_beat REAL,
    fair_probability REAL,
    best_bid REAL,
    best_ask REAL,
    fee_rate REAL,
    book_payload_json TEXT NOT NULL,
    evaluation_payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_observations_signal_observed
    ON execution_observations (signal_id, observed_at);
CREATE TABLE IF NOT EXISTS alpaca_indicative_option_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    bid_price REAL NOT NULL,
    ask_price REAL NOT NULL,
    feed TEXT NOT NULL,
    quality_label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alpaca_option_quotes_symbol_observed
    ON alpaca_indicative_option_quotes (option_symbol, observed_at);
CREATE TABLE IF NOT EXISTS realtime_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    spot REAL,
    up_ask REAL,
    down_ask REAL,
    fair_up_probability REAL,
    signal_status TEXT NOT NULL,
    skip_reasons_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_realtime_evaluations_market_evaluated
    ON realtime_evaluations (market_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_realtime_evaluations_signal_market_evaluated
    ON realtime_evaluations (market_id, evaluated_at)
    WHERE fair_up_probability IS NOT NULL
      AND signal_status IN ('PAPER_UP', 'PAPER_DOWN', 'OBSERVATION_ONLY_UP', 'OBSERVATION_ONLY_DOWN');
CREATE TABLE IF NOT EXISTS spot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    observed_second TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    published_at TEXT,
    confidence REAL,
    feed_id TEXT,
    UNIQUE (source, symbol, observed_second)
);
CREATE INDEX IF NOT EXISTS idx_spot_observations_symbol_observed
    ON spot_observations (symbol, observed_at);
CREATE TABLE IF NOT EXISTS spot_source_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    observed_second TEXT NOT NULL,
    symbol TEXT NOT NULL,
    primary_source TEXT NOT NULL,
    primary_price REAL NOT NULL CHECK (primary_price > 0),
    primary_published_at TEXT,
    pyth_price REAL NOT NULL CHECK (pyth_price > 0),
    pyth_published_at TEXT,
    pyth_confidence REAL,
    pyth_feed_id TEXT,
    difference_bps REAL NOT NULL,
    UNIQUE (symbol, primary_source, observed_second)
);
CREATE INDEX IF NOT EXISTS idx_spot_source_comparisons_symbol_observed
    ON spot_source_comparisons (symbol, observed_at);
CREATE TABLE IF NOT EXISTS source_close_calibrations (
    market_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (market_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_source_close_calibrations_date
    ON source_close_calibrations (market_date, symbol);
CREATE TABLE IF NOT EXISTS pyth_daily_closes (
    market_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    close_price REAL NOT NULL CHECK (close_price > 0),
    candle_at TEXT NOT NULL,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (market_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_pyth_daily_closes_symbol_date
    ON pyth_daily_closes (symbol, market_date);
CREATE TABLE IF NOT EXISTS checkpoint_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    checkpoint_date TEXT NOT NULL,
    checkpoint_name TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    fair_up_probability REAL NOT NULL CHECK (fair_up_probability >= 0 AND fair_up_probability <= 1),
    up_ask REAL,
    down_ask REAL,
    model_version TEXT NOT NULL,
    option_iv REAL,
    payload_json TEXT NOT NULL,
    checkpoint_target_at TEXT,
    checkpoint_delay_seconds REAL,
    eligible_for_calibration INTEGER NOT NULL DEFAULT 1 CHECK (eligible_for_calibration IN (0, 1)),
    UNIQUE (market_id, checkpoint_date, checkpoint_name)
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_observations_market_checkpoint
    ON checkpoint_observations (market_id, checkpoint_date, checkpoint_name);
CREATE TABLE IF NOT EXISTS portfolio_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    risk_group TEXT NOT NULL,
    edge REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SELECTED', 'REJECTED')),
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_decisions_batch_created
    ON portfolio_decisions (batch_id, created_at);
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'SETTLED')),
    contracts REAL NOT NULL CHECK (contracts > 0),
    entry_ask REAL NOT NULL CHECK (entry_ask >= 0 AND entry_ask <= 1),
    entry_fee REAL NOT NULL CHECK (entry_fee >= 0),
    entry_slippage REAL NOT NULL CHECK (entry_slippage >= 0),
    fair_probability REAL NOT NULL CHECK (fair_probability >= 0 AND fair_probability <= 1),
    model_version TEXT NOT NULL,
    entry_payload_json TEXT NOT NULL,
    settled_at TEXT,
    settlement_outcome TEXT,
    payout REAL,
    realized_pnl REAL,
    settlement_payload_json TEXT,
    included_in_calibration INTEGER NOT NULL DEFAULT 1 CHECK (included_in_calibration IN (0, 1)),
    exclusion_reason TEXT,
    UNIQUE (market_id, outcome)
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_status_opened
    ON paper_positions (status, opened_at);
CREATE TABLE IF NOT EXISTS maker_shadow_quotes (
    quote_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CANCELLED')),
    limit_price REAL NOT NULL CHECK (limit_price > 0 AND limit_price < 1),
    fair_probability REAL NOT NULL CHECK (fair_probability >= 0 AND fair_probability <= 1),
    theoretical_edge REAL NOT NULL,
    best_bid REAL NOT NULL CHECK (best_bid >= 0 AND best_bid <= 1),
    best_ask REAL NOT NULL CHECK (best_ask >= 0 AND best_ask <= 1),
    touch_count INTEGER NOT NULL DEFAULT 0 CHECK (touch_count >= 0),
    last_touched_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_maker_quote
    ON maker_shadow_quotes (market_id, outcome) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_maker_shadow_quotes_status_created
    ON maker_shadow_quotes (status, created_at);
CREATE TABLE IF NOT EXISTS market_contract_reviews (
    market_id TEXT PRIMARY KEY,
    reviewed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
    reason TEXT NOT NULL,
    contract_json TEXT
);
CREATE TABLE IF NOT EXISTS market_settlements (
    market_id TEXT PRIMARY KEY,
    settled_at TEXT NOT NULL,
    winning_outcome TEXT NOT NULL CHECK (winning_outcome IN ('UP', 'DOWN')),
    payload_json TEXT NOT NULL
);
"""

