"""status.py — read-only snapshot of the harness. No API calls, no key needed.

Run it anytime to see how things stand: what's been fetched, which predictions
are still open, which have resolved and whether the model beat the market, and
the lifetime model cost. This never touches the network, so it's instant and free.

    python status.py             # summary + tables
    python status.py --reasoning # also print each prediction's reasoning
"""

import os
import sys

import config
import record


def _closer(model_prob, market_prob, outcome) -> str:
    """Who was nearer the realized outcome on this single market."""
    m = abs(model_prob - outcome)
    k = abs(market_prob - outcome)
    if abs(m - k) < 1e-9:
        return "tie"
    return "model" if m < k else "market"


def main() -> None:
    show_reasoning = "--reasoning" in sys.argv

    if not os.path.exists(config.DB_PATH):
        print(f"No database yet ({config.DB_PATH}).")
        print("Run:  python fetch_markets.py   then   python analyze.py")
        return

    conn = record.connect()
    record.init_db(conn)
    n_markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    preds = record.all_predictions(conn)
    cost = record.total_token_cost(conn)
    conn.close()

    resolved = [p for p in preds if p["resolved"]]
    open_preds = [p for p in preds if not p["resolved"]]

    print("=" * 72)
    print("Polytrade status   (read-only snapshot — no network, no API key)")
    print("=" * 72)
    print(f"  database              : {config.DB_PATH}")
    print(f"  model                 : {config.ANTHROPIC_MODEL}")
    print(f"  markets fetched       : {n_markets}")
    print(f"  predictions made      : {len(preds)}")
    print(f"    open (awaiting)     : {len(open_preds)}")
    print(f"    resolved & scored   : {len(resolved)}")
    print(f"  lifetime model cost   : ${cost:.4f}")

    if not preds:
        print("\nNo predictions yet — run  python analyze.py  to create some.")
        return

    if open_preds:
        print("\n--- Open predictions (awaiting real-world resolution) ---")
        print(f"  {'model':>6}{'market':>8}{'edge':>8}  {'conf':<5}{'resolves':<12}question")
        for p in open_preds:
            res = (p["resolution_date"] or "")[:10]
            print(f"  {p['model_prob']:>6.2f}{p['market_prob']:>8.2f}{p['edge']:>+8.2f}  "
                  f"{(p['model_confidence'] or '-'):<5}{res:<12}{p['question'][:40]}")

    if resolved:
        print("\n--- Resolved predictions (who landed closer to the truth) ---")
        print(f"  {'model':>6}{'market':>8}{'result':>8}{'closer':>9}  question")
        for p in resolved:
            outcome = p["outcome"]
            label = {1.0: "YES", 0.0: "NO"}.get(outcome, f"{outcome:.2f}")
            closer = _closer(p["model_prob"], p["market_prob"], outcome)
            print(f"  {p['model_prob']:>6.2f}{p['market_prob']:>8.2f}{label:>8}{closer:>9}  "
                  f"{p['question'][:38]}")
        wins = sum(1 for p in resolved
                   if _closer(p["model_prob"], p["market_prob"], p["outcome"]) == "model")
        print(f"\n  model landed closer than the market on {wins}/{len(resolved)} resolved markets")
        print("  (full Brier / calibration / hypothetical P&L:  python score.py)")

    if show_reasoning:
        print("\n--- Reasoning behind each prediction ---")
        for p in preds:
            print(f"\n[model {p['model_prob']:.2f} vs market {p['market_prob']:.2f}] "
                  f"{p['question'][:60]}")
            print(f"  {p['model_reasoning']}")


if __name__ == "__main__":
    main()
