"""Stable command-to-domain catalog; command spellings are public compatibility surface."""

COMMAND_GROUPS = {
    "market": ("init-db", "list-markets", "scan-markets", "scan-event", "scan-equity-events", "snapshot-book", "snapshot-market"),
    "data": ("download-yahoo-closes", "batch-backfill-settled-markets", "backfill-settled-market-data", "backfill-pyth-intraday-spots", "backtest-pyth-clob"),
    "research": ("evaluate-baseline", "evaluate-nasdaq-baseline", "historical-backtest", "replay-settled", "replay-observations", "strategy-diagnostics", "close-source-calibration"),
    "calibration": ("calibrate-paper", "calibrate-observations", "calibrate-first-signals", "walk-forward-probability-calibration", "calibrate-checkpoints", "buffer-sweep", "walk-forward-buffer-sweep", "walk-forward-top-five"),
    "above_x": ("discover-above-x-history", "backfill-above-x-history", "above-x-coverage", "backtest-above-x", "walk-forward-above-x-veto", "sync-above-x-veto-shadow", "discover-price-ladders", "collect-price-ladders", "settle-price-ladders", "price-ladder-report"),
    "operations": ("stream-shadow", "supervise-shadow", "paper-positions", "maker-shadow-quotes", "portfolio-decisions", "paper-performance", "dashboard", "research-dashboard", "settle-paper-positions", "snapshot-alpaca-options", "validate-option-pricing"),
}


def command_group(command: str) -> str:
    for group, commands in COMMAND_GROUPS.items():
        if command in commands:
            return group
    raise KeyError(f"unknown command: {command}")
