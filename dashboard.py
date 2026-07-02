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
import math
import os
import re
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
    # Color is driven by a CSS variable so the light/dark toggle recolors it too.
    line_var = "var(--good)" if last >= starting else "var(--bad)"
    by = fy(starting)

    # Points fed to the hover handler: [viewBox_x, viewBox_y, value, "date"].
    points_js = "[" + ",".join(
        f'[{fx(i):.1f},{fy(v):.1f},{v:.2f},"{dates[i]}"]'
        for i, v in enumerate(values)
    ) + "]"
    points_js = points_js.replace("</", "<\\/")  # guard against </script> breakout

    return (
        f'<div class="chartwrap">'
        f'<svg id="eqsvg" class="linechart" viewBox="0 0 {W} {H}" '
        f'style="width:100%;height:auto">'
        f'<line class="grid" x1="{pl}" y1="{by:.1f}" x2="{W - pr}" y2="{by:.1f}" '
        f'stroke-dasharray="4 4"/>'
        f'<text class="ax" x="{pl - 6}" y="{by + 3:.1f}" font-size="11" '
        f'text-anchor="end">${starting:,.0f}</text>'
        f'<text class="ax" x="{pl - 6}" y="{pt + 8:.1f}" font-size="11" '
        f'text-anchor="end">${vmax:,.0f}</text>'
        f'<text class="ax" x="{pl - 6}" y="{pt + ph:.1f}" font-size="11" '
        f'text-anchor="end">${vmin:,.0f}</text>'
        f'<polyline points="{poly}" fill="none" style="stroke:{line_var}" '
        f'stroke-width="2"/>'
        f'<circle cx="{fx(n - 1):.1f}" cy="{fy(last):.1f}" r="3.5" '
        f'style="fill:{line_var}"/>'
        f'<text x="{W - pr}" y="{fy(last) - 7:.1f}" style="fill:{line_var}" '
        f'font-size="12" text-anchor="end">${last:,.0f}</text>'
        # hover crosshair + snap dot (hidden until the mouse is over the chart)
        f'<line id="eqguide" class="crosshair" y1="{pt}" y2="{pt + ph}" '
        f'stroke-width="1" visibility="hidden"/>'
        f'<circle id="eqdot" r="4" style="fill:{line_var}" class="snapdot" '
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


def _money_signed(x: float) -> str:
    """'+$12.34' / '-$5.00' — sign first, then the dollar sign."""
    return f'{"+" if x >= 0 else "-"}${abs(x):,.2f}'


def _position_modal_html(question, side, model_p, market_p, edge, conf, reasoning,
                         url, value, pnl_d, pnl_pct, entry_p, now_p) -> str:
    """Pre-render the click-through detail for one open position (no JS assembly).

    Everything shown here is already on disk — the reasoning was frozen by
    analyze.py — so opening this costs nothing and makes no model call."""
    def _pct(x):
        return "&ndash;" if x is None else f"{x * 100:.0f}%"
    edge_txt = "&ndash;" if edge is None else f'{edge * 100:+.0f} pts'
    pcls = "good" if pnl_d >= 0 else "bad"
    return (
        f'<h3 class="pm-q">{html.escape(question)}</h3>'
        f'<div class="pm-interp">{_interpret_side(question, side)}</div>'
        f'<div class="pm-nums">'
        f'<span>side <b>{html.escape(side)}</b></span>'
        f'<span>value <b>${value:,.2f}</b></span>'
        f'<span>P&amp;L <b class="{pcls}">{_money_signed(pnl_d)} ({pnl_pct:+.0%})</b></span>'
        f'<span>entry <b>{entry_p:.2f}</b></span>'
        f'<span>now <b>{now_p:.2f}</b></span></div>'
        f'<div class="pm-nums">'
        f'<span>model P(yes) <b>{_pct(model_p)}</b></span>'
        f'<span>market P(yes) <b>{_pct(market_p)}</b></span>'
        f'<span>edge <b>{edge_txt}</b></span>'
        f'<span>confidence <b>{html.escape(conf)}</b></span></div>'
        f'<a class="pm-link" href="{html.escape(url)}" target="_blank" '
        f'rel="noopener noreferrer">View this market on Polymarket &#8599;</a>'
        f'<div class="pm-label">Model reasoning at decision time</div>'
        f'<div class="pm-reason">{html.escape(reasoning)}</div>'
    )


def _polymarket_url(question: str) -> str:
    """Best-effort Polymarket event URL from the question text.

    Polymarket event slugs are the lowercased title with apostrophes dropped and
    every other run of non-alphanumerics collapsed to a single hyphen. E.g.
    'Strait of Hormuz traffic returns to normal by July 15?' ->
    strait-of-hormuz-traffic-returns-to-normal-by-july-15."""
    s = (question or "").lower().replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return f"https://polymarket.com/event/{s}"


_PIE_COLORS = ["#6ea8fe", "#f4a261", "#4ade80", "#e879f9", "#facc15", "#f87171",
               "#34d399", "#a78bfa", "#fb923c", "#38bdf8", "#fb7185", "#a3e635"]


def _pie_svg(slices: list) -> str:
    """Donut chart of open positions sized by current value. Each slice reveals
    its market name (and value / share) ONLY on hover — no always-on labels.

    `slices` is a list of dicts: {name, value}. Pure client-side rendering."""
    slices = [s for s in slices if float(s["value"]) > 0]
    if not slices:
        return ('<div class="empty">No open positions to chart right now.</div>')
    total = sum(float(s["value"]) for s in slices)
    cx, cy, R, r = 130, 130, 116, 66
    paths = []
    data = []
    a0 = -math.pi / 2
    for i, s in enumerate(slices):
        val = float(s["value"])
        frac = val / total
        color = _PIE_COLORS[i % len(_PIE_COLORS)]
        if len(slices) == 1:
            # A single position is a full ring; arc math degenerates, so draw circles.
            seg = (f'<circle cx="{cx}" cy="{cy}" r="{(R + r) / 2:.1f}" fill="none" '
                   f'stroke="{color}" stroke-width="{R - r}" class="slice" '
                   f'data-i="{i}"/>')
        else:
            a1 = a0 + frac * 2 * math.pi
            x0, y0 = cx + R * math.cos(a0), cy + R * math.sin(a0)
            x1, y1 = cx + R * math.cos(a1), cy + R * math.sin(a1)
            xi1, yi1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            xi0, yi0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            large = 1 if (a1 - a0) > math.pi else 0
            seg = (f'<path d="M{x0:.2f},{y0:.2f} A{R},{R} 0 {large} 1 {x1:.2f},{y1:.2f} '
                   f'L{xi1:.2f},{yi1:.2f} A{r},{r} 0 {large} 0 {xi0:.2f},{yi0:.2f} Z" '
                   f'fill="{color}" class="slice" data-i="{i}"/>')
            a0 = a1
        paths.append(seg)
        data.append({"name": s["name"], "value": val, "pct": frac})
    data_json = json.dumps(data).replace("</", "<\\/")
    return (
        f'<div class="chartwrap piewrap">'
        f'<svg id="piesvg" viewBox="0 0 260 260" style="width:260px;max-width:100%;'
        f'height:auto">{"".join(paths)}'
        f'<text id="pie-cn" class="pie-center-n" x="130" y="126" text-anchor="middle">'
        f'{len(slices)}</text>'
        f'<text id="pie-cl" class="pie-center-l" x="130" y="144" text-anchor="middle">'
        f'open</text></svg>'
        f'<div id="pietip" class="chart-tip"></div>'
        f'<script>(function(){{var D={data_json};'
        f'var svg=document.getElementById("piesvg"),tip=document.getElementById("pietip"),'
        f'cn=document.getElementById("pie-cn"),cl=document.getElementById("pie-cl");'
        f'var slices=svg.querySelectorAll(".slice");'
        f'function money(v){{return "$"+Number(v).toLocaleString(undefined,'
        f'{{maximumFractionDigits:0}});}}'
        f'slices.forEach(function(el){{'
        f'el.addEventListener("mousemove",function(e){{'
        f'var d=D[+el.getAttribute("data-i")];'
        f'slices.forEach(function(o){{o.style.opacity=(o===el)?"1":"0.35";}});'
        f'cn.textContent=money(d.value);cl.textContent=(d.pct*100).toFixed(0)+"% of book";'
        f'var r=svg.getBoundingClientRect();'
        f'tip.style.display="block";tip.style.left=(e.clientX-r.left)+"px";'
        f'tip.style.top=(e.clientY-r.top)+"px";'
        f'tip.innerHTML="<b>"+d.name.replace(/</g,"&lt;")+"</b><span>"+money(d.value)+'
        f'" &middot; "+(d.pct*100).toFixed(0)+"%</span>";'
        f'}});'
        f'el.addEventListener("mouseleave",function(){{'
        f'slices.forEach(function(o){{o.style.opacity="1";}});'
        f'tip.style.display="none";cn.textContent="{len(slices)}";cl.textContent="open";'
        f'}});}});'
        f'}})();</script></div>'
    )


# Keyword heuristics for categorizing a market. Best-effort and FREE — pure
# string matching, no model call. First matching category wins (most specific
# first). Imperfect by nature; treat the distributions as a rough guide.
# Science before Sports so "Fields Medal" / "Nobel" beat the generic "medal".
_INDUSTRY_RULES = [
    ("Crypto", ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "dogecoin",
                "xrp", "coinbase", "binance", "stablecoin", "token", "blockchain")),
    ("Tech / AI", ("ai model", "a.i.", "gpt", "claude", "openai", "anthropic", "llm",
                   "chatgpt", "gemini", "grok", "nvidia", "chip", "software")),
    ("Science / Space", ("fields medal", "nobel", "rocket", "spacex", "mars", "nasa",
                         "vaccine", "climate", "hurricane", "earthquake", "starship")),
    ("Sports", ("world cup", "fifa", "nba", "nfl", "super bowl", "champions league",
                "premier league", "golden ball", "ballon", "olympic", "playoff",
                "tournament", "championship", "cup", "medal", "match", "grand prix")),
    ("Economy / Macro", ("inflation", "gdp", "the fed", "interest rate", "cpi", "jobs",
                         "unemployment", "recession", "rate cut", "rate hike", "tariff",
                         "debt ceiling", "stock", "s&p", "nasdaq", "earnings")),
    ("Geopolitics / Conflict", ("war", "ceasefire", "missile", "invasion", "sanction",
                                "hostage", "treaty", "nuclear", "troops", "military",
                                "withdrawal", "negotiation", "strait", "border",
                                "conflict", "airstrike", "mou")),
    ("Politics / Elections", ("election", "president", "senate", "parliament", "mayor",
                              "prime minister", "vote", "poll", "congress", "governor",
                              "referendum", "primary", "candidate", "nominee", "cabinet",
                              "coalition", "dissolved", "impeach")),
    ("Entertainment", ("album", "movie", "film", "box office", "grammy", "oscar",
                       "spotify", "gta", "netflix", "celebrity", "song", "single",
                       "tour", "release")),
]

_COUNTRY_RULES = [
    ("Iran", ("iran", "iranian", "hormuz", "tehran")),
    ("Israel", ("israel", "israeli", "knesset", "netanyahu", "gaza")),
    ("Ukraine", ("ukraine", "ukrainian", "kyiv", "kiev", "zelensky")),
    ("Russia", ("russia", "russian", "putin", "moscow", "kremlin")),
    ("United Kingdom", ("united kingdom", "uk", "britain", "british", "manchester",
                        "london", "england", "starmer", "sunak")),
    ("South Korea", ("south korea", "korean", "seoul")),
    ("France", ("france", "french", "macron", "paris")),
    ("China", ("china", "chinese", "beijing", "xi jinping", "taiwan")),
    ("Germany", ("germany", "german", "berlin", "scholz")),
    ("United States", ("united states", "u.s.", "usa", "american", "america", "trump",
                       "biden", "congress", "the fed", "white house", "supreme court")),
]


def _kw_in(text: str, kw: str) -> bool:
    """Whole-token match: kw must be bounded by non-alphanumerics on both sides.
    Prevents false hits like 'nfl' inside 'inflation' or 'uk' inside 'ukraine'."""
    start = 0
    while True:
        i = text.find(kw, start)
        if i == -1:
            return False
        before = text[i - 1] if i > 0 else " "
        after = text[i + len(kw)] if i + len(kw) < len(text) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = i + 1


def _classify(question: str, description: str = "") -> tuple[str, str]:
    """(industry, country) from whole-token keyword matching over question+description."""
    text = f" {(question or '').lower()} {(description or '').lower()} "
    industry = "Other"
    for label, kws in _INDUSTRY_RULES:
        if any(_kw_in(text, k) for k in kws):
            industry = label
            break
    country = "Global / Other"
    for label, kws in _COUNTRY_RULES:
        if any(_kw_in(text, k) for k in kws):
            country = label
            break
    return industry, country


def _dist_bars(title: str, hint: str, agg: dict) -> str:
    """Horizontal-bar breakdown. `agg` maps label -> [count, value]; bars are
    sized by value share and annotated with count and dollar value."""
    items = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)
    total = sum(v[1] for v in agg.values()) or 1.0
    rows = []
    for label, (count, value) in items:
        pct = value / total * 100.0
        rows.append(
            f'<div class="dist-row">'
            f'<span class="dist-label" title="{html.escape(label)}">{html.escape(label)}</span>'
            f'<span class="dist-track"><span class="dist-fill" style="width:{pct:.1f}%">'
            f'</span></span>'
            f'<span class="dist-meta">{count} &middot; ${value:,.0f}</span></div>'
        )
    return (f'<div class="dist"><div class="dist-title">{html.escape(title)} '
            f'<span class="hint">{html.escape(hint)}</span></div>{"".join(rows)}</div>')


def _trade_modal_html(t, pred, industry, country, url) -> str:
    """The 'why' behind one movement: the trade itself plus the model's frozen
    reasoning for that market (already on disk — no model call, no extra cost)."""
    when = (t["timestamp"] or "")[:16].replace("T", " ")
    pnl = t["realized_pnl"]
    pnl_html = ("&ndash;" if pnl is None
                else f'<b class="{"good" if pnl >= 0 else "bad"}">{_money_signed(float(pnl))}</b>')
    q = t["question"] or ""
    side = t["side"] or ""
    action = t["action"] or ""

    def _pct(x):
        return "&ndash;" if x is None else f"{float(x) * 100:.0f}%"
    if pred:
        model_p, market_p, edge = pred["model_prob"], pred["market_prob"], pred["edge"]
        conf = pred["model_confidence"] or "-"
        reasoning = pred["model_reasoning"] or "No stored reasoning for this market."
    else:
        model_p = market_p = edge = None
        conf = "-"
        reasoning = "No stored reasoning for this market."
    edge_txt = "&ndash;" if edge is None else f'{float(edge) * 100:+.0f} pts'
    interp = _interpret_side(q, side) if side in ("LONG", "SHORT") else ""
    interp_block = f'<div class="pm-interp">{interp}</div>' if interp else ""
    return (
        f'<h3 class="pm-q">{html.escape(q)}</h3>'
        f'<div class="pm-nums"><span>action <b>{html.escape(action)}</b></span>'
        f'<span>side <b>{html.escape(side)}</b></span>'
        f'<span>price <b>{float(t["price"] or 0):.2f}</b></span>'
        f'<span>realized P&amp;L {pnl_html}</span>'
        f'<span>reason <b>{html.escape(t["reason"] or "")}</b></span>'
        f'<span>when <b>{html.escape(when)} UTC</b></span></div>'
        f'{interp_block}'
        f'<div class="pm-nums"><span>model P(yes) <b>{_pct(model_p)}</b></span>'
        f'<span>market P(yes) <b>{_pct(market_p)}</b></span>'
        f'<span>edge <b>{edge_txt}</b></span>'
        f'<span>confidence <b>{html.escape(conf)}</b></span>'
        f'<span>industry <b>{html.escape(industry)}</b></span>'
        f'<span>country <b>{html.escape(country)}</b></span></div>'
        f'<a class="pm-link" href="{html.escape(url)}" target="_blank" '
        f'rel="noopener noreferrer">View this market on Polymarket &#8599;</a>'
        f'<div class="pm-label">Model reasoning at decision time</div>'
        f'<div class="pm-reason">{html.escape(reasoning)}</div>'
    )


def _filter_select(sid: str, label: str, options: list) -> str:
    """A labeled <select> whose first option clears the filter (value="")."""
    opts = [f'<option value="">{html.escape(label)}</option>']
    for o in options:
        opts.append(f'<option value="{html.escape(o)}">{html.escape(o)}</option>')
    return (f'<select id="{sid}" class="filter-sel" onchange="{_FILTER_FN[sid[:2]]}()">'
            + "".join(opts) + '</select>')


# which JS filter function each control triggers, keyed by id prefix
_FILTER_FN = {"fp": "filterPos", "fm": "filterMov"}


def _annualized(ret: float, created_at: str, now: datetime):
    """Linear (simple, non-compounding) annualization, per the user's mental model:
    +2% over one month reads as +24%/yr. Returns (annual_ret, days_elapsed)."""
    try:
        start = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None, None
    days = max((now - start).total_seconds() / 86400.0, 1e-9)
    if days < 0.5:
        return None, days  # too little history to extrapolate meaningfully
    return ret * (365.0 / days), days


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
    now = datetime.now(timezone.utc)
    ann, days = _annualized(ret, pf["created_at"], now)
    positions_value = sum(float(p["last_value"] or p["cost_basis"] or 0) for p in positions)
    unrealized = sum(
        float(p["last_value"] if p["last_value"] is not None else p["cost_basis"] or 0)
        - float(p["cost_basis"] or 0) for p in positions)

    # modal payloads (key -> pre-rendered HTML string), injected on click
    modals = {}

    cards = [
        _stat_card("Starting", f"${starting:,.0f}"),
        (f'<div class="card clickable-card" onclick="showModal(\'equity\')">'
         f'<div class="label">Total equity <span class="more">details ›</span></div>'
         f'<div class="value {cls}">${total:,.2f}</div>'
         f'<div class="sub {cls}">{ret:+.1%}</div></div>'),
        _stat_card("Cash", f"${cash:,.2f}"),
        (f'<div class="card clickable-card" onclick="showModal(\'realized\')">'
         f'<div class="label">Realized P&amp;L <span class="more">details ›</span></div>'
         f'<div class="value {"good" if realized >= 0 else "bad"}">${realized:,.2f}</div>'
         f'</div>'),
        (f'<div class="card clickable-card" onclick="showModal(\'open\')">'
         f'<div class="label">Open positions <span class="more">details ›</span></div>'
         f'<div class="value">{len(positions)}</div></div>'),
    ]
    svg = _equity_svg(eq, starting)

    # ---- Total-equity detail modal (ROI + linear annualized ROI + breakdown) ----
    ann_txt = f'{ann:+.1%}' if ann is not None else '&ndash;'
    days_txt = f'{days:.1f} days' if days is not None else '&ndash;'
    start_txt = (pf["created_at"] or "")[:10]
    modals["equity"] = (
        f'<h3 class="pm-q">Total equity &mdash; ${total:,.2f}</h3>'
        f'<div class="pm-nums">'
        f'<span>starting <b>${starting:,.0f}</b></span>'
        f'<span>return so far <b class="{cls}">{ret:+.1%}</b></span>'
        f'<span>annualized (linear) <b class="{cls}">{ann_txt}</b></span></div>'
        f'<div class="pm-nums">'
        f'<span>held for <b>{days_txt}</b></span>'
        f'<span>since <b>{html.escape(start_txt)}</b></span></div>'
        f'<div class="pm-nums">'
        f'<span>cash <b>${cash:,.2f}</b></span>'
        f'<span>positions value <b>${positions_value:,.2f}</b></span>'
        f'<span>realized P&amp;L <b class="{"good" if realized >= 0 else "bad"}">'
        f'{_money_signed(realized)}</b></span>'
        f'<span>unrealized P&amp;L <b class="{"good" if unrealized >= 0 else "bad"}">'
        f'{_money_signed(unrealized)}</b></span></div>'
        f'<div class="pm-note">Annualized is a simple linear extrapolation of the '
        f'return so far (e.g. +2% in a month reads as +24%/yr), not a compounded '
        f'CAGR. All figures are fictional paper money.</div>'
    )

    # Reasoning is already frozen in the predictions table (written by analyze.py).
    # Joining to it here just displays what's on disk — no model call, no extra cost.
    preds_by_id = {
        r["market_id"]: r
        for r in conn.execute("SELECT * FROM predictions").fetchall()
    }
    # descriptions help the (free, heuristic) industry/country classifier
    desc_by_id = {
        r["market_id"]: (r["description"] or "")
        for r in conn.execute("SELECT market_id, description FROM markets").fetchall()
    }
    cls_by_id = {}  # market_id -> (industry, country), memoized for reuse in movements

    def _class_for(mid, q):
        if mid not in cls_by_id:
            cls_by_id[mid] = _classify(q, desc_by_id.get(mid, ""))
        return cls_by_id[mid]

    if positions:
        prows = []
        open_rows = []   # richer rows for the "Open positions" detail modal
        pie_slices = []
        # distribution accumulators: label -> [count, value]
        dist_ind, dist_country, dist_conf, dist_side = ({} for _ in range(4))

        def _bump(agg, label, value):
            slot = agg.setdefault(label, [0, 0.0])
            slot[0] += 1
            slot[1] += value

        for p in positions:
            mid = p["market_id"]
            cb = float(p["cost_basis"] or 0)
            lv = float(p["last_value"] if p["last_value"] is not None else cb)
            pnl_d = lv - cb
            upct = pnl_d / cb if cb else 0.0
            pcls = "good" if pnl_d >= 0 else "bad"
            side = p["side"] or ""
            entry_p = float(p["entry_price"] or 0)
            now_p = float(p["last_price"] or 0)
            q = p["question"] or ""
            pred = preds_by_id.get(mid)
            conf = (pred["model_confidence"] if pred else "") or "-"
            industry, country = _class_for(mid, q)

            _bump(dist_ind, industry, lv)
            _bump(dist_country, country, lv)
            _bump(dist_conf, conf, lv)
            _bump(dist_side, side or "-", lv)

            data_attrs = (f'data-side="{html.escape(side)}" data-conf="{html.escape(conf)}" '
                          f'data-ind="{html.escape(industry)}" '
                          f'data-country="{html.escape(country)}" data-amount="{cb:.2f}"')
            prows.append(
                f'<tr class="clickable" {data_attrs} '
                f'onclick="showModal(\'pos:{html.escape(mid)}\')" '
                f'title="Click for the reasoning behind this trade">'
                f'<td class="q">{html.escape(q[:60])}'
                f'<span class="why-chip">why?</span></td>'
                f'<td>{html.escape(side)}</td>'
                f'<td>{html.escape(conf)}</td>'
                f'<td>{entry_p:.2f}</td>'
                f'<td>{now_p:.2f}</td>'
                f'<td>${lv:,.0f}</td>'
                f'<td class="{pcls}">{_money_signed(pnl_d)}</td>'
                f'<td class="{pcls}">{upct:+.0%}</td></tr>'
            )
            open_rows.append(
                f'<tr><td class="q">{html.escape(q[:70])}</td><td>{html.escape(side)}</td>'
                f'<td>{html.escape(industry)}</td><td>{html.escape(country)}</td>'
                f'<td>{html.escape(conf)}</td>'
                f'<td>${cb:,.2f}</td><td>${lv:,.2f}</td>'
                f'<td class="{pcls}">{_money_signed(pnl_d)}</td>'
                f'<td class="{pcls}">{upct:+.0%}</td></tr>'
            )
            pie_slices.append({"name": q, "value": lv})

            reasoning = (pred["model_reasoning"] if pred else "") or (
                "No stored reasoning for this position.")
            model_p = float(pred["model_prob"]) if pred else float(p["model_prob"] or 0)
            market_p = float(pred["market_prob"]) if pred else None
            edge = float(pred["edge"]) if pred else None
            modals[f"pos:{mid}"] = _position_modal_html(
                q, side, model_p, market_p, edge, conf, reasoning,
                _polymarket_url(q), lv, pnl_d, upct, entry_p, now_p)

        industries = sorted({r[0] for r in cls_by_id.values()})
        countries = sorted({r[1] for r in cls_by_id.values()})
        confs = [c for c in ("high", "med", "low", "-") if c in dist_conf]
        filt = (
            '<div class="filters">'
            + _filter_select("fp-side", "All sides", ["LONG", "SHORT"])
            + _filter_select("fp-conf", "All confidence", confs)
            + _filter_select("fp-ind", "All industries", industries)
            + _filter_select("fp-country", "All countries", countries)
            + '<input id="fp-min" class="filter-num" type="number" min="0" step="1" '
              'placeholder="min $ invested" oninput="filterPos()">'
            + '<button class="filter-reset" onclick="resetPos()">reset</button>'
            + '<span id="fp-count" class="hint"></span></div>'
        )
        pos_block = ('<h3>Open positions <span class="hint">(click a row for the '
                     'reasoning &amp; a link to Polymarket &mdash; free, already '
                     'stored)</span></h3>' + filt
                     + '<table><thead><tr><th>market</th>'
                     '<th>side</th><th>conf</th><th>entry</th><th>now</th><th>value</th>'
                     '<th>P&amp;L $</th><th>P&amp;L %</th></tr></thead>'
                     '<tbody id="pos-tbody">'
                     + "".join(prows) + '</tbody></table>')

        modals["open"] = (
            f'<h3 class="pm-q">Open positions ({len(positions)})</h3>'
            f'<div class="pm-note">Every open position with its live mark, industry and '
            f'country. Click a row in the main table for the model\'s full reasoning and '
            f'a Polymarket link.</div><table class="pm-table"><thead><tr><th>market</th>'
            f'<th>side</th><th>industry</th><th>country</th><th>conf</th><th>invested</th>'
            f'<th>value</th><th>P&amp;L $</th><th>P&amp;L %</th></tr></thead><tbody>'
            + "".join(open_rows) + '</tbody></table>')

        pie_block = ('<h3>Position mix <span class="hint">(by current value &mdash; '
                     'hover a slice for the market)</span></h3>' + _pie_svg(pie_slices))
        dist_block = (
            '<h3>Distributions <span class="hint">(open positions, by current value; '
            'industry &amp; country are best-effort keyword guesses)</span></h3>'
            '<div class="dist-grid">'
            + _dist_bars("By industry", "", dist_ind)
            + _dist_bars("By country", "", dist_country)
            + _dist_bars("By confidence", "", dist_conf)
            + _dist_bars("By side", "", dist_side)
            + '</div>')
    else:
        pos_block = '<h3>Open positions</h3><div class="empty">None open right now.</div>'
        pie_block = ''
        dist_block = ''
        modals["open"] = ('<h3 class="pm-q">Open positions (0)</h3>'
                          '<div class="pm-note">None open right now.</div>')

    if trades:
        trows = []
        for t in trades:
            tid = t["id"]
            mid = t["market_id"]
            q = t["question"] or ""
            action = t["action"] or ""
            side = t["side"] or ""
            pnl = t["realized_pnl"]
            pnl_txt = "" if pnl is None else f"{pnl:+.2f}"
            pnl_cls = "" if pnl is None else ("good" if pnl >= 0 else "bad")
            when = (t["timestamp"] or "")[:16].replace("T", " ")
            amount = float(t["shares"] or 0) * float(t["price"] or 0)
            if amount <= 0:
                amount = abs(float(t["cash_delta"] or 0))
            pred = preds_by_id.get(mid)
            conf = (pred["model_confidence"] if pred else "") or "-"
            industry, country = _class_for(mid, q)
            data_attrs = (f'data-action="{html.escape(action)}" data-side="{html.escape(side)}" '
                          f'data-conf="{html.escape(conf)}" data-amount="{amount:.2f}"')
            trows.append(
                f'<tr class="clickable" {data_attrs} '
                f'onclick="showModal(\'trd:{tid}\')" '
                f'title="Click for the reasoning behind this movement">'
                f'<td>{html.escape(when)}</td>'
                f'<td>{html.escape(action)}</td>'
                f'<td>{html.escape(side)}</td>'
                f'<td class="q">{html.escape(q[:48])}<span class="why-chip">why?</span></td>'
                f'<td>{float(t["price"] or 0):.2f}</td>'
                f'<td class="{pnl_cls}">{pnl_txt}</td>'
                f'<td>{html.escape(t["reason"] or "")}</td></tr>'
            )
            modals[f"trd:{tid}"] = _trade_modal_html(
                t, pred, industry, country, _polymarket_url(q))

        actions = [a for a in ("BUY", "SELL", "SETTLE")
                   if any((t["action"] or "") == a for t in trades)]
        m_sides = sorted({(t["side"] or "") for t in trades if t["side"]})
        trade_confs = {(preds_by_id[t["market_id"]]["model_confidence"] or "-")
                       for t in trades if preds_by_id.get(t["market_id"])}
        m_confs = [c for c in ("high", "med", "low", "-") if c in trade_confs]
        mfilt = (
            '<div class="filters">'
            + _filter_select("fm-action", "All actions", actions)
            + _filter_select("fm-side", "All sides", m_sides)
            + _filter_select("fm-conf", "All confidence", m_confs)
            + '<input id="fm-min" class="filter-num" type="number" min="0" step="1" '
              'placeholder="min $ amount" oninput="filterMov()">'
            + '<button class="filter-reset" onclick="resetMov()">reset</button>'
            + '<span id="fm-count" class="hint"></span></div>'
        )
        ledger_block = ('<h3>Movements <span class="hint">(most recent first &mdash; '
                        'click a row for the reasoning)</span></h3>' + mfilt
                        + '<table><thead><tr><th>when (UTC)</th><th>action</th><th>side</th>'
                        '<th>market</th><th>price</th><th>P&amp;L</th><th>why</th></tr>'
                        '</thead><tbody id="mov-tbody">' + "".join(trows)
                        + '</tbody></table>')
    else:
        ledger_block = ('<h3>Movements</h3><div class="empty">No trades yet — the first '
                        'positions open on the next cloud cycle.</div>')

    # ---- Realized P&L detail modal (closed trades: sells + settlements) ----
    closes = [t for t in record.get_trades(conn) if t["realized_pnl"] is not None]
    if closes:
        crows = "".join(
            f'<tr><td>{html.escape((t["timestamp"] or "")[:16].replace("T", " "))}</td>'
            f'<td>{html.escape(t["action"] or "")}</td>'
            f'<td>{html.escape(t["side"] or "")}</td>'
            f'<td class="q">{html.escape((t["question"] or "")[:60])}</td>'
            f'<td class="{"good" if t["realized_pnl"] >= 0 else "bad"}">'
            f'{_money_signed(float(t["realized_pnl"]))}</td>'
            f'<td>{html.escape(t["reason"] or "")}</td></tr>'
            for t in closes
        )
        modals["realized"] = (
            f'<h3 class="pm-q">Realized P&amp;L &mdash; {_money_signed(realized)}</h3>'
            f'<div class="pm-note">{len(closes)} closed trade(s). Realized P&amp;L is '
            f'locked in when a position is sold (take-profit / stop-loss / edge) or '
            f'settled at resolution.</div>'
            f'<table class="pm-table"><thead><tr><th>when (UTC)</th><th>action</th>'
            f'<th>side</th><th>market</th><th>P&amp;L</th><th>why</th></tr></thead>'
            f'<tbody>{crows}</tbody></table>')
    else:
        modals["realized"] = (
            f'<h3 class="pm-q">Realized P&amp;L &mdash; {_money_signed(realized)}</h3>'
            f'<div class="pm-note">No positions have closed yet.</div>')

    # ---- one modal for every clickable card / position row ----
    modals_json = json.dumps(modals).replace("</", "<\\/")
    modal_block = (
        '<div id="pm-modal" class="modal-backdrop" onclick="hideModal(event)">'
        '<div class="modal" onclick="event.stopPropagation()">'
        '<button class="modal-x" onclick="hideModal(event)" aria-label="Close">'
        '&times;</button><div id="pm-body"></div></div></div>'
        f'<script>var PT_MODALS={modals_json};'
        'function showModal(k){var h=PT_MODALS[k];if(!h)return;'
        'document.getElementById("pm-body").innerHTML=h;'
        'document.getElementById("pm-modal").style.display="flex";}'
        'function hideModal(e){if(e)e.stopPropagation();'
        'document.getElementById("pm-modal").style.display="none";}'
        'document.addEventListener("keydown",function(e){'
        'if(e.key==="Escape")hideModal();});'
        # ---- client-side table filters (open positions + movements) ----
        'function _v(id){var e=document.getElementById(id);return e?e.value:"";}'
        'function _n(id){var e=document.getElementById(id);'
        'return e&&e.value!==""?parseFloat(e.value):null;}'
        'function _apply(tbody,cnt,tests){var b=document.getElementById(tbody);'
        'if(!b)return;var rows=b.querySelectorAll("tr"),shown=0;'
        'rows.forEach(function(r){var ok=tests.every(function(t){return t(r.dataset);});'
        'r.style.display=ok?"":"none";if(ok)shown++;});'
        'var c=document.getElementById(cnt);'
        'if(c)c.textContent=shown+" of "+rows.length+" shown";}'
        'function filterPos(){var side=_v("fp-side"),conf=_v("fp-conf"),'
        'ind=_v("fp-ind"),co=_v("fp-country"),mn=_n("fp-min");'
        '_apply("pos-tbody","fp-count",['
        'function(d){return !side||d.side===side;},'
        'function(d){return !conf||d.conf===conf;},'
        'function(d){return !ind||d.ind===ind;},'
        'function(d){return !co||d.country===co;},'
        'function(d){return mn===null||parseFloat(d.amount)>=mn;}]);}'
        'function resetPos(){["fp-side","fp-conf","fp-ind","fp-country","fp-min"]'
        '.forEach(function(id){var e=document.getElementById(id);if(e)e.value="";});'
        'filterPos();}'
        'function filterMov(){var act=_v("fm-action"),side=_v("fm-side"),'
        'conf=_v("fm-conf"),mn=_n("fm-min");'
        '_apply("mov-tbody","fm-count",['
        'function(d){return !act||d.action===act;},'
        'function(d){return !side||d.side===side;},'
        'function(d){return !conf||d.conf===conf;},'
        'function(d){return mn===null||parseFloat(d.amount)>=mn;}]);}'
        'function resetMov(){["fm-action","fm-side","fm-conf","fm-min"]'
        '.forEach(function(id){var e=document.getElementById(id);if(e)e.value="";});'
        'filterMov();}'
        'if(document.getElementById("pos-tbody"))filterPos();'
        'if(document.getElementById("mov-tbody"))filterMov();</script>'
    )

    return (f'<h2>Paper portfolio <span class="hint">(fictional ${starting:,.0f} — '
            f'no real money)</span></h2>'
            f'<div class="cards">{"".join(cards)}</div>'
            f'<h3>Equity over time</h3>{svg}{pos_block}{pie_block}{dist_block}'
            f'{ledger_block}{modal_block}')


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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polytrade dashboard</title>
<script>
  /* set theme before first paint so there is no flash of the wrong mode */
  (function(){{try{{var t=localStorage.getItem("pt-theme")||"dark";
    document.documentElement.setAttribute("data-theme",t);}}catch(e){{
    document.documentElement.setAttribute("data-theme","dark");}}}})();
</script>
<style>
  /* ---- theme tokens: dark is default, [data-theme=light] is pastel ---- */
  :root, :root[data-theme="dark"] {{
    --bg:#0f1216; --panel:#1a1f27; --panel2:#11161d; --border:#262d38;
    --border2:#232a34; --text:#e7ecf2; --muted:#8a94a3; --head:#c7d0db;
    --good:#4ade80; --bad:#f87171; --accent:#6ea8fe; --chip-bd:#37404d;
    --hover:#202632; --grid:#3a4452; --cross:#5b6675; --code:#232a34;
    --tip-bg:#0b0e12; --tip-bd:#333c48; --scrim:rgba(5,7,10,.66); color-scheme: dark; }}
  :root[data-theme="light"] {{
    --bg:#f2f0fb; --panel:#ffffff; --panel2:#faf9ff; --border:#e6e2f2;
    --border2:#ece9f6; --text:#2c2a3a; --muted:#7a768e; --head:#4a4660;
    --good:#25955a; --bad:#d1495b; --accent:#5b6ee0; --chip-bd:#d9d4ec;
    --hover:#f1eefb; --grid:#dcd7ee; --cross:#b7b0d4; --code:#efecfa;
    --tip-bg:#ffffff; --tip-bd:#e0dbf1; --scrim:rgba(60,55,90,.30); color-scheme: light; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: var(--bg); color: var(--text); }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }}
  .topbar {{ display: flex; align-items: flex-start; justify-content: space-between;
            gap: 12px; }}
  h1 {{ font-size: 22px; margin: 0 0 2px; }}
  .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 22px; }}
  .theme-toggle {{ flex: none; background: var(--panel); color: var(--text);
                  border: 1px solid var(--border); border-radius: 999px;
                  padding: 7px 14px; font-size: 13px; cursor: pointer;
                  font-family: inherit; }}
  .theme-toggle:hover {{ border-color: var(--accent); }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
          padding: 14px 16px; min-width: 130px; flex: 1; }}
  .card.wide {{ flex-basis: 100%; }}
  .clickable-card {{ cursor: pointer; transition: border-color .12s, transform .12s; }}
  .clickable-card:hover {{ border-color: var(--accent); transform: translateY(-1px); }}
  .more {{ color: var(--accent); font-size: 10px; letter-spacing: 0; text-transform: none; }}
  .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
           letter-spacing: .04em; }}
  .value {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
  .sub {{ color: var(--muted); font-size: 12px; }}
  .row2 {{ display: flex; gap: 26px; margin-top: 8px; font-size: 18px; }}
  h2 {{ font-size: 15px; margin: 26px 0 8px; }}
  h3 {{ font-size: 13px; margin: 18px 0 6px; color: var(--head); }}
  .hint {{ color: var(--muted); font-weight: 400; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border2); }}
  th:first-child, td.q {{ text-align: left; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; }}
  .good {{ color: var(--good); }}
  .bad {{ color: var(--bad); }}
  .empty {{ background: var(--panel); border: 1px dashed var(--border); border-radius: 12px;
           padding: 18px; color: var(--muted); font-size: 14px; }}
  code {{ background: var(--code); padding: 1px 6px; border-radius: 5px; }}
  .foot {{ margin-top: 34px; color: var(--muted); font-size: 12px; }}
  /* charts (equity line + position donut) */
  .chartwrap {{ position: relative; }}
  .piewrap {{ display: flex; justify-content: center; }}
  .linechart {{ background: var(--panel2); border: 1px solid var(--border2);
               border-radius: 10px; }}
  .ax {{ fill: var(--muted); }}
  .grid {{ stroke: var(--grid); }}
  .crosshair {{ stroke: var(--cross); }}
  .snapdot {{ stroke: var(--bg); }}
  .slice {{ cursor: pointer; transition: opacity .1s; }}
  .pie-center-n {{ fill: var(--text); font-size: 22px; font-weight: 700; }}
  .pie-center-l {{ fill: var(--muted); font-size: 11px; text-transform: uppercase;
                  letter-spacing: .04em; }}
  .chart-tip {{ position: absolute; display: none; transform: translate(-50%, -125%);
               pointer-events: none; background: var(--tip-bg); border: 1px solid var(--tip-bd);
               border-radius: 8px; padding: 6px 9px; font-size: 12px; white-space: nowrap;
               box-shadow: 0 4px 14px rgba(0,0,0,.28); z-index: 5; max-width: 280px; }}
  .chart-tip b {{ display: block; font-size: 13px; color: var(--text);
                 white-space: normal; }}
  .chart-tip span {{ color: var(--muted); font-size: 11px; }}
  /* clickable open-position rows */
  tr.clickable {{ cursor: pointer; }}
  tr.clickable:hover td {{ background: var(--hover); }}
  .why-chip {{ margin-left: 8px; font-size: 10px; color: var(--muted);
              border: 1px solid var(--chip-bd); border-radius: 999px; padding: 1px 7px;
              text-transform: uppercase; letter-spacing: .04em; vertical-align: middle; }}
  tr.clickable:hover .why-chip {{ color: var(--text); border-color: var(--accent); }}
  /* modal shared by every clickable card / position */
  .modal-backdrop {{ display: none; position: fixed; inset: 0; z-index: 50;
                    background: var(--scrim); align-items: center;
                    justify-content: center; padding: 20px; }}
  .modal {{ position: relative; background: var(--panel); border: 1px solid var(--border);
           border-radius: 14px; max-width: 660px; width: 100%; max-height: 82vh;
           overflow-y: auto; padding: 22px 24px 24px; box-shadow: 0 18px 50px rgba(0,0,0,.35); }}
  .modal-x {{ position: absolute; top: 10px; right: 12px; background: none; border: none;
             color: var(--muted); font-size: 24px; line-height: 1; cursor: pointer; }}
  .modal-x:hover {{ color: var(--text); }}
  .pm-q {{ font-size: 16px; color: var(--text); margin: 2px 40px 12px 0; }}
  .pm-interp {{ background: var(--panel2); border: 1px solid var(--border); border-radius: 10px;
               padding: 11px 13px; font-size: 13.5px; line-height: 1.5; color: var(--text); }}
  .pm-nums {{ display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 14px 0 4px;
             font-size: 12.5px; color: var(--muted); }}
  .pm-nums b {{ color: var(--text); }}
  .pm-link {{ display: inline-block; margin-top: 14px; color: var(--accent);
             font-size: 13px; text-decoration: none; font-weight: 600; }}
  .pm-link:hover {{ text-decoration: underline; }}
  .pm-label {{ margin-top: 16px; color: var(--muted); font-size: 11px; text-transform: uppercase;
              letter-spacing: .04em; }}
  .pm-reason {{ margin-top: 6px; font-size: 13.5px; line-height: 1.6; color: var(--head);
               white-space: pre-wrap; }}
  .pm-note {{ color: var(--muted); font-size: 12.5px; line-height: 1.5; margin: 6px 0 4px; }}
  .pm-table {{ margin-top: 10px; }}
  /* table filter controls */
  .filters {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
             margin: 4px 0 10px; }}
  .filter-sel, .filter-num {{ background: var(--panel); color: var(--text);
             border: 1px solid var(--border); border-radius: 8px; padding: 5px 9px;
             font-size: 12.5px; font-family: inherit; }}
  .filter-num {{ width: 130px; }}
  .filter-sel:hover, .filter-num:focus {{ border-color: var(--accent); outline: none; }}
  .filter-reset {{ background: none; border: 1px solid var(--border); color: var(--muted);
             border-radius: 8px; padding: 5px 11px; font-size: 12px; cursor: pointer;
             font-family: inherit; }}
  .filter-reset:hover {{ border-color: var(--accent); color: var(--text); }}
  /* distribution bars */
  .dist-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
               gap: 16px 26px; }}
  .dist {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
          padding: 12px 14px; }}
  .dist-title {{ font-size: 12px; font-weight: 600; text-transform: uppercase;
                letter-spacing: .04em; color: var(--head); margin-bottom: 9px; }}
  .dist-row {{ display: flex; align-items: center; gap: 10px; margin: 5px 0; font-size: 12.5px; }}
  .dist-label {{ flex: 0 0 34%; white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; color: var(--text); }}
  .dist-track {{ flex: 1; height: 9px; background: var(--border2); border-radius: 999px;
                overflow: hidden; }}
  .dist-fill {{ display: block; height: 100%; background: var(--accent); border-radius: 999px; }}
  .dist-meta {{ flex: 0 0 auto; color: var(--muted); font-variant-numeric: tabular-nums;
               min-width: 78px; text-align: right; }}
</style></head><body><div class="wrap">
  <div class="topbar">
    <div><h1>Polytrade dashboard</h1>
      <div class="meta">model {html.escape(config.ANTHROPIC_MODEL)} &middot; updated {updated}
        &middot; paper-trading measurement &mdash; no real trades</div></div>
    <button id="themebtn" class="theme-toggle" onclick="toggleTheme()">Theme</button>
  </div>
  <script>
    function applyThemeLabel(t){{
      var b=document.getElementById("themebtn");
      if(b) b.textContent = (t==="light") ? "\\u2600 Light" : "\\u263E Dark";
    }}
    function toggleTheme(){{
      var d=document.documentElement;
      var t=(d.getAttribute("data-theme")==="light")?"dark":"light";
      d.setAttribute("data-theme",t);
      try{{ localStorage.setItem("pt-theme",t); }}catch(e){{}}
      applyThemeLabel(t);
    }}
    applyThemeLabel(document.documentElement.getAttribute("data-theme")||"dark");
  </script>
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
