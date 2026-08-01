# Polymarket Stock Daily Direction Bot Plan

## Purpose

Build a research-first bot for Polymarket daily US-stock direction markets. The
bot estimates the probability of the market's exact resolution condition, compares
that estimate with executable Polymarket prices, and initially records paper
decisions only. It must not submit live orders in the first phase.

## Current Decisions

| Topic | Decision |
| --- | --- |
| Repository | `/Users/cheng-kaihuang/Polymarket-stock` |
| Initial mode | Shadow only: collect signals and hypothetical trades, no wallet or orders |
| Initial symbols | SPY, QQQ, AAPL, NVDA, TSLA, subject to available Polymarket markets |
| Initial options source | Alpaca free Indicative feed, used for development and research only |
| Live-grade options source | Massive/Polygon adapter added with strict entitlement, recency, and free-tier rate gates; evaluate a real-time options plan or an eligible Tradier account before IV-valid paper entries |
| Polymarket execution | Reuse the proven patterns in `poly-maker-main` only after shadow validation |
| Capital | No live capital allocation in phase 1 |

## What the Bot Actually Decides

This is not a simple macro-news classifier. It combines several inputs to estimate
the probability that a specific market resolves Yes or No:

1. Parse the Polymarket market wording, reference price, deadline, timezone, and
   resolution source. The exact contract definition is the source of truth.
2. Collect underlying stock/ETF price and session state: previous close, premarket,
   regular session, and remaining time until the resolution observation.
3. Derive an option-implied distribution from the nearest liquid expiry and strikes.
   The key signal is implied volatility and skew, not Black-Scholes alone.
4. Add structured event and macro adjustments: earnings, FOMC/CPI/NFP, dividends,
   scheduled company events, halts, and broad-market regime. These are features and
   risk gates, not a free-form AI prediction.
5. Produce `fair_yes_probability` with a confidence interval and data-quality flags.
6. Read the Polymarket order book and calculate executable edge from the published
   best ask, the official per-token Polymarket taker fee, latency, and a model-error
   buffer. Do not invent a flat fee, stock-brokerage cost, or slippage assumption.
7. Record a paper decision only when all hard risk and data-quality gates pass.

The first live version may buy either Yes or No, but only where the market price is
meaningfully below the conservative fair probability for that outcome. It should
not trade merely because a classifier labels the stock as up or down.

## Data Strategy

### Phase 1: Free and delayed research data

- Alpaca free options feed is acceptable for API integration, schemas, historical
  research, delayed analysis, and shadow logging.
- Its Indicative option quotes/trades are delayed or modified, so it is forbidden
  as the sole live pricing input.
- Polymarket Gamma API discovers markets; CLOB public endpoints supply order-book
  prices and liquidity.

### Phase 2: Live data decision

Evaluate these paths before any live execution:

| Provider | Expected cost | Fit | Constraint |
| --- | --- | --- | --- |
| Tradier brokerage API | Potentially $0 account tier / $10 Pro, subject to eligibility | Strong low-cost candidate for real-time stock and option data | Requires an eligible brokerage account; verify agreement and entitlement |
| Alpaca Algo Trader Plus | $99/month | Clean OPRA feed and existing Python ecosystem | Higher fixed cost |
| Alpaca free | $0/month | Development and shadow research | Indicative/delayed option data; not live-grade |

We will validate actual API responses, timestamps, quote quality, and permissions
with the user's own account before marking any source as live-grade.

## Current Research State (2026-07-26)

### Strategy and evidence

- The supervisor remains shadow-only. It can record paper entries but has no
  wallet, private key, order-submission, or live-execution path.
- The active model-error buffer is fixed at **2%** for all real-time
  observations and IV-valid paper-entry evaluations. The former 5% fallback
  uplift is disabled; unavailable IV remains an observation-only entry gate.
- A 90-calendar-day discovery found 121 settled NVDA/TSLA markets across 61
  trading days. Local research data contains Pyth one-minute spot, CLOB Up/Down
  history, Pyth reference prices, and Gamma settlements for all 121 markets.
- The first non-leaking replay has 79 market-days with a complete 21-close
  volatility lookback. It compares 1% and 2% buffers at 12:00, 14:00, and
  15:30 New York checkpoints. The 2% variant had fewer trades and better
  all-sample pre-fee proxy PnL, while four walk-forward windows selected
  2%, 1%, 1%, and 2%. This is evidence to keep 2% for shadow collection, not
  evidence to enable live trading or to claim a stable optimum.
- Historical CLOB `prices-history` stores a single price, not historical
  executable ask/bid/depth. Any historical PnL based on it is explicitly an
  execution-price proxy and pre-fee unless a recorded fee assumption is stated.

### Live collection contract

- Every accepted daily equity contract is parsed as a Pyth close-versus-prior
  Pyth close contract. At runtime, `price_to_beat` is fetched once from Pyth
  Benchmarks for the prior NYSE close; Nasdaq data is retained only for
  realized-volatility history and is labelled non-settlement.
- Pyth Pro History was used only for the one-time 61-day one-minute backfill.
  The local key was removed after completion. Routine shadow collection does
  not require Pyth Pro History.
- At each accepted checkpoint, the journal now stores Pyth threshold, spot,
  fair probability, official fee rate, and reconstructed CLOB top-five bid/ask
  levels for both outcomes. A paper entry stores the same data and schedules
  1/5/15/30-minute markouts. These records are the required input for the next
  executable-ask, fee-aware replay.
- The CLOB collector reconstructs level-2 depth from the full `book` snapshot
  plus subsequent `price_change` events. Snapshot storage is deliberately
  limited to checkpoints, actual paper entries, and markouts rather than every
  WebSocket update.
- The supervisor now also subscribes read-only to Pyth Hermes using the same
  equity feed IDs used for `price_to_beat`. It stores no more than one Pyth or
  cross-check-source spot observation and one comparison per symbol per second.
  Pyth is the hard primary source because it is the resolution source;
  missing/stale Pyth data, or a fresh difference above 0.5%, blocks paper entry.
  A stale Finnhub/Alpaca cross-check remains a quality flag only. `PYTH_API_KEY`
  is optional before the 2026-08-18 authentication change and is used for both
  Hermes and Pyth Benchmarks afterwards.

### Next research gates

1. Collect at least 60 further trading days or 100 executable paper entries
   before changing the 2% buffer or considering a live pilot.
2. Run nested walk-forward selection across buffer, minimum edge, and entry
   checkpoint using recorded executable asks, fees, depth, and markouts.
3. Research an exit only when the current executable bid, fees, and observed
   post-entry markouts support it. The default remains buy-and-hold to official
   settlement; a near-threshold late-day filter blocks new entries, not exits
   for existing positions.
4. Keep up to 18 active markets for shadow collection. Storage is not expected
   to be a constraint because depth is stored only at bounded research events;
   portfolio selection remains capped separately.
5. Collect volatility-model comparison data using one supervisor process. Keep
   `CLOSE_TO_CLOSE` as the primary paper-decision model and record `EWMA`
   (`decay=0.94`) under each realtime evaluation's `comparison_models` payload.
   Do not run two independent supervisors for this comparison because that
   duplicates streams and paper positions.

## Repository and CWD SOP

The canonical and only repository root is:

```text
/Users/cheng-kaihuang/Polymarket-stock
```

Before any edit, test, migration, data collection, staging, or push, verify:

```zsh
ROOT=/Users/cheng-kaihuang/Polymarket-stock
test "$(git -C "$ROOT" rev-parse --show-toplevel)" = "$ROOT"
```

Use absolute paths or `git -C "$ROOT"`; do not rely on a tool or editor's
implicit current directory. A Codex session may retain obsolete workspace
metadata. In particular, never create or write
`/Users/cheng-kaihuang/Documents/Polymarket-stock`: that path was an accidental
non-Git directory created by a misdirected patch tool on 2026-07-26 and was
removed. If a tool reports that path as its CWD, stop using implicit file-edit
operations and reopen the task against the canonical root.

## Architecture

```text
Gamma market discovery -> contract parser -> market metadata store
                                           |
Underlying + options + events ------------> fair-probability model
                                           |
Polymarket CLOB order book ----------------> edge and risk gate -> shadow journal
                                                                  |
                                                later: reviewed execution adapter
```

Planned modules:

- `market_discovery`: finds candidate daily stock markets and validates their terms.
- `market_contract`: normalizes the resolution rule and observation timestamps.
- `market_data`: adapters for Alpaca, then an optional Tradier provider.
- `pricing`: option-implied probability, volatility regime adjustments, and model
  confidence. It will explicitly separate overnight and regular-session risk.
- `events`: structured calendars and risk flags; no unbounded LLM trade decisions.
- `polymarket_data`: public CLOB order book, midpoint, spread, and liquidity.
- `edge_engine`: conservative expected-value and minimum-edge calculation.
- `risk`: stale-data, event, liquidity, concentration, and loss limits.
- `journal`: SQLite logs of inputs, fair values, decisions, fills, and final outcomes.
- `execution`: disabled initially; later a narrow adapter based on the existing
  Polymarket client patterns in `poly-maker-main`.

## Phased Delivery

### Phase 0: Foundation

- [x] Initialize the Git repository. Python project scaffolding remains pending.
- [x] Add configuration with `SHADOW_MODE=true` as a non-optional default.
- [x] Add SQLite journal and deterministic structured logging.
- [x] Add unit tests for contract parsing, probability math, and edge calculation.

### Phase 1: Market observation

- [x] Cursor-scan active tagged equity events and discover relevant Polymarket markets.
- [x] Persist raw resolution terms and reject unvalidated candidates from trading.
- [x] Read public CLOB book snapshots and identify executable Yes/No prices.
- [x] Integrate Alpaca free data and label all data freshness/quality limitations.

### Phase 2: Fair-probability research

- [x] Implement baseline binary option probability model.
- [x] Estimate separate overnight and regular-session volatility regimes.
- [x] Select near-the-money options and filter illiquid/stale contracts.
- [x] Add event risk gates: earnings, major economic releases, dividends, and halts.
- [x] Add calibration metrics: Brier score, log loss, and conservative paper PnL.

### Phase 3: Shadow validation

- [ ] Run for at least 20 trading days and collect a sufficient sample across
  symbols and market conditions.
- [ ] Compare fair probability, executable market price, actual settlement, and
  hypothetical net outcome.
- [ ] Require positive out-of-sample edge after costs and conservative error buffer.
- [ ] Review failures manually before enabling any execution code.

### Next Code Update: Multi-Market Shadow Lifecycle

**Target operating mode**

```text
scheduled market discovery
-> reviewed active daily-equity universe
-> dynamic multi-market WebSocket subscriptions
-> freshness-gated evaluation per market
-> idempotent paper entry at executable ask
-> hold to official settlement
-> realized paper PnL and calibration dataset
```

This is the next implementation block inside Phase 3. It automates observation
and paper lifecycle management only. It does not add a wallet, private key,
order submission, or an intraday exit strategy.

| Step | Code change | Guardrails and acceptance criteria |
| --- | --- | --- |
| 3.1 Market scheduler | Complete: configurable discovery loop (default every 15 minutes) refreshes active `stocks,equities` candidates. | Deduplicates by market ID, retains active daily Up/Down markets, records refresh results, and never deletes open paper positions. |
| 3.2 Universe manager | Complete: in-memory active-universe manager maps market ID, symbol, resolution time, and CLOB token IDs. | Enforces configurable market cap and minimum time to resolution; rejects candidates without a ticker template. |
| 3.3 Multi-market streams | Complete: supervisor uses one shared Polymarket stream and one shared quote stream, rebuilding subscriptions when the universe changes. | Reuses reconnect/backoff and freshness gates; prevents multiple Finnhub connections. |
| 3.4 Per-market evaluator | Complete: real-time baseline evaluator runs independently per market. | Persists source timestamps, asks, fair probability, model version, and skip reasons; delayed/missing spot is not a signal. |
| 3.5 Paper position ledger | Complete: `paper_positions` captures immutable entry inputs and costs. | Entry is idempotent per market and only one open outcome is allowed; no averaging, pyramiding, or automatic exit. |
| 3.6 Settlement tracker | Complete: each refresh and the one-shot command reconcile open positions from Gamma market status. | State machine is `OPEN -> SETTLED`; settlement uses published outcome, not stock-price inference. |
| 3.7 Realized PnL and calibration | Complete: settlement stores payout/PnL; `paper-performance` reports PnL, hit rate, Brier, and log loss. | Open positions are excluded from calibration. |
| 3.8 Operator controls | Complete: supervisor, position status, performance, and one-shot settlement commands are available. | Graceful shutdown preserves state; no command enables live trading or changes capital limits. |
| 3.9 Contract integrity | Complete: a strict parser accepts only the observed Pyth daily-close contract template and journals each acceptance or rejection. | Validates ticker, outcome order, Pyth feed, prior-trading-day clause, exact 50-50 tie rule, and unrounded-price rule before observation. |
| 3.10 Data quality and session gates | Complete: evaluation records session, feed freshness, order-book integrity, and cross-source spot comparison. | Blocks non-regular sessions, missing/crossed books, stale/incomplete streams, and fresh-reference spot divergence over 0.5%. |
| 3.11 Official Polymarket fee model | Complete: fetches each outcome token's CLOB `base_fee`, caches it, and applies `shares * feeRate * price * (1 - price)` rounded to 5 decimals. | No stock-trading fee, flat 1% fee, or fixed slippage is used. An unavailable official fee rate is explicit rather than substituted. |
| 3.12 Quote-stream liveness | Complete: Finnhub WebSocket reconnects when it remains open but sends no messages for 60 seconds. | Prevents a silent stale spot stream from appearing healthy; normal freshness gates remain active until the next trade arrives. |
| 3.13 Maker shadow quotes | Complete: records passive `1c`-tick quote proposals below unbuffered fair value, requires a 2c limit move plus a 30-second minimum quote lifetime before repricing, and journals quote touches without assuming fills. | Default theoretical maker edge is `0.5c`; no order, fill, queue position, rebate, or PnL is fabricated. |
| 3.15 Contract trading-date gate | Complete: only evaluates a daily equity contract on the New York date of its published close. | Existing entries opened before that date are preserved with `PRECONTRACT_TRADE_DATE` and excluded from calibration/reporting. |
| 3.16 IV-valid research gate | Complete: realized-vol fallback remains observation-only; paper entries require a fresh, complete near-ATM option-IV surface. | Immutable 10:00/12:00/14:00/15:30 New York checkpoints support walk-forward calibration. |
| 3.17 Portfolio-aware paper batches | Complete: IV-backed and explicitly labelled realized-volatility-fallback paper candidates are selected in 30-second batches with daily, risk-group, and direction limits. | Every selection/rejection is recorded in `portfolio_decisions`; no live execution is introduced. |
| 3.18 Independent option-pricing validation | Complete: local Black-Scholes-Merton and CRR binomial calculations cross-check quote inputs and recover midpoint IV. | No provider, scrape, journal write, supervisor dependency, or entry path exists; every result is explicitly research-only. |
| 3.19 Remote calendar resilience | Complete: Finnhub calendar timeouts are converted to an explicit unavailable data gate, with a 60-second per-process cooldown. | A failed remote calendar never terminates the supervisor or silently permits a paper entry. |
| 3.20 Checkpoint buffer calibration research | Complete: only checkpoints captured within five minutes of their scheduled New York time are calibration-eligible; `buffer-sweep` replays one hold-to-settlement entry per market-day and `walk-forward-buffer-sweep` selects only on earlier dates. The supervisor uses a fixed 2% buffer for IV-backed and explicitly labelled realized-volatility-fallback paper entries. | Research reports never alter supervisor thresholds automatically. Buffer selection requires independent later-day validation, not same-day tick count or historical win-rate maximization. |
| 3.21 Top-5 walk-forward policy | Complete: training-only binned probability shrinkage selects checkpoint/buffer/edge parameters before frozen later-date validation; each day has at most five entries. | Sparse bands remain raw; no validation outcome can fit the calibrator or policy. |
| 3.22 Incremental benchmark diagnostics | Complete: model direction is compared with market favorite, spot-versus-Pyth threshold, and market-majority baselines using fee-aware one-share PnL. | High directional accuracy is never treated as positive executable edge. |
| 3.23 Execution and exit diagnostics | Complete: top-five depth VWAP, delayed-entry slippage, and executable-bid 1/5/15/30-minute exits are compared with hold-to-settlement. | Missing depth is unfillable, never substituted with best ask/bid. |
| 3.24 Pyth source-error controls | Complete: fresh sub-0.5% divergence plus Pyth confidence adds bounded model buffer; diagnostics exclude stale timestamp pairs. | The existing 0.5% fresh divergence remains a hard gate. |
| 3.25 Volatility-regime diagnostics and disagreement gate | Complete: primary versus comparison fair probabilities are stored and summarized; fallback disagreement preserves the signal but blocks paper entry. Per-minute Pyth paths also report partial-session realized volatility against earlier same-symbol/checkpoint history. | Intraday regime anomalies remain research-only until independent dates are sufficient; IV-valid behavior remains separate and no option IV is fabricated. |
| 3.14 Test and rollout | In progress: deterministic unit coverage is added for core lifecycle behavior, contract/data-quality gates, and dual volatility-model comparison. | Run the supervisor through multiple market sessions before relying on paper-entry results; compare settled primary versus EWMA outcomes out of sample. |

### Phase 3: Verifiable Research

| Step | Code change | Guardrails and acceptance criteria |
| --- | --- | --- |
| 3.12 Validated option IV and skew | Complete: read-only Tradier and Massive/Polygon chain adapters select liquid near-ATM put/call IV and record skew. | Requires a current, entitled data source for the IV blend; unavailable, delayed, unconfigured, or unsuitable data uses a clearly labelled realized-volatility-fallback paper entry. |
| 3.13 Settlement replay | Complete: `replay-settled` evaluates immutable paper entries; `replay-observations` evaluates one valid observation per officially settled market. | Never reconstructs a price after the fact; paper and all-observation results remain separate to expose selection bias. |
| 3.14 Conservative calibration | Complete: paper and all-observation calibration derive MAE/p90 error only after 30 settled samples; a saved recommendation can only tighten thresholds. | No small-sample fitting and no automatic lowering of buffer or edge floor. |
| 3.15 Calendar and event gates | Complete: NYSE core holidays are hard gates; Finnhub earnings calendar is combined with versioned local JSON for FOMC, CPI, and symbol-specific events. | Invalid local calendar blocks entries; special closures/early closes remain an explicit operator responsibility. |
| 3.17 Operator visibility | Complete: Rich live terminal dashboard, human-readable supervisor summaries, JSON mode, and SQLite-backed state reduce raw-event inspection. The dashboard separately shows each New York day's capped three-entry paper portfolio and all-settled-market first-signal results. | Dashboard refreshes without controlling the bot; JSONL remains the canonical audit log. |
| 3.18 Probability calibration and sizing guardrails | Complete: immutable first-signal reports stratify direction, IV regime, time, probability, source, model version, and Pyth-threshold distance; a date-separated walk-forward shrinkage report prevents in-sample fitting. | Reports never change entries. Kelly remains disabled; IV-valid and fallback cohorts each require 100 settled signals before operator review. |
| 3.16 Observation rollout | In progress: supervisor emits IV/fallback, session, contract, risk, and settlement metadata for 20+ trading days. | Review replay and calibration outputs before discussing an execution adapter. |

**Explicit strategy decision for this block**

- Paper entries represent buying the approved Up or Down outcome at its executable
  ask and holding it through the market's official resolution.
- There is no take-profit, stop-loss, averaging, early-sale, or real market-making
  logic in this block. Maker shadow quotes are observations only; they never assume
  queue position, fill, fee rebate, inventory, or PnL.
- A paper position is evidence for calibration, not proof that a future live trade
  should be placed.

### Phase 4: Controlled live pilot (requires explicit approval)

- [ ] Add an execution adapter with a separate `LIVE_TRADING_ENABLED=false` gate.
- [ ] Reuse Polymarket collateral, signing, neg-risk, and order-lifecycle safeguards.
- [ ] Set hard per-market, per-symbol, daily-loss, stale-data, and near-close limits.
- [ ] Begin with a maximum of 5 to 10 USDC per approved trade.
- [ ] Keep automated shutdown available and reconcile every order/fill.

## Non-Negotiable Safety Rules

- No live order submission, private key loading, or wallet interaction in Phases 0-3.
- Never trade a contract whose resolution wording or reference price is not parsed.
- Do not use a delayed/indicative option feed as a live fair-value source.
- Reject stale, crossed, too-wide, or insufficiently liquid option and Polymarket data.
- Do not trade around earnings or major scheduled releases until event handling has
  been validated separately.
- Model uncertainty increases the required edge; it never increases position size.
- The bot cannot change its own capital limits, disable risk gates, or enable live
  trading.

## Open Questions

- [ ] Which Polymarket daily equity market templates are reliably available and
  how exactly do they define the reference price and close?
- [ ] Is Tradier brokerage account opening and real-time market-data entitlement
  available for this user and suitable for automated research use?
- [ ] Do the observed Polymarket markets have enough depth after fees to support
  the desired small-size strategy?
- [ ] Which scheduled-event data source should become authoritative?
- [ ] What performance threshold and sample size are required before a live pilot?

## Change Log

| Date | Decision or change | Status |
| --- | --- | --- |
| 2026-07-18 | Created initial research-first plan; chose Alpaca free for shadow development and deferred live data vendor choice. | Active |
| 2026-07-18 | Initialized the Git repository at `/Users/cheng-kaihuang/Polymarket-stock`. | Complete |
| 2026-07-18 | Completed Phase 0 Python scaffolding: shadow-only configuration, SQLite journal, JSONL logs, baseline math, and unit tests. | Complete |
| 2026-07-18 | Added Phase 1 public-only Gamma discovery, CLOB order-book snapshots, and mandatory review status for all discovered markets. | Complete |
| 2026-07-18 | Added Alpaca free Indicative option-quote adapter. Local live Gamma validation is blocked by a Python TLS certificate-chain error; SSL verification remains enabled. | Active environment issue |
| 2026-07-18 | Corrected discovery for Polymarket daily equity events: preserve `Up`/`Down` outcome labels and support exact event-slug scans. TSLA daily markets resolve against Pyth regular-session close and include 50-50 tie/no-trade rules. | Complete |
| 2026-07-18 | Replaced capped market-list discovery with Gamma event keyset pagination across `stocks` and `equities` tags for broad daily-equity coverage. | Complete |
| 2026-07-18 | Reduced Phase 1 operator steps: snapshot both CLOB outcome books by market ID, or automatically during broad equity scans. | Complete |
| 2026-07-18 | Added Phase 2 research core: option-implied IV, two-session volatility blending, event gates, conservative edge evaluation, and calibration metrics. | Complete |
| 2026-07-18 | Added a provider-independent realized-volatility fallback using verified daily closes; stale fallback data blocks paper recommendations and increases the error buffer. | Complete |
| 2026-07-18 | Added Nasdaq public baseline provider with local cache failover; cached or stale data cannot bypass the conservative recommendation gate. | Complete |
| 2026-07-19 | Added read-only Polymarket Market and Alpaca IEX WebSocket observation streams, with 500 ms debounce and freshness tracking. Streams only record shadow re-evaluation requests. | Complete |
| 2026-07-19 | Added Finnhub as the default read-only equity quote provider while retaining the Alpaca IEX adapter for side-by-side validation. | Complete |
| 2026-07-19 | Added local candidate listing with market IDs and fixed Polymarket text-heartbeat handling in the shadow WebSocket stream. | Complete |
| 2026-07-19 | Added freshness-gated real-time realized-volatility evaluations from Finnhub/Alpaca spot and Polymarket WebSocket books. Every evaluation or skip is persisted for later calibration; no execution path was added. | Complete |
| 2026-07-19 | Added automatic reconnect with capped exponential backoff for public WebSocket closures and throttled repeated stale-state records to one per minute. | Complete |
| 2026-07-19 | Defined the next Phase 3 implementation as a multi-market shadow lifecycle: scheduled discovery, dynamic stream supervision, idempotent hold-to-settlement paper positions, and official settlement reconciliation. | Planned |
| 2026-07-19 | Implemented the initial multi-market shadow lifecycle: scheduled universe refresh, shared streams, per-market evaluation, paper ledger, Gamma settlement reconciliation, and paper performance reporting. | Complete |
| 2026-07-19 | Added strict Pyth daily-equity contract parsing, a persisted acceptance/rejection journal, and data-quality gates for US session, empty/crossed books, stale feeds, and quote-source disagreement. | Complete |
| 2026-07-19 | Added validated Tradier option-IV/skew integration with realized-vol fallback, immutable paper replay, conservative calibration, NYSE core-holiday gates, and structured local event-risk calendar. | Complete |
| 2026-07-19 | Replaced the flat 1% fee plus fixed slippage approximation with the official per-token Polymarket CLOB fee rate and published taker-fee formula. The paper ledger stores only this fee; no stock-brokerage cost is modeled. | Complete |
| 2026-07-20 | Diagnosed a silent Finnhub WebSocket stall during the US regular session. Added a 60-second no-message watchdog so the existing reconnect loop recreates the subscription instead of retaining stale stock prices. | Complete |
| 2026-07-20 | Added maker shadow quotes: passive quote proposals below fair value, tick-level cancel/reprice history, and ask-touch observations without assumed fills or maker rebate. | Complete |
| 2026-07-20 | Added maker quote reprice throttling after observing approximately 14-second quote lifetimes: hold an active quote unless the proposed limit moves at least 2c and the quote has been live for at least 30 seconds. | Complete |
| 2026-07-21 | Blocked next-day daily contracts from entering the preceding New York trading day, and added a journal migration that excludes earlier pre-contract paper entries from calibration without deleting them. | Complete |
| 2026-07-22 | Initially made realized-vol fallback observation-only, added immutable New York checkpoint observations, gated paper entries on fresh option IV, and added diversified batch selection with decision journaling. Superseded by the 2026-07-28 labelled fallback-entry policy. | Complete |
| 2026-07-22 | Added a Massive (formerly Polygon) option-IV adapter. Verified the configured Currencies Basic key returns `403 NOT_AUTHORIZED` for U.S. option snapshots; the adapter now makes at most five requests per minute, stops after an entitlement failure, and rejects delayed quotes from IV-valid paper entry. | Complete |
| 2026-07-22 | Added independent BSM/CRR binomial option-pricing validation after reviewing Norn-Finance-API-Server. It validates trusted quote inputs locally, but deliberately does not use its Yahoo Finance/MarketWatch data path or public deployment. | Complete |
| 2026-07-24 | Fixed Nasdaq quote parsing for its current `Jul 24, 2026 9:39 AM ET` timestamp format and normalize it to UTC. This prevents valid real-time spot quotes from being rejected as missing and restores active-market construction. | Complete |
| 2026-07-25 | Added non-leaking checkpoint buffer research. Late checkpoint captures are preserved but excluded; buffer sweeps use original asks and frozen official fees, select at most one first eligible entry per market-day, and walk-forward reports require distinct earlier and later trading dates. | Complete |
| 2026-07-26 | Backfilled 121 settled NVDA/TSLA markets across 61 trading days with Pyth one-minute spot, Pyth references, CLOB history, and Gamma settlement. Added local Pyth/CLOB batch replay and 1%/2% walk-forward comparison; retain 2% for shadow collection only. | Complete |
| 2026-07-28 | Enabled explicitly labelled realized-volatility-fallback paper entries when option IV is unavailable. Recorded official settlements for the 14 July 27 signal markets; first-entry hold-to-settlement was 10/14, while the 347 tick-level signals are retained only as correlated observations. | Complete |
| 2026-07-28 | Made Pyth Hermes the hard primary live-spot source because Polymarket daily-equity contracts resolve from Pyth. Finnhub/Alpaca remains a fresh-only divergence cross-check, so an infrequently traded stock cannot be rejected solely for an external trade-feed gap. | Complete |
| 2026-07-26 | Aligned daily contract `price_to_beat` to Pyth Benchmarks prior-close reference. Added bounded checkpoint/paper-entry execution observations, top-five reconstructed CLOB depth, fee capture, and 1/5/15/30-minute paper-entry markouts. | Complete |
| 2026-07-26 | Identified and removed an accidental non-Git directory at `/Users/cheng-kaihuang/Documents/Polymarket-stock` caused by stale Codex CWD metadata. Added canonical-root SOP to this plan and `AGENTS.md`. | Complete |
| 2026-07-27 | Added Pyth Hermes dual-source spot collection, bounded SQLite observations/comparisons, source freshness/divergence paper gate, and optional API-key authentication for Hermes and Benchmarks. | Complete |
| 2026-07-29 | Added selected-side first-signal calibration cohorts with Wilson intervals, no-lookahead probability-calibration walk-forward research, and explicit fixed-small-position-only/Kelly-disabled readiness in the dashboard and CLI. | Complete |
| 2026-08-01 | Added training-calibrated capped Top-5 walk-forward, incremental direction benchmarks, depth/latency execution diagnostics, fresh Pyth divergence analysis and uncertainty buffer, volatility-model disagreement gate, and executable-bid exit replay. | Complete |
