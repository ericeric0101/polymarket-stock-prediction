# Repository SOP

## Canonical root

All work for this repository uses exactly one root:

```text
/Users/cheng-kaihuang/Polymarket-stock
```

Before modifying files, running tests, staging, or pushing, verify:

```zsh
ROOT=/Users/cheng-kaihuang/Polymarket-stock
test "$(git -C "$ROOT" rev-parse --show-toplevel)" = "$ROOT"
```

Use absolute paths or explicit `git -C "$ROOT"` commands. Do not rely on an
editor, agent, or patch tool's implicit CWD. If the environment reports
`/Users/cheng-kaihuang/Documents/Polymarket-stock`, treat it as stale session
metadata: do not write there and reopen the task against the canonical root.

## Data and safety

- `data/` and `logs/` are local runtime and research artifacts. They are ignored
  by Git and must not be staged unless the operator explicitly requests it.
- Preserve shadow-only operation: never add wallet, private-key, order-submission,
  or live-trading code without explicit approval.
- Daily equity contracts resolve against Pyth. Use Pyth prior-close reference as
  `price_to_beat`; Nasdaq is non-settlement data used only for volatility history.
- Pyth Pro History is one-time/offline research infrastructure, not a required
  day-to-day supervisor dependency.
- Historical CLOB price history is not an executable ask/bid archive. Treat
  backtests from it as price proxies; use live execution observations for
  fee/depth/markout research.
- The regular shadow supervisor uses one shared data stream for dual-model
  comparison: `CLOSE_TO_CLOSE` is the primary paper-decision model and `EWMA`
  is the default comparison model. Comparison results are stored in each
  realtime evaluation payload under `comparison_models`; comparison models
  must never create a second paper position.
