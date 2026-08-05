# Polymarket Stock / Polymarket 美股預測市場研究工具

## English

### Purpose

Shadow-only research tooling for Polymarket daily US equity-direction markets.
It observes markets, estimates a conservative baseline probability, and records
shadow re-evaluation events. It contains no wallet integration, private-key
handling, order-submission code, or live-trading path.

### Setup

```zsh
cd $REPO_ROOT
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
cp .env.example .env
polymarket-stock init-db
python -m unittest discover -s tests -v
```

`SHADOW_MODE=true` and `LIVE_TRADING_ENABLED=false` are enforced at process
startup. Do not change these safeguards for research runs.

If a VPN, proxy, or security tool re-signs HTTPS traffic, export its trusted
root CA as a PEM file and set `SSL_CERT_FILE` in `.env`. TLS verification must
remain enabled.

### Market Observation

```zsh
# Broad daily-equity discovery and optional order-book capture
polymarket-stock scan-equity-events --tag-slugs stocks,equities --max-pages-per-tag 100
polymarket-stock scan-equity-events --tag-slugs stocks,equities --snapshot-books
polymarket-stock list-markets --symbol TSLA

# Inspect a known event or capture both Up/Down books from a market ID
polymarket-stock scan-event --slug tsla-up-or-down-on-july-20-2026 --symbols TSLA
polymarket-stock snapshot-market --market-id 2958682
```

Discovery uses public, read-only Polymarket Gamma endpoints. It paginates the
`stocks` and `equities` tags, deduplicates candidates, and keeps only active,
unclosed daily-direction binary markets. This is broad coverage, not a promise
to find every Polymarket market. Every discovery result is `REVIEW_REQUIRED`;
verify the published resolution wording and settlement source manually.

Daily markets can use `Up`/`Down` rather than `Yes`/`No`. The journal preserves
the exact outcome label and its CLOB token ID. `snapshot-market` captures both
outcome books without requiring token IDs to be pasted into the shell.

### Baseline Fair Probability

The baseline estimates a fair Up probability from spot, prior close, and
realized volatility. It adds a conservative model-error buffer, so a quoted
price difference is not automatically a paper-trade conclusion.

```zsh
# Verified local daily CSV: requires Date,Close columns and at least 21 rows
polymarket-stock evaluate-baseline \
  --market-id 2958682 \
  --history-csv /path/to/TSLA_daily.csv \
  --spot 380.84 \
  --resolves-at 2026-07-20T20:00:00Z

# No-key fallback: Nasdaq public daily closes plus last reported quote
polymarket-stock evaluate-nasdaq-baseline \
  --market-id 2958682 --symbol TSLA --resolves-at 2026-07-20T20:00:00Z
```

Run `snapshot-market` first so the evaluator has current Up/Down asks. The
Nasdaq provider is explicitly `NON_SETTLEMENT`: it is not Polymarket's Pyth
settlement price and its quote may not be real time. Successful public results
are cached in `data/baseline_cache/`; an outage can use only a still-fresh cache
and never bypasses the conservative recommendation gate.

Option snapshots are research-only and always use Alpaca's `indicative` feed:

```zsh
polymarket-stock snapshot-alpaca-options --symbols SPY260718C00600000
```

Indicative option data is not a live-grade pricing source.

### Phase 1–3 Volatility Research

The realized-volatility research path now supports explicit, comparable
estimators without changing the default shadow behavior:

```zsh
# Phase 1: responsive close-to-close EWMA
polymarket-stock evaluate-baseline \
  --market-id 2958682 --history-csv /path/to/TSLA_daily.csv \
  --spot 380.84 --resolves-at 2026-07-20T20:00:00Z \
  --volatility-estimator EWMA --volatility-decay 0.94

# Phase 3: OHLC estimator from a Date,Open,High,Low,Close CSV
polymarket-stock evaluate-baseline \
  --market-id 2958682 --history-csv /path/to/TSLA_daily.csv \
  --ohlc-history-csv /path/to/TSLA_ohlc.csv \
  --spot 380.84 --resolves-at 2026-07-20T20:00:00Z \
  --volatility-estimator YANG_ZHANG
```

The default remains `CLOSE_TO_CLOSE`. `GARMAN_KLASS` and `YANG_ZHANG` require
validated OHLC bars and are research comparison modes only. Yahoo's existing
non-settlement chart adapter can export OHLC bars through its Python API as
`YahooDailyBarSeries`. Event-conditioned volatility is also report-only and
requires a minimum historical event cohort; it does not bypass event gates or
enable any trading path. Compare estimators out of sample with the existing
walk-forward calibration commands before changing a model default.

The real-time supervisor computes a dual-mode comparison in one process by
default: `CLOSE_TO_CLOSE` remains the primary paper-decision model while
`EWMA` is stored inside each evaluation's `comparison_models` payload. The
comparison model never creates a second paper position. To change the primary
or comparison modes explicitly:

```zsh
polymarket-stock supervise-shadow \
  --volatility-estimator CLOSE_TO_CLOSE \
  --comparison-estimators EWMA \
  --volatility-decay 0.94
```

### Real-Time Shadow Streams

Finnhub is the default stock-quote provider and does not require a brokerage
account. Add its API key to `.env`; do not commit it:

```dotenv
FINNHUB_API_KEY=...
```

```zsh
polymarket-stock stream-shadow --market-id 2958682 --symbol TSLA --duration-seconds 0

# Keep the existing Alpaca IEX adapter available when you want to compare it.
polymarket-stock stream-shadow --market-id 2958682 --symbol TSLA --spot-provider alpaca
```

This consumes Polymarket's public Market WebSocket and the selected stock-quote
WebSocket, coalesces incoming book/spot updates with a 500 ms debounce, and
prints and logs `REALTIME_BASELINE_EVALUATED` records. Each record includes the
freshness-gated spot, executable Up/Down asks, fair probability, raw net edge,
and any skip reason. It also persists every result in the local SQLite journal.
Repeated identical stale-data states are recorded at most once per minute, and
the public streams reconnect automatically after a transient network closure.
`--duration-seconds 0` runs until interrupted. Alpaca's free IEX feed is not a
consolidated SIP feed; Finnhub coverage and latency should likewise be measured
against the market before relying on it. This stream does not yet obtain live
option IV, so it is an observation and timing layer only, not a trading signal.

### Isolated Price-Ladder Research

Polymarket `closes above K` markets are collected as an independent research
sidecar. Only strict binary Yes/No contracts whose rules name the matching Pyth
`Equity.US.<SYMBOL>/USD` close feed are accepted. The collector saves executable
Yes/No bid, ask, top-five-level depth, immutable checkpoint snapshots, and
official settlement in `price_ladder_*` SQLite tables. It never writes paper
positions or changes the supervisor, Top-5 policy, entry gates, or sizing.

```zsh
# Terminal 3: discover TSLA/NVDA ladders and poll public CLOB books every minute.
polymarket-stock collect-price-ladders \
  --symbols TSLA,NVDA --interval-seconds 60 --duration-seconds 0

# Terminal 4: separate localhost interface. Open http://127.0.0.1:8765
polymarket-stock research-dashboard --host 127.0.0.1 --port 8765

# Optional one-shot discovery, settlement reconciliation, and JSON comparison.
polymarket-stock discover-price-ladders --symbols TSLA,NVDA
polymarket-stock settle-price-ladders
polymarket-stock price-ladder-report --date 2026-08-03
```

### Isolated Above-X Historical Replay

The `closes above $K` markets are intentionally separate from the core Up/Down
policy. Gamma historical events are discovered with cursor pagination and
per-symbol title filters; only markets whose rules explicitly use the Pyth
close feed are retained.

```zsh
# Discover closed TSLA/NVDA Above-X markets for the available three-month window.
polymarket-stock discover-above-x-history \
  --symbols TSLA,NVDA --date-start 2026-05-04 --date-end 2026-08-02 \
  --output data/historical/above_x_discovery.json

# Download Yes/No CLOB price-history proxies, Gamma settlement, and Pyth final.
# Existing local Pyth references are reused; PYTH_PRO_API_KEY is only needed
# when a matching local final reference is unavailable.
polymarket-stock backfill-above-x-history \
  --discovery-json data/historical/above_x_discovery.json \
  --output-dir data/historical/above_x

# Check missing files before interpreting results.
polymarket-stock above-x-coverage

# Replay 12:00, 14:00, and 15:30 using a CLOB historical-price proxy.
polymarket-stock backtest-above-x \
  --discovery-json data/historical/above_x_discovery.json \
  --data-dir data/historical/above_x \
  --spot-data-dir data/historical/90d \
  --minimum-edge 0.02 --lookback-days 20 \
  --output data/historical/above_x_replay.json
```

### Core Above-X Veto Shadow

Above-X does not create a second trading strategy. Its first live role is a
read-only confirmation/veto diagnostic for an existing Core Up/Down checkpoint
entry. `VETO` is recorded separately and never changes a Core paper position.

```zsh
# Historical, non-leaking 12:00 policy selection.
polymarket-stock walk-forward-above-x-veto \
  --checkpoint 1200_EDT --buffer 0.02 --minimum-edge 0.02 \
  --training-days 6 --validation-days 2 \
  --output data/historical/above_x_veto_walk_forward.json

# Optional manual sync. collect-price-ladders runs this automatically after each poll.
polymarket-stock sync-above-x-veto-shadow --minimum-strikes 3 --maximum-width 0.30
```

The historical search considers `BASELINE`, `VETO_DISAGREEMENT`, and
`REQUIRE_CONFIRMATION` with 3/4/5 valid strikes. It does not tune historical
spread/width because the CLOB history endpoint lacks historical bid/ask/depth.
Live snapshots do have that information and label wide or unbracketed curves
`UNRELIABLE`. Do not promote this diagnostic to a Core gate until it has enough
fully out-of-sample Core entries.

This replay reports observations, trades, Brier/log loss, win rate, and PnL,
but historical `prices-history` is a price proxy rather than a recorded
executable ask/bid/depth. A missing CLOB, Pyth, settlement, or intraday spot
file causes the contract to be skipped and is visible in `above-x-coverage`;
it never creates core paper positions. The localhost research dashboard has an
`Above-X Research` tab showing the same coverage and replay status.

The research UI has separate `Core Up/Down`, `Price Distribution`, and
`Cross-Market` views. Ladder probabilities are fitted with weighted monotonic
regression because `P(close > K)` must decrease as `K` rises. The comparison
reports only `CONFIRM`, `MIXED`, `DISAGREE`, or `UNRELIABLE`; wide books,
insufficient strikes, an unbracketed price-to-beat, and raw monotonic violations
remain visible. No probability averaging is used for entries.

### Current Limits

- No live orders, wallet access, private keys, or execution adapter exist.
- Public discovery may miss markets outside the configured tags or unusual
  contract templates.
- Settlement wording, Pyth reference details, event risk, liquidity, and fees
  require human review before any future execution work.
- Shadow results must be evaluated over sufficient settled markets before a
  controlled live-pilot proposal can be considered.

### Multi-Market Shadow Supervisor

```zsh
# Refresh active equity markets every 15 minutes, supervise up to 18 markets,
# and create only idempotent hold-to-settlement paper positions.
polymarket-stock supervise-shadow --spot-provider finnhub --duration-seconds 0

# Inspect the paper lifecycle and realized calibration results.
polymarket-stock paper-positions --status OPEN
polymarket-stock paper-positions --status SETTLED
polymarket-stock paper-performance
polymarket-stock settle-paper-positions

# Inspect research-only passive maker quotes. These are not orders or fills.
polymarket-stock maker-shadow-quotes --status ACTIVE
polymarket-stock maker-shadow-quotes --status CANCELLED

# Review selected and rejected 30-second paper-entry batches.
polymarket-stock portfolio-decisions --limit 100
polymarket-stock calibrate-checkpoints
```

The supervisor shares one Polymarket stream and one stock-quote stream, restarts
those subscriptions when the active universe changes, and uses Gamma's published
closed/resolved market state for settlement reconciliation. It does not use stock
prices to infer settlement. It also subscribes read-only to matching Pyth Hermes
equity feeds: at most one source observation and one Pyth-versus-cross-check
comparison per symbol per second are stored. Pyth is the hard primary source
because daily-equity contracts settle from Pyth. Missing/stale Pyth data, or a
fresh Pyth/cross-check difference above 0.5%, blocks paper entry while retaining
the observation for calibration. A stale Finnhub/Alpaca cross-check alone is a
quality flag, not an entry block.

### Exact Pyth Close Calibration

Daily Up/Down contracts resolve from Pyth's final regular-session one-minute
candle, not from Finnhub. During the Pyth Pro trial, set the key only in the
local `.env` file:

```dotenv
PYTH_PRO_API_KEY=replace_with_your_trial_key
```

`PYTH_PRO_API_KEY` is also used for the server-side Hermes stream when
`PYTH_API_KEY` is not set. Set `PYTH_API_KEY` only when using a separate
Core/Hermes credential.


When `supervise-shadow` remains running, it automatically performs one
research-only calibration after 16:03 New York time. For every Finnhub symbol
with a quote captured in the 15:59–16:00 ET window, it downloads the official
Pyth final-minute close and prior Pyth close, then stores: absolute error in
basis points, source timestamps, Pyth/Finnhub Up/Down classification, and
whether the source difference would have flipped the contract outcome. This
never changes a paper entry or a Core signal.

The post-close command is idempotent and is only needed to retry a date or
produce a JSON report manually; it is not a fourth always-running terminal:

```zsh
polymarket-stock close-source-calibration \
  --market-date 2026-08-03 \
  --output data/close_source_calibration_2026-08-03.json
```

After the trial, the supervisor skips this exact-candle diagnostic when the Pro
key is absent. Finnhub and Polymarket collection continue normally, but new
exact Pyth-close calibration rows cannot be created.

Normal-session source observations and Pyth/Finnhub comparisons are retained at
one row per symbol/source/minute. The final 15:55–16:00 ET window remains
second-level so the exact-close calibration can detect a close-window mismatch.
This retention policy affects SQLite persistence only: the in-memory streaming
risk gates and live evaluations still respond to every received update.

### Pyth-Outage / Finnhub-Only Fallback

`FINNHUB_ONLY` is the default runtime mode. It uses Finnhub spot plus
Polymarket CLOB quotes, while retaining the cached official Pyth final close as
the daily threshold whenever it exists. A spot within 35 bps of an *estimated*
threshold is labelled `NEAR_ESTIMATED_THRESHOLD` and gets an additional
model-error buffer; it is not suppressed solely for that reason.

`PYTH_PRIMARY` remains an opt-in diagnostic mode. It requires a working Hermes
feed for the current equity symbols; it intentionally produces no model signal
when that feed is unavailable rather than silently substituting a different
primary source.

```zsh
# After Pyth access ends: no Hermes stream, no Pyth freshness gate.
polymarket-stock supervise-shadow \
  --spot-provider finnhub \
  --spot-mode FINNHUB_ONLY \
  --finnhub-threshold-safety-bps 35 \
  --duration-seconds 0
```

This is not an exact zero-Pyth replacement. A daily contract's threshold is
the prior **Pyth final Close**, which Polymarket metadata does not provide as a
number. While Pro access exists, the supervisor caches every active symbol's
final Pyth candle after 16:03 ET. Finnhub-only mode reads that cache first. If
the cache is absent, it combines any available Nasdaq daily close, Yahoo daily
close, and locally captured Finnhub final regular-session trade. Each source is
debiased using prior exact Pyth-close observations, and the median becomes the
threshold. The dashboard shows source count, calibration sample count, estimated
P90 error, and one of `CALIBRATED_MULTI_SOURCE_HIGH`,
`CALIBRATED_MULTI_SOURCE_MEDIUM`, or `SINGLE_SOURCE_ESTIMATE`. The model applies
an uncertainty buffer proportional to that error. Therefore the strategy can
continue issuing research and paper recommendations after Pyth access ends, but
it cannot claim exact Pyth threshold alignment until the estimate has enough
out-of-sample validation.

If Finnhub's earnings-calendar request times out, the supervisor remains running
and reports `SUPERVISOR_EVENT_CALENDAR_UNAVAILABLE`. Affected markets receive the
hard risk gate `EVENT_CALENDAR_UNAVAILABLE`, so observation continues but no paper
entry is created without a usable calendar. The client waits at most five seconds
per request and applies a 60-second retry cooldown to avoid stalling once per market.

Daily equity contracts are eligible only on the New York calendar date of their
published close. A next-day contract is never evaluated, quoted, or entered
during the preceding trading day. Existing paper entries that predate their
contract's New York trading date remain in the SQLite audit trail but are marked
`PRECONTRACT_TRADE_DATE` and excluded from calibration and paper-performance reports.

The supervisor also records maker shadow quotes. For each valid Fair Up/Down
evaluation it proposes a passive `1c`-tick buy below fair value with a default
`0.5c` theoretical edge. To prevent quote churn, it only reprices when the
proposed limit changes by at least `2c` and the active quote has lived for at
least 30 seconds. Both thresholds are configurable on `supervise-shadow` with
`--maker-reprice-minimum-price-change` and
`--maker-minimum-quote-lifetime-seconds`.
`TOUCHED` means the published ask reached the quote; it is not treated as a fill,
does not earn a rebate, and does not create a paper position.

A current `IV_VALID` near-ATM call/put surface uses the blended IV model. When
IV is unavailable, the realized-volatility fallback can still enter the paper
batch with `OPTION_IV_FALLBACK_REALIZED_VOL` and
`PAPER_ENTRY_REALIZED_VOL_FALLBACK` recorded in its payload. Eligible signals
are collected for 30 seconds, then selected with conservative defaults: three
per day, one per static risk group, and two per direction. Every selected or
rejected candidate is stored in `portfolio_decisions`; later analysis must
separate IV-backed and realized-volatility-fallback entries.

### Option-IV Provider Limits

The supervisor uses `POLYGON_API_KEY` for Massive (formerly Polygon) when it is
configured, otherwise it uses `TRADIER_API_TOKEN`. Massive Currencies Basic and
Options Basic do not include U.S. option-chain snapshots. A 403 entitlement
response is recorded once and disables further Massive requests for that process.
The client also enforces a five-calls-per-minute ceiling. Massive's 15-minute
delayed option plans remain observation-only: only fresh quotes explicitly
labelled `REAL-TIME` can produce `IV_VALID` and enter a paper batch.

### Offline Option-Pricing Validation

`validate-option-pricing` is an isolated BSM/CRR-binomial cross-check inspired
by open-source option calculators. It is useful for checking numerical inputs
from a trusted quote source; it never fetches Yahoo/MarketWatch, writes a
position, or changes supervisor signals.

```zsh
polymarket-stock validate-option-pricing \
  --spot 100 --strike 100 --bid 10.40 --ask 10.50 \
  --annual-volatility 0.20 --seconds-to-expiry 31557600 \
  --option-type call --risk-free-rate 0.05 --style european
```

The JSON output is always `RESEARCH_ONLY_VALIDATED` with `entry_eligible: false`.

### Checkpoint Buffer Research

These reports replay immutable, on-time checkpoints only. They never alter the
supervisor, paper positions, or future live-trading settings. A checkpoint is
eligible only when its first valid observation arrives within five minutes of
the scheduled New York time.

```zsh
polymarket-stock buffer-sweep \
  --minimum-buffer 0.00 \
  --maximum-buffer 0.20 \
  --buffer-step 0.01 \
  --minimum-edge 0.02 \
  --output data/buffer_sweep.json

polymarket-stock walk-forward-buffer-sweep \
  --training-days 20 \
  --validation-days 5 \
  --minimum-training-trades 10 \
  --output data/walk_forward_buffer_sweep.json
```

Each replay selects at most one first eligible hold-to-settlement entry per
market-day. Compare coverage, trade count, net PnL, Brier/log loss, and later-day
performance together. A large buffer can appear perfect simply because it makes
no trades.

The live evaluator keeps a 2% base uncertainty buffer for both IV-backed and
realized-volatility-fallback inputs. Fallback entries remain explicitly labelled
so their outcomes can be evaluated separately before any execution decision.

### Top-5 Walk-Forward and Strategy Diagnostics

```zsh
polymarket-stock walk-forward-top-five --training-days 4 --validation-days 2
polymarket-stock strategy-diagnostics --shares 10 --output data/strategy_diagnostics.json
```

`walk-forward-top-five` fits binned probability shrinkage on training dates only, searches checkpoint, buffer, and minimum-edge combinations, caps each day at five entries, and applies the frozen policy to later validation dates. `--raw-probabilities` provides an explicit uncalibrated comparison. It never forces five entries.

`strategy-diagnostics` compares model direction with the market favorite, spot versus Pyth threshold, and market-majority baselines. It also reports top-five-depth VWAP, delayed-entry slippage, fresh Pyth/cross-source divergence, primary-versus-EWMA probability disagreement, partial-session Pyth realized volatility versus prior matching checkpoints, and executable-bid exit markouts. The source report samples one stored comparison per minute and excludes stale or timestamp-missing pairs; the original per-second rows remain in SQLite. All PnL includes frozen taker fees where available and remains shadow research.

Fresh cross-source differences below the existing 0.5% hard gate now add a bounded model-error buffer, including Pyth confidence. A realized-volatility fallback signal is recorded but cannot open a paper position when the primary and comparison volatility models disagree on direction or by at least ten probability points.

## 繁體中文

### 用途

這是一個針對 Polymarket 美股單日漲跌市場的純研究與 shadow 模擬工具。它會掃描市場、估算保守的基準機率，並記錄 shadow 重新評估事件。目前沒有錢包整合、私鑰讀取、下單程式或實盤交易路徑。

### 安裝與初始化

```zsh
cd $REPO_ROOT
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
cp .env.example .env
polymarket-stock init-db
python -m unittest discover -s tests -v
```

程式啟動時會強制要求 `SHADOW_MODE=true` 與
`LIVE_TRADING_ENABLED=false`。研究階段不可關閉這些安全限制。

若 VPN、Proxy 或資安軟體會重新簽發 HTTPS 憑證，請匯出其受信任根憑證為 PEM 檔，並在 `.env` 設定 `SSL_CERT_FILE`。不可關閉 TLS/SSL 憑證驗證。

### 每日啟動指令

以下程序必須在不同 Terminal 分別執行。每個 Terminal 都先進入同一個 canonical repo 並啟用
`.venv`；不要使用 `$STALE_REPO_ROOT`。

**Terminal 1：主 bot（會自動寫入同一個 SQLite DB）**

```zsh
cd $REPO_ROOT
source .venv/bin/activate

polymarket-stock supervise-shadow \
  --spot-provider finnhub \
  --spot-mode FINNHUB_ONLY \
  --volatility-estimator CLOSE_TO_CLOSE \
  --comparison-estimators EWMA \
  --volatility-decay 0.94 \
  --scan-interval-seconds 900 \
  --max-markets 18 \
  --max-daily-paper-entries 5 \
  --duration-seconds 0
```

**Terminal 2：TSLA / NVDA price-ladder collector（會寫入獨立 `price_ladder_*` tables）**

```zsh
cd $REPO_ROOT
source .venv/bin/activate

polymarket-stock collect-price-ladders \
  --symbols TSLA,NVDA \
  --interval-seconds 60 \
  --duration-seconds 0
```

**Terminal 3：localhost research dashboard（唯讀，不會自行啟動 bot 或 collector）**

```zsh
cd $REPO_ROOT
source .venv/bin/activate

polymarket-stock research-dashboard \
  --host 127.0.0.1 \
  --port 8765 \
  --limit 18 \
  --daily-entry-limit 5
```

保持 Terminal 3 運作，然後用瀏覽器開啟 `http://127.0.0.1:8765`。頁面右上角會同時顯示
台灣 `TW` 與紐約 `NY` 時間。電腦重開機或該 Terminal 關閉後，必須重新執行這個指令。

**Terminal 4（可選）：原本的 Rich terminal dashboard**

```zsh
cd $REPO_ROOT
source .venv/bin/activate

polymarket-stock dashboard \
  --refresh-seconds 3 \
  --limit 18 \
  --daily-entry-limit 5
```

Terminal 1 與 Terminal 2 都會自動初始化並持續寫入 `.env` 所指定的 journal DB；兩種 dashboard
都只讀取 DB。各程序按一次 `Ctrl+C` 即可乾淨停止。若要把每日 paper 上限從 `5` 改成
`8`，Terminal 1 使用 `--max-daily-paper-entries 8`，Terminal 4 也要同步使用
`--daily-entry-limit 8`，否則顯示的分母會和 bot 設定不同。

localhost 的 Core 頁面已包含原本 Rich dashboard 的 checkpoint matrix、Top Recommendations、
Daily Paper Portfolio、OPEN/SETTLED、W/L/PnL、全部 first-signal 勝率與 sizing readiness。
因此日常運行可以不開 Terminal 4；Terminal 4 只保留作為純文字備援。

主 bot 在週末、NYSE 假日與盤後會自動觀察下一個 NYSE 交易日的 Polymarket Up/Down 合約，
localhost 仍會顯示市場隱含 Up 機率、Up/Down top-of-book、頂層數量、資料年齡與狀態。
此時 `Decision mode` 會明確標示 `OBSERVATION ONLY`，不建立 checkpoint 或 paper entry；沒有新的
underlying spot 時，model probability 顯示 `UNAVAILABLE`。localhost 本身是唯讀頁面，因此要取得
持續變動的 order book，Terminal 1 的 `supervise-shadow` 仍必須保持運作。

### 市場掃描與訂單簿觀察

```zsh
# 廣泛掃描單日美股市場；第二個指令會一併擷取訂單簿
polymarket-stock scan-equity-events --tag-slugs stocks,equities --max-pages-per-tag 100
polymarket-stock scan-equity-events --tag-slugs stocks,equities --snapshot-books
polymarket-stock list-markets --symbol TSLA

# 查看已知事件，或依 market ID 自動擷取 Up / Down 兩側訂單簿
polymarket-stock scan-event --slug tsla-up-or-down-on-july-20-2026 --symbols TSLA
polymarket-stock snapshot-market --market-id 2958682
```

掃描器使用 Polymarket 公開、唯讀的 Gamma API，分頁讀取 `stocks` 與
`equities` 標籤後去重，只保留進行中、未關閉的單日方向二元市場。這能提供廣泛覆蓋，但不保證找到 Polymarket 上每一個市場。所有結果均為 `REVIEW_REQUIRED`，仍必須人工確認市場的結算文字與結算資料來源。

單日市場的 outcome 可能是 `Up`/`Down`，而不是 `Yes`/`No`。資料庫會保留 Polymarket 原始 outcome 標籤與對應的 CLOB token ID；`snapshot-market` 會自動抓取雙邊訂單簿，不需要把含有 `|` 的 token ID 手動貼到 shell。

### 基準合理價格

基準模型使用現價、前一日收盤價與已實現波動率估算 Up 的合理機率，並固定加入保守的 model-error buffer。因此市場價格與模型價格有差距，並不等於已經得到 paper trade 或交易結論。

```zsh
# 使用已驗證的本機日線 CSV，需有 Date,Close 欄位且至少 21 筆資料
polymarket-stock evaluate-baseline \
  --market-id 2958682 \
  --history-csv /path/to/TSLA_daily.csv \
  --spot 380.84 \
  --resolves-at 2026-07-20T20:00:00Z

# 不用 API key 的備援：Nasdaq 公開日線與最後報價
polymarket-stock evaluate-nasdaq-baseline \
  --market-id 2958682 --symbol TSLA --resolves-at 2026-07-20T20:00:00Z
```

執行估值前先跑 `snapshot-market`，讓程式取得最新的 Up/Down ask。Nasdaq 資料來源明確標示為 `NON_SETTLEMENT`：它不是 Polymarket 使用的 Pyth 結算價，且回傳的報價不一定是即時報價。成功取得的公開資料會快取於 `data/baseline_cache/`；暫時 API 故障時，只有仍在有效期限內的快取可以使用，且不會略過保守的推薦門檻。

期權快照僅供研究，並固定使用 Alpaca `indicative` feed：

```zsh
polymarket-stock snapshot-alpaca-options --symbols SPY260718C00600000
```

`indicative` 期權資料不是可用於即時定價的資料源。

### 即時 Shadow Stream

Finnhub 是預設的股價報價 provider，不需要券商帳戶。請把 API key 填入
`.env`，不要提交至 Git：

```dotenv
FINNHUB_API_KEY=...
```

```zsh
polymarket-stock stream-shadow --market-id 2958682 --symbol TSLA --duration-seconds 0

# 保留既有 Alpaca IEX adapter，日後可用它和 Finnhub 比較。
polymarket-stock stream-shadow --market-id 2958682 --symbol TSLA --spot-provider alpaca
```

此指令會連接 Polymarket 公開 Market WebSocket 與選定的美股報價 WebSocket。它以 500 ms debounce 合併短時間內的訂單簿與現貨更新，輸出並記錄 `REALTIME_BASELINE_EVALUATED`。每筆結果都包含 freshness gate 後的 spot、可成交 Up/Down ask、合理機率、raw net edge 與 skip reason，也會寫入本機 SQLite journal。`--duration-seconds 0` 代表持續運行至手動中斷。免費 Alpaca IEX 不是完整 SIP 整合報價；Finnhub 的覆蓋與延遲也應先和市場比較驗證。目前 stream 尚未取得即時期權 IV；它只負責觀察與觸發重新評估，並不是交易訊號。
重複且相同的 stale-data 狀態最多每分鐘記錄一次；公開資料流因暫時網路中斷關閉時會自動重連。美股休市期間，沒有 Finnhub trade 時會顯示 `MISSING_SPOT`，這是預期的安全結果。

每次 `stream-shadow` 與 supervisor 都會先重讀 journal 保存的 Gamma 原始條款，只接受目前已觀察到的 Pyth `Equity.US.<TICKER>/USD` 日收盤模板：`Up/Down` 順序、前一交易日比較、50-50 平手規則，以及 Pyth 未四捨五入收盤價都必須存在。實時評估也會輸出 `market_session`、bid/ask、資料年齡、`reference_spot` 與 `cross_source_difference`；以下狀態會被拒絕並記錄：非正常交易時段、缺少或 crossed 的任一 outcome order book、未就緒/過期 stream，以及當 Nasdaq reference quote 在 15 秒內仍和 streaming spot 相差超過 0.5%。

### 獨立 Price-Ladder 研究

Polymarket 的 `收盤高於固定價 K` 市場由獨立 sidecar 收集。程式只接受規則明確指定對應
Pyth `Equity.US.<SYMBOL>/USD` 收盤 feed、outcome 為 Yes/No 的二元合約；資料會寫入獨立
`price_ladder_*` SQLite tables，包含 Yes/No 可成交 bid/ask、前五層 depth、不可變
checkpoint 快照與官方結算。它不會寫入 paper position，也不會改變 supervisor、Top-5、
entry gate 或 sizing。

```zsh
# Terminal 3：每分鐘掃描並收集 TSLA / NVDA 價格階梯訂單簿
polymarket-stock collect-price-ladders \
  --symbols TSLA,NVDA --interval-seconds 60 --duration-seconds 0

# Terminal 4：獨立 localhost 研究介面，瀏覽器開啟 http://127.0.0.1:8765
polymarket-stock research-dashboard --host 127.0.0.1 --port 8765

# 可選的一次性掃描、結算對帳與指定日期 JSON 報表
polymarket-stock discover-price-ladders --symbols TSLA,NVDA
polymarket-stock settle-price-ladders
polymarket-stock price-ladder-report --date 2026-08-03
```

`localhost` 不是永久網址，也不是另一個雲端服務。每次電腦重開機或 dashboard terminal 關閉後，
都要在 repo 內重新執行 `polymarket-stock research-dashboard --host 127.0.0.1 --port 8765`，
並保持該 terminal 運行，再用瀏覽器開啟 `http://127.0.0.1:8765`。按 `Ctrl+C` 即停止網站；
collector 與原本 bot 是另外的程序，不會因為開啟這個網頁而自動啟動。

網頁將 `Trade Today` 與研究分頁隔離。`Trade Today` 是唯一顯示 Core `Up/Down`
checkpoint、Top-5 paper portfolio 與入場 gate 的地方；目前預設僅 `12:00 EDT` 的不可變
checkpoint 可以建立 paper entry。`Price Distribution` 顯示 TSLA/NVDA 的 live `Above-X`
研究候選：每個 strike 的 `Yes/No` 側、當前可成交 ask、曲線機率、未扣費的 gross edge、
executable width 與品質警告。它不是下單指令：price-ladder collector 尚未保存每個 token 的
即時 taker fee，所以必須從 gross edge 再扣下單當下 fee 後自行判斷。

`Cross-Market` 只列出實際已收集 ladder 的 symbols，且只會在同一交易日的
`12:00 / 14:00 / 15:30 EDT` Core checkpoint 與 ladder checkpoint 都存在後產生比較。
在此之前它會明確說明是等待 ladder snapshots 或等待 matching checkpoint，不會用空白或把
無 ladder 的核心市場偽裝成 `UNRELIABLE`。因為 `P(close > K)` 理論上必須隨 strike 上升而下降，
曲線使用加權 monotonic regression；spread 太寬、strike 不足、price-to-beat 沒有被階梯包住
或原始曲線違反單調性都會顯示，且不會把兩個市場機率直接平均後拿去進場。

`Above-X Research` 保留歷史 replay、coverage 與 veto 結果。它和 Price Distribution 的 live
研究候選都不會改變 supervisor、Core paper entry、Top-5 或 sizing。

### 目前限制

- 專案沒有實盤下單、錢包存取、私鑰或 execution adapter。
- 公開掃描可能遺漏設定標籤以外的市場或特殊市場模板。
- 結算文字、Pyth 參考價格、事件風險、流動性與費用，未來若要進入執行階段前都必須人工檢查。
- 需要累積足夠數量的已結算 shadow 結果，證明扣除成本後仍有穩定優勢，才可提出受控實盤試行方案。

### 多市場 Shadow Supervisor

```zsh
# 每 15 分鐘更新活躍美股市場，最多管理 18 個市場，並只建立可冪等、持有至結算的 paper position。
polymarket-stock supervise-shadow --spot-provider finnhub --duration-seconds 0

# 查看 paper lifecycle 與已結算的校準結果。
polymarket-stock paper-positions --status OPEN
polymarket-stock paper-positions --status SETTLED
polymarket-stock paper-performance
polymarket-stock settle-paper-positions
```

supervisor 共享一條 Polymarket stream 與一條股價 stream，active universe 改變時會重建訂閱集合，並依 Gamma 公布的 closed/resolved 狀態對帳結算；不會自行根據股價推斷市場結算。

若 Finnhub 財報日曆請求逾時，supervisor 不會終止，而會輸出
`SUPERVISOR_EVENT_CALENDAR_UNAVAILABLE`。受影響市場會加上
`EVENT_CALENDAR_UNAVAILABLE` hard risk gate：觀察和資料記錄會繼續，但在日曆資料可用前不會建立 paper entry。每次請求最多等待 5 秒，失敗後會有 60 秒 retry cooldown，避免每個市場各自卡住一次。

每日股票合約只會在其公布收盤價所屬的紐約日期啟用。隔日合約不會在前一個交易日被估值、掛 maker quote 或建立 paper position。既有的前一日 paper entry 會保留在 SQLite 稽核紀錄中，但會標記為 `PRECONTRACT_TRADE_DATE`，並排除於校準與 paper-performance 報表。

supervisor 也會記錄 maker shadow quote。每個有效的 Fair Up/Down 評估都會提出一個低於 fair value、以 `1c` tick 對齊的被動買價，預設理論 edge 為至少 `0.5c`。為避免頻繁取消重掛，只有建議限價至少改變 `2c`，且現有 quote 已存在至少 30 秒，才會重掛；可用 `--maker-reprice-minimum-price-change` 與 `--maker-minimum-quote-lifetime-seconds` 調整。`TOUCHED` 只表示公開 ask 曾觸及該價位，不是成交、不會取得 rebate，也不會建立 paper position。

具有新鮮、完整近 ATM call/put `IV_VALID` surface 的訊號會使用 IV 混合模型。若 IV 無法取得，realized-volatility fallback 仍可進入 paper batch，但 payload 會明確記錄 `OPTION_IV_FALLBACK_REALIZED_VOL` 與 `PAPER_ENTRY_REALIZED_VOL_FALLBACK`。所有符合條件的訊號仍會觀察和記錄，但預設只由 immutable `1200_EDT` checkpoint 的首次有效快照建立 paper-entry 候選，避免開盤任意 tick 與 checkpoint 策略混在一起。研究其他時點時，必須顯式使用 `--paper-entry-checkpoints 1200_EDT,1400_EDT`。候選會先累積 30 秒，再依保守預設做批次選擇：每日最多 3 筆、每個靜態風險群組最多 1 筆、同方向最多 2 筆。每個通過或拒絕的候選都會寫入 `portfolio_decisions`；後續分析必須區分 IV-backed 與 realized-volatility fallback entry。

美國交易日判定使用紐約時區的週一至週五 `09:30-16:00`，並套用 NYSE 核心休市日。特殊臨時休市與提早收盤仍須由 event calendar 補入。

### Phase 3 可驗證研究

若要讓 fair probability 使用 option IV，請先確認資料商提供的是**即時**美股 option chain。
目前支援 Massive（原 Polygon）與 Tradier；請在 `.env` 加入其中一種：

```dotenv
POLYGON_API_KEY=...
TRADIER_API_TOKEN=...
```

supervisor 會優先使用 Massive，否則使用 Tradier，從 options chain 選擇到期日在市場結算日
當天或之後、且 near-ATM 的 call/put，驗證 quote 年齡、bid/ask spread、IV 與資料時間框。
Massive 的 Currencies Basic 與 Options Basic 免費層沒有美股 option snapshot 權限；Options
Starter 的 15 分鐘延遲資料也會被拒絕為 `IV_VALID`。程式在 403 後不再重試，並把每程序
呼叫限制為最多每 12 秒一次。只有新鮮即時 IV 才會使用 `75% option IV + 25% realized
volatility`；其餘狀態採用 realized-volatility fallback，仍可進入 paper batch，但會以明確 quality flag 標示，供後續分層分析。

### 離線選擇權定價驗證

`validate-option-pricing` 是獨立的 BSM/CRR binomial 交叉檢查工具，用於驗證來自可信報價來源
的數學輸入。它不會讀取 Yahoo/MarketWatch、不會建立 position，也不會改變 supervisor 訊號：

```zsh
polymarket-stock validate-option-pricing \
  --spot 100 --strike 100 --bid 10.40 --ask 10.50 \
  --annual-volatility 0.20 --seconds-to-expiry 31557600 \
  --option-type call --risk-free-rate 0.05 --style european
```

輸出永遠帶有 `RESEARCH_ONLY_VALIDATED` 與 `entry_eligible: false`。

可選的 `data/event_calendar.json` 可加入 earnings、FOMC、CPI 等結構化風險事件；落在市場
結算前的 blocking event 會阻擋 paper entry：

```json
[
  {"kind": "FOMC", "starts_at": "2026-07-29T18:00:00Z", "symbols": ["*"], "blocking": true},
  {"kind": "earnings", "starts_at": "2026-07-22T20:00:00Z", "symbols": ["TSLA"], "blocking": true}
]
```

NYSE 核心休市日（含 Good Friday、Juneteenth 與聖誕節）已作為 hard gate；特殊臨時休市與提早收盤仍需由 event calendar 補入。

已結算資料可用以下指令做不可變 entry replay 與保守校準：

```zsh
polymarket-stock replay-settled
polymarket-stock replay-settled --output data/replay_report.json
polymarket-stock calibrate-paper
polymarket-stock calibrate-paper --write
polymarket-stock replay-observations
polymarket-stock calibrate-observations
polymarket-stock dashboard
```

`calibrate-paper --write` 只會在至少 30 筆已結算 paper position 時寫入
`data/model_calibration.json`。下一次 supervisor 啟動時只會提高、絕不降低 model-error buffer 與 minimum-edge floor。

### Checkpoint Buffer 研究

這些報表只重播不可變、且準時取得的 checkpoint；不會改變 supervisor、paper position 或任何未來實盤設定。固定 checkpoint 的第一筆有效資料必須在預定紐約時間後 5 分鐘內抵達，才可用於正式校準。

```zsh
polymarket-stock buffer-sweep \
  --minimum-buffer 0.00 \
  --maximum-buffer 0.20 \
  --buffer-step 0.01 \
  --minimum-edge 0.02 \
  --output data/buffer_sweep.json

polymarket-stock walk-forward-buffer-sweep \
  --training-days 20 \
  --validation-days 5 \
  --minimum-training-trades 10 \
  --output data/walk_forward_buffer_sweep.json
```

每次重播每市場每日最多選擇一筆、第一個合格並持有至結算的 entry。必須一起比較 coverage、交易數、淨 PnL、Brier/log loss 與後續交易日表現；過大的 buffer 可能因為完全沒有交易而看似完美。

即時 evaluator 對 IV-backed 與 realized-volatility fallback 輸入皆使用 2% 基礎不確定性 buffer。fallback entry 會保留明確 quality flag，且在評估或校準時必須與 IV-backed entry 分開報告。

### Probability Calibration and Sizing Gate

```zsh
# One first model-side signal per officially settled market. The report separates
# direction, IV/fallback regime, time bucket, probability band, provider, model version,
# and distance from the contract's Pyth threshold.
polymarket-stock calibrate-first-signals --output data/first_signal_calibration.json

# Fits probability shrinkage on earlier distinct New York trading dates and scores only
# later dates. It never changes supervisor thresholds or a live decision.
polymarket-stock walk-forward-probability-calibration \
  --training-days 20 \
  --validation-days 5 \
  --minimum-training-samples 50
```

`calibrate-first-signals` reports Wilson confidence intervals as well as Brier/log loss; a high win rate alone is not calibration. `walk-forward-probability-calibration` requires 25 distinct New York dates by default and returns an explicit insufficient-data status otherwise.

Sizing remains `FIXED_SMALL_POSITION_ONLY`. Kelly is always disabled by code and has no execution path. A cohort needs at least 100 settled first signals for an `OPERATOR_REVIEW_REQUIRED` status; that is not approval to trade or to use Kelly. IV-valid and realized-volatility-fallback cohorts are never pooled for this gate.

### Top-5 Walk-Forward 與策略診斷

```zsh
polymarket-stock walk-forward-top-five --training-days 4 --validation-days 2
polymarket-stock strategy-diagnostics --shares 10 --output data/strategy_diagnostics.json
```

`walk-forward-top-five` 的機率校正只使用 training dates，接著挑選 checkpoint、buffer 與 minimum edge，再把完全鎖定的策略套到 validation dates。每天是「最多五筆」，不會為了湊滿五筆強迫交易；`--raw-probabilities` 可輸出未校正對照。

`strategy-diagnostics` 同時比較模型方向、市場熱門方向、spot 相對 Pyth threshold 與市場多數方向，並分析 top-five depth VWAP、延遲成交 slippage、Pyth/Finnhub 新鮮報價差、CLOSE_TO_CLOSE/EWMA 分歧、Pyth 當日 realized volatility 相對過往同 checkpoint 的異常，以及使用可成交 bid 的 1/5/15/30 分鐘 exit 回放。跨來源報價以每分鐘一筆規則抽樣並排除 stale/缺 timestamp 的 pair，逐秒原始資料仍完整保留。

未達 0.5% hard gate 的新鮮跨來源誤差與 Pyth confidence 會增加 bounded model-error buffer。realized-vol fallback 若與 comparison volatility model 方向不同或 fair probability 相差至少 10 個百分點，仍保留觀察，但不建立 paper position。

supervisor 預設使用精簡的單行人類輸出，完整 JSON 仍會寫入 `logs/shadow_bot.jsonl`。若要把 terminal 輸出也交給程式解析，加入 `--output-format json`。`dashboard` 是持續刷新的 Rich terminal UI，預設每 3 秒更新，按 `q` 或 `Ctrl+C` 離開。核心矩陣依 symbol 固定排序，分別保存 `12:00 / 14:00 / 15:30 EDT` 的方向、selected-side fair probability、當時 ask 與扣除 fee/buffer 後的 edge；最後一欄以 checkpoint 當時保存的 minimum edge 顯示 `ENTER`、`SKIP` 與最高建議買價。底部 Top Recommendations 預設最多 5 筆，並顯示 paper entry 的實際 New York 建立時間、結算 W/L/PnL，以及全部已結算市場每市場第一筆模型訊號的獨立勝率；`dashboard --once` 會輸出相同內容的單次純文字快照。`replay-observations` 與 `calibrate-observations` 使用所有有有效 fair probability 且已官方結算的市場，而不是只使用 paper entries。
