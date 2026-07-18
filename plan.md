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
| Live-grade options source | Deferred. Evaluate Tradier brokerage data first; Alpaca OPRA remains a fallback |
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
6. Read the Polymarket order book and calculate executable edge after fees, spread,
   slippage, latency, and a model-error buffer.
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
