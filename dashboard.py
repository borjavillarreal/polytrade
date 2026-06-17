"""dashboard.py — generate a little visual dashboard from the local DB and open it.

Read-only and free: no network, no API key, no money. It reads polytrade.db,
writes dashboard.html, and opens it in your browser. Run it anytime:

    python3 dashboard.py

To refresh resolutions first (which markets have settled), run `python3 score.py`
beforehand — that's the step that talks to Polymarket. This script only displays
what's already stored.
"""

import html
import os
import webbrowser
from datetime import datetime, timezone

import config
import record
import score


def _stat_card(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="sub">{html.escape(sub)}</div>' if sub else ""
    return (f'<div class="card"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(value)}</div>{sub_html}</div>')


def _build_html(conn) -> str:
    n_markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    preds = record.all_predictions(conn)
    resolved = [p for p in preds if p["resolved"]]
    open_preds = [p for p in preds if not p["resolved"]]
    cost = record.total_token_cost(conn)

    brier = score.brier_scores(resolved)
    pnl = score.hypothetical_pnl(resolved)
    calib = score.calibration_table(resolved)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ---- stat cards ----
    cards = [
        _stat_card("Markets fetched", str(n_markets)),
        _stat_card("Predictions made", str(len(preds))),
        _stat_card("Open", str(len(open_preds)), "awaiting resolution"),
        _stat_card("Resolved", str(len(resolved)), "scored"),
        _stat_card("Model cost so far", f"${cost:.2f}"),
    ]

    # ---- scoreboard ----
    if brier["n"]:
        delta = brier["market"] - brier["model"]
        winner = "model ahead" if delta > 0 else ("market ahead" if delta < 0 else "tie")
        cls = "good" if delta > 0 else ("bad" if delta < 0 else "")
        brier_block = (
            f'<div class="card wide"><div class="label">Brier score '
            f'(lower is better)</div>'
            f'<div class="row2"><span>model <b>{brier["model"]:.3f}</b></span>'
            f'<span>market <b>{brier["market"]:.3f}</b></span>'
            f'<span class="{cls}"><b>{winner}</b></span></div></div>'
        )
        roi = f'{pnl["roi"]:+.1%}' if pnl["roi"] is not None else "-"
        pnl_cls = "good" if pnl["pnl"] > 0 else ("bad" if pnl["pnl"] < 0 else "")
        pnl_block = (
            f'<div class="card wide"><div class="label">Hypothetical P&amp;L '
            f'(paper — ${config.BET_SIZE_USD:.0f}/bet when |edge| &gt; '
            f'{config.EDGE_THRESHOLD})</div>'
            f'<div class="row2"><span>bets <b>{pnl["placed"]}</b></span>'
            f'<span>P&amp;L <b class="{pnl_cls}">${pnl["pnl"]:,.0f}</b></span>'
            f'<span>ROI <b class="{pnl_cls}">{roi}</b></span></div></div>'
        )
        scoreboard = f'<div class="cards">{brier_block}{pnl_block}</div>'

        calib_rows = "".join(
            f'<tr><td>{html.escape(c["range"])}</td><td>{c["n"]}</td>'
            f'<td>{c["predicted"]:.2f}</td><td>{c["actual"]:.2f}</td></tr>'
            for c in calib if c["n"]
        )
        calib_block = (
            '<h2>Calibration (resolved predictions)</h2>'
            '<table><thead><tr><th>model prob bucket</th><th>n</th>'
            '<th>predicted</th><th>actual</th></tr></thead>'
            f'<tbody>{calib_rows}</tbody></table>'
        )
    else:
        scoreboard = ('<div class="empty">No markets have resolved yet — the '
                      'scoreboard fills in once predictions mature and you run '
                      '<code>score.py</code>.</div>')
        calib_block = ""

    # ---- open predictions table ----
    if open_preds:
        rows = "".join(
            f'<tr><td class="q">{html.escape(p["question"][:90])}</td>'
            f'<td>{p["model_prob"]:.2f}</td><td>{p["market_prob"]:.2f}</td>'
            f'<td class="{"good" if p["edge"] > 0 else "bad"}">{p["edge"]:+.2f}</td>'
            f'<td>{html.escape(p["model_confidence"] or "-")}</td>'
            f'<td>{html.escape((p["resolution_date"] or "")[:10])}</td></tr>'
            for p in sorted(open_preds, key=lambda r: abs(r["edge"]), reverse=True)
        )
        open_block = (
            '<h2>Open predictions <span class="hint">(sorted by edge size)</span></h2>'
            '<table><thead><tr><th>question</th><th>model</th><th>market</th>'
            '<th>edge</th><th>conf</th><th>resolves</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )
    else:
        open_block = ('<div class="empty">No open predictions yet. Run '
                      '<code>fetch_markets.py</code> then <code>analyze.py</code>.</div>')

    # ---- resolved predictions table ----
    resolved_block = ""
    if resolved:
        def _closer(p):
            m = abs(p["model_prob"] - p["outcome"])
            k = abs(p["market_prob"] - p["outcome"])
            return "tie" if abs(m - k) < 1e-9 else ("model" if m < k else "market")

        def _result_label(outcome):
            if outcome == 1.0:
                return "YES"
            if outcome == 0.0:
                return "NO"
            return f"{outcome:.2f}"

        res_rows = []
        for p in resolved:
            closer = _closer(p)
            cls = "good" if closer == "model" else ""
            res_rows.append(
                f'<tr><td class="q">{html.escape(p["question"][:90])}</td>'
                f'<td>{p["model_prob"]:.2f}</td><td>{p["market_prob"]:.2f}</td>'
                f'<td>{_result_label(p["outcome"])}</td>'
                f'<td class="{cls}">{closer}</td></tr>'
            )
        resolved_block = (
            '<h2>Resolved predictions</h2>'
            '<table><thead><tr><th>question</th><th>model</th><th>market</th>'
            '<th>result</th><th>closer</th></tr></thead>'
            f'<tbody>{"".join(res_rows)}</tbody></table>'
        )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Polytrade dashboard</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #0f1216; color: #e7ecf2; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }}
  h1 {{ font-size: 22px; margin: 0 0 2px; }}
  .meta {{ color: #8a94a3; font-size: 13px; margin-bottom: 22px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }}
  .card {{ background: #1a1f27; border: 1px solid #262d38; border-radius: 12px;
          padding: 14px 16px; min-width: 130px; flex: 1; }}
  .card.wide {{ flex-basis: 100%; }}
  .label {{ color: #8a94a3; font-size: 12px; text-transform: uppercase;
           letter-spacing: .04em; }}
  .value {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
  .sub {{ color: #8a94a3; font-size: 12px; }}
  .row2 {{ display: flex; gap: 26px; margin-top: 8px; font-size: 18px; }}
  h2 {{ font-size: 15px; margin: 26px 0 8px; }}
  .hint {{ color: #8a94a3; font-weight: 400; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: right; padding: 7px 10px; border-bottom: 1px solid #232a34; }}
  th:first-child, td.q {{ text-align: left; }}
  th {{ color: #8a94a3; font-weight: 600; font-size: 11px; text-transform: uppercase; }}
  .good {{ color: #4ade80; }}
  .bad {{ color: #f87171; }}
  .empty {{ background: #1a1f27; border: 1px dashed #2c3542; border-radius: 12px;
           padding: 18px; color: #9aa4b2; font-size: 14px; }}
  code {{ background: #232a34; padding: 1px 6px; border-radius: 5px; }}
  .foot {{ margin-top: 34px; color: #6b7480; font-size: 12px; }}
</style></head><body><div class="wrap">
  <h1>Polytrade dashboard</h1>
  <div class="meta">model {html.escape(config.ANTHROPIC_MODEL)} &middot; updated {updated}
    &middot; paper-trading measurement &mdash; no real trades</div>
  <div class="cards">{''.join(cards)}</div>
  {scoreboard}
  {open_block}
  {resolved_block}
  {calib_block}
  <div class="foot">Read-only snapshot. Refresh resolutions with
    <code>python3 score.py</code>, then re-run <code>python3 dashboard.py</code>.</div>
</div></body></html>"""


def generate(open_browser: bool = True):
    """Regenerate dashboard.html from the DB. Returns the path, or None if no DB."""
    if not os.path.exists(config.DB_PATH):
        print(f"No database yet ({config.DB_PATH}). Run fetch_markets.py + analyze.py first.")
        return None
    conn = record.connect()
    record.init_db(conn)
    out_html = _build_html(conn)
    conn.close()

    out_path = os.path.abspath("dashboard.html")
    with open(out_path, "w") as fh:
        fh.write(out_html)
    if open_browser:
        webbrowser.open("file://" + out_path)
    return out_path


def main() -> None:
    path = generate(open_browser=True)
    if path:
        print(f"Wrote {path}")
        print("Opened in your browser.")


if __name__ == "__main__":
    main()
