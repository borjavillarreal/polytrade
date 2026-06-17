"""score.py — resolve matured markets and score the model against the market.

Run this later (e.g. daily). It:
  1. Re-fetches resolution status for predictions where resolved=0 and the
     resolution_date has passed, and fills `outcome` once a market has closed.
  2. Computes Brier scores (model vs. market) over resolved predictions.
  3. Prints a calibration table (model_prob bucketed into deciles: predicted vs.
     actual hit rate).
  4. Computes hypothetical P&L from betting BET_SIZE on every resolved market
     where |edge| > EDGE_THRESHOLD, priced at the FROZEN market_prob.

Read-only against Polymarket; the only writes are filling resolved/outcome in
the local DB. No trades are placed.
"""

from datetime import datetime, timezone

import config
import polymarket
import record


# --------------------------------------------------------------------------
# Step 1: resolve matured markets
# --------------------------------------------------------------------------
def resolve_due(conn) -> tuple[int, int]:
    now_iso = datetime.now(timezone.utc).isoformat()
    due = record.unresolved_due(conn, now_iso)
    newly_resolved = 0
    still_open = 0
    for row in due:
        try:
            outcome = polymarket.resolution_for(row["market_id"])
        except RuntimeError as exc:
            print(f"  skip {row['market_id']}: fetch failed ({exc})")
            still_open += 1
            continue
        if outcome is None or not (0.0 <= outcome <= 1.0):
            still_open += 1  # past its date but not yet settled on-chain
            continue
        record.mark_resolved(conn, row["market_id"], outcome,
                             datetime.now(timezone.utc).isoformat())
        newly_resolved += 1
    conn.commit()
    return newly_resolved, still_open


# --------------------------------------------------------------------------
# Step 2: Brier scores
# --------------------------------------------------------------------------
def brier_scores(resolved) -> dict:
    if not resolved:
        return {"n": 0, "model": None, "market": None}
    model_sse = sum((r["model_prob"] - r["outcome"]) ** 2 for r in resolved)
    market_sse = sum((r["market_prob"] - r["outcome"]) ** 2 for r in resolved)
    n = len(resolved)
    return {"n": n, "model": model_sse / n, "market": market_sse / n}


# --------------------------------------------------------------------------
# Step 3: calibration table
# --------------------------------------------------------------------------
def calibration_table(resolved, buckets: int = config.CALIBRATION_BUCKETS) -> list[dict]:
    rows = []
    for b in range(buckets):
        lo = b / buckets
        hi = (b + 1) / buckets
        # last bucket is inclusive of 1.0
        in_bucket = [
            r for r in resolved
            if (lo <= r["model_prob"] < hi) or (b == buckets - 1 and r["model_prob"] == 1.0)
        ]
        if in_bucket:
            pred = sum(r["model_prob"] for r in in_bucket) / len(in_bucket)
            actual = sum(r["outcome"] for r in in_bucket) / len(in_bucket)
        else:
            pred = actual = None
        rows.append({
            "range": f"[{lo:.1f}, {hi:.1f}{']' if b == buckets - 1 else ')'}",
            "n": len(in_bucket),
            "predicted": pred,
            "actual": actual,
        })
    return rows


# --------------------------------------------------------------------------
# Step 4: hypothetical P&L
# --------------------------------------------------------------------------
def hypothetical_pnl(resolved) -> dict:
    """Bet BET_SIZE on every market where |edge| > EDGE_THRESHOLD, at frozen price.

    edge > 0  -> model thinks 'Yes' underpriced -> buy Yes shares at market_prob.
                 payoff per share = outcome (1 if Yes won, 0 if No, 0.5 if void).
    edge < 0  -> model thinks 'Yes' overpriced  -> buy No shares at (1-market_prob).
                 payoff per share = (1 - outcome).
    """
    bet = config.BET_SIZE_USD
    placed = 0
    wins = 0
    staked = 0.0
    pnl = 0.0
    details = []

    for r in resolved:
        edge = r["edge"]
        if abs(edge) <= config.EDGE_THRESHOLD:
            continue
        price = r["market_prob"]
        outcome = r["outcome"]

        if edge > 0:  # buy Yes
            if price <= 0.0 or price >= 1.0:
                continue  # degenerate price, no position
            shares = bet / price
            payoff = shares * outcome
            side = "YES"
        else:  # buy No
            no_price = 1.0 - price
            if no_price <= 0.0 or no_price >= 1.0:
                continue
            shares = bet / no_price
            payoff = shares * (1.0 - outcome)
            side = "NO"

        trade_pnl = payoff - bet
        placed += 1
        staked += bet
        pnl += trade_pnl
        if trade_pnl > 0:
            wins += 1
        details.append({
            "question": r["question"],
            "side": side,
            "price": price,
            "model_prob": r["model_prob"],
            "outcome": outcome,
            "pnl": trade_pnl,
        })

    roi = (pnl / staked) if staked else None
    return {
        "placed": placed,
        "wins": wins,
        "staked": staked,
        "pnl": pnl,
        "roi": roi,
        "details": details,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def main() -> None:
    conn = record.connect()
    record.init_db(conn)

    print("Resolving matured markets...")
    newly_resolved, still_open = resolve_due(conn)

    resolved = record.resolved_predictions(conn)
    all_preds = record.all_predictions(conn)
    open_count = record.open_prediction_count(conn)

    brier = brier_scores(resolved)
    calib = calibration_table(resolved)
    pnl = hypothetical_pnl(resolved)

    conn.close()

    print("=" * 64)
    print("score.py summary")
    print("=" * 64)
    print(f"  total predictions      : {len(all_preds)}")
    print(f"  open (unresolved)      : {open_count}")
    print(f"  newly resolved this run: {newly_resolved}")
    print(f"  past-date, not settled : {still_open}")
    print(f"  resolved & scored      : {len(resolved)}")

    print("\n--- Brier score (lower is better) ---")
    if brier["n"]:
        print(f"  n = {brier['n']}")
        print(f"  model  Brier : {brier['model']:.4f}")
        print(f"  market Brier : {brier['market']:.4f}")
        delta = brier["market"] - brier["model"]
        verdict = "model beats market" if delta > 0 else "market beats model"
        print(f"  edge         : {delta:+.4f}  ({verdict})")
    else:
        print("  (no resolved markets yet)")

    print("\n--- Calibration (model_prob deciles) ---")
    print(f"  {'bucket':<12}{'n':>5}{'predicted':>12}{'actual':>10}")
    for c in calib:
        if c["n"]:
            print(f"  {c['range']:<12}{c['n']:>5}{c['predicted']:>12.3f}{c['actual']:>10.3f}")
        else:
            print(f"  {c['range']:<12}{c['n']:>5}{'-':>12}{'-':>10}")

    print(f"\n--- Hypothetical P&L  (bet ${config.BET_SIZE_USD:.0f} when |edge| > "
          f"{config.EDGE_THRESHOLD}) ---")
    if pnl["placed"]:
        print(f"  bets placed   : {pnl['placed']}")
        print(f"  winning bets  : {pnl['wins']}  ({pnl['wins'] / pnl['placed']:.0%})")
        print(f"  total staked  : ${pnl['staked']:,.2f}")
        print(f"  total P&L     : ${pnl['pnl']:,.2f}")
        print(f"  ROI           : {pnl['roi']:+.1%}")
    else:
        print("  (no resolved markets cleared the edge threshold yet)")

    print("\nNOTE: This is a measurement of forecasting skill, not investment "
          "advice. No real trades were placed.")


if __name__ == "__main__":
    main()
