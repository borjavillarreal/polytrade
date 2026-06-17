"""SQLite persistence layer for the harness.

Owns two tables:

  markets      -- snapshot of qualifying markets written by fetch_markets.py
  predictions  -- one row per (market_id) decision written by analyze.py

DESIGN RULE: predictions.market_prob is FROZEN at decision time and is never
overwritten. Re-running analyze.py must not double-insert: market_id is the
PRIMARY KEY of predictions, and inserts use INSERT OR IGNORE + an explicit
existence check.

This module never places trades. It only records measurements.
"""

import sqlite3
from typing import Iterable, Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id        TEXT PRIMARY KEY,
    condition_id     TEXT,
    question         TEXT NOT NULL,
    description      TEXT,               -- full resolution rules (decisive for forecasting)
    target_outcome   TEXT NOT NULL,      -- the outcome whose probability we measure
    yes_price        REAL NOT NULL,      -- market-implied P(target_outcome) at fetch
    volume           REAL,
    liquidity        REAL,
    yes_token_id     TEXT,
    resolution_date  TEXT,               -- ISO8601 endDate
    fetch_timestamp  TEXT NOT NULL       -- ISO8601 UTC when this snapshot was taken
);

CREATE TABLE IF NOT EXISTS predictions (
    market_id         TEXT PRIMARY KEY,  -- prevents double-insert (idempotency)
    question          TEXT NOT NULL,
    target_outcome    TEXT NOT NULL,
    model_prob        REAL NOT NULL,     -- model P(target_outcome resolves true)
    market_prob       REAL NOT NULL,     -- FROZEN market price at decision time
    edge              REAL NOT NULL,     -- model_prob - market_prob
    model_confidence  TEXT,              -- low | med | high
    model_reasoning   TEXT,
    model_name        TEXT,
    token_cost_usd    REAL,              -- estimated $ cost of the analysis call
    fetch_timestamp   TEXT NOT NULL,
    decision_timestamp TEXT NOT NULL,
    resolution_date   TEXT,
    resolved          INTEGER NOT NULL DEFAULT 0,   -- bool
    outcome           REAL,              -- resolved P(target): 0.0 / 0.5 / 1.0, NULL until resolved
    scored_timestamp  TEXT
);

-- ----- paper-trading simulator (FICTIONAL money) -----
CREATE TABLE IF NOT EXISTS portfolio (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    starting_cash  REAL NOT NULL,
    cash           REAL NOT NULL,         -- uninvested fictional cash
    created_at     TEXT NOT NULL,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    market_id        TEXT PRIMARY KEY,
    question         TEXT,
    side             TEXT,                -- LONG (bought Yes) | SHORT (bought No)
    shares           REAL,
    entry_price      REAL,                -- price of the side bought
    cost_basis       REAL,                -- cash spent on shares (excl. fee)
    model_prob       REAL,                -- model fair value of Yes (exit target)
    entry_timestamp  TEXT,
    last_price       REAL,                -- most recent price of the side (mark)
    last_value       REAL,                -- most recent mark-to-market value
    last_marked      TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    market_id     TEXT,
    question      TEXT,
    action        TEXT,                   -- BUY | SELL | SETTLE
    side          TEXT,                   -- LONG | SHORT
    shares        REAL,
    price         REAL,                   -- fill price of the side
    cash_delta    REAL,                   -- change to cash (negative for BUY)
    fee           REAL,
    realized_pnl  REAL,                   -- on SELL/SETTLE (NULL for BUY)
    reason        TEXT                    -- entry | take_profit | stop_loss |
                                          --   edge_closed | resolved
);

CREATE TABLE IF NOT EXISTS equity_curve (
    timestamp        TEXT PRIMARY KEY,
    cash             REAL,
    positions_value  REAL,
    total_value      REAL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(_SCHEMA)
        # lightweight migration: add columns introduced after a DB was first created
        cols = {r[1] for r in conn.execute("PRAGMA table_info(markets)").fetchall()}
        if "description" not in cols:
            conn.execute("ALTER TABLE markets ADD COLUMN description TEXT")
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------
# markets table (written by fetch_markets.py)
# --------------------------------------------------------------------------
def upsert_market(conn: sqlite3.Connection, m: dict, fetch_timestamp: str) -> None:
    """Insert or replace a market snapshot. Latest fetch wins for the snapshot;
    the frozen decision price lives in predictions, so refreshing here is safe."""
    conn.execute(
        """
        INSERT INTO markets (market_id, condition_id, question, description,
                             target_outcome, yes_price, volume, liquidity,
                             yes_token_id, resolution_date, fetch_timestamp)
        VALUES (:market_id, :condition_id, :question, :description,
                :target_outcome, :yes_price, :volume, :liquidity,
                :yes_token_id, :resolution_date, :fetch_timestamp)
        ON CONFLICT(market_id) DO UPDATE SET
            description=excluded.description,
            yes_price=excluded.yes_price,
            volume=excluded.volume,
            liquidity=excluded.liquidity,
            resolution_date=excluded.resolution_date,
            fetch_timestamp=excluded.fetch_timestamp
        """,
        {
            "market_id": m["market_id"],
            "condition_id": m.get("condition_id"),
            "question": m["question"],
            "description": m.get("description", ""),
            "target_outcome": m["target_outcome"],
            "yes_price": m["target_prob"],
            "volume": m.get("volume"),
            "liquidity": m.get("liquidity"),
            "yes_token_id": m.get("yes_token_id"),
            "resolution_date": m.get("resolution_date"),
            "fetch_timestamp": fetch_timestamp,
        },
    )


def markets_without_predictions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Markets that have been fetched but not yet analyzed (idempotency source)."""
    return conn.execute(
        """
        SELECT m.* FROM markets m
        LEFT JOIN predictions p ON p.market_id = m.market_id
        WHERE p.market_id IS NULL
        ORDER BY m.volume DESC
        """
    ).fetchall()


# --------------------------------------------------------------------------
# predictions table (written by analyze.py, updated by score.py)
# --------------------------------------------------------------------------
def prediction_exists(conn: sqlite3.Connection, market_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM predictions WHERE market_id = ?", (market_id,)
    ).fetchone()
    return row is not None


def insert_prediction(conn: sqlite3.Connection, pred: dict) -> bool:
    """Insert a frozen prediction. Returns False if one already existed
    (INSERT OR IGNORE on the PRIMARY KEY guarantees no double-insert)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO predictions (
            market_id, question, target_outcome, model_prob, market_prob, edge,
            model_confidence, model_reasoning, model_name, token_cost_usd,
            fetch_timestamp, decision_timestamp, resolution_date, resolved, outcome
        ) VALUES (
            :market_id, :question, :target_outcome, :model_prob, :market_prob, :edge,
            :model_confidence, :model_reasoning, :model_name, :token_cost_usd,
            :fetch_timestamp, :decision_timestamp, :resolution_date, 0, NULL
        )
        """,
        pred,
    )
    return cur.rowcount > 0


def unresolved_due(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """Unresolved predictions whose resolution_date has passed."""
    return conn.execute(
        """
        SELECT * FROM predictions
        WHERE resolved = 0
          AND resolution_date IS NOT NULL
          AND resolution_date <= ?
        ORDER BY resolution_date ASC
        """,
        (now_iso,),
    ).fetchall()


def mark_resolved(conn: sqlite3.Connection, market_id: str, outcome: float,
                  scored_timestamp: str) -> None:
    conn.execute(
        "UPDATE predictions SET resolved = 1, outcome = ?, scored_timestamp = ? "
        "WHERE market_id = ?",
        (outcome, scored_timestamp, market_id),
    )


def all_predictions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM predictions ORDER BY decision_timestamp").fetchall()


def resolved_predictions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM predictions WHERE resolved = 1 ORDER BY decision_timestamp"
    ).fetchall()


def open_prediction_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE resolved = 0"
    ).fetchone()[0]


def total_token_cost(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(token_cost_usd), 0) FROM predictions").fetchone()
    return float(row[0])


# --------------------------------------------------------------------------
# paper-trading simulator (FICTIONAL money)
# --------------------------------------------------------------------------
def ensure_portfolio(conn: sqlite3.Connection, starting_cash: float, now_iso: str) -> None:
    exists = conn.execute("SELECT 1 FROM portfolio WHERE id = 1").fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO portfolio (id, starting_cash, cash, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (starting_cash, starting_cash, now_iso, now_iso),
        )


def get_portfolio(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()


def set_cash(conn: sqlite3.Connection, cash: float, now_iso: str) -> None:
    conn.execute("UPDATE portfolio SET cash = ?, updated_at = ? WHERE id = 1", (cash, now_iso))


def open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions ORDER BY entry_timestamp").fetchall()


def held_market_ids(conn: sqlite3.Connection) -> set:
    return {r["market_id"] for r in conn.execute("SELECT market_id FROM positions")}


def insert_position(conn: sqlite3.Connection, pos: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO positions
            (market_id, question, side, shares, entry_price, cost_basis,
             model_prob, entry_timestamp, last_price, last_value, last_marked)
        VALUES (:market_id, :question, :side, :shares, :entry_price, :cost_basis,
                :model_prob, :entry_timestamp, :last_price, :last_value, :last_marked)
        """,
        pos,
    )


def mark_position(conn: sqlite3.Connection, market_id: str, last_price: float,
                  last_value: float, marked: str) -> None:
    conn.execute(
        "UPDATE positions SET last_price = ?, last_value = ?, last_marked = ? "
        "WHERE market_id = ?",
        (last_price, last_value, marked, market_id),
    )


def close_position(conn: sqlite3.Connection, market_id: str) -> None:
    conn.execute("DELETE FROM positions WHERE market_id = ?", (market_id,))


def insert_trade(conn: sqlite3.Connection, trade: dict) -> None:
    conn.execute(
        """
        INSERT INTO trades
            (timestamp, market_id, question, action, side, shares, price,
             cash_delta, fee, realized_pnl, reason)
        VALUES (:timestamp, :market_id, :question, :action, :side, :shares, :price,
                :cash_delta, :fee, :realized_pnl, :reason)
        """,
        trade,
    )


def insert_equity_point(conn: sqlite3.Connection, timestamp: str, cash: float,
                        positions_value: float, total_value: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO equity_curve (timestamp, cash, positions_value, total_value) "
        "VALUES (?, ?, ?, ?)",
        (timestamp, cash, positions_value, total_value),
    )


def get_trades(conn: sqlite3.Connection, limit: int = 0) -> list[sqlite3.Row]:
    q = "SELECT * FROM trades ORDER BY id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q).fetchall()


def get_equity_curve(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM equity_curve ORDER BY timestamp").fetchall()


def realized_pnl_total(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades WHERE realized_pnl IS NOT NULL"
    ).fetchone()
    return float(row[0])


def candidate_entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Open predictions we don't already hold, joined to a current market price."""
    return conn.execute(
        """
        SELECT p.market_id, p.question, p.model_prob, p.target_outcome,
               m.yes_price AS current_price
        FROM predictions p
        JOIN markets m ON m.market_id = p.market_id
        WHERE p.resolved = 0
          AND p.market_id NOT IN (SELECT market_id FROM positions)
          AND p.market_id NOT IN (SELECT DISTINCT market_id FROM trades)
        """
    ).fetchall()
