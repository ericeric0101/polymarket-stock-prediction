# Polymarket Stock

Shadow-only research foundation for Polymarket daily US equity-direction markets.
It has no wallet integration, private-key handling, or order-submission code.

## Setup

```zsh
cd /Users/cheng-kaihuang/Polymarket-stock
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
cp .env.example .env
polymarket-stock init-db
python -m unittest discover -s tests -v
```

`SHADOW_MODE=true` and `LIVE_TRADING_ENABLED=false` are enforced at process
startup. Phase 0 only prepares deterministic research records; it cannot trade.

If a network proxy or security tool re-signs HTTPS traffic, export its trusted root
CA as a PEM file and set `SSL_CERT_FILE` in `.env`. Do not disable TLS verification.

## Phase 1 public observation

```zsh
polymarket-stock scan-markets --symbols SPY,QQQ,AAPL,NVDA,TSLA
polymarket-stock scan-event --slug tsla-up-or-down-on-july-20-2026 --symbols TSLA
polymarket-stock scan-equity-events --tag-slugs stocks,equities --max-pages-per-tag 100
polymarket-stock snapshot-book --market-id <gamma-market-id> --token-id <clob-token-id>
polymarket-stock snapshot-alpaca-options --symbols SPY260718C00600000
```

Both commands call public read-only Polymarket endpoints and store raw snapshots
locally. Discovered markets are always `REVIEW_REQUIRED`; no discovery result is
treated as a validated trading contract.

Daily equity markets may resolve as `Up`/`Down` rather than `Yes`/`No`. The journal
preserves the published outcome labels and their corresponding CLOB token IDs.

`scan-equity-events` uses Gamma keyset pagination across the `stocks` and
`equities` tags, deduplicates markets, and persists only active, unclosed,
daily-direction binary candidates. It is intentionally not an all-Polymarket scan.

Alpaca option data is always requested using `feed=indicative` in this project.
It is saved for research only and labelled non-live-grade.
