"""Self-contained HTML report.

One file, no external requests: no CDN scripts, no web fonts, no remote images.
Charts are inline SVG. It opens from the filesystem and works offline, which
matters when the contents are your bank statement.
"""

from __future__ import annotations

import html
from datetime import datetime

from . import config
from .analysis import PeriodReport

# Colour-blind-safe qualitative sequence, ordered so adjacent slices differ in
# lightness as well as hue — legible in greyscale and to most colour vision types.
PALETTE = [
    "#4269d0", "#efb118", "#ff725c", "#6cc5b0", "#3ca951", "#ff8ab7",
    "#a463f2", "#97bbf5", "#9c6b4e", "#9498a0", "#1f7a8c", "#d4a373",
]
CONFIDENCE_COLOURS = {"high": "#3ca951", "medium": "#efb118", "low": "#9498a0"}


def render(report: PeriodReport) -> str:
    cur = report.currency
    title = f"Spending report — {report.period.label}"
    parts = [
        _head(title),
        "<body>",
        _header(report, title),
        _reconciliation(report, cur),
        _categories(report, cur),
        _daily(report, cur),
        _merchants(report, cur),
        _cash(report, cur),
        _insights(report, cur),
        _recurring(report, cur),
        _largest(report, cur),
        _quality(report),
        _footer(report),
        "</body></html>",
    ]
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------

def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _m(value: float, cur: str) -> str:
    return _e(config.money(value, cur))


def _head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root {{
  --bg: #ffffff; --panel: #f7f8fa; --border: #e2e5ea; --text: #14161a;
  --muted: #5c6370; --accent: #4269d0; --good: #3ca951; --warn: #efb118;
  --bad: #d1495b;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #14161a; --panel: #1c1f26; --border: #2c313b; --text: #e8eaee;
    --muted: #9aa2b1; --accent: #97bbf5; --good: #6cc5b0; --warn: #efb118;
    --bad: #ff725c;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0 1rem 4rem; background: var(--bg); color: var(--text);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; }}
header.top {{ padding: 2.5rem 0 1.5rem; border-bottom: 1px solid var(--border); }}
h1 {{ font-size: 1.75rem; margin: 0 0 .35rem; letter-spacing: -.01em; }}
h2 {{ font-size: 1.15rem; margin: 2.5rem 0 .85rem; letter-spacing: -.005em; }}
h3 {{ font-size: .95rem; margin: 1.25rem 0 .4rem; }}
p {{ margin: .5rem 0; }}
.sub {{ color: var(--muted); font-size: .9rem; }}
.tiles {{ display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); margin: 1.25rem 0; }}
.tile {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: .85rem 1rem; }}
.tile .label {{ color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; }}
.tile .value {{ font-size: 1.4rem; font-weight: 600; margin-top: .15rem; font-variant-numeric: tabular-nums; }}
.tile .note {{ color: var(--muted); font-size: .8rem; margin-top: .15rem; }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
.scroll {{ overflow-x: auto; }}
th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--border); }}
th {{ color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }}
td.num, th.num {{ text-align: right; white-space: nowrap; }}
tbody tr:hover {{ background: var(--panel); }}
.bar {{ height: 8px; border-radius: 4px; background: var(--accent); min-width: 2px; }}
.bar-cell {{ width: 26%; }}
.chip {{ display: inline-block; padding: .1rem .45rem; border-radius: 5px; font-size: .72rem;
         font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: #14161a; }}
.card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
         padding: 1rem 1.15rem; margin: .85rem 0; }}
.card h3 {{ margin-top: 0; display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; }}
.card .save {{ color: var(--good); font-weight: 600; white-space: nowrap; }}
.card ul {{ margin: .5rem 0 0; padding-left: 1.1rem; color: var(--muted); font-size: .87rem; }}
.card .action {{ margin-top: .6rem; padding-top: .6rem; border-top: 1px dashed var(--border); }}
.banner {{ border-left: 3px solid var(--good); background: var(--panel); padding: .7rem 1rem;
           border-radius: 0 8px 8px 0; margin: 1rem 0; }}
.banner.warn {{ border-left-color: var(--warn); }}
.banner.bad {{ border-left-color: var(--bad); }}
ul.notes {{ padding-left: 1.1rem; color: var(--muted); }}
svg {{ display: block; max-width: 100%; height: auto; }}
footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border);
          color: var(--muted); font-size: .82rem; }}
@media print {{ body {{ padding: 0; }} .card, .tile {{ break-inside: avoid; }} }}
</style></head>"""


def _header(report: PeriodReport, title: str) -> str:
    rec = report.reconciliation
    cur = report.currency
    accounts = ", ".join(report.accounts) or "no account"
    tiles = [
        ("Left the account", _m(rec.total_out, cur),
         f"{report.period.days} days"),
        ("Real spending", _m(rec.consumption, cur),
         f"{_m(rec.consumption / report.period.months, cur)} a month"),
        ("Money in", _m(rec.income, cur),
         f"net {_m(rec.income - rec.total_out, cur)}"),
        ("Could be cut", _m(report.monthly_reducible, cur) + " / month",
         f"{_m(report.annual_reducible, cur)} a year"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="label">{_e(label)}</div>'
        f'<div class="value">{value}</div><div class="note">{note}</div></div>'
        for label, value, note in tiles
    )
    return f"""<div class="wrap">
<header class="top">
  <h1>{_e(title)}</h1>
  <div class="sub">{_e(report.period.start)} to {_e(report.period.end)} &middot;
    {_e(accounts)} &middot; generated {_e(datetime.now().strftime('%d %b %Y %H:%M'))}</div>
</header>
<div class="tiles">{tile_html}</div>"""


def _reconciliation(report: PeriodReport, cur: str) -> str:
    rec = report.reconciliation
    rows = [
        ("Consumption — real spending", rec.consumption),
        ("Debt repayments", rec.debt),
        ("Into savings and investments", rec.savings),
        ("Transfers to your own accounts", rec.transfers),
        ("Excluded by you", rec.excluded),
    ]
    body = "".join(
        f"<tr><td>{_e(label)}</td><td class='num'>{_m(value, cur)}</td>"
        f"<td class='num'>{value / rec.total_out * 100:.1f}%</td></tr>"
        for label, value in rows if value
    ) if rec.total_out else ""

    if rec.balances:
        banner = (f'<div class="banner">Reconciled: every one of the '
                  f'{_m(rec.total_out, cur)} that left the account is in exactly one '
                  f'bucket below. Till slips add detail but never add money.</div>')
    else:
        banner = (f'<div class="banner bad">Not reconciled — the buckets differ from the '
                  f'total by {_m(rec.difference, cur)}. Please report this.</div>')

    return f"""<h2>Where every rand went</h2>
{banner}
<div class="scroll"><table><thead><tr><th>Bucket</th><th class="num">Amount</th>
<th class="num">Share</th></tr></thead><tbody>{body}
<tr><td><strong>Total out</strong></td>
<td class="num"><strong>{_m(rec.total_out, cur)}</strong></td>
<td class="num"><strong>100.0%</strong></td></tr></tbody></table></div>"""


def _donut(report: PeriodReport, cur: str, limit: int = 9) -> str:
    """A donut of the top categories, drawn as inline SVG arcs."""
    buckets = [b for b in report.categories if b.total > 0]
    if not buckets:
        return ""
    total = sum(b.total for b in buckets)
    shown = buckets[:limit]
    other = sum(b.total for b in buckets[limit:])
    slices = [(b.name, b.total) for b in shown]
    if other > 0:
        slices.append(("Everything else", other))

    size, radius, thickness = 240, 96, 30
    centre = size / 2
    import math
    angle = -math.pi / 2
    arcs, legend = [], []
    for index, (name, value) in enumerate(slices):
        span = value / total * 2 * math.pi
        end = angle + span
        large = 1 if span > math.pi else 0
        x1 = centre + radius * math.cos(angle)
        y1 = centre + radius * math.sin(angle)
        x2 = centre + radius * math.cos(end)
        y2 = centre + radius * math.sin(end)
        colour = PALETTE[index % len(PALETTE)]
        # A stroked arc, so one path draws each ring segment.
        arcs.append(
            f'<path d="M {x1:.2f} {y1:.2f} A {radius} {radius} 0 {large} 1 '
            f'{x2:.2f} {y2:.2f}" fill="none" stroke="{colour}" '
            f'stroke-width="{thickness}"><title>{_e(name)}: '
            f'{_e(config.money(value, cur))} ({value / total * 100:.1f}%)</title></path>'
        )
        legend.append(
            f'<li><span style="display:inline-block;width:10px;height:10px;'
            f'border-radius:2px;background:{colour};margin-right:.45rem"></span>'
            f'{_e(name)} — {_m(value, cur)} ({value / total * 100:.1f}%)</li>'
        )
        angle = end

    return f"""<div style="display:flex;gap:1.5rem;flex-wrap:wrap;align-items:center">
<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img"
     aria-label="Share of spending by category">{''.join(arcs)}
<text x="{centre}" y="{centre - 6}" text-anchor="middle" fill="currentColor"
      font-size="12" opacity=".65">total</text>
<text x="{centre}" y="{centre + 14}" text-anchor="middle" fill="currentColor"
      font-size="15" font-weight="600">{_e(config.money(total, cur))}</text></svg>
<ul style="list-style:none;padding:0;margin:0;font-size:.87rem;line-height:1.9;flex:1;min-width:240px">
{''.join(legend)}</ul></div>"""


def _categories(report: PeriodReport, cur: str) -> str:
    if not report.categories:
        return ""
    total = sum(b.total for b in report.categories)
    largest = report.categories[0].total
    rows = []
    for index, bucket in enumerate(report.categories):
        colour = PALETTE[index % len(PALETTE)]
        pct = bucket.total / total * 100 if total else 0
        width = bucket.total / largest * 100 if largest else 0
        note = (f"{_m(bucket.from_cash, cur)} from cash slips"
                if bucket.from_cash else "")
        rows.append(
            f"<tr><td>{_e(bucket.name)}<br><span class='sub'>{_e(note)}</span></td>"
            f"<td class='num'>{_m(bucket.total, cur)}</td>"
            f"<td class='num'>{bucket.count}</td>"
            f"<td class='num'>{pct:.1f}%</td>"
            f"<td class='bar-cell'><div class='bar' style='width:{width:.1f}%;"
            f"background:{colour}'></div></td></tr>"
        )
    return f"""<h2>By type of spend</h2>
{_donut(report, cur)}
<div class="scroll"><table><thead><tr><th>Category</th><th class="num">Total</th>
<th class="num">Count</th><th class="num">Share</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody>
<tfoot><tr><td><strong>Total excluding transfers</strong></td>
<td class="num"><strong>{_m(total, cur)}</strong></td><td colspan="3"></td></tr></tfoot>
</table></div>"""


def _daily(report: PeriodReport, cur: str) -> str:
    """A day-by-day bar chart, so spikes and quiet weeks are visible."""
    if len(report.daily) < 2:
        return ""
    values = [v for _d, v in report.daily]
    peak = max(values)
    width, height, pad = 960, 180, 24
    span = max(1, len(report.daily))
    bar_w = max(1.5, (width - 2 * pad) / span - 2)
    bars = []
    for index, (day, value) in enumerate(report.daily):
        x = pad + index * (width - 2 * pad) / span
        h = (value / peak) * (height - 2 * pad) if peak else 0
        bars.append(
            f'<rect x="{x:.1f}" y="{height - pad - h:.1f}" width="{bar_w:.1f}" '
            f'height="{h:.1f}" rx="1.5" fill="var(--accent)" opacity=".85">'
            f'<title>{_e(day)}: {_e(config.money(value, cur))}</title></rect>'
        )
    average = sum(values) / len(values)
    avg_y = height - pad - (average / peak) * (height - 2 * pad) if peak else height - pad
    return f"""<h2>Day by day</h2>
<p class="sub">Peak day {_m(peak, cur)} &middot; average
{_m(average, cur)} across {len(report.daily)} day(s) with spending.</p>
<div class="scroll"><svg viewBox="0 0 {width} {height}" role="img"
  aria-label="Spending per day">
{''.join(bars)}
<line x1="{pad}" y1="{avg_y:.1f}" x2="{width - pad}" y2="{avg_y:.1f}"
      stroke="var(--warn)" stroke-width="1" stroke-dasharray="4 3"></line>
<text x="{width - pad}" y="{avg_y - 5:.1f}" text-anchor="end" fill="var(--warn)"
      font-size="11">average</text>
</svg></div>"""


def _merchants(report: PeriodReport, cur: str, limit: int = 20) -> str:
    if not report.merchants:
        return ""
    largest = report.merchants[0].total
    rows = []
    for bucket in report.merchants[:limit]:
        avg = bucket.total / bucket.count if bucket.count else 0
        width = bucket.total / largest * 100 if largest else 0
        rows.append(
            f"<tr><td>{_e(bucket.name)}</td>"
            f"<td class='num'>{_m(bucket.total, cur)}</td>"
            f"<td class='num'>{bucket.count}</td>"
            f"<td class='num'>{_m(avg, cur)}</td>"
            f"<td class='bar-cell'><div class='bar' style='width:{width:.1f}%'></div></td></tr>"
        )
    return f"""<h2>By merchant</h2>
<div class="scroll"><table><thead><tr><th>Merchant</th><th class="num">Total</th>
<th class="num">Times</th><th class="num">Average</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def _cash(report: PeriodReport, cur: str) -> str:
    rec = report.reconciliation
    if not rec.cash_withdrawn:
        return ""
    rows = "".join(
        f"<tr><td>{_e(entry['category'])}</td>"
        f"<td class='num'>{_m(entry['total'], cur)}</td>"
        f"<td class='num'>{entry['count']}</td></tr>"
        for entry in report.cash_reallocated
    )
    if rec.cash_unexplained > 0:
        rows += (f"<tr><td>Still unexplained</td>"
                 f"<td class='num'>{_m(rec.cash_unexplained, cur)}</td>"
                 f"<td class='num'>&ndash;</td></tr>")
    return f"""<h2>What the cash bought</h2>
<div class="banner{'' if rec.cash_unexplained <= 0 else ' warn'}">
{_m(rec.cash_withdrawn, cur)} was withdrawn. Till slips explain
{_m(rec.cash_explained, cur)} of it, and that value has been moved out of
&ldquo;Cash Withdrawals&rdquo; into the categories below. Nothing was added — a cash
slip describes money that already left the account at the ATM.</div>
<div class="scroll"><table><thead><tr><th>Category</th><th class="num">From cash</th>
<th class="num">Slips</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def _insights(report: PeriodReport, cur: str) -> str:
    savings = [i for i in report.insights if i.counts_to_total]
    context = [i for i in report.insights if not i.counts_to_total]
    if not savings and not context:
        return ""
    cards = []
    for index, insight in enumerate(savings, start=1):
        colour = CONFIDENCE_COLOURS.get(insight.confidence, "#9498a0")
        evidence = "".join(f"<li>{_e(line)}</li>" for line in insight.evidence[:6])
        action = (f'<div class="action"><strong>Do this:</strong> '
                  f'{_e(insight.action)}</div>' if insight.action else "")
        cards.append(f"""<div class="card">
<h3><span>{index}. {_e(insight.title)}
  <span class="chip" style="background:{colour}">{_e(insight.confidence)}</span></span>
<span class="save">{_m(insight.monthly_saving, cur)}/month</span></h3>
<p>{_e(insight.detail)}</p>
<p class="sub">Spent this period {_m(insight.period_amount, cur)} &middot;
   saving {_m(insight.annual_saving, cur)} a year</p>
{f'<ul>{evidence}</ul>' if evidence else ''}
{action}</div>""")

    consumption_month = report.reconciliation.consumption / report.period.months
    pct = (report.monthly_reducible / consumption_month * 100) if consumption_month else 0
    context_html = ""
    if context:
        items = "".join(f"<li><strong>{_e(i.title)}</strong> — {_e(i.detail)}</li>"
                        for i in context)
        context_html = (f'<h3>For context, with no saving claimed</h3>'
                        f'<ul class="notes">{items}</ul>')

    return f"""<h2>What could be cut</h2>
<div class="banner">Around {_m(report.monthly_reducible, cur)} a month
({_m(report.annual_reducible, cur)} a year) looks realistically reducible — {pct:.0f}%
of monthly consumption spend. Suggestions do not overlap: each transaction feeds at
most one of them, so these figures can be added up.</div>
{''.join(cards)}
{context_html}"""


def _recurring(report: PeriodReport, cur: str) -> str:
    if not report.recurring:
        return ""
    fixed = [e for e in report.recurring if e["fixed"]]
    variable = [e for e in report.recurring if not e["fixed"]]

    def table(entries: list[dict], show_range: bool) -> str:
        rows = "".join(
            f"<tr><td>{_e(entry['merchant'])}</td>"
            f"<td>{_e(entry['category'])}</td>"
            f"<td class='num'>{_m(entry['typical_amount'], cur)}</td>"
            + (f"<td class='num'>{_m(entry['min_amount'], cur)} – "
               f"{_m(entry['max_amount'], cur)}</td>" if show_range else
               f"<td class='num'>{_m(entry['annualised'], cur)}</td>")
            + f"<td class='num'>{entry['months_seen']}</td>"
            f"<td>{_e(entry['last_seen'])}</td></tr>"
            for entry in entries[:25]
        )
        third = "Range" if show_range else "Per year"
        return (f'<div class="scroll"><table><thead><tr><th>Merchant</th>'
                f'<th>Category</th><th class="num">Per month</th>'
                f'<th class="num">{third}</th><th class="num">Months seen</th>'
                f'<th>Last seen</th></tr></thead><tbody>{rows}</tbody></table></div>')

    blocks = []
    if fixed:
        total = sum(e["annualised"] for e in fixed)
        blocks.append(
            f'<h3>Fixed — same amount every month</h3>'
            f'<p class="sub">Decided once and then rarely revisited. Committed '
            f'annually: <strong>{_m(total, cur)}</strong>.</p>' + table(fixed, False))
    if variable:
        blocks.append(
            '<h3>Monthly but variable</h3>'
            '<p class="sub">Recurs every month, but the amount moves — utilities and '
            'a regular big shop behave this way.</p>' + table(variable, True))

    return f"""<h2>Monthly commitments</h2>
<p class="sub">Detected by grouping on a normalised description, so a changing
reference number does not hide a repeat charge, and by requiring roughly one
occurrence a month — frequent small visits are treated as a habit, not a commitment.</p>
{''.join(blocks)}"""


def _largest(report: PeriodReport, cur: str) -> str:
    if not report.largest:
        return ""
    rows = "".join(
        f"<tr><td>{_e(entry['date'])}</td>"
        f"<td class='num'>{_m(entry['amount'], cur)}</td>"
        f"<td>{_e(entry['category'])}</td>"
        f"<td>{_e(entry['description'])}</td></tr>"
        for entry in report.largest[:12]
    )
    return f"""<h2>Largest single outflows</h2>
<div class="scroll"><table><thead><tr><th>Date</th><th class="num">Amount</th>
<th>Category</th><th>Description</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def _quality(report: PeriodReport) -> str:
    if not report.data_quality:
        return ""
    items = "".join(f"<li>{_e(note)}</li>" for note in report.data_quality)
    return f"""<h2>Worth knowing about these numbers</h2>
<ul class="notes">{items}</ul>"""


def _footer(report: PeriodReport) -> str:
    coverage = report.slip_coverage
    matched = coverage.get("value_matched", 0)
    pct = coverage.get("pct_of_consumption", 0)
    return f"""<footer>
Generated by SpendTrack from bank statement CSVs and till slips.
Till slips confirmed {_m(matched, report.currency)} of spend ({pct}% of consumption);
they add detail and never add money. {report.months_observed} month(s) of data loaded.
Everything here was computed locally — no data left this machine.
</footer></div>"""
