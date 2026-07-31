"""A spending report as one self-contained HTML file.

Why this exists alongside a perfectly good web UI: the web UI needs the machine
running, on the network, with a passphrase typed in. This is a file. It opens
from a phone's Files app with no server, no network and no laptop awake — put it
in your cloud-storage folder and the report is simply there.

The constraint that shapes everything below: **no external requests, ever.** No
CDN scripts, no web fonts, no remote images, no analytics. A page holding your
bank statement must not phone anywhere, and must not stop working when it cannot.
So charts are inline SVG drawn by hand, styles are an inline stylesheet, and the
whole thing is a single file you can email, archive or open on a plane.

Second constraint: it is read on a phone. Layout is single-column and narrow by
default, widening only where there is room. Tables that would need horizontal
scrolling are built as stacked rows instead.

This is read-only by design. Correcting a category or linking a slip needs the
real app; trying to make a static file do that would mean either a server or
JavaScript that writes somewhere, and both defeat the point.
"""

from __future__ import annotations

import html
import math
import sqlite3
from datetime import date, datetime

from .analytics import PeriodSummary, Recurring
from .config import Config
from .money import fmt
from .periods import Period

# Qualitative palette ordered so neighbouring slices differ in lightness as well
# as hue: it stays readable in greyscale, when printed, and for the commoner
# forms of colour blindness.
PALETTE = [
    "#4269d0", "#efb118", "#ff725c", "#6cc5b0", "#3ca951", "#ff8ab7",
    "#a463f2", "#97bbf5", "#9c6b4e", "#9498a0", "#1f7a8c", "#d4a373",
]

MAX_DONUT_SLICES = 8
MAX_MERCHANTS = 15
MAX_FRIVOLOUS = 12


def _e(value: object) -> str:
    """Escape for HTML. Everything user-derived goes through this.

    Merchant names come from bank narration, which is arbitrary text this
    program did not choose. Interpolating it raw would be an injection bug in a
    file the user is about to open in a browser.
    """
    return html.escape(str(value if value is not None else ""))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_report(
    conn: sqlite3.Connection,
    summary: PeriodSummary,
    *,
    cfg: Config,
    advice=None,
    recurring: list[Recurring] | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Build the whole document. Returns HTML as a string.

    ``advice`` and ``recurring`` are optional: passing them adds sections,
    omitting them simply leaves those out, so a caller that only wants the
    numbers does not pay for the analysis.
    """
    sym = cfg.currency_symbol
    stamp = generated_at or datetime.now()

    parts = [
        _head(summary),
        _header(summary, sym, stamp),
        _headline(summary, sym),
        _reconciliation(summary, sym),
        _categories(summary, sym),
        _daily(summary, sym),
        _merchants(summary, sym),
        _recurring_section(recurring or [], sym),
        _advice_section(advice, sym),
        _footer(summary, stamp),
    ]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Document shell
# ---------------------------------------------------------------------------


def _head(summary: PeriodSummary) -> str:
    title = f"Spending — {summary.period.label}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
/* Everything inline. The file must work with no network at all. */
:root {{
  color-scheme: light dark;
  --bg: #fcfcfb;
  --card: #ffffff;
  --sunken: #f4f4f2;
  --line: #e2e2dd;
  --ink: #1a1a19;
  --ink-2: #55554f;
  --ink-3: #82827a;
  --good: #3ca951;
  --warn: #efb118;
  --bad: #d1453b;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #191918;
    --card: #232322;
    --sunken: #2b2b29;
    --line: #3a3a37;
    --ink: #f2f2ef;
    --ink-2: #b9b9b2;
    --ink-3: #8d8d85;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 1rem 0.9rem 3rem;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}}
.wrap {{ max-width: 46rem; margin: 0 auto; }}
h1 {{ font-size: 1.3rem; margin: 0 0 0.15rem; letter-spacing: -0.01em; }}
h2 {{ font-size: 1.02rem; margin: 2rem 0 0.75rem; letter-spacing: -0.005em; }}
.sub {{ color: var(--ink-3); font-size: 0.83rem; margin: 0 0 1.25rem; }}
.card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 1rem;
  margin-bottom: 0.75rem;
}}
.big {{ font-size: 1.85rem; font-weight: 650; letter-spacing: -0.02em; }}
.label {{ font-size: 0.78rem; color: var(--ink-3); text-transform: uppercase;
          letter-spacing: 0.04em; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; }}
@media (max-width: 30rem) {{ .grid {{ grid-template-columns: 1fr; }} }}
.num {{ font-variant-numeric: tabular-nums; }}

/* Rows: a table would need sideways scrolling on a phone. */
.row {{
  display: flex; align-items: baseline; gap: 0.6rem;
  padding: 0.5rem 0; border-bottom: 1px solid var(--line);
}}
.row:last-child {{ border-bottom: 0; }}
.row .name {{ flex: 1; min-width: 0; overflow-wrap: anywhere; }}
.row .val {{ font-variant-numeric: tabular-nums; white-space: nowrap;
             font-weight: 600; }}
.row .meta {{ font-size: 0.78rem; color: var(--ink-3); white-space: nowrap; }}

.bar {{ height: 4px; border-radius: 2px; background: var(--sunken);
        margin-top: 0.3rem; overflow: hidden; }}
.bar > span {{ display: block; height: 100%; border-radius: 2px; }}

.swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px;
           margin-right: 0.45rem; flex: 0 0 auto; }}
.legend {{ list-style: none; padding: 0; margin: 0; }}
.legend li {{ display: flex; align-items: center; gap: 0.1rem;
              padding: 0.3rem 0; font-size: 0.87rem; }}
.legend .name {{ flex: 1; min-width: 0; overflow-wrap: anywhere; }}
.legend .val {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}

.chart {{ display: flex; gap: 1.25rem; flex-wrap: wrap; align-items: center; }}
.chart svg {{ flex: 0 0 auto; max-width: 100%; height: auto; }}
.chart .legend {{ flex: 1 1 15rem; }}

.pill {{ display: inline-block; padding: 0.1rem 0.45rem; border-radius: 99px;
         font-size: 0.72rem; font-weight: 600; background: var(--sunken);
         color: var(--ink-2); }}
.pill.good {{ background: rgba(60,169,81,0.15); color: var(--good); }}
.pill.warn {{ background: rgba(239,177,24,0.16); color: #9a6f00; }}
.pill.bad  {{ background: rgba(209,69,59,0.14); color: var(--bad); }}
@media (prefers-color-scheme: dark) {{
  .pill.warn {{ color: var(--warn); }}
}}

.note {{ font-size: 0.83rem; color: var(--ink-2); margin: 0.6rem 0 0;
         line-height: 1.5; }}
.muted {{ color: var(--ink-3); }}
.finding {{ padding: 0.85rem 0; border-bottom: 1px solid var(--line); }}
.finding:last-child {{ border-bottom: 0; }}
.finding h3 {{ font-size: 0.95rem; margin: 0 0 0.3rem; }}
.finding .assume {{ font-size: 0.79rem; color: var(--ink-3); margin: 0.4rem 0 0;
                    font-style: italic; }}
footer {{ margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--line);
          font-size: 0.78rem; color: var(--ink-3); line-height: 1.6; }}

@media print {{
  body {{ background: #fff; color: #000; padding: 0; }}
  .card {{ break-inside: avoid; border-color: #ccc; }}
  h2 {{ break-after: avoid; }}
}}
</style>
</head>
<body><div class="wrap">"""


def _header(summary: PeriodSummary, sym: str, stamp: datetime) -> str:
    p = summary.period
    return (
        f"<h1>Spending — {_e(p.label)}</h1>"
        f'<p class="sub">{_e(p.start)} to {_e(p.end)} · '
        f"{summary.transaction_count} transactions · "
        f'generated {_e(stamp.strftime("%d %b %Y, %H:%M"))}</p>'
    )


def _footer(summary: PeriodSummary, stamp: datetime) -> str:
    return f"""<footer>
<p>Generated by spendtracker on {_e(stamp.strftime("%d %B %Y at %H:%M"))} for the
period {_e(summary.period.label)}. This file is self-contained: it makes no
network requests and works offline.</p>
<p>It is a snapshot, not a live view. Re-run
<code>spendtracker report --html</code> after importing new statements. To
correct a category or link a till slip, use the app itself.</p>
<p class="muted">Contains your bank statement data. Treat the file accordingly.</p>
</footer></div></body></html>"""


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _headline(summary: PeriodSummary, sym: str) -> str:
    s = summary
    months = max(s.period.days / 30.44, 0.01)
    return f"""<div class="grid">
<div class="card">
  <div class="label">Total spend</div>
  <div class="big num">{_e(fmt(s.spend_cents, sym))}</div>
  <div class="muted" style="font-size:.82rem">
    {_e(fmt(s.spend_per_month_cents, sym))} a month
  </div>
</div>
<div class="card">
  <div class="label">Money in</div>
  <div class="big num">{_e(fmt(s.total_inflow_cents, sym))}</div>
  <div class="muted" style="font-size:.82rem">
    net {_e(fmt(s.net_cents, sym))}
  </div>
</div>
<div class="card">
  <div class="label">Essential</div>
  <div class="big num">{_e(fmt(s.essential_cents, sym))}</div>
  <div class="muted" style="font-size:.82rem">
    {(1 - s.discretionary_share) * 100:.0f}% of spend
  </div>
</div>
<div class="card">
  <div class="label">Discretionary</div>
  <div class="big num">{_e(fmt(s.discretionary_cents, sym))}</div>
  <div class="muted" style="font-size:.82rem">
    {s.discretionary_share * 100:.0f}% of spend
  </div>
</div>
</div>"""


def _reconciliation(summary: PeriodSummary, sym: str) -> str:
    """Does the breakdown add up to what the bank says left the account?

    This is the claim the whole app rests on, so it is stated plainly rather
    than buried: if the residual is not zero, every number above is suspect.
    """
    rec = summary.reconciliation
    reconciles = rec.residual_cents == 0

    if reconciles:
        badge = '<span class="pill good">balances</span>'
        line = (
            "Every rand that left the account is accounted for in the "
            "categories below."
        )
    else:
        badge = '<span class="pill bad">does not balance</span>'
        line = (
            f"<strong>{_e(fmt(abs(rec.residual_cents), sym))} is unexplained.</strong> "
            "The breakdown below does not sum to what the bank shows leaving the "
            "account, so treat these figures as provisional."
        )

    bits = [
        f'<h2>Does this add up? {badge}</h2>',
        '<div class="card">',
        f'<p class="note" style="margin-top:0">{line}</p>',
        '<div class="row"><span class="name">Bank says out</span>'
        f'<span class="val num">{_e(fmt(rec.bank_outflow_cents, sym))}</span></div>',
        '<div class="row"><span class="name">Breakdown totals</span>'
        f'<span class="val num">{_e(fmt(rec.breakdown_total_cents, sym))}</span></div>',
    ]

    if rec.cash_withdrawn_cents:
        explained = rec.cash_explained_cents
        pct = explained / rec.cash_withdrawn_cents * 100 if rec.cash_withdrawn_cents else 0
        bits.append(
            '<div class="row"><span class="name">Cash withdrawn'
            f'<span class="meta"> · {pct:.0f}% explained by slips</span></span>'
            f'<span class="val num">{_e(fmt(rec.cash_withdrawn_cents, sym))}</span></div>'
        )

    if rec.coverage_gaps:
        days = sum(g.days for g in rec.coverage_gaps)
        bits.append(
            f'<p class="note"><span class="pill warn">gap</span> '
            f"{days} day(s) in this period have no statement covering them, so "
            "spending then is missing rather than zero.</p>"
        )

    if rec.uncategorised_cents:
        bits.append(
            f'<p class="note">{_e(fmt(rec.uncategorised_cents, sym))} across '
            f"{rec.uncategorised_count} transaction(s) is still uncategorised, "
            'and sits under "Uncategorised" below.</p>'
        )

    for warning in rec.warnings[:4]:
        bits.append(f'<p class="note">{_e(warning)}</p>')

    bits.append("</div>")
    return "\n".join(bits)


def _donut(slices: list[tuple[str, int]], total: int, sym: str) -> str:
    """Top categories as inline SVG arcs.

    Drawn as stroked arc paths rather than filled wedges: one path per segment,
    no fill-rule surprises, and the ring thickness is a single attribute.
    """
    if not slices or total <= 0:
        return ""

    size, radius, thickness = 200, 78, 26
    centre = size / 2
    angle = -math.pi / 2
    arcs, legend = [], []

    for index, (name, value) in enumerate(slices):
        share = value / total
        span = share * 2 * math.pi
        colour = PALETTE[index % len(PALETTE)]

        # A full circle cannot be drawn as a single arc — the start and end
        # points coincide and the path collapses to nothing.
        if share >= 0.9999:
            arcs.append(
                f'<circle cx="{centre}" cy="{centre}" r="{radius}" fill="none" '
                f'stroke="{colour}" stroke-width="{thickness}"></circle>'
            )
        else:
            end = angle + span
            large = 1 if span > math.pi else 0
            x1 = centre + radius * math.cos(angle)
            y1 = centre + radius * math.sin(angle)
            x2 = centre + radius * math.cos(end)
            y2 = centre + radius * math.sin(end)
            arcs.append(
                f'<path d="M {x1:.2f} {y1:.2f} A {radius} {radius} 0 {large} 1 '
                f'{x2:.2f} {y2:.2f}" fill="none" stroke="{colour}" '
                f'stroke-width="{thickness}"><title>{_e(name)}: '
                f"{_e(fmt(value, sym))} ({share * 100:.1f}%)</title></path>"
            )
            angle = end

        legend.append(
            f'<li><span class="swatch" style="background:{colour}"></span>'
            f'<span class="name">{_e(name)}</span>'
            f'<span class="val">{_e(fmt(value, sym))} '
            f'<span class="muted">{share * 100:.0f}%</span></span></li>'
        )

    return f"""<div class="chart">
<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img"
     aria-label="Share of spending by category">
{"".join(arcs)}
<text x="{centre}" y="{centre - 4}" text-anchor="middle" fill="currentColor"
      font-size="11" opacity=".6">total</text>
<text x="{centre}" y="{centre + 14}" text-anchor="middle" fill="currentColor"
      font-size="14" font-weight="650">{_e(fmt(total, sym))}</text>
</svg>
<ul class="legend">{"".join(legend)}</ul>
</div>"""


def _categories(summary: PeriodSummary, sym: str) -> str:
    buckets = [b for b in summary.by_category if b.total_cents > 0]
    if not buckets:
        return ""

    total = sum(b.total_cents for b in buckets)
    shown = buckets[:MAX_DONUT_SLICES]
    rest = sum(b.total_cents for b in buckets[MAX_DONUT_SLICES:])
    slices = [(b.name, b.total_cents) for b in shown]
    if rest > 0:
        slices.append(("Everything else", rest))

    rows = []
    largest = buckets[0].total_cents or 1
    for index, b in enumerate(buckets):
        colour = PALETTE[index % len(PALETTE)] if index < MAX_DONUT_SLICES else "var(--ink-3)"
        width = b.total_cents / largest * 100
        rows.append(
            f'<div class="row"><span class="name">{_e(b.name)}'
            f'<span class="meta"> · {b.count}</span>'
            f'<span class="bar"><span style="width:{width:.1f}%;background:{colour}"></span></span>'
            f'</span><span class="val num">{_e(fmt(b.total_cents, sym))}</span></div>'
        )

    return (
        "<h2>Where it went</h2>"
        f'<div class="card">{_donut(slices, total, sym)}</div>'
        f'<div class="card">{"".join(rows)}</div>'
    )


def _daily(summary: PeriodSummary, sym: str) -> str:
    """Day-by-day spend as an SVG column chart.

    Bars are positioned by date rather than by index, so a gap in the data
    reads as a gap rather than being silently closed up.
    """
    points = [(d, c) for d, c in summary.daily_cents]
    if len(points) < 2:
        return ""

    peak = max(c for _d, c in points) or 1
    start, end = summary.period.start, summary.period.end
    span_days = max((end - start).days, 1)

    width, height, pad = 640, 120, 4
    bar_w = max(width / (span_days + 1) - 1, 1.2)

    bars = []
    for day, cents in points:
        if cents <= 0:
            continue
        x = (day - start).days / span_days * (width - bar_w)
        h = max(cents / peak * (height - pad), 1)
        bars.append(
            f'<rect x="{x:.2f}" y="{height - h:.2f}" width="{bar_w:.2f}" '
            f'height="{h:.2f}" fill="{PALETTE[0]}" rx="1">'
            f"<title>{_e(day)}: {_e(fmt(cents, sym))}</title></rect>"
        )

    if not bars:
        return ""

    busiest_day, busiest_cents = max(points, key=lambda p: p[1])
    active = [c for _d, c in points if c > 0]
    typical = sorted(active)[len(active) // 2] if active else 0

    return f"""<h2>Day by day</h2>
<div class="card">
<svg viewBox="0 0 {width} {height}" width="100%" height="{height}"
     preserveAspectRatio="none" role="img" aria-label="Daily spending">
{"".join(bars)}
</svg>
<div class="row" style="border-top:1px solid var(--line);margin-top:.6rem">
  <span class="name muted">Busiest day</span>
  <span class="val num">{_e(fmt(busiest_cents, sym))}
    <span class="meta">{_e(busiest_day)}</span></span>
</div>
<div class="row">
  <span class="name muted">Typical day with spending</span>
  <span class="val num">{_e(fmt(typical, sym))}</span>
</div>
</div>"""


def _merchants(summary: PeriodSummary, sym: str) -> str:
    buckets = [b for b in summary.by_merchant if b.total_cents > 0][:MAX_MERCHANTS]
    if not buckets:
        return ""

    largest = buckets[0].total_cents or 1
    rows = []
    for b in buckets:
        width = b.total_cents / largest * 100
        rows.append(
            f'<div class="row"><span class="name">{_e(b.name)}'
            f'<span class="meta"> · {b.count} visit(s)</span>'
            f'<span class="bar"><span style="width:{width:.1f}%;background:{PALETTE[2]}"></span></span>'
            f'</span><span class="val num">{_e(fmt(b.total_cents, sym))}</span></div>'
        )

    return f'<h2>Who got the money</h2><div class="card">{"".join(rows)}</div>'


def _recurring_section(recurring: list[Recurring], sym: str) -> str:
    active = [r for r in recurring if r.still_active]
    if not active:
        return ""

    active = sorted(active, key=lambda r: r.annualised_cents, reverse=True)
    annual = sum(r.annualised_cents for r in active)

    rows = []
    for r in active[:MAX_MERCHANTS]:
        varies = ' <span class="pill warn">varies</span>' if r.amount_varies else ""
        rows.append(
            f'<div class="row"><span class="name">{_e(r.merchant)}{varies}'
            f'<span class="meta"> · {_e(r.cadence)}</span></span>'
            f'<span class="val num">{_e(fmt(r.typical_cents, sym))}</span></div>'
        )

    return f"""<h2>Committed every month</h2>
<div class="card">
<p class="note" style="margin-top:0">{len(active)} live commitment(s), costing
about <strong>{_e(fmt(annual // 12, sym))} a month</strong> —
{_e(fmt(annual, sym))} a year.</p>
{"".join(rows)}
</div>"""


def _advice_section(advice, sym: str) -> str:
    if advice is None or not advice.findings:
        return ""

    findings = advice.findings[:8]
    blocks = []
    for f in findings:
        confidence = {
            "high": '<span class="pill good">likely</span>',
            "medium": '<span class="pill warn">possible</span>',
            "low": '<span class="pill">speculative</span>',
        }.get(f.confidence, "")
        blocks.append(
            f"""<div class="finding">
<h3>{_e(f.title)} {confidence}</h3>
<div class="row" style="border:0;padding:.2rem 0">
  <span class="name muted" style="font-size:.83rem">{_e(f.detail)}</span>
  <span class="val num">{_e(fmt(f.monthly_saving_cents, sym))}<span class="meta">/mo</span></span>
</div>
<p class="assume">Assumes: {_e(f.assumption)}</p>
</div>"""
        )

    return f"""<h2>Where you could cut</h2>
<div class="card">
<p class="note" style="margin-top:0">Up to
<strong>{_e(fmt(advice.monthly_total_cents, sym))} a month</strong>
({_e(fmt(advice.annual_total_cents, sym))} a year) if every suggestion below
were acted on. Each states the assumption it rests on — these are estimates,
not predictions.</p>
{"".join(blocks)}
</div>"""
