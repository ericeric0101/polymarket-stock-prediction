# Engineering Specification

This file is the repository engineering authority. Read it before every change.

## Safety
- This is a shadow-only research tool: no wallet, signing, order submission, live execution, or dynamic/Kelly sizing.
- Missing, stale, or conflicting data must fail closed and emit a quality flag.

## Architecture
- Dependencies flow downward: core/model -> adapters -> storage -> runtime -> analytics/presentation -> commands.
- `cli.py`, `ShadowJournal`, and `MultiMarketShadowSupervisor` are compatibility facades.
- No cross-module private imports, wildcard imports, duplicate helpers, or new root-level modules.
- General modules stay under 600 lines; CLI handlers under 400; 1200 is a hard limit.

## Types, time, storage, async
- All public APIs are typed. Never use `object` plus `getattr`; define a `Protocol` or explicit union.
- All timestamps are timezone-aware UTC ISO-8601. New York conversion uses the shared time utility only.
- Only `storage/` imports SQLite. Queries need a LIMIT or time window; SQL performs filtering.
- Atomic multi-write operations use one transaction.
- Synchronous I/O in async paths uses `asyncio.to_thread`; fire-and-forget writes use `JournalWriter`. Background error handlers may not throw and drains require a timeout.

## Payload contract
- `evaluation_payload.py` is the only payload schema/accessor authority.
- New writes call `validate_for_write`; old rows use `validate_for_read`.
- Readers use `read_*` accessors, never direct payload lookups for versioned fields.
- Payload field changes require a version bump, accessor migration, and golden-field test update.

## CI and verification
- Never narrow CI commands to make checks pass. Use explicit per-file technical-debt ignores and remove them over time.
- Before commit: `ruff check src tests`, `ruff format --check src tests`, `mypy`, `pytest -q`, and `git diff --check`.
- Do not add naive datetimes, unbounded list queries, bare exception swallowing, personal absolute paths, or direct live-trading paths.
