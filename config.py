"""Central configuration for the Polymarket forward paper-trading harness.

EVERY threshold, model choice, and knob lives here. Nothing in this project
places real trades, holds a wallet, or touches Polymarket credentials. It reads
public, read-only endpoints and the Anthropic API only.
"""

# --------------------------------------------------------------------------
# Anthropic / model
# --------------------------------------------------------------------------
# Model used by analyze.py. Default is Sonnet 4.6 per spec; override freely.
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Effort for adaptive thinking (low | medium | high | max). Forecasting benefits
# from some deliberation; medium is a reasonable cost/quality balance.
ANTHROPIC_EFFORT = "medium"

# Cap on output tokens per analysis call. Adaptive thinking + web-search summaries
# share this budget with the final JSON, so keep headroom to avoid truncation.
ANTHROPIC_MAX_TOKENS = 6000

# Let the model gather current context. Caps the number of web searches per call.
ENABLE_WEB_SEARCH = True
WEB_SEARCH_MAX_USES = 6

# Anthropic list price per MILLION tokens, used only to estimate $ cost per call
# for logging. Update if pricing changes (verified for claude-sonnet-4-6,
# 2026-06-17: $3.00 input / $15.00 output per MTok).
PRICE_INPUT_PER_MTOK = 3.00
PRICE_OUTPUT_PER_MTOK = 15.00
# Cached-input pricing (reads ~0.1x, writes ~1.25x). Used for cost logging only.
PRICE_CACHE_WRITE_PER_MTOK = 3.75
PRICE_CACHE_READ_PER_MTOK = 0.30
# Web search server-tool list price (per 1,000 searches), for cost logging.
PRICE_WEB_SEARCH_PER_1K = 10.00

# --------------------------------------------------------------------------
# Market selection (fetch_markets.py)
# --------------------------------------------------------------------------
# Only consider markets with at least this much total USD volume (liquidity proxy).
# Low floor on purpose: we want broad coverage incl. long shots, but skip markets
# with essentially no volume — those can't realistically be traded.
MIN_VOLUME_USD = 1_000.0

# Resolution-date window: markets must resolve between N and M days from the
# fetch timestamp. Wide window = a large pool with a mix of fast-resolving and
# longer-dated markets, which keeps the paper trader active and recycling capital.
MIN_DAYS_TO_RESOLUTION = 1
MAX_DAYS_TO_RESOLUTION = 120

# Stop after collecting this many qualifying markets in one fetch run. Storing a
# market is free (DB only); the real cost guard is the analysis budget below, so
# this is set high to build a broad candidate pool. Markets are volume-sorted, so
# the most liquid/tradeable ones are stored first.
MAX_MARKETS_PER_FETCH = 1000

# How many markets the Gamma API returns per page while we scan.
GAMMA_PAGE_LIMIT = 100
# Hard cap on pages scanned, so a bad filter can't paginate forever.
GAMMA_MAX_PAGES = 40

# --------------------------------------------------------------------------
# Analysis run limits (analyze.py)
# --------------------------------------------------------------------------
# Max markets to analyze in a single analyze.py run (count guardrail). The dollar
# budget caps below usually bind first, but this caps the loop length regardless.
MAX_ANALYZE_PER_RUN = 250

# --- ANALYSIS BUDGET CAPS (real $ spent on the Anthropic API) ----------------
# Each analysis (Sonnet + news web search) costs ~$0.10 on average. These caps are
# hard ceilings so a large market backlog can never run up a surprise bill.
#   * Per-cycle cap: analyze.py stops once this run's estimated spend reaches it.
#   * Lifetime cap: analyze.py stops once cumulative spend (all predictions ever,
#     summed from the DB) reaches it. This is the master safety limit.
MAX_ANALYSIS_USD_PER_CYCLE = 10.0
MAX_LIFETIME_ANALYSIS_USD = 30.0

# Seconds to sleep between Anthropic calls (simple client-side rate limiting).
ANALYZE_RATE_LIMIT_SECONDS = 2.0

# Retry/backoff for transient API failures (the SDK also retries internally).
API_MAX_RETRIES = 4
API_BACKOFF_BASE_SECONDS = 2.0
# Max times to resume a single call after a server-tool pause_turn.
MAX_PAUSE_CONTINUATIONS = 6

# --------------------------------------------------------------------------
# Scoring (score.py)
# --------------------------------------------------------------------------
# Hypothetical bet is placed only when |model_prob - market_prob| exceeds this.
EDGE_THRESHOLD = 0.10
# Stake per qualifying market in the hypothetical P&L.
BET_SIZE_USD = 100.0
# Number of calibration buckets (deciles by default).
CALIBRATION_BUCKETS = 10

# --------------------------------------------------------------------------
# Paper-trading simulator (paper_trading.py) — FICTIONAL money only.
# Simulates a portfolio: opens positions on the model's edge, takes profit,
# cuts losses, and settles at resolution. No wallet, no real funds, no orders.
# --------------------------------------------------------------------------
PAPER_TRADING_ENABLED = True
STARTING_CAPITAL = 1000.0          # fictional starting bankroll (USD)

# Entry: open a position when the LIVE edge (model_prob - current price) exceeds
# this. Direction is LONG (buy "Yes") if the model thinks Yes is underpriced,
# SHORT (buy "No") if overpriced. Lower = more active (more trades qualify).
TRADE_ENTRY_EDGE = 0.05
# Risk sizing: stake this fraction of current total equity per new position. Small
# so the bankroll spreads across many positions (diversification + deployment)...
POSITION_SIZE_FRACTION = 0.06
# ...capped at this many dollars, and never more than available cash.
MAX_POSITION_USD = 150.0
# Don't bother opening a position smaller than this (avoids dust trades).
MIN_TRADE_STAKE_USD = 5.0
# Price band for the side we'd buy. Wide on purpose to allow long shots; we still
# avoid the extreme tails where there's effectively no market.
MIN_ENTRY_PRICE = 0.02
MAX_ENTRY_PRICE = 0.98
# At most this many open positions at once (diversification + cash control).
MAX_OPEN_POSITIONS = 30

# --- Capital recycling --------------------------------------------------------
# When a new candidate's edge beats an open position's REMAINING edge by at least
# this margin, and there's no free cash (or we're at the position cap), sell the
# weakest open position(s) to fund the better trade. Keeps the bankroll working in
# the highest-edge opportunities instead of sitting idle.
ROTATE_ENABLED = True
ROTATE_EDGE_IMPROVEMENT = 0.05

# Exits (whichever triggers first):
TAKE_PROFIT_PCT = 0.40             # close when a position is up >= 40%
STOP_LOSS_PCT = 0.25               # close when a position is down >= 25%
# Also take profit when price reaches the model's fair value (edge captured).
EXIT_ON_EDGE_CLOSED = True

# Approximate round-trip friction (spread + slippage + fees) as a fraction of
# each trade's notional, charged on entry and on market exits. Keeps the sim
# from being unrealistically optimistic. Settlement at resolution is free.
TRADE_FEE_PCT = 0.01

# --------------------------------------------------------------------------
# 24/7 automation (run_cycle.py / run_forever.py) — paper mode, NO real trades
# --------------------------------------------------------------------------
# Hours between automated cycles in run_forever.py.
CYCLE_INTERVAL_HOURS = 6
# Fire a macOS desktop notification when a NEW prediction's |edge| is at least this.
ALERT_EDGE_THRESHOLD = 0.15
# Master switch for desktop notifications.
ENABLE_DESKTOP_ALERTS = True

# --------------------------------------------------------------------------
# Storage / endpoints (verified live 2026-06-17)
# --------------------------------------------------------------------------
DB_PATH = "polytrade.db"

# Gamma Markets API — public, no auth. Listing + single-market metadata.
GAMMA_BASE = "https://gamma-api.polymarket.com"
# CLOB API — public, no auth for reads. Order book / current price.
CLOB_BASE = "https://clob.polymarket.com"

# HTTP politeness.
HTTP_TIMEOUT_SECONDS = 30
HTTP_USER_AGENT = "polytrade-research/0.1 (read-only measurement tool)"
