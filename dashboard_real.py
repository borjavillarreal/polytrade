"""dashboard_real.py — static dashboard for the REAL-money pilot.

Generates real_dashboard.html from the real_* tables; the cloud workflow
publishes it as real/index.html so it lives at <pages>/real/ next to the
paper dashboard at the site root. Pure read of on-disk data — no API calls.
"""

import html
from datetime import datetime, timezone

import config
import record

OUT = "real_dashboard.html"


def _card(label: str, value: str, sub: str = "", cls: str = "") -> str:
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return (f'<div class="card"><div class="label">{html.escape(label)}</div>'
            f'<div class="value {cls}">{value}</div>{sub_html}</div>')


def _equity_svg(rows) -> str:
    if len(rows) < 2:
        return ('<div class="empty">The equity curve appears after the first '
                'two cycles with the engine live.</div>')
    W, H, PL, PT = 860, 180, 52, 12
    pw, ph = W - PL - 14, H - PT - 30
    vals = [float(r["total_value"]) for r in rows]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    n = len(rows)
    pts, dots = [], []
    for i, r in enumerate(rows):
        x = PL + pw * (i / (n - 1))
        y = PT + ph * (1 - (float(r["total_value"]) - lo) / (hi - lo))
        pts.append(f"{x:.1f},{y:.1f}")
        ts = html.escape((r["timestamp"] or "")[:16].replace("T", " "))
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" class="dot">'
                    f'<title>${float(r["total_value"]):.2f} — {ts} UTC</title></circle>')
    base = config.REAL_STARTING_BANKROLL_USD
    yb = PT + ph * (1 - (base - lo) / (hi - lo)) if lo <= base <= hi else None
    baseline = (f'<line x1="{PL}" y1="{yb:.1f}" x2="{PL + pw}" y2="{yb:.1f}" '
                f'class="base"/>' if yb is not None else "")
    return (f'<svg viewBox="0 0 {W} {H}" class="chart">'
            f'<text x="4" y="{PT + 8}" class="lab">${hi:,.0f}</text>'
            f'<text x="4" y="{PT + ph}" class="lab">${lo:,.0f}</text>'
            f'{baseline}<polyline points="{" ".join(pts)}" class="line"/>'
            f'{"".join(dots)}</svg>')


def generate() -> None:
    conn = record.connect()
    record.init_db(conn)

    status = record.real_meta_get(conn, "status") or "no cycle has run yet"
    kill = record.real_meta_get(conn, "kill_switch_tripped_at")
    positions = conn.execute(
        "SELECT * FROM real_positions ORDER BY cost_basis DESC").fetchall()
    trades = conn.execute(
        "SELECT * FROM real_trades ORDER BY id DESC LIMIT 200").fetchall()
    curve = conn.execute(
        "SELECT * FROM real_equity_curve ORDER BY timestamp").fetchall()
    conn.close()

    starting = config.REAL_STARTING_BANKROLL_USD
    last = curve[-1] if curve else None
    equity = float(last["total_value"]) if last else starting
    cash = float(last["cash"]) if last else starting
    invested = float(last["positions_value"]) if last else 0.0
    net = equity - starting
    realized = sum(float(t["realized_pnl"] or 0) for t in trades)
    net_cls = "good" if net >= 0 else "bad"

    live = status.split(" | ")[0]
    if kill:
        banner_cls, banner = "warn", (f"KILL SWITCH TRIPPED {html.escape(kill[:16])} UTC "
                                      "— buying halted, exits still run. Reset: "
                                      "<code>python3 real_trading.py --reset-kill-switch</code>")
    elif live.startswith("LIVE"):
        banner_cls, banner = "ok", f"Engine status: {html.escape(status)}"
    else:
        banner_cls, banner = "warn", f"Engine status: {html.escape(status)}"

    pos_rows = []
    for p in positions:
        cb = float(p["cost_basis"] or 0)
        lv = float(p["last_value"] if p["last_value"] is not None else cb)
        pnl = lv - cb
        pct = (pnl / cb * 100) if cb else 0.0
        cls = "good" if pnl >= 0 else "bad"
        pos_rows.append(
            f'<tr><td class="q">{html.escape((p["question"] or "")[:90])}</td>'
            f'<td>{html.escape(p["side"] or "")}</td>'
            f'<td>${cb:.2f}</td><td>{float(p["entry_price"] or 0):.2f}</td>'
            f'<td>{float(p["last_price"] or 0):.2f}</td>'
            f'<td class="{cls}">${pnl:+.2f} ({pct:+.1f}%)</td>'
            f'<td>{html.escape((p["entry_timestamp"] or "")[:10])}</td></tr>')
    pos_block = (
        '<table><thead><tr><th>market</th><th>side</th><th>invested</th>'
        '<th>entry</th><th>now</th><th>P&amp;L</th><th>opened</th></tr></thead>'
        f'<tbody>{"".join(pos_rows)}</tbody></table>' if pos_rows else
        '<div class="empty">No open real positions.</div>')

    tr_rows = []
    for t in trades:
        pnl = t["realized_pnl"]
        pnl_html = (f'<span class="{"good" if pnl >= 0 else "bad"}">${pnl:+.2f}</span>'
                    if pnl is not None else "—")
        tr_rows.append(
            f'<tr><td>{html.escape((t["timestamp"] or "")[:16].replace("T", " "))}</td>'
            f'<td>{html.escape(t["action"] or "")}</td>'
            f'<td class="q">{html.escape((t["question"] or "")[:70])}</td>'
            f'<td>{html.escape(t["side"] or "")}</td>'
            f'<td>{float(t["price"] or 0):.2f}</td>'
            f'<td>${abs(float(t["cash_delta"] or 0)):.2f}</td>'
            f'<td>{pnl_html}</td>'
            f'<td>{html.escape(t["reason"] or "")}</td></tr>')
    ledger_block = (
        '<table><thead><tr><th>time (UTC)</th><th>action</th><th>market</th>'
        '<th>side</th><th>price</th><th>amount</th><th>realized</th>'
        '<th>why</th></tr></thead>'
        f'<tbody>{"".join(tr_rows)}</tbody></table>' if tr_rows else
        '<div class="empty">No real trades yet. The engine enters only when a '
        f'prediction clears conviction {config.REAL_ENTRY_CONVICTION:.2f} and '
        'all risk caps allow it.</div>')

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polytrade — real-money pilot</title>
<script>(function(){{try{{var t=localStorage.getItem("pt-theme")||"dark";
document.documentElement.setAttribute("data-theme",t);}}catch(e){{
document.documentElement.setAttribute("data-theme","dark");}}}})();</script>
<style>
:root, :root[data-theme="dark"] {{ --bg:#0f1216; --panel:#1a1f27; --border:#262d38;
  --text:#e7ecf2; --muted:#8a94a3; --head:#c7d0db; --good:#4ade80; --bad:#f87171;
  --accent:#6ea8fe; --ok-bg:#12271a; --ok-bd:#1f5131; --warn-bg:#2b2213;
  --warn-bd:#6b5420; --code:#232a34; color-scheme:dark; }}
:root[data-theme="light"] {{ --bg:#f2f0fb; --panel:#ffffff; --border:#e6e2f2;
  --text:#2e2a3b; --muted:#8d87a0; --head:#564f6e; --good:#2e9e62; --bad:#d96270;
  --accent:#7a6ff0; --ok-bg:#e7f6ec; --ok-bd:#bfe5cc; --warn-bg:#fdf3dc;
  --warn-bd:#eedca8; --code:#efecf9; color-scheme:light; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,
  BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.wrap {{ max-width:960px; margin:0 auto; padding:26px 16px 60px; }}
h1 {{ font-size:21px; margin:0; }} h2 {{ font-size:16px; margin:28px 0 10px; }}
.meta {{ color:var(--muted); font-size:12.5px; margin-top:3px; }}
.topbar {{ display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }}
.btn {{ background:var(--panel); color:var(--text); border:1px solid var(--border);
  border-radius:8px; padding:6px 12px; font-size:12.5px; cursor:pointer;
  text-decoration:none; display:inline-block; }}
.banner {{ border-radius:10px; padding:10px 14px; margin:16px 0; font-size:13px; }}
.banner.ok {{ background:var(--ok-bg); border:1px solid var(--ok-bd); }}
.banner.warn {{ background:var(--warn-bg); border:1px solid var(--warn-bd); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px; margin:14px 0; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px;
  padding:12px 14px; }}
.label {{ color:var(--muted); font-size:11.5px; text-transform:uppercase;
  letter-spacing:.05em; }}
.value {{ font-size:20px; font-weight:650; margin-top:3px; }}
.sub {{ color:var(--muted); font-size:12px; margin-top:2px; }}
.good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px; background:var(--panel);
  border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); }}
th {{ color:var(--head); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }}
tr:last-child td {{ border-bottom:none; }} .q {{ max-width:380px; }}
.empty {{ background:var(--panel); border:1px dashed var(--border); border-radius:10px;
  padding:16px; color:var(--muted); }}
code {{ background:var(--code); border-radius:5px; padding:1px 6px; font-size:12px; }}
.chart {{ width:100%; height:auto; background:var(--panel); border:1px solid var(--border);
  border-radius:10px; }}
.line {{ fill:none; stroke:var(--accent); stroke-width:2; }}
.dot {{ fill:var(--accent); }} .base {{ stroke:var(--muted); stroke-dasharray:4 4;
  stroke-width:1; opacity:.6; }}
.lab {{ fill:var(--muted); font-size:10px; }}
.note {{ color:var(--muted); font-size:12.5px; }}
@media (max-width:720px) {{ .q {{ max-width:170px; }} }}
</style></head><body><div class="wrap">
<div class="topbar"><div><h1>Polytrade — real-money pilot</h1>
<div class="meta">updated {updated} &middot; bankroll ${starting:,.0f} &middot;
tickets ${config.REAL_MIN_TICKET_USD:.0f}&ndash;${config.REAL_MAX_TICKET_USD:.0f}
&middot; entry bar {config.REAL_ENTRY_CONVICTION:.2f}
&middot; daily-loss halt ${config.REAL_DAILY_LOSS_LIMIT_USD:.0f}</div></div>
<div><a class="btn" href="../">&larr; Paper dashboard</a>
<button class="btn" id="themebtn" onclick="toggleTheme()">Theme</button></div></div>
<script>
function applyThemeLabel(t){{var b=document.getElementById("themebtn");
if(b)b.textContent=(t==="light")?"\\u2600 Light":"\\u263E Dark";}}
function toggleTheme(){{var d=document.documentElement;
var t=(d.getAttribute("data-theme")==="light")?"dark":"light";
d.setAttribute("data-theme",t);try{{localStorage.setItem("pt-theme",t);}}catch(e){{}}
applyThemeLabel(t);}}
applyThemeLabel(document.documentElement.getAttribute("data-theme")||"dark");
</script>
<div class="banner {banner_cls}">{banner}</div>
<div class="cards">
{_card("Equity", f"${equity:,.2f}", f"vs ${starting:,.0f} deposited", net_cls)}
{_card("Net P&L", f"${net:+,.2f}", f"{(net / starting * 100) if starting else 0:+.1f}%", net_cls)}
{_card("Cash", f"${cash:,.2f}")}
{_card("Invested", f"${invested:,.2f}", f"{len(positions)} open position(s)")}
{_card("Realized P&L", f"${realized:+,.2f}", "closed + settled",
       "good" if realized >= 0 else "bad")}
</div>
<h2>Equity over time</h2>
{_equity_svg(curve)}
<h2>Open positions</h2>
{pos_block}
<h2>Every movement</h2>
{ledger_block}
<p class="note">Orders are Fill-Or-Kill marketable limits placed by the automated
engine each cycle. Settled wins are booked here at resolution but the USDC
becomes spendable only after you claim the position in the Polymarket app.</p>
</div></body></html>"""

    with open(OUT, "w") as fh:
        fh.write(page)
    print(f"  [dashboard_real] wrote {OUT}")


if __name__ == "__main__":
    generate()
