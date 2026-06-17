"""run_cycle.py — one full automated cycle. Paper mode: measures only, never trades.

In order, each step is safe and self-contained:
  1. fetch_markets  — refresh candidate markets                    (free)
  2. analyze        — make predictions on any NEW markets          (small API cost)
  3. score          — resolve any markets that have settled        (free)
  4. dashboard      — regenerate dashboard.html (no browser pop)   (free)
  5. alerts         — macOS notification for big NEW edges / new resolutions

NO real trades, NO wallet, NO private keys. This is the engine that
run_forever.py calls on a schedule.
"""

import os
import subprocess
from datetime import datetime, timezone

import config
import record
import fetch_markets
import analyze
import score
import dashboard


def _notify(title: str, message: str) -> None:
    """Best-effort macOS desktop notification."""
    if not config.ENABLE_DESKTOP_ALERTS:
        return
    safe = message.replace("\\", "").replace('"', "'")
    safe_title = title.replace("\\", "").replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "{safe_title}" sound name "Glass"'],
            check=False, capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass  # not on macOS, or osascript unavailable — alerts just no-op


def _prediction_ids(conn) -> set:
    return {r["market_id"] for r in conn.execute("SELECT market_id FROM predictions")}


def _resolved_ids(conn) -> set:
    return {r["market_id"] for r in
            conn.execute("SELECT market_id FROM predictions WHERE resolved = 1")}


def main() -> None:
    started = datetime.now(timezone.utc)
    print("#" * 64)
    print(f"# run_cycle  {started.isoformat()}")
    print("#" * 64)

    conn = record.connect()
    record.init_db(conn)
    pred_before = _prediction_ids(conn)
    resolved_before = _resolved_ids(conn)
    conn.close()

    # --- pipeline (each step manages its own DB connection + prints a summary) ---
    for name, fn in (("fetch_markets", fetch_markets.main),
                     ("analyze", analyze.main),
                     ("score", score.main)):
        try:
            fn()
        except SystemExit as exc:        # analyze exits if the API key is missing
            print(f"  [{name}] skipped: {exc}")
        except Exception as exc:          # never let one step kill the whole cycle
            print(f"  [{name}] failed: {exc}")

    try:
        dashboard.generate(open_browser=False)
    except Exception as exc:
        print(f"  [dashboard] failed: {exc}")

    # --- alerts: what changed this cycle ---
    conn = record.connect()
    big = conn.execute(
        "SELECT market_id, question, model_prob, market_prob, edge FROM predictions "
        "WHERE resolved = 0 AND ABS(edge) >= ?",
        (config.ALERT_EDGE_THRESHOLD,),
    ).fetchall()
    resolved_after = _resolved_ids(conn)
    conn.close()

    new_big = [r for r in big if r["market_id"] not in pred_before]
    newly_resolved = resolved_after - resolved_before

    if new_big:
        top = max(new_big, key=lambda r: abs(r["edge"]))
        _notify(
            f"Polytrade: {len(new_big)} new high-edge market(s)",
            f"Top: {top['question'][:80]} (edge {top['edge']:+.2f}, "
            f"model {top['model_prob']:.2f} vs market {top['market_prob']:.2f})",
        )
    if newly_resolved:
        _notify(
            f"Polytrade: {len(newly_resolved)} market(s) resolved",
            "Run the dashboard to see how the model scored.",
        )

    # Write this cycle's NEW alerts to a file. The cloud workflow turns a non-empty
    # alerts.txt into a GitHub issue that emails you. Cleared when nothing is new,
    # so you never get re-emailed about markets you've already been told about.
    alert_lines = []
    for r in sorted(new_big, key=lambda x: abs(x["edge"]), reverse=True):
        alert_lines.append(
            f"- {r['question']}\n    model {r['model_prob']:.2f} vs market "
            f"{r['market_prob']:.2f}  (edge {r['edge']:+.2f})"
        )
    if newly_resolved:
        alert_lines.append(
            f"- {len(newly_resolved)} market(s) just resolved — see the dashboard "
            "for how the model scored vs. the market."
        )
    if alert_lines:
        with open("alerts.txt", "w") as fh:
            fh.write("\n".join(alert_lines) + "\n")
    elif os.path.exists("alerts.txt"):
        os.remove("alerts.txt")

    print("-" * 64)
    print(f"cycle done in {(datetime.now(timezone.utc) - started).total_seconds():.0f}s  |  "
          f"new high-edge alerts: {len(new_big)}  |  newly resolved: {len(newly_resolved)}")
    print("-" * 64)


if __name__ == "__main__":
    main()
