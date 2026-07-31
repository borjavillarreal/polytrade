"""paper_trading.py — a fictional-money portfolio simulator. NO real trades.

Each cycle it:
  1. Marks every open position to the current Polymarket price.
  2. Exits positions that hit take-profit, stop-loss, the model's fair value, or
     that have resolved (settled to the real outcome).
  3. Opens new positions on markets where the model still sees a live edge,
     sized as a fraction of current equity and capped by available cash.
  4. Records the portfolio's total value to an equity-curve timeline.

Everything is fictional. There is no wallet, no order, no real money. This exists
to see how a $1,000 paper bankroll WOULD move before any real funds are involved.

Position model (uniform in the Yes price):
  LONG  = bought "Yes" at entry; value = shares * current_yes_price;
          settles to shares * outcome.
  SHORT = bought "No" at entry; value = shares * (1 - current_yes_price);
          settles to shares * (1 - outcome).
"""

import json
import os
from datetime import datetime, timezone

import config
import polymarket
import record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_manual_picks() -> list:
    """market_ids a human chose to force into the book (from MANUAL_PICKS_PATH).
    Accepts a JSON list of id strings, or of {"market_id": ...} objects."""
    path = getattr(config, "MANUAL_PICKS_PATH", "manual_picks.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return []
    picks = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            picks.append(item)
        elif isinstance(item, dict) and item.get("market_id"):
            picks.append(str(item["market_id"]))
    return picks


def _conviction(edge_abs: float, confidence: str) -> float:
    """How sure we are of a bet: quantitative edge scaled by the model's own
    qualitative confidence. This single number both gates entry (>= ENTRY_CONVICTION)
    and sizes the ticket — surer bets get bigger stakes, shakier ones smaller."""
    weight = config.CONFIDENCE_WEIGHT.get((confidence or "med").lower(), 1.0)
    return edge_abs * weight


def _target_stake(equity: float, conviction: float) -> float:
    """Base stake scaled by how far conviction beats the entry bar: a bet at
    exactly ENTRY_CONVICTION gets POSITION_SIZE_FRACTION of equity; 2x conviction
    gets ~2x that. Clamped to [MIN, MAX]."""
    ratio = conviction / config.ENTRY_CONVICTION if config.ENTRY_CONVICTION else 1.0
    raw = config.POSITION_SIZE_FRACTION * equity * ratio
    return max(config.MIN_TRADE_STAKE_USD, min(raw, config.MAX_POSITION_USD))


def _side_price(side: str, yes_price: float) -> float:
    """Current price of the side we hold, given the current Yes price."""
    return yes_price if side == "LONG" else (1.0 - yes_price)


def _settle_value(side: str, shares: float, outcome: float) -> float:
    """Cash a position settles to at resolution (outcome = resolved Yes prob)."""
    return shares * (outcome if side == "LONG" else (1.0 - outcome))


def run(conn) -> dict:
    """Run one paper-trading cycle. Returns a summary dict (also used for alerts)."""
    summary = {"buys": [], "sells": [], "settles": [], "equity": None,
               "cash": None, "return_pct": None, "open": 0, "enabled": False}
    if not config.PAPER_TRADING_ENABLED:
        return summary
    summary["enabled"] = True

    now = _now()
    record.ensure_portfolio(conn, config.STARTING_CAPITAL, now)
    pf = record.get_portfolio(conn)
    cash = float(pf["cash"])
    starting = float(pf["starting_cash"])

    # ---- 1 & 2: mark + exit open positions ----
    for pos in record.open_positions(conn):
        snap = None
        try:
            snap = polymarket.price_and_status(pos["market_id"])
        except RuntimeError:
            snap = None
        if not snap:
            continue  # couldn't price this cycle; leave the position untouched

        # Backfill the Polymarket URL slug (free: we already fetched this market).
        if snap.get("slug"):
            record.set_market_slug(conn, pos["market_id"], snap["slug"])

        side = pos["side"]
        shares = float(pos["shares"])
        cost_basis = float(pos["cost_basis"])
        yes_price = snap["yes_price"]

        # Resolved -> settle at the real outcome (no exit fee on settlement).
        if snap["closed"] and snap["outcome"] is not None:
            proceeds = _settle_value(side, shares, snap["outcome"])
            realized = proceeds - cost_basis
            cash += proceeds
            record.insert_trade(conn, {
                "timestamp": now, "market_id": pos["market_id"],
                "question": pos["question"], "action": "SETTLE", "side": side,
                "shares": shares, "price": snap["outcome"], "cash_delta": proceeds,
                "fee": 0.0, "realized_pnl": realized, "reason": "resolved",
            })
            record.close_position(conn, pos["market_id"])
            # Keep the predictions table in sync: a market can close BEFORE its
            # scheduled resolution_date, which score.py's date-gated pass would
            # otherwise miss. We already hold the outcome, so this is free.
            record.mark_resolved(conn, pos["market_id"], snap["outcome"], now)
            summary["settles"].append({"question": pos["question"], "pnl": realized})
            continue

        side_price = _side_price(side, yes_price)
        current_value = shares * side_price
        unreal_pct = (current_value - cost_basis) / cost_basis if cost_basis else 0.0

        hit_sl = unreal_pct <= -config.STOP_LOSS_PCT
        hit_tp = unreal_pct >= config.TAKE_PROFIT_PCT
        edge_closed = config.EXIT_ON_EDGE_CLOSED and (
            (side == "LONG" and yes_price >= pos["model_prob"]) or
            (side == "SHORT" and yes_price <= pos["model_prob"])
        )

        if hit_sl or hit_tp or edge_closed:
            fee = current_value * config.TRADE_FEE_PCT
            proceeds = current_value - fee
            realized = proceeds - cost_basis
            cash += proceeds
            reason = "stop_loss" if hit_sl else ("take_profit" if hit_tp else "edge_closed")
            record.insert_trade(conn, {
                "timestamp": now, "market_id": pos["market_id"],
                "question": pos["question"], "action": "SELL", "side": side,
                "shares": shares, "price": side_price, "cash_delta": proceeds,
                "fee": fee, "realized_pnl": realized, "reason": reason,
            })
            record.close_position(conn, pos["market_id"])
            summary["sells"].append({"question": pos["question"], "pnl": realized,
                                     "reason": reason})
        else:
            record.mark_position(conn, pos["market_id"], side_price, current_value, now)

    # ---- 3: open new positions ----
    open_count = len(record.open_positions(conn))
    positions_value = sum(float(p["last_value"] or 0) for p in record.open_positions(conn))
    equity = cash + positions_value
    fee_mult = 1.0 + config.TRADE_FEE_PCT

    # candidate pool: open predictions we neither hold nor have traded, priced live
    pool = []
    for row in record.candidate_entries(conn):
        price = float(row["current_price"])
        if 0.0 < price < 1.0:
            pool.append((row, price))
    pool_by_id = {row["market_id"]: (row, price) for row, price in pool}

    def _enter(row, price, live_edge, stake, reason):
        """Open one position for `stake` USD (fee on top). Mutates cash/open_count."""
        nonlocal cash, open_count
        side = "LONG" if live_edge > 0 else "SHORT"
        side_price = price if side == "LONG" else (1.0 - price)
        if side_price < config.MIN_ENTRY_PRICE or side_price > config.MAX_ENTRY_PRICE:
            return False
        fee = stake * config.TRADE_FEE_PCT
        if stake < config.MIN_TRADE_STAKE_USD or (stake + fee) > cash:
            return False
        shares = stake / side_price
        cash -= (stake + fee)
        open_count += 1
        record.insert_position(conn, {
            "market_id": row["market_id"], "question": row["question"], "side": side,
            "shares": shares, "entry_price": side_price, "cost_basis": stake,
            "model_prob": float(row["model_prob"]), "entry_timestamp": now,
            "last_price": side_price, "last_value": stake, "last_marked": now,
        })
        record.insert_trade(conn, {
            "timestamp": now, "market_id": row["market_id"], "question": row["question"],
            "action": "BUY", "side": side, "shares": shares, "price": side_price,
            "cash_delta": -(stake + fee), "fee": fee, "realized_pnl": None,
            "reason": reason,
        })
        summary["buys"].append({"question": row["question"], "side": side,
                                "stake": stake, "edge": live_edge, "reason": reason})
        return True

    # 3a: auto-entries — every candidate whose CONVICTION (edge x confidence)
    # clears the bar, surest first. Pre-filters the tradeable price band so
    # untouchable longshots don't clog the queue. Each entry reserves
    # MIN_TRADE_STAKE_USD for every still-to-fund candidate so a strong bet
    # can't starve the rest — all the good bets fit, just with scaled tickets.
    def _in_band(live_edge: float, price: float) -> bool:
        sp = price if live_edge > 0 else (1.0 - price)
        return config.MIN_ENTRY_PRICE <= sp <= config.MAX_ENTRY_PRICE

    qualifying = []
    for row, price in pool:
        live_edge = float(row["model_prob"]) - price
        conv = _conviction(abs(live_edge), row["model_confidence"])
        if conv >= config.ENTRY_CONVICTION and _in_band(live_edge, price):
            qualifying.append((conv, live_edge, row, price))
    qualifying.sort(key=lambda c: c[0], reverse=True)

    free = max(0, config.MAX_OPEN_POSITIONS - open_count)
    qualifying = qualifying[:free]
    unfunded = []
    for idx, (conv, live_edge, row, price) in enumerate(qualifying):
        remaining_after = len(qualifying) - idx - 1
        reserve = remaining_after * config.MIN_TRADE_STAKE_USD
        affordable = (cash / fee_mult) - reserve
        stake = min(_target_stake(equity, conv), affordable)
        if stake < config.MIN_TRADE_STAKE_USD or not _enter(row, price, live_edge, stake, "entry"):
            unfunded.append((conv, live_edge, row, price))

    # 3b: rotation — if cash couldn't fund a clearly better candidate, sell the
    # weakest open position (lowest REMAINING conviction toward the model's fair
    # value) to free capital. Only when the newcomer beats the weakest by
    # ROTATE_MIN_IMPROVEMENT, and at most ROTATE_MAX_PER_CYCLE swaps per cycle.
    if config.ROTATE_ENABLED and unfunded:
        confs = record.position_confidences(conn)
        rotations = 0
        for conv, live_edge, row, price in unfunded:
            if rotations >= config.ROTATE_MAX_PER_CYCLE:
                break
            held = []
            for pos in record.open_positions(conn):
                if pos["entry_timestamp"] == now:
                    continue  # never rotate out something bought this same cycle
                lp = float(pos["last_price"] or pos["entry_price"] or 0)
                yes = lp if pos["side"] == "LONG" else (1.0 - lp)
                rem_edge = (float(pos["model_prob"]) - yes if pos["side"] == "LONG"
                            else yes - float(pos["model_prob"]))
                held.append((_conviction(max(rem_edge, 0.0), confs.get(pos["market_id"])), pos))
            if not held:
                break
            held.sort(key=lambda h: h[0])
            weakest_conv, weakest = held[0]
            if conv < weakest_conv + config.ROTATE_MIN_IMPROVEMENT:
                break  # candidates are sorted; nothing further will qualify either
            value = float(weakest["last_value"] or weakest["cost_basis"] or 0)
            fee = value * config.TRADE_FEE_PCT
            proceeds = value - fee
            realized = proceeds - float(weakest["cost_basis"] or 0)
            cash += proceeds
            open_count -= 1
            record.insert_trade(conn, {
                "timestamp": now, "market_id": weakest["market_id"],
                "question": weakest["question"], "action": "SELL",
                "side": weakest["side"], "shares": float(weakest["shares"] or 0),
                "price": float(weakest["last_price"] or 0), "cash_delta": proceeds,
                "fee": fee, "realized_pnl": realized, "reason": "rotated",
            })
            record.close_position(conn, weakest["market_id"])
            summary["sells"].append({"question": weakest["question"],
                                     "pnl": realized, "reason": "rotated"})
            stake = min(_target_stake(equity, conv), cash / fee_mult)
            if _enter(row, price, live_edge, stake, "entry"):
                rotations += 1

    # 3c: manual picks — human-chosen close calls forced in, ignoring the bar.
    for pid in _load_manual_picks():
        if open_count >= config.MAX_OPEN_POSITIONS:
            break
        if pid not in pool_by_id:
            continue  # already held/traded/resolved, or unpriced
        row, price = pool_by_id[pid]
        live_edge = float(row["model_prob"]) - price
        conv = max(_conviction(abs(live_edge), row["model_confidence"]),
                   config.CLOSE_CALL_CONVICTION_FLOOR)
        stake = min(_target_stake(equity, conv), cash / fee_mult)
        _enter(row, price, live_edge, stake, "manual_pick")

    # ---- 4: snapshot equity to the timeline ----
    positions_value = sum(float(p["last_value"] or 0) for p in record.open_positions(conn))
    total = cash + positions_value
    record.set_cash(conn, cash, now)
    record.insert_equity_point(conn, now, cash, positions_value, total)
    conn.commit()

    summary.update({
        "equity": total, "cash": cash, "open": open_count,
        "return_pct": (total - starting) / starting if starting else 0.0,
    })
    return summary


def main() -> None:
    conn = record.connect()
    record.init_db(conn)
    s = run(conn)
    conn.close()
    if not s["enabled"]:
        print("paper trading disabled in config.")
        return
    print(f"paper trading: equity ${s['equity']:.2f} "
          f"({s['return_pct']:+.1%} vs ${config.STARTING_CAPITAL:.0f})  "
          f"cash ${s['cash']:.2f}  open {s['open']}  "
          f"buys {len(s['buys'])} sells {len(s['sells'])} settles {len(s['settles'])}")


if __name__ == "__main__":
    main()
