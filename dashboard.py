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


def _equity_svg(values: list, starting: float) -> str:
    """Inline SVG line chart of total portfolio value over time."""
    if len(values) < 2:
        return ('<div class="empty">The equity timeline fills in once the simulator '
                'has run for a few cycles.</div>')
    W, H = 900, 220
    pl, pr, pt, pb = 56, 14, 14, 26
    pw, ph = W - pl - pr, H - pt - pb
    vmin = min(min(values), starting)
    vmax = max(max(values), starting)
    if vmax - vmin < 1e-9:
        vmin -= 1.0
        vmax += 1.0
    span = vmax - vmin
    n = len(values)

    def fx(i):
        return pl + pw * (i / (n - 1))

    def fy(v):
        return pt + ph * (1 - (v - vmin) / span)

    poly = " ".join(f"{fx(i):.1f},{fy(v):.1f}" for i, v in enumerate(values))
    last = values[-1]
    color = "#4ade80" if last >= starting else "#f87171"
    by = fy(starting)
    return (
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;background:#11161d;'
        f'border:1px solid #232a34;border-radius:10px">'
        f'<line x1="{pl}" y1="{by:.1f}" x2="{W - pr}" y2="{by:.1f}" stroke="#3a4452" '
        f'stroke-dasharray="4 4"/>'
        f'<text x="{pl - 6}" y="{by + 3:.1f}" fill="#8a94a3" font-size="11" '
        f'text-anchor="end">${starting:,.0f}</text>'
        f'<text x="{pl - 6}" y="{pt + 8:.1f}" fill="#8a94a3" font-size="11" '
        f'text-anchor="end">${vmax:,.0f}</text>'
        f'<text x="{pl - 6}" y="{pt + ph:.1f}" fill="#8a94a3" font-size="11" '
        f'text-anchor="end">${vmin:,.0f}</text>'
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2"/>'
        f'<circle cx="{fx(n - 1):.1f}" cy="{fy(last):.1f}" r="3.5" fill="{color}"/>'
        f'<text x="{W - pr}" y="{fy(last) - 7:.1f}" fill="{color}" font-size="12" '
        f'text-anchor="end">${last:,.0f}</text>'
        f'</svg>'
    )


def _portfolio_section(conn) -> str:
    pf = record.get_portfolio(conn)
    if not pf:
        return ('<h2>Paper portfolio</h2><div class="empty">The $'
                f'{config.STARTING_CAPITAL:,.0f} paper portfolio starts trading on the '
                'next cloud cycle.</div>')

    eq = record.get_equity_curve(conn)
    positions = record.open_positions(conn)
    trades = record.get_trades(conn, limit=30)
    realized = record.realized_pnl_total(conn)
    starting = float(pf["starting_cash"])
    cash = float(pf["cash"])
    if eq:
        total = float(eq[-1]["total_value"])
    else:
        total = cash + sum(float(p["last_value"] or 0) for p in positions)
    ret = (total - starting) / starting if starting else 0.0
    cls = "good" if total >= starting else "bad"

    cards = [
        _stat_card("Starting", f"${starting:,.0f}"),
        (f'<div class="card"><div class="label">Total equity</div>'
         f'<div class="value {cls}">${total:,.2f}</div>'
         f'<div class="sub {cls}">{ret:+.1%}</div></div>'),
        _stat_card("Cash", f"${cash:,.2f}"),
        _stat_card("Realized P&L", f"${realized:,.2f}"),
        _stat_card("Open positions", str(len(positions))),
    ]
    svg = _equity_svg([float(p["total_value"]) for p in eq], starting)

    if positions:
        prows = []
        for p in positions:
            cb = float(p["cost_basis"] or 0)
            lv = float(p["last_value"] if p["last_value"] is not None else cb)
            upct = (lv - cb) / cb if cb else 0.0
            pcls = "good" if lv >= cb else "bad"
            prows.append(
                f'<tr><td class="q">{html.escape((p["question"] or "")[:68])}</td>'
                f'<td>{html.escape(p["side"] or "")}</td>'
                f'<td>{float(p["entry_price"] or 0):.2f}</td>'
                f'<td>{float(p["last_price"] or 0):.2f}</td>'
                f'<td>${lv:,.0f}</td>'
                f'<td class="{pcls}">{upct:+.0%}</td></tr>'
            )
        pos_block = ('<h3>Open positions</h3><table><thead><tr><th>market</th>'
                     '<th>side</th><th>entry</th><th>now</th><th>value</th>'
                     '<th>P&amp;L</th></tr></thead><tbody>'
                     + "".join(prows) + '</tbody></table>')
    else:
        pos_block = '<h3>Open positions</h3><div class="empty">None open right now.</div>'

    if trades:
        trows = []
        for t in trades:
            pnl = t["realized_pnl"]
            pnl_txt = "" if pnl is None else f"{pnl:+.2f}"
            pnl_cls = "" if pnl is None else ("good" if pnl >= 0 else "bad")
            when = (t["timestamp"] or "")[:16].replace("T", " ")
            trows.append(
                f'<tr><td>{html.escape(when)}</td>'
                f'<td>{html.escape(t["action"] or "")}</td>'
                f'<td>{html.escape(t["side"] or "")}</td>'
                f'<td class="q">{html.escape((t["question"] or "")[:52])}</td>'
                f'<td>{float(t["price"] or 0):.2f}</td>'
                f'<td class="{pnl_cls}">{pnl_txt}</td>'
                f'<td>{html.escape(t["reason"] or "")}</td></tr>'
            )
        ledger_block = ('<h3>Movements <span class="hint">(most recent first)</span></h3>'
                        '<table><thead><tr><th>when (UTC)</th><th>action</th><th>side</th>'
                        '<th>market</th><th>price</th><th>P&amp;L</th><th>why</th></tr>'
                        '</thead><tbody>' + "".join(trows) + '</tbody></table>')
    else:
        ledger_block = ('<h3>Movements</h3><div class="empty">No trades yet — the first '
                        'positions open on the next cloud cycle.</div>')

    return (f'<h2>Paper portfolio <span class="hint">(fictional ${starting:,.0f} — '
            f'no real money)</span></h2>'
            f'<div class="cards">{"".join(cards)}</div>'
            f'<h3>Equity over time</h3>{svg}{pos_block}{ledger_block}')


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

    portfolio_block = _portfolio_section(conn)

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
  h3 {{ font-size: 13px; margin: 18px 0 6px; color: #c7d0db; }}
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
  {portfolio_block}
  <h2>Model vs. market <span class="hint">(the underlying forecasting test)</span></h2>
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
