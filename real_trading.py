"""real_trading.py — REAL-MONEY pilot on Polymarket. Places ACTUAL orders.

Safety model (all knobs in config.py):
  * Inert unless config.REAL_TRADING_ENABLED is True AND the wallet secrets
    exist in the environment (POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS,
    optional POLYMARKET_SIGNATURE_TYPE). Without them: no-op, status recorded.
  * Hard caps: ticket band, max new buys per cycle, max open positions.
  * Spendable cash is min(internal ledger, live on-chain USDC balance), so the
    engine can never commit more than the wallet actually holds.
  * Kill switch: if net realized P&L over the trailing 24h hits
    -REAL_DAILY_LOSS_LIMIT_USD, buying halts (exits still run) until
    `python3 real_trading.py --reset-kill-switch`.
  * Entry bar (REAL_ENTRY_CONVICTION) is stricter than the paper book's.
  * All orders are Fill-Or-Kill marketable limits: they either fill at the
    stated price or better, or do nothing — no resting orders left behind.

Fills are ledgered at the limit price (worst case); the live-balance clamp
corrects any drift in the engine's favor. Settlement note: Polymarket pays
resolved positions after they are CLAIMED in the UI — the ledger books the
win at resolution, but the USDC only becomes spendable once you claim it.
"""

import argparse
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import polymarket
import record

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (AssetType, BalanceAllowanceParams,
                                           OrderArgs, OrderType)
    from py_clob_client.order_builder.constants import BUY, SELL
    HAVE_CLOB = True
except ImportError:          # keeps every non-trading entry point importable
    HAVE_CLOB = False

KILL_KEY = "kill_switch_tripped_at"
STATUS_KEY = "status"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _credentials() -> Optional[dict]:
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
    if not pk or not funder:
        return None
    try:
        sig = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
    except ValueError:
        sig = 1
    return {"key": pk, "funder": funder, "signature_type": sig}


def _client(creds: dict) -> "ClobClient":
    client = ClobClient(
        config.CLOB_BASE,
        key=creds["key"],
        chain_id=137,                       # Polygon mainnet
        signature_type=creds["signature_type"],
        funder=creds["funder"],
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def _usdc_balance(client) -> float:
    """Live spendable USDC in the Polymarket account (6-decimal units)."""
    resp = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = (resp or {}).get("balance", "0")
    return float(raw) / 1_000_000.0


def _tick_size(client, token_id: str) -> float:
    try:
        return float(client.get_tick_size(token_id))
    except Exception:
        return 0.01


def _token_ids(market_id: str) -> tuple[Optional[str], Optional[str]]:
    """(yes_token_id, no_token_id) straight from Gamma, so SHORTs work even for
    markets stored before the no-token id was needed."""
    raw = polymarket.get_market(market_id)
    if not raw:
        return None, None
    ids = polymarket._parse_json_array(raw.get("clobTokenIds"))
    yes = str(ids[0]) if len(ids) > 0 else None
    no = str(ids[1]) if len(ids) > 1 else None
    return yes, no


def _round_to_tick(price: float, tick: float) -> float:
    return max(tick, min(1.0 - tick, round(round(price / tick) * tick, 4)))


def _place_fok(client, token_id: str, side: str, price: float, size: float):
    """Post a Fill-Or-Kill order. Returns (ok, order_id, error_text)."""
    try:
        order = client.create_order(OrderArgs(
            price=price, size=size, side=side, token_id=token_id))
        resp = client.post_order(order, OrderType.FOK)
        if isinstance(resp, dict) and resp.get("success"):
            return True, str(resp.get("orderID") or resp.get("orderId") or ""), ""
        return False, "", str(resp)
    except Exception as exc:
        return False, "", str(exc)


def _conviction(edge_abs: float, confidence: Optional[str]) -> float:
    weight = config.CONFIDENCE_WEIGHT.get((confidence or "med").lower(), 1.0)
    return edge_abs * weight


def _ticket(conviction: float) -> float:
    scale = conviction / config.REAL_ENTRY_CONVICTION if config.REAL_ENTRY_CONVICTION else 1.0
    return max(config.REAL_MIN_TICKET_USD,
               min(config.REAL_MIN_TICKET_USD * scale, config.REAL_MAX_TICKET_USD))


def _set_status(conn, text: str) -> None:
    record.real_meta_set(conn, STATUS_KEY, f"{text} | {_now_iso()}")
    conn.commit()
    print(f"  [real_trading] {text}")


def _ledger_cash(conn) -> float:
    delta = conn.execute(
        "SELECT COALESCE(SUM(cash_delta), 0) FROM real_trades").fetchone()[0]
    return config.REAL_STARTING_BANKROLL_USD + float(delta)


def _realized_last_24h(conn) -> float:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM real_trades "
        "WHERE realized_pnl IS NOT NULL AND timestamp >= ?", (cutoff,)).fetchone()
    return float(row[0])


def _record_trade(conn, **kw) -> None:
    conn.execute(
        """INSERT INTO real_trades (timestamp, market_id, question, action, side,
                                    shares, price, cash_delta, realized_pnl,
                                    reason, order_id)
           VALUES (:timestamp, :market_id, :question, :action, :side, :shares,
                   :price, :cash_delta, :realized_pnl, :reason, :order_id)""",
        {"realized_pnl": None, "order_id": None, **kw})


def run(conn) -> dict:
    """One real-money cycle: mark/exit/settle, then (maybe) enter. Never raises."""
    summary = {"enabled": False, "buys": [], "sells": [], "settles": [],
               "cash": 0.0, "equity": 0.0, "open": 0}

    if not config.REAL_TRADING_ENABLED:
        _set_status(conn, "DISABLED in config.py (REAL_TRADING_ENABLED = False)")
        return summary
    creds = _credentials()
    if not creds:
        _set_status(conn, "AWAITING CREDENTIALS — add POLYMARKET_PRIVATE_KEY and "
                          "POLYMARKET_FUNDER_ADDRESS as GitHub Actions secrets")
        return summary
    if not HAVE_CLOB:
        _set_status(conn, "py-clob-client is not installed (pip install -r requirements.txt)")
        return summary

    try:
        client = _client(creds)
        balance = _usdc_balance(client)
    except Exception as exc:
        hint = (" — if the key/address are right, try POLYMARKET_SIGNATURE_TYPE="
                f"{2 if creds['signature_type'] == 1 else 1}")
        _set_status(conn, f"CREDENTIAL CHECK FAILED: {str(exc)[:200]}{hint}")
        return summary

    summary["enabled"] = True
    now = _now_iso()
    cash = min(_ledger_cash(conn), balance)

    # ---- 1) mark to market, settle, and exit open real positions ----
    positions = conn.execute("SELECT * FROM real_positions").fetchall()
    for pos in positions:
        snap = polymarket.price_and_status(pos["market_id"])
        if not snap:
            continue
        yes = snap["yes_price"]
        side_price = yes if pos["side"] == "LONG" else 1.0 - yes
        shares = float(pos["shares"])
        cost = float(pos["cost_basis"])

        if snap["closed"]:
            outcome = snap["outcome"]
            final = outcome if pos["side"] == "LONG" else 1.0 - outcome
            proceeds = shares * final
            pnl = proceeds - cost
            _record_trade(conn, timestamp=now, market_id=pos["market_id"],
                          question=pos["question"], action="SETTLE",
                          side=pos["side"], shares=shares, price=final,
                          cash_delta=proceeds, realized_pnl=pnl,
                          reason="resolved (claim in the Polymarket UI)")
            conn.execute("DELETE FROM real_positions WHERE market_id = ?",
                         (pos["market_id"],))
            conn.commit()
            summary["settles"].append({"question": pos["question"], "pnl": pnl})
            continue

        conn.execute(
            "UPDATE real_positions SET last_price=?, last_value=?, last_marked=? "
            "WHERE market_id=?",
            (side_price, shares * side_price, now, pos["market_id"]))
        conn.commit()

        ret = (shares * side_price - cost) / cost if cost else 0.0
        model_fair = float(pos["model_prob"] or 0.5)
        edge_closed = (config.EXIT_ON_EDGE_CLOSED and
                       ((pos["side"] == "LONG" and yes >= model_fair) or
                        (pos["side"] == "SHORT" and yes <= model_fair)))
        reason = None
        if ret >= config.TAKE_PROFIT_PCT:
            reason = "take_profit"
        elif ret <= -config.STOP_LOSS_PCT:
            reason = "stop_loss"
        elif edge_closed:
            reason = "edge_closed"
        if not reason:
            continue

        token = pos["token_id"]
        best_bid = polymarket.get_clob_price(token, side="SELL") if token else None
        if not best_bid or best_bid <= 0:
            continue
        tick = _tick_size(client, token)
        limit = _round_to_tick(best_bid - config.REAL_PRICE_CUSHION_TICKS * tick, tick)
        ok, order_id, err = _place_fok(client, token, SELL, limit, shares)
        if not ok:
            print(f"    SELL failed ({pos['question'][:50]}): {err[:120]}")
            continue
        proceeds = shares * limit
        pnl = proceeds - cost
        _record_trade(conn, timestamp=now, market_id=pos["market_id"],
                      question=pos["question"], action="SELL", side=pos["side"],
                      shares=shares, price=limit, cash_delta=proceeds,
                      realized_pnl=pnl, reason=reason, order_id=order_id)
        conn.execute("DELETE FROM real_positions WHERE market_id = ?",
                     (pos["market_id"],))
        conn.commit()
        cash += proceeds
        summary["sells"].append({"question": pos["question"], "pnl": pnl,
                                 "reason": reason})

    # ---- 2) kill switch ----
    realized24 = _realized_last_24h(conn)
    if realized24 <= -config.REAL_DAILY_LOSS_LIMIT_USD and \
            not record.real_meta_get(conn, KILL_KEY):
        record.real_meta_set(conn, KILL_KEY, now)
        conn.commit()
    kill_tripped = record.real_meta_get(conn, KILL_KEY)

    # ---- 3) entries ----
    open_count = conn.execute("SELECT COUNT(*) FROM real_positions").fetchone()[0]
    if not kill_tripped:
        scored = []
        for cand in record.real_candidate_entries(conn):
            snap = polymarket.price_and_status(cand["market_id"])
            if not snap or snap["closed"]:
                continue
            yes = snap["yes_price"]
            edge = float(cand["model_prob"]) - yes
            conviction = _conviction(abs(edge), cand["model_confidence"])
            if conviction < config.REAL_ENTRY_CONVICTION:
                continue
            side = "LONG" if edge > 0 else "SHORT"
            side_price = yes if side == "LONG" else 1.0 - yes
            if not (config.MIN_ENTRY_PRICE <= side_price <= config.MAX_ENTRY_PRICE):
                continue
            scored.append((conviction, side, dict(cand)))
        scored.sort(key=lambda t: t[0], reverse=True)

        bought = 0
        for conviction, side, cand in scored:
            if bought >= config.REAL_MAX_BUYS_PER_CYCLE:
                break
            if open_count >= config.REAL_MAX_OPEN_POSITIONS:
                break
            if cash < config.REAL_MIN_TICKET_USD:
                break
            yes_tok, no_tok = _token_ids(cand["market_id"])
            token = yes_tok if side == "LONG" else no_tok
            if not token:
                continue
            best_ask = polymarket.get_clob_price(token, side="BUY")
            if not best_ask or not (config.MIN_ENTRY_PRICE <= best_ask
                                    <= config.MAX_ENTRY_PRICE):
                continue
            tick = _tick_size(client, token)
            limit = _round_to_tick(
                best_ask + config.REAL_PRICE_CUSHION_TICKS * tick, tick)
            stake = min(_ticket(conviction), cash, config.REAL_MAX_TICKET_USD)
            shares = math.floor(stake / limit * 100) / 100.0
            if shares < 5:                     # CLOB minimum order size
                shares = 5.0
            cost = shares * limit
            if cost > min(cash, config.REAL_MAX_TICKET_USD * 1.25):
                continue
            ok, order_id, err = _place_fok(client, token, BUY, limit, shares)
            if not ok:
                print(f"    BUY failed ({cand['question'][:50]}): {err[:120]}")
                continue
            conn.execute(
                """INSERT OR REPLACE INTO real_positions
                   (market_id, question, side, token_id, shares, entry_price,
                    cost_basis, model_prob, entry_timestamp, last_price,
                    last_value, last_marked)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cand["market_id"], cand["question"], side, token, shares,
                 limit, cost, float(cand["model_prob"]), now, limit, cost, now))
            _record_trade(conn, timestamp=now, market_id=cand["market_id"],
                          question=cand["question"], action="BUY", side=side,
                          shares=shares, price=limit, cash_delta=-cost,
                          reason="entry", order_id=order_id)
            conn.commit()
            cash -= cost
            open_count += 1
            bought += 1
            summary["buys"].append({"question": cand["question"], "side": side,
                                    "stake": cost})

    # ---- 4) snapshot ----
    pos_value = conn.execute(
        "SELECT COALESCE(SUM(last_value), 0) FROM real_positions").fetchone()[0]
    equity = cash + float(pos_value)
    conn.execute(
        "INSERT OR REPLACE INTO real_equity_curve "
        "(timestamp, cash, positions_value, total_value) VALUES (?,?,?,?)",
        (now, cash, float(pos_value), equity))
    summary.update({"cash": cash, "equity": equity, "open": open_count})
    state = "KILL SWITCH TRIPPED (exits only)" if kill_tripped else "LIVE"
    _set_status(conn, f"{state} — balance ${balance:.2f}, equity ${equity:.2f}, "
                      f"{open_count} open, 24h realized ${realized24:+.2f}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-money Polymarket pilot")
    parser.add_argument("--reset-kill-switch", action="store_true",
                        help="re-enable buying after a daily-loss halt")
    args = parser.parse_args()

    conn = record.connect()
    record.init_db(conn)
    if args.reset_kill_switch:
        record.real_meta_set(conn, KILL_KEY, None)
        conn.commit()
        print("kill switch reset — buying re-enabled next cycle")
    else:
        s = run(conn)
        print(f"real cycle: enabled={s['enabled']} buys={len(s['buys'])} "
              f"sells={len(s['sells'])} settles={len(s['settles'])} "
              f"equity=${s['equity']:.2f}")
    conn.close()


if __name__ == "__main__":
    main()
