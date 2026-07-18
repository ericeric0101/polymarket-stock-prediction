# Polymarket Stock / Polymarket 美股預測市場研究工具

## English

### Purpose

Shadow-only research tooling for Polymarket daily US equity-direction markets.
It observes markets, estimates a conservative baseline probability, and records
shadow re-evaluation events. It contains no wallet integration, private-key
handling, order-submission code, or live-trading path.

### Setup

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
startup. Do not change these safeguards for research runs.

If a VPN, proxy, or security tool re-signs HTTPS traffic, export its trusted
root CA as a PEM file and set `SSL_CERT_FILE` in `.env`. TLS verification must
remain enabled.

### Market Observation

```zsh
# Broad daily-equity discovery and optional order-book capture
polymarket-stock scan-equity-events --tag-slugs stocks,equities --max-pages-per-tag 100
polymarket-stock scan-equity-events --tag-slugs stocks,equities --snapshot-books

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

### Real-Time Shadow Streams

Add your own Alpaca credentials to `.env`; do not commit them:

```dotenv
ALPACA_API_KEY_ID=...
ALPACA_API_SECRET_KEY=...
```

```zsh
polymarket-stock stream-shadow --market-id 2958682 --symbol TSLA --duration-seconds 0
```

This consumes Polymarket's public Market WebSocket and Alpaca Basic IEX stock
WebSocket, coalesces incoming book/spot updates with a 500 ms debounce, and
logs `SHADOW_REEVALUATION_REQUESTED` events to `logs/shadow_bot.jsonl`.
`--duration-seconds 0` runs until interrupted. Alpaca's free IEX feed is not a
consolidated SIP feed, and this stream does not yet obtain live option IV. It
is therefore an observation and timing layer only, not a trading signal.

### Current Limits

- No live orders, wallet access, private keys, or execution adapter exist.
- Public discovery may miss markets outside the configured tags or unusual
  contract templates.
- Settlement wording, Pyth reference details, event risk, liquidity, and fees
  require human review before any future execution work.
- Shadow results must be evaluated over sufficient settled markets before a
  controlled live-pilot proposal can be considered.

## 繁體中文

### 用途

這是一個針對 Polymarket 美股單日漲跌市場的純研究與 shadow 模擬工具。它會掃描市場、估算保守的基準機率，並記錄 shadow 重新評估事件。目前沒有錢包整合、私鑰讀取、下單程式或實盤交易路徑。

### 安裝與初始化

```zsh
cd /Users/cheng-kaihuang/Polymarket-stock
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

### 市場掃描與訂單簿觀察

```zsh
# 廣泛掃描單日美股市場；第二個指令會一併擷取訂單簿
polymarket-stock scan-equity-events --tag-slugs stocks,equities --max-pages-per-tag 100
polymarket-stock scan-equity-events --tag-slugs stocks,equities --snapshot-books

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

請把自己的 Alpaca 憑證填入 `.env`，不要提交至 Git：

```dotenv
ALPACA_API_KEY_ID=...
ALPACA_API_SECRET_KEY=...
```

```zsh
polymarket-stock stream-shadow --market-id 2958682 --symbol TSLA --duration-seconds 0
```

此指令會連接 Polymarket 公開 Market WebSocket 與 Alpaca Basic IEX 美股 WebSocket。它以 500 ms debounce 合併短時間內的訂單簿與現貨更新，並將 `SHADOW_REEVALUATION_REQUESTED` 事件寫入 `logs/shadow_bot.jsonl`。`--duration-seconds 0` 代表持續運行至手動中斷。免費的 Alpaca IEX 不是完整 SIP 整合報價，而且目前 stream 尚未取得即時期權 IV；它只負責觀察與觸發重新評估，並不是交易訊號。

### 目前限制

- 專案沒有實盤下單、錢包存取、私鑰或 execution adapter。
- 公開掃描可能遺漏設定標籤以外的市場或特殊市場模板。
- 結算文字、Pyth 參考價格、事件風險、流動性與費用，未來若要進入執行階段前都必須人工檢查。
- 需要累積足夠數量的已結算 shadow 結果，證明扣除成本後仍有穩定優勢，才可提出受控實盤試行方案。
