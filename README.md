# Polytrade — forward paper-trading harness for LLM market forecasting

A **measurement tool**, not a trading bot. It tests one question: *can an LLM
identify mispriced Polymarket markets?* It places **zero real trades**, needs **no
Polymarket credentials, wallet, or private keys**, and reads only public,
read-only data.

The only external account required is an Anthropic API key (so the model can
think and search the web). Everything Polymarket-side is anonymous GET requests.

---

## Why forward-testing, and why backtesting here is invalid

The naive way to "test" a forecaster is to point it at already-resolved markets
and check its hit rate. **That is invalid for an LLM**, for several compounding
reasons:

1. **Training-data leakage.** The model was trained on text from the past. For
   any market that has already resolved, the outcome is very likely *somewhere*
   in its training corpus (news recaps, Wikipedia, social media). Asking it to
   "predict" Bitcoin's price on a past date, or who won a past election, measures
   recall, not foresight.

2. **Search leakage.** Give the model a web-search tool and point it at a
   resolved event, and it will simply find the result. Even with a prompt that
   says "pretend it's last March," a single recap article collapses the exercise.

3. **Survivorship / selection bias.** The set of markets that *have* resolved is
   not a random sample of the markets that *will* resolve. Conditioning on
   resolution changes the distribution.

4. **No frozen decision price.** A real edge has to be measured against the price
   that was actually available *at decision time*. Reconstructing historical
   order books accurately is hard, and any slippage in that reconstruction
   silently flatters or punishes the model.

The only sound test is **forward**: make the prediction **before** the outcome is
known, freeze the market price at that moment, then wait for reality.

This harness enforces that discipline:

- It selects markets that resolve **14–45 days out** — far enough that the answer
  is genuinely unknown, near enough to score within the experiment.
- It **freezes `market_prob`** at decision time and never overwrites it.
- It instructs the model to reason only from information available **as of the
  fetch timestamp**, and to **ignore any source that appears to state the final
  resolution** (a guard against accidental leakage on near-resolved or
  already-reported events).
- It scores only **after** real-world resolution, re-fetched from Polymarket.

Even with these guards, treat the web-search guard as best-effort: the cleanest
signal comes from markets whose resolution genuinely postdates the prediction.

---

## Architecture

| Script | Role |
|---|---|
| `config.py` | Every threshold, the model choice, the liquidity filter, the edge cutoff — all knobs live here. |
| `polymarket.py` | Read-only client for the Gamma + CLOB public APIs. No writes, no auth. |
| `record.py` | SQLite schema and all DB read/write helpers (`markets` + `predictions` tables). |
| `fetch_markets.py` | Pull active, unresolved, liquid markets resolving in the window; snapshot them. |
| `analyze.py` | For each un-analyzed market, ask the model for `P(Yes)` (web search on), freeze a prediction. |
| `score.py` | Re-fetch resolutions, fill outcomes, compute Brier + calibration + hypothetical P&L. |

Data flow:

```
fetch_markets.py ──► markets table ──► analyze.py ──► predictions table ──► score.py
   (snapshot)                          (frozen decision)                    (resolve + score)
```

### Endpoints (verified live 2026-06-17)

- **Gamma Markets API** `GET https://gamma-api.polymarket.com/markets`
  (`closed=false&active=true&order=volume&ascending=false&limit=&offset=`) — list
  active markets with `outcomePrices`, `volume`, `endDate`, `clobTokenIds`, etc.
  `GET /markets/{id}` for a single market. A closed market reports
  `outcomePrices` as a near one-hot vector (e.g. `["1","0"]`); `["0.5","0.5"]`
  is a void.
- **CLOB API** `GET https://clob.polymarket.com/price?token_id=&side=BUY` (best
  ask) and `GET /book?token_id=` (order book). Used as a liquidity cross-check.

Both are public and require no authentication. The verification date is noted in
the source comments of `polymarket.py`.

---

## What gets measured

- **Brier score**, model vs. market, over resolved predictions
  (`(prob − outcome)²`, lower is better). The market is the benchmark to beat.
- **Calibration table** — model probabilities bucketed into deciles, showing
  predicted vs. actual hit rate. A well-calibrated forecaster's 70% bucket
  resolves Yes ~70% of the time.
- **Hypothetical P&L** — bet `$BET_SIZE` (default $100) on every resolved market
  where `|edge| > EDGE_THRESHOLD` (default 0.10), priced at the **frozen**
  `market_prob`. Buys "Yes" when the model thinks it's underpriced, "No" when
  overpriced. This is a bookkeeping exercise on paper — **no money moves.**

---

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # the ONLY credential needed
```

No Polymarket key, wallet, or seed phrase is required or accepted.

## Usage

```bash
# 1. Snapshot qualifying markets (run whenever you want fresh candidates)
python fetch_markets.py

# 2. Make + freeze predictions (idempotent: never re-predicts a market)
python analyze.py

#    ... wait days/weeks for markets to resolve ...

# 3. Resolve and score (safe to run repeatedly; fills outcomes as they settle)
python score.py
```

A typical cadence: run `fetch_markets.py` + `analyze.py` once or twice a week to
build up a sample, and `score.py` daily to pick up resolutions.

Each script prints a clean summary: markets analyzed, total token cost, current
open predictions, and scored results so far.

### Dashboard, status, and 24/7 automation (paper mode)

```bash
python status.py        # quick text snapshot (free, no network)
python dashboard.py     # visual dashboard in your browser (free, no network)
python run_cycle.py     # one full cycle: fetch -> analyze -> score -> dashboard -> alerts
python run_forever.py   # repeat run_cycle every CYCLE_INTERVAL_HOURS, with desktop alerts
```

`run_forever.py` is the hands-off paper-mode runner: it keeps making and scoring
predictions on a schedule and fires a macOS notification when a new market clears
`ALERT_EDGE_THRESHOLD` or when markets resolve. It still places **no real trades**.
It runs while the Terminal stays open and the Mac is awake; for reboot-proof or
off-machine operation you'd add a `launchd` job or a cloud server (separate step).

---

## Design guarantees

- **Idempotent.** `predictions.market_id` is the PRIMARY KEY; `analyze.py` skips
  any market already predicted and inserts via `INSERT OR IGNORE`. Re-running
  never double-inserts.
- **Frozen decision price.** `market_prob` is written once, at decision time, and
  never updated — the edge is always measured against the price the model
  actually faced.
- **Graceful failure.** API calls retry with exponential backoff; markets that
  fail to parse or fetch are logged and skipped, not fatal.
- **Rate limited.** A configurable sleep separates Anthropic calls.
- **Cost logged.** Estimated `$` cost (tokens + web searches) is recorded per
  prediction and summed per run and lifetime.

---

## Tuning

All in `config.py`: `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`),
`MIN_VOLUME_USD`, the `MIN/MAX_DAYS_TO_RESOLUTION` window, `MAX_ANALYZE_PER_RUN`,
`EDGE_THRESHOLD`, `BET_SIZE_USD`, rate-limit and backoff settings, and the list
prices used for cost estimation.

---

## Scope boundary (read before extending)

This project intentionally contains **no trade execution, no wallet handling, and
no private keys**. It is a forward-test measurement harness and nothing more.
Live trading — order signing, position management, on-chain interaction — would
be a **separate, explicitly-gated module** with its own review. Do not bolt
execution onto these scripts.
