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
# Lowered to widen the candidate pool so there are more bets to make.
MIN_VOLUME_USD = 10_000.0

# Resolution-date window: markets must resolve between N and M days from the
# fetch timestamp. Widened (sooner + further out) so many more markets qualify;
# the sooner end also raises turnover, which means more movement in the book.
MIN_DAYS_TO_RESOLUTION = 5
MAX_DAYS_TO_RESOLUTION = 75

# Stop after collecting this many qualifying markets in one fetch run.
MAX_MARKETS_PER_FETCH = 80

# How many markets the Gamma API returns per page while we scan.
GAMMA_PAGE_LIMIT = 100
# Hard cap on pages scanned. Kept under Gamma's deep-pagination limit (it returns
# HTTP 422 past a max offset ~2000); the fetch also stops gracefully if it hits it.
GAMMA_MAX_PAGES = 20

# --------------------------------------------------------------------------
# Analysis run limits (analyze.py)
# --------------------------------------------------------------------------
# Max markets to analyze in a single analyze.py run (cost guardrail).
MAX_ANALYZE_PER_RUN = 50

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

# Entry is gated on CONVICTION, not raw edge. Conviction blends the quantitative
# edge with the model's own qualitative confidence:
#     conviction = |live_edge| * CONFIDENCE_WEIGHT[model_confidence]
# so a high-confidence call clears the bar at a smaller edge than a low-confidence
# one, and the stake scales with the same number (surer bet -> bigger ticket).
CONFIDENCE_WEIGHT = {"high": 1.3, "med": 1.0, "low": 0.5}
# Open a position when conviction >= this. With the weights above: med enters at
# edge 0.06, high at ~0.046, low only at 0.12. Direction is LONG (buy "Yes") if
# the model thinks Yes is underpriced, SHORT (buy "No") if overpriced.
ENTRY_CONVICTION = 0.06
# Kept for scoring/alert displays that still speak in raw-edge terms.
TRADE_ENTRY_EDGE = 0.10
# Risk sizing: BASE stake as a fraction of current equity for an entry sitting
# right at ENTRY_CONVICTION. Stake scales linearly with conviction/ENTRY_CONVICTION,
# so a 2x-conviction bet gets ~2x the ticket — capped by MAX_POSITION_USD. Kept
# modest so many bets can be funded at once (diversification over concentration).
POSITION_SIZE_FRACTION = 0.03
# ...capped at this many dollars, and never more than available cash.
MAX_POSITION_USD = 120.0
# Never stake less than this on a bet (avoids meaningless dust positions). The
# sizer reserves this much per still-to-fund candidate so that all qualifying
# bets fit rather than the first few draining the cash.
MIN_TRADE_STAKE_USD = 5.0
# Skip a side priced outside this band (avoid illiquid longshots / no-upside).
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95
# Cap on simultaneous open positions. High enough that the cap rarely drops a
# genuinely good bet — cash + per-bet sizing is the real limiter, not this.
MAX_OPEN_POSITIONS = 100

# Rotation: when cash can't fund a clearly better candidate, sell the weakest
# open position (lowest remaining conviction) to free capital — but only if the
# newcomer beats it by at least ROTATE_MIN_IMPROVEMENT, and at most
# ROTATE_MAX_PER_CYCLE swaps per cycle so fees don't bleed the book.
ROTATE_ENABLED = True
ROTATE_MIN_IMPROVEMENT = 0.03
ROTATE_MAX_PER_CYCLE = 2

# "Close calls": candidates whose conviction lands in [CLOSE_CALL_CONVICTION_FLOOR,
# ENTRY_CONVICTION) — real signal, just under the auto-entry bar. Surfaced on the
# dashboard for a human to review qualitatively; never auto-traded.
CLOSE_CALL_CONVICTION_FLOOR = 0.035
# Human-chosen market_ids to force into the book regardless of the conviction bar
# (JSON list of id strings). Populated when you pick a close call to bet.
MANUAL_PICKS_PATH = "manual_picks.json"

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
