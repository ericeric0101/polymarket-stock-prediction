# Engineering Architecture

This project remains a research-only Polymarket equity observer. It does not sign, submit, or manage live orders.

## Runtime boundaries

- `cli.py` is a backward-compatible facade; all command parsing and dispatch live in `commands/entrypoint.py`.
- `commands/catalog.py` is the stable command-to-domain map used to keep market, data, research, calibration, Above-X, and operations commands discoverable.
- `cli_runtime.py` owns long-running operator commands: the multi-market supervisor, terminal dashboard, localhost research dashboard, price-ladder collector, and settlement reconciliation.
- `supervisor.py` owns active-market lifecycle, model evaluation, and paper-entry selection.
- `supervisor_settlement.py` owns official paper/model outcome reconciliation.
- `stream_routing.py` owns fan-out from each shared public stream to its active market coordinators.
- `streaming.py` owns WebSocket protocol handling, freshness tracking, and debouncing only. It has no SQLite-specific retry behavior.

## Shared types

- `domain.py` holds dependency-free runtime types shared across orchestration modules.

## Storage boundaries

- `journal.py` is the compatibility facade and domain journal for core Up/Down research and paper positions.
- `price_ladder_journal.py` owns only Above-X / price-ladder research tables.
- `storage/sqlite.py` owns the shared SQLite connection policy: busy timeout, bounded commit retries, commit, rollback, and close.
- `storage/writer.py` serializes non-critical stream observations and evaluations through one async queue, executing each synchronous journal write in `asyncio.to_thread`.

Operations that require an immediate result, such as checkpoint de-duplication, maker-shadow quote reconciliation, paper-position creation, and settlement writes, run in `asyncio.to_thread` and retain their journal transaction boundary.

## Data scope

Spot and evaluation stream writes are asynchronous because they are observational. Checkpoint, paper-entry, maker-quote, and settlement operations are synchronous from the caller's perspective because their return values influence subsequent control flow. SQL date filters must be used for daily research queries rather than loading the full observation history and filtering in Python.

## Verification

Before merging changes, run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check --select F,E9 src tests
.venv/bin/python -m mypy src/polymarket_stock/evaluation_payload.py
git diff --check
```

The GitHub Actions workflow runs the same baseline checks.
