"""dashboard.py — generate a little visual dashboard from the local DB and open it.

Read-only and free: no network, no API key, no money. It reads polytrade.db,
writes dashboard.html, and opens it in your browser. Run it anytime:

    python3 dashboard.py

To refresh resolutions first (which markets have settled), run `python3 score.py`
beforehand — that's the step that talks to Polymarket. This script only displays
what's already stored.
"""

import html
import json
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


def _equity_svg(rows: list, starting: float) -> str:
    """Inline SVG line chart of total portfolio value over time.

    Hovering the chart snaps a crosshair to the nearest recorded point and shows
    that point's dollar value and timestamp. This is pure client-side rendering
    of data already in the DB — it makes no network calls and costs nothing."""
    if len(rows) < 2:
        return ('<div class="empty">The equity timeline fills in once the simulator '
                'has run for a few cycles.</div>')
    values = [float(r["total_value"]) for r in rows]
    dates = [(r["timestamp"] or "")[:16].replace("T", " ") for r in rows]
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

    # Points fed to the hover handler: [viewBox_x, viewBox_y, value, "date"].
    points_js = "[" + ",".join(
        f'[{fx(i):.1f},{fy(v):.1f},{v:.2f},"{dates[i]}"]'
        for i, v in enumerate(values)
    ) + "]"
    points_js = points_js.replace("</", "<\\/")  # guard against </script> breakout

    return (
        f'<div class="chartwrap">'
        f'<svg id="eqsvg" viewBox="0 0 {W} {H}" style="width:100%;height:auto;'
        f'background:#11161d;border:1px solid #232a34;border-radius:10px">'
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
        # hover crosshair + snap dot (hidden until the mouse is over the chart)
        f'<line id="eqguide" y1="{pt}" y2="{pt + ph}" stroke="#5b6675" '
        f'stroke-width="1" visibility="hidden"/>'
        f'<circle id="eqdot" r="4" fill="{color}" stroke="#0f1216" '
        f'stroke-width="1.5" visibility="hidden"/>'
        # transparent hit area on top so mouse events fire anywhere on the chart
        f'<rect id="eqhit" x="{pl}" y="{pt}" width="{pw}" height="{ph}" '
        f'fill="transparent" style="cursor:crosshair"/>'
        f'</svg>'
        f'<div id="eqtip" class="chart-tip"></div>'
        f'<script>(function(){{'
        f'var pts={points_js},W={W};'
        f'var svg=document.getElementById("eqsvg"),hit=document.getElementById("eqhit"),'
        f'guide=document.getElementById("eqguide"),dot=document.getElementById("eqdot"),'
        f'tip=document.getElementById("eqtip");'
        f'function fmt(v){{return "$"+Number(v).toLocaleString(undefined,'
        f'{{minimumFractionDigits:2,maximumFractionDigits:2}});}}'
        f'function move(e){{'
        f'var r=svg.getBoundingClientRect(),s=r.width/W;'
        f'var mx=(e.clientX-r.left)/s,best=0,bd=1e9;'
        f'for(var i=0;i<pts.length;i++){{var d=Math.abs(pts[i][0]-mx);'
        f'if(d<bd){{bd=d;best=i;}}}}'
        f'var p=pts[best];'
        f'guide.setAttribute("x1",p[0]);guide.setAttribute("x2",p[0]);'
        f'guide.setAttribute("visibility","visible");'
        f'dot.setAttribute("cx",p[0]);dot.setAttribute("cy",p[1]);'
        f'dot.setAttribute("visibility","visible");'
        f'tip.style.display="block";'
        f'tip.innerHTML="<b>"+fmt(p[2])+"</b><span>"+p[3]+" UTC</span>";'
        f'tip.style.left=(p[0]*s)+"px";tip.style.top=(p[1]*s)+"px";'
        f'}}'
        f'function leave(){{guide.setAttribute("visibility","hidden");'
        f'dot.setAttribute("visibility","hidden");tip.style.display="none";}}'
        f'hit.addEventListener("mousemove",move);'
        f'hit.addEventListener("mouseleave",leave);'
        f'}})();</script>'
        f'</div>'
    )


def _interpret_side(question: str, side: str) -> str:
    """Plain-language meaning of a LONG/SHORT bet, in terms of the actual question.

    LONG = the model bought 'Yes' (expects the market to resolve YES).
    SHORT = the model bought 'No' (expects the market to resolve NO)."""
    q = (question or "").strip().rstrip("?").strip()
    if side == "LONG":
        return (f'<b>LONG = betting YES.</b> The model thinks the market underprices this, '
                f'so it expects the answer to be <b>yes</b>: it <b>does</b> expect that '
                f'&ldquo;{html.escape(q)}&rdquo;.')
    if side == "SHORT":
        return (f'<b>SHORT = betting NO.</b> The model thinks the market overprices this, '
                f'so it expects the answer to be <b>no</b>: it does <b>not</b> expect that '
                f'&ldquo;{html.escape(q)}&rdquo;.')
    return html.escape(side or "")


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
    svg = _equity_svg(eq, starting)

    # Reasoning is already frozen in the predictions table (written by analyze.py).
    # Joining to it here just displays what's on disk — no model call, no extra cost.
    preds_by_id = {
        r["market_id"]: r
        for r in conn.execute("SELECT * FROM predictions").fetchall()
    }

    if positions:
        prows = []
        detail_map = {}
        for p in positions:
            mid = p["market_id"]
            cb = float(p["cost_basis"] or 0)
            lv = float(p["last_value"] if p["last_value"] is not None else cb)
            upct = (lv - cb) / cb if cb else 0.0
            pcls = "good" if lv >= cb else "bad"
            prows.append(
                f'<tr class="clickable" onclick="showPos(\'{html.escape(mid)}\')" '
                f'title="Click for the reasoning behind this trade">'
                f'<td class="q">{html.escape((p["question"] or "")[:68])}'
                f'<span class="why-chip">why?</span></td>'
                f'<td>{html.escape(p["side"] or "")}</td>'
                f'<td>{float(p["entry_price"] or 0):.2f}</td>'
                f'<td>{float(p["last_price"] or 0):.2f}</td>'
                f'<td>${lv:,.0f}</td>'
                f'<td class="{pcls}">{upct:+.0%}</td></tr>'
            )
            pred = preds_by_id.get(mid)
            reasoning = (pred["model_reasoning"] if pred else "") or (
                "No stored reasoning for this position.")
            conf = (pred["model_confidence"] if pred else "") or "-"
            model_p = float(pred["model_prob"]) if pred else float(p["model_prob"] or 0)
            market_p = float(pred["market_prob"]) if pred else None
            edge = float(pred["edge"]) if pred else None
            detail_map[mid] = {
                "question": p["question"] or "",
                "side": p["side"] or "",
                "interp": _interpret_side(p["question"] or "", p["side"] or ""),
                "model": model_p,
                "market": market_p,
                "edge": edge,
                "conf": conf,
                "reasoning": reasoning,
            }
        pos_block = ('<h3>Open positions <span class="hint">(click a row for the '
                     'reasoning &mdash; free, already stored)</span></h3>'
                     '<table><thead><tr><th>market</th>'
                     '<th>side</th><th>entry</th><th>now</th><th>value</th>'
                     '<th>P&amp;L</th></tr></thead><tbody>'
                     + "".join(prows) + '</tbody></table>')
        data_json = json.dumps(detail_map).replace("</", "<\\/")
        pos_block += (
            '<div id="posmodal" class="modal-backdrop" onclick="hidePos(event)">'
            '<div class="modal" onclick="event.stopPropagation()">'
            '<button class="modal-x" onclick="hidePos(event)" '
            'aria-label="Close">&times;</button>'
            '<div id="pm-body"></div></div></div>'
            f'<script>var POS={data_json};'
            'function showPos(id){var d=POS[id];if(!d)return;'
            'var pct=function(x){return (x==null)?"&ndash;":(x*100).toFixed(0)+"%";};'
            'var edge=(d.edge==null)?"&ndash;":((d.edge>=0?"+":"")+(d.edge*100).toFixed(0)+" pts");'
            'var esc=function(s){var e=document.createElement("div");e.textContent=s;'
            'return e.innerHTML;};'
            'var h="<h3 class=\\"pm-q\\">"+esc(d.question)+"</h3>";'
            'h+="<div class=\\"pm-interp\\">"+d.interp+"</div>";'
            'h+="<div class=\\"pm-nums\\">"+'
            '"<span>model P(yes) <b>"+pct(d.model)+"</b></span>"+'
            '"<span>market P(yes) <b>"+pct(d.market)+"</b></span>"+'
            '"<span>edge <b>"+edge+"</b></span>"+'
            '"<span>confidence <b>"+esc(d.conf)+"</b></span>"+"</div>";'
            'h+="<div class=\\"pm-label\\">Model reasoning at decision time</div>";'
            'h+="<div class=\\"pm-reason\\">"+esc(d.reasoning)+"</div>";'
            'document.getElementById("pm-body").innerHTML=h;'
            'document.getElementById("posmodal").style.display="flex";}'
            'function hidePos(e){if(e)e.stopPropagation();'
            'document.getElementById("posmodal").style.display="none";}'
            'document.addEventListener("keydown",function(e){'
            'if(e.key==="Escape")hidePos();});</script>'
        )
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
  /* interactive equity chart tooltip */
  .chartwrap {{ position: relative; }}
  .chart-tip {{ position: absolute; display: none; transform: translate(-50%, -125%);
               pointer-events: none; background: #0b0e12; border: 1px solid #333c48;
               border-radius: 8px; padding: 6px 9px; font-size: 12px; white-space: nowrap;
               box-shadow: 0 4px 14px rgba(0,0,0,.45); z-index: 5; }}
  .chart-tip b {{ display: block; font-size: 14px; color: #e7ecf2; }}
  .chart-tip span {{ color: #8a94a3; font-size: 11px; }}
  /* clickable open-position rows */
  tr.clickable {{ cursor: pointer; }}
  tr.clickable:hover td {{ background: #202632; }}
  .why-chip {{ margin-left: 8px; font-size: 10px; color: #8a94a3;
              border: 1px solid #37404d; border-radius: 999px; padding: 1px 7px;
              text-transform: uppercase; letter-spacing: .04em; vertical-align: middle; }}
  tr.clickable:hover .why-chip {{ color: #cdd5df; border-color: #4a5563; }}
  /* position-reasoning modal */
  .modal-backdrop {{ display: none; position: fixed; inset: 0; z-index: 50;
                    background: rgba(5,7,10,.66); align-items: center;
                    justify-content: center; padding: 20px; }}
  .modal {{ position: relative; background: #161b22; border: 1px solid #2a323d;
           border-radius: 14px; max-width: 640px; width: 100%; max-height: 82vh;
           overflow-y: auto; padding: 22px 24px 24px; box-shadow: 0 18px 50px rgba(0,0,0,.6); }}
  .modal-x {{ position: absolute; top: 10px; right: 12px; background: none; border: none;
             color: #8a94a3; font-size: 24px; line-height: 1; cursor: pointer; }}
  .modal-x:hover {{ color: #e7ecf2; }}
  .pm-q {{ font-size: 16px; color: #e7ecf2; margin: 2px 40px 12px 0; }}
  .pm-interp {{ background: #1c2531; border: 1px solid #2a3644; border-radius: 10px;
               padding: 11px 13px; font-size: 13.5px; line-height: 1.5; color: #d7dee8; }}
  .pm-nums {{ display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 14px 0 4px;
             font-size: 12.5px; color: #8a94a3; }}
  .pm-nums b {{ color: #e7ecf2; }}
  .pm-label {{ margin-top: 16px; color: #8a94a3; font-size: 11px; text-transform: uppercase;
              letter-spacing: .04em; }}
  .pm-reason {{ margin-top: 6px; font-size: 13.5px; line-height: 1.6; color: #c7d0db;
               white-space: pre-wrap; }}
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
