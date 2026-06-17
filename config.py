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
MIN_VOLUME_USD = 20_000.0

# Resolution-date window: markets must resolve between N and M days from the
# fetch timestamp. Short enough to score within the experiment, long enough that
# the outcome is genuinely uncertain.
MIN_DAYS_TO_RESOLUTION = 14
MAX_DAYS_TO_RESOLUTION = 45

# Stop after collecting this many qualifying markets in one fetch run.
MAX_MARKETS_PER_FETCH = 40

# How many markets the Gamma API returns per page while we scan.
GAMMA_PAGE_LIMIT = 100
# Hard cap on pages scanned, so a bad filter can't paginate forever.
GAMMA_MAX_PAGES = 40

# --------------------------------------------------------------------------
# Analysis run limits (analyze.py)
# --------------------------------------------------------------------------
# Max markets to analyze in a single analyze.py run (cost guardrail).
MAX_ANALYZE_PER_RUN = 25

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
