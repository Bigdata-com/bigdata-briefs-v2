"""Portfolio visualization — generates a self-contained HTML report with Plotly charts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime


# ── Colour palette ────────────────────────────────────────────────────────────
_SENTIMENT_COLORS = {
    "bullish":          "#16a34a",   # green-600
    "slightly_bullish": "#86efac",   # green-300
    "neutral":          "#94a3b8",   # slate-400
    "slightly_bearish": "#fca5a5",   # red-300
    "bearish":          "#dc2626",   # red-600
}

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f172a; color: #e2e8f0; }
.page { max-width: 1400px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 1.6rem; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }
.sub { color: #94a3b8; font-size: 0.85rem; margin-bottom: 24px; }
.kpi-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }
.kpi { background: #1e293b; border: 1px solid #334155; border-radius: 10px;
        padding: 14px 20px; flex: 1; min-width: 130px; }
.kpi .val { font-size: 1.5rem; font-weight: 700; }
.kpi .lbl { font-size: 0.75rem; color: #94a3b8; margin-top: 2px; }
.green { color: #4ade80; }  .red { color: #f87171; }  .gray { color: #94a3b8; }
.charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
               margin-bottom: 28px; }
.chart-box { background: #1e293b; border: 1px solid #334155;
             border-radius: 10px; padding: 16px; }
.chart-box.wide { grid-column: 1 / -1; }
.chart-box h3 { font-size: 0.9rem; font-weight: 600; color: #cbd5e1;
                margin-bottom: 10px; letter-spacing: 0.03em; text-transform: uppercase; }
table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
th { text-align: left; padding: 8px 10px; color: #64748b; font-weight: 600;
     border-bottom: 1px solid #334155; white-space: nowrap; }
td { padding: 7px 10px; border-bottom: 1px solid #1e293b; }
tr:hover td { background: #1e293b; }
.badge { display: inline-block; border-radius: 4px; padding: 2px 7px;
         font-size: 0.7rem; font-weight: 600; }
.up { color: #4ade80; } .dn { color: #f87171; } .na { color: #475569; }
@media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; }
  .chart-box.wide { grid-column: 1; } }
"""


def _pct(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{decimals}f}%"


def _color_pct(v: float | None) -> str:
    if v is None:
        return "<span class='na'>—</span>"
    cls = "up" if v > 0 else "dn" if v < 0 else "na"
    return f"<span class='{cls}'>{_pct(v)}</span>"


def _sentiment_badge(score: float | None) -> str:
    if score is None:
        return "<span class='badge' style='background:#1e293b;color:#64748b'>N/A</span>"
    if score > 0.15:
        label, bg = "Bullish", "#166534"
    elif score > 0.05:
        label, bg = "Sl. Bullish", "#14532d"
    elif score < -0.15:
        label, bg = "Bearish", "#7f1d1d"
    elif score < -0.05:
        label, bg = "Sl. Bearish", "#450a0a"
    else:
        label, bg = "Neutral", "#1e3a5f"
    return f"<span class='badge' style='background:{bg};color:#e2e8f0'>{label}</span>"


def _fmt_price(price: float | None, currency: str | None) -> str:
    if price is None:
        return "—"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "JPY": "¥"}.get(currency or "", "")
    return f"{sym}{price:,.2f}"


def _fmt_mcap(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    return f"${v/1e6:.0f}M"


def render_portfolio_html(portfolio_result: dict, output_path: str | None = None) -> str:
    """Generate a self-contained HTML report and return the file path.

    Args:
        portfolio_result: Output from run_portfolio() or run_snapshot().
        output_path: Where to save the file. Defaults to a temp file.
    Returns:
        Absolute path to the generated HTML file.
    """
    companies = portfolio_result.get("companies", [])
    meta = portfolio_result.get("metadata", {})
    summary = portfolio_result.get("portfolio_summary", {})

    ok = [c for c in companies if not c.get("error") and c.get("signals")]
    dist = summary.get("sentiment_distribution", {})

    # ── KPI cards ─────────────────────────────────────────────────────────────
    total = meta.get("total_companies", len(companies))
    resolved = meta.get("resolved", len(ok))
    wall = meta.get("wall_time_s", "—")
    med_sent = summary.get("median_sentiment_score")
    med_1d = summary.get("median_change_pct_1d")

    kpi_html = f"""
    <div class='kpi-row'>
      <div class='kpi'><div class='val'>{total}</div><div class='lbl'>Companies</div></div>
      <div class='kpi'><div class='val'>{resolved}</div><div class='lbl'>Resolved</div></div>
      <div class='kpi'><div class='val' style='color:#4ade80'>{dist.get('bullish',0)+dist.get('slightly_bullish',0)}</div><div class='lbl'>Bullish</div></div>
      <div class='kpi'><div class='val' style='color:#94a3b8'>{dist.get('neutral',0)}</div><div class='lbl'>Neutral</div></div>
      <div class='kpi'><div class='val' style='color:#f87171'>{dist.get('bearish',0)+dist.get('slightly_bearish',0)}</div><div class='lbl'>Bearish</div></div>
      <div class='kpi'><div class='val {"green" if med_sent and med_sent>0 else "red" if med_sent and med_sent<0 else "gray"}'>{f"{med_sent:+.3f}" if med_sent is not None else "—"}</div><div class='lbl'>Median Sentiment</div></div>
      <div class='kpi'><div class='val {"green" if med_1d and med_1d>0 else "red" if med_1d and med_1d<0 else "gray"}'>{_pct(med_1d) if med_1d is not None else "—"}</div><div class='lbl'>Median 1D Chg</div></div>
      <div class='kpi'><div class='val'>{wall}s</div><div class='lbl'>Fetch time</div></div>
    </div>"""

    # ── Plotly data ────────────────────────────────────────────────────────────
    scatter_x, scatter_y, scatter_size, scatter_color, scatter_text, scatter_hover = [], [], [], [], [], []

    def _sent_color(score):
        if score is None: return "#94a3b8"
        if score > 0.15: return "#16a34a"
        if score > 0.05: return "#86efac"
        if score < -0.15: return "#dc2626"
        if score < -0.05: return "#fca5a5"
        return "#94a3b8"

    for c in ok:
        sent = (c.get("signals") or {}).get("sentiment", {})
        mkt = (c.get("market") or {})
        score = sent.get("current")
        chg1d = mkt.get("change_pct_1d")
        mcap = mkt.get("market_cap") or 1e9
        name = c.get("ticker") or c.get("name", "?")
        scatter_x.append(score if score is not None else 0)
        scatter_y.append(chg1d if chg1d is not None else 0)
        scatter_size.append(max(8, min(50, (mcap ** 0.3) / 2000)))
        scatter_color.append(_sent_color(score))
        scatter_text.append(name)
        scatter_hover.append(
            f"{c.get('name','')}<br>Sentiment: {score:+.3f}<br>1D: {_pct(chg1d)}<br>"
            f"Price: {_fmt_price(mkt.get('price'), mkt.get('currency'))}<br>"
            f"Market Cap: {_fmt_mcap(mcap)}"
        )

    # Top/bottom sentiment momentum
    def _momentum(c):
        v = (c.get("signals") or {}).get("sentiment", {}).get("momentum")
        return v if v is not None else 0.0

    def _chg1d(c):
        v = (c.get("market") or {}).get("change_pct_1d")
        return v if v is not None else 0.0

    ranked_sent = sorted(ok, key=_momentum, reverse=True)
    top_sent = ranked_sent[:8]
    bot_sent = ranked_sent[-8:][::-1]

    ranked_price = [c for c in ok if (c.get("market") or {}).get("change_pct_1d") is not None]
    ranked_price.sort(key=_chg1d, reverse=True)
    top_price = ranked_price[:8]
    bot_price = ranked_price[-8:][::-1]

    def _bar_data(items, key_fn, label_fn):
        vals = [key_fn(c) for c in items]
        labels = [label_fn(c) for c in items]
        colors = ["#4ade80" if v >= 0 else "#f87171" for v in vals]
        return vals, labels, colors

    s_vals_top, s_lbls_top, s_cols_top = _bar_data(top_sent, _momentum, lambda c: c.get("ticker") or c.get("name","")[:12])
    s_vals_bot, s_lbls_bot, s_cols_bot = _bar_data(bot_sent, _momentum, lambda c: c.get("ticker") or c.get("name","")[:12])
    p_vals_top, p_lbls_top, p_cols_top = _bar_data(top_price, _chg1d,   lambda c: c.get("ticker") or c.get("name","")[:12])
    p_vals_bot, p_lbls_bot, p_cols_bot = _bar_data(bot_price, _chg1d,   lambda c: c.get("ticker") or c.get("name","")[:12])

    plotly_data = {
        "scatter": {
            "x": scatter_x, "y": scatter_y, "size": scatter_size,
            "color": scatter_color, "text": scatter_text, "hover": scatter_hover,
        },
        "sent_top":   {"vals": s_vals_top, "lbls": s_lbls_top, "cols": s_cols_top},
        "sent_bot":   {"vals": s_vals_bot, "lbls": s_lbls_bot, "cols": s_cols_bot},
        "price_top":  {"vals": p_vals_top, "lbls": p_lbls_top, "cols": p_cols_top},
        "price_bot":  {"vals": p_vals_bot, "lbls": p_lbls_bot, "cols": p_cols_bot},
    }

    # ── Full company table ─────────────────────────────────────────────────────
    table_rows = []
    for c in sorted(ok, key=lambda x: (x.get("signals") or {}).get("sentiment", {}).get("current") or 0, reverse=True):
        sent = (c.get("signals") or {}).get("sentiment", {})
        ma   = (c.get("signals") or {}).get("media_attention", {})
        mkt  = (c.get("market") or {})
        score = sent.get("current")
        mom   = sent.get("momentum")
        z1mo  = sent.get("zscore_1mo")
        table_rows.append(f"""
        <tr>
          <td><strong>{c.get("ticker") or ""}</strong></td>
          <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{c.get("name","")}</td>
          <td>{c.get("sector","")}</td>
          <td>{_sentiment_badge(score)}</td>
          <td class='{"up" if score and score>0 else "dn" if score and score<0 else "na"}'>{f"{score:+.3f}" if score is not None else "—"}</td>
          <td class='{"up" if mom and mom>0 else "dn" if mom and mom<0 else "na"}'>{f"{mom:+.3f}" if mom is not None else "—"}</td>
          <td class='{"up" if z1mo and z1mo>1 else "dn" if z1mo and z1mo<-1 else "na"}'>{f"{z1mo:+.1f}" if z1mo is not None else "—"}</td>
          <td class='{"up" if ma.get("zscore_1mo") and ma["zscore_1mo"]>1 else "na"}'>{f"{ma.get('zscore_1mo'):+.1f}" if ma.get("zscore_1mo") is not None else "—"}</td>
          <td>{_fmt_price(mkt.get("price"), mkt.get("currency"))}</td>
          <td>{_color_pct(mkt.get("change_pct_1d"))}</td>
          <td>{_color_pct(mkt.get("change_pct_5d"))}</td>
          <td>{_color_pct(mkt.get("change_pct_1m"))}</td>
          <td>{_color_pct(mkt.get("change_pct_ytd"))}</td>
          <td>{_fmt_mcap(mkt.get("market_cap"))}</td>
        </tr>""")

    table_html = f"""
    <table>
      <thead><tr>
        <th>Ticker</th><th>Name</th><th>Sector</th><th>Sentiment</th>
        <th>Score</th><th>Momentum</th><th>Z(1mo)</th><th>Media Z(1mo)</th>
        <th>Price</th><th>1D%</th><th>5D%</th><th>1M%</th><th>YTD%</th><th>Mkt Cap</th>
      </tr></thead>
      <tbody>{"".join(table_rows)}</tbody>
    </table>"""

    # ── Narrative section ──────────────────────────────────────────────────────
    narrative_html = ""
    for c in companies:
        if c.get("narrative"):
            ticker = c.get("ticker") or c.get("name", "")
            name = c.get("name", "")
            narr = c["narrative"].replace("\n", "<br>").replace("## ", "<h4 style='margin:10px 0 4px;color:#93c5fd'>").replace("<h4", "</p><h4").replace("**", "<strong>").replace("**", "</strong>")
            narrative_html += f"""
            <div style='background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:14px'>
              <div style='font-weight:700;font-size:0.95rem;color:#f8fafc;margin-bottom:8px'>{name} <span style='color:#64748b;font-size:0.8rem'>({ticker})</span></div>
              <div style='font-size:0.83rem;color:#cbd5e1;line-height:1.65'><p>{narr}</p></div>
            </div>"""

    # ── Assemble HTML ──────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    days = meta.get("days", "—")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio Monitor — {ts}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <h1>Portfolio Monitor</h1>
  <div class="sub">Generated {ts} · {total} companies · {days}d lookback · {wall}s fetch time</div>

  {kpi_html}

  <div class="charts-grid">

    <!-- 2D Scatter: Sentiment vs Price Change -->
    <div class="chart-box wide">
      <h3>Sentiment Score vs 1D Price Change</h3>
      <div id="scatter" style="height:420px"></div>
    </div>

    <!-- Top sentiment movers -->
    <div class="chart-box">
      <h3>Top Sentiment Momentum</h3>
      <div id="sent_top" style="height:300px"></div>
    </div>
    <div class="chart-box">
      <h3>Bottom Sentiment Momentum</h3>
      <div id="sent_bot" style="height:300px"></div>
    </div>

    <!-- Top price movers -->
    <div class="chart-box">
      <h3>Top 1D Price Gainers</h3>
      <div id="price_top" style="height:300px"></div>
    </div>
    <div class="chart-box">
      <h3>Top 1D Price Losers</h3>
      <div id="price_bot" style="height:300px"></div>
    </div>

    <!-- Full table -->
    <div class="chart-box wide">
      <h3>Full Portfolio Table (sorted by sentiment score)</h3>
      {table_html}
    </div>

    <!-- Narratives -->
    {"" if not narrative_html else f'<div class="chart-box wide"><h3>Analyst Narratives</h3>' + narrative_html + '</div>'}

  </div>
</div>

<script>
const D = {json.dumps(plotly_data)};
const dark = {{ paper_bgcolor:'#0f172a', plot_bgcolor:'#0f172a',
                font:{{color:'#94a3b8',size:11}},
                xaxis:{{gridcolor:'#1e293b',zerolinecolor:'#334155'}},
                yaxis:{{gridcolor:'#1e293b',zerolinecolor:'#334155'}},
                margin:{{t:20,r:10,b:40,l:50}} }};

// Scatter
Plotly.newPlot('scatter', [{{
  type:'scatter', mode:'markers+text',
  x: D.scatter.x, y: D.scatter.y,
  text: D.scatter.text,
  textposition: 'top center',
  textfont: {{size: 9, color:'#cbd5e1'}},
  hovertext: D.scatter.hover,
  hoverinfo: 'text',
  marker: {{
    size: D.scatter.size,
    color: D.scatter.color,
    line: {{color:'#0f172a', width:1}},
    opacity: 0.85,
  }},
}}], {{
  ...dark,
  xaxis: {{...dark.xaxis, title:'Sentiment Score', zeroline:true}},
  yaxis: {{...dark.yaxis, title:'1D Price Change (%)', zeroline:true,
           ticksuffix:'%'}},
  shapes:[
    {{type:'line',x0:0,x1:0,y0:0,y1:1,yref:'paper',line:{{color:'#475569',width:1,dash:'dot'}}}},
    {{type:'line',x0:0,x1:1,xref:'paper',y0:0,y1:0,line:{{color:'#475569',width:1,dash:'dot'}}}},
  ],
}}, {{responsive:true, displayModeBar:false}});

// Bar chart helper
function bar(divId, data, xTitle) {{
  Plotly.newPlot(divId, [{{
    type:'bar', orientation:'h',
    x: data.vals, y: data.lbls,
    marker: {{color: data.cols}},
    hovertemplate: '%{{y}}: %{{x:.3f}}<extra></extra>',
  }}], {{
    ...dark,
    xaxis: {{...dark.xaxis, title: xTitle, ticksuffix: xTitle.includes('%') ? '%' : ''}},
    yaxis: {{...dark.yaxis, autorange:'reversed'}},
  }}, {{responsive:true, displayModeBar:false}});
}}

bar('sent_top',  D.sent_top,  'Momentum');
bar('sent_bot',  D.sent_bot,  'Momentum');
bar('price_top', D.price_top, '1D Change %');
bar('price_bot', D.price_bot, '1D Change %');
</script>
</body>
</html>"""

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="portfolio_")
        os.close(fd)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return os.path.abspath(output_path)
