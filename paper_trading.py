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

from datetime import datetime, timezone

import config
import polymarket
import record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # ---- 3: open new positions on live edges ----
    open_count = len(record.open_positions(conn))
    positions_value = sum(float(p["last_value"] or 0) for p in record.open_positions(conn))
    equity = cash + positions_value

    candidates = []
    for row in record.candidate_entries(conn):
        price = float(row["current_price"])
        if not (0.0 < price < 1.0):
            continue
        live_edge = float(row["model_prob"]) - price
        if abs(live_edge) >= config.TRADE_ENTRY_EDGE:
            candidates.append((abs(live_edge), live_edge, row, price))
    candidates.sort(key=lambda c: c[0], reverse=True)

    for _, live_edge, row, price in candidates:
        if open_count >= config.MAX_OPEN_POSITIONS:
            break
        side = "LONG" if live_edge > 0 else "SHORT"
        side_price = price if side == "LONG" else (1.0 - price)
        if side_price < config.MIN_ENTRY_PRICE or side_price > config.MAX_ENTRY_PRICE:
            continue

        stake = min(config.POSITION_SIZE_FRACTION * equity, config.MAX_POSITION_USD)
        affordable = cash / (1.0 + config.TRADE_FEE_PCT)
        stake = min(stake, affordable)
        if stake < 1.0:
            continue  # out of meaningful cash

        shares = stake / side_price
        fee = stake * config.TRADE_FEE_PCT
        cash -= (stake + fee)
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
            "reason": "entry",
        })
        open_count += 1
        summary["buys"].append({"question": row["question"], "side": side,
                                "stake": stake, "edge": live_edge})

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
