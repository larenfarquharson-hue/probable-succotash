"""Terminal report rendering."""

from __future__ import annotations

import shutil
from datetime import date

from . import config
from .analysis import PeriodReport

BAR_CHARS = "▏▎▍▌▋▊▉█"


def width() -> int:
    return max(72, min(shutil.get_terminal_size((100, 24)).columns, 110))


def render(report: PeriodReport, top_merchants: int = 12,
           show_recurring: bool = True, show_insights: bool = True) -> str:
    cur = report.currency
    out: list[str] = []
    w = width()

    out.append(_rule("=", w))
    out.append(_centre(f"SPENDING REPORT — {report.period.label.upper()}", w))
    if report.accounts:
        out.append(_centre(f"account(s): {', '.join(report.accounts)}", w))
    out.append(_rule("=", w))
    out.append("")

    out += _reconciliation(report, cur, w)
    out.append("")
    out += _where_it_went(report, cur, w)
    out.append("")
    out += _merchants(report, cur, w, top_merchants)
    if report.cash_reallocated:
        out.append("")
        out += _cash_detail(report, cur, w)
    if show_recurring and report.recurring:
        out.append("")
        out += _recurring(report, cur, w)
    out.append("")
    out += _largest(report, cur, w)
    if show_insights and report.insights:
        out.append("")
        out += _insights(report, cur, w)
    if report.data_quality:
        out.append("")
        out += _quality(report, w)
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------

def _rule(char: str, w: int) -> str:
    return char * w


def _centre(text: str, w: int) -> str:
    return text.center(w)


def _heading(text: str, w: int) -> list[str]:
    return [text, _rule("-", w)]


def _bar(value: float, largest: float, cells: int) -> str:
    """A proportional bar drawn with eighth-block characters."""
    if largest <= 0 or value <= 0:
        return ""
    eighths = int(round(value / largest * cells * 8))
    full, rest = divmod(eighths, 8)
    bar = "█" * full
    if rest:
        bar += BAR_CHARS[rest - 1]
    return bar


def _money(value: float, cur: str) -> str:
    return config.money(value, cur)


def _reconciliation(report: PeriodReport, cur: str, w: int) -> list[str]:
    rec = report.reconciliation
    out = _heading("WHERE EVERY RAND WENT", w)
    rows = [
        ("Total out of the account", rec.total_out, True),
        ("  Consumption (real spending)", rec.consumption, False),
        ("  Debt repayments", rec.debt, False),
        ("  Into savings and investments", rec.savings, False),
        ("  Transfers to your own accounts", rec.transfers, False),
        ("  Excluded by you", rec.excluded, False),
    ]
    for label, value, emphasis in rows:
        if value == 0 and not emphasis:
            continue
        share = f"{value / rec.total_out * 100:5.1f}%" if rec.total_out else "    - "
        out.append(f"{label:<44}{_money(value, cur):>16}  {share}")
    out.append("")
    out.append(f"{'Money in':<44}{_money(rec.income, cur):>16}")
    if rec.refunds:
        out.append(f"{'  of which refunds and reversals':<44}{_money(rec.refunds, cur):>16}")
    net = rec.income - rec.total_out
    out.append(f"{'Net change for the period':<44}{_money(net, cur):>16}")
    out.append("")
    if rec.balances:
        out.append(f"Reconciled: the buckets above account for all "
                   f"{_money(rec.total_out, cur)} that left the account.")
    else:
        out.append(f"NOT RECONCILED — buckets differ from the total by "
                   f"{_money(rec.difference, cur)}. Please report this.")
    if rec.cash_withdrawn:
        out.append(f"Cash: {_money(rec.cash_withdrawn, cur)} withdrawn, "
                   f"{_money(rec.cash_explained, cur)} explained by till slips, "
                   f"{_money(rec.cash_unexplained, cur)} still unexplained.")
    coverage = report.slip_coverage
    if coverage.get("value_matched"):
        out.append(f"Till slips cover {_money(coverage['value_matched'], cur)} of spend "
                   f"({coverage.get('pct_of_consumption', 0)}% of consumption).")
    return out


def _where_it_went(report: PeriodReport, cur: str, w: int) -> list[str]:
    out = _heading("BY TYPE OF SPEND", w)
    if not report.categories:
        out.append("(nothing to show)")
        return out
    total = sum(b.total for b in report.categories)
    largest = report.categories[0].total
    bar_cells = max(10, w - 62)
    for bucket in report.categories:
        share = bucket.total / total * 100 if total else 0
        note = ""
        if bucket.from_cash:
            note = f" ({_money(bucket.from_cash, cur)} from cash slips)"
        out.append(
            f"{bucket.name[:28]:<28}{_money(bucket.total, cur):>13}{share:>7.1f}%  "
            f"{_bar(bucket.total, largest, bar_cells)}{note}"
        )
    out.append(_rule("-", w))
    out.append(f"{'Total categorised':<28}{_money(total, cur):>13}")
    rec = report.reconciliation
    left_out = round(rec.transfers + rec.excluded, 2)
    if left_out:
        out.append(f"  plus {_money(rec.transfers, cur)} transferred between your own "
                   f"accounts and {_money(rec.excluded, cur)} you excluded,")
        out.append(f"  giving the {_money(rec.total_out, cur)} total above.")
    out.append("")
    out.append("By group:")
    gtotal = sum(b.total for b in report.groups) or 1
    for bucket in report.groups:
        out.append(f"  {bucket.name[:26]:<26}{_money(bucket.total, cur):>13}"
                   f"{bucket.total / gtotal * 100:>7.1f}%")
    return out


def _merchants(report: PeriodReport, cur: str, w: int, limit: int) -> list[str]:
    out = _heading(f"BY MERCHANT (top {limit})", w)
    if not report.merchants:
        out.append("(nothing to show)")
        return out
    largest = report.merchants[0].total
    bar_cells = max(10, w - 62)
    for bucket in report.merchants[:limit]:
        avg = bucket.total / bucket.count if bucket.count else 0
        out.append(
            f"{bucket.name[:28]:<28}{_money(bucket.total, cur):>13}"
            f"{bucket.count:>5}x{_money(avg, cur):>12}  "
            f"{_bar(bucket.total, largest, bar_cells - 12)}"
        )
    remaining = report.merchants[limit:]
    if remaining:
        out.append(f"{'... and ' + str(len(remaining)) + ' more':<28}"
                   f"{_money(sum(b.total for b in remaining), cur):>13}")
    return out


def _cash_detail(report: PeriodReport, cur: str, w: int) -> list[str]:
    rec = report.reconciliation
    out = _heading("WHAT THE CASH BOUGHT", w)
    out.append(f"Withdrawn in this period: {_money(rec.cash_withdrawn, cur)}. Till slips "
               f"account for {_money(rec.cash_explained, cur)} of it:")
    for entry in report.cash_reallocated:
        out.append(f"  {entry['category'][:28]:<28}{_money(entry['total'], cur):>13}"
                   f"  ({entry['count']} slip(s))")
    if rec.cash_unexplained > 0:
        out.append(f"  {'Still unexplained':<28}{_money(rec.cash_unexplained, cur):>13}")
    out.append("")
    out.append("These amounts were moved out of Cash Withdrawals into the categories")
    out.append("above. Nothing was added — the period total is unchanged.")
    return out


def _recurring(report: PeriodReport, cur: str, w: int) -> list[str]:
    fixed = [e for e in report.recurring if e["fixed"]]
    variable = [e for e in report.recurring if not e["fixed"]]
    out = _heading("MONTHLY COMMITMENTS", w)

    if fixed:
        out.append("Fixed — same amount every month, decided once:")
        out.append(f"  {'Merchant':<24}{'Per month':>12}{'Per year':>12}  Months  Category")
        for entry in fixed[:20]:
            out.append(
                f"  {str(entry['merchant'])[:24]:<24}"
                f"{_money(entry['typical_amount'], cur):>12}"
                f"{_money(entry['annualised'], cur):>12}"
                f"{entry['months_seen']:>8}  {str(entry['category'])[:20]}"
            )
        total_annual = sum(e["annualised"] for e in fixed)
        out.append(f"  {'Committed annually':<24}{'':>12}{_money(total_annual, cur):>12}")

    if variable:
        out.append("")
        out.append("Monthly but variable — recurs, amount moves:")
        out.append(f"  {'Merchant':<24}{'Typical':>12}{'Range':>22}  Category")
        for entry in variable[:12]:
            span = (f"{_money(entry['min_amount'], cur)}–"
                    f"{_money(entry['max_amount'], cur)}")
            out.append(
                f"  {str(entry['merchant'])[:24]:<24}"
                f"{_money(entry['typical_amount'], cur):>12}{span:>22}  "
                f"{str(entry['category'])[:20]}"
            )
    if not fixed and not variable:
        out.append("(none detected — this needs at least two months of data)")
    return out


def _largest(report: PeriodReport, cur: str, w: int) -> list[str]:
    out = _heading("LARGEST SINGLE OUTFLOWS", w)
    for entry in report.largest[:10]:
        out.append(f"{entry['date']}  {_money(entry['amount'], cur):>13}  "
                   f"{str(entry['category'])[:22]:<22} {entry['description'][:w - 58]}")
    return out


def _insights(report: PeriodReport, cur: str, w: int) -> list[str]:
    out = _heading("WHAT COULD BE CUT", w)
    savings = [i for i in report.insights if i.counts_to_total]
    context = [i for i in report.insights if not i.counts_to_total]

    for index, insight in enumerate(savings, start=1):
        out.append(f"{index}. {insight.title}  [{insight.confidence} confidence]")
        out.append(f"   Spent this period: {_money(insight.period_amount, cur)}")
        out.append(f"   Could save: {_money(insight.monthly_saving, cur)} a month "
                   f"({_money(insight.annual_saving, cur)} a year)")
        for line in _wrap(insight.detail, w - 6):
            out.append(f"   {line}")
        if insight.action:
            for num, line in enumerate(_wrap(insight.action, w - 12)):
                out.append(f"   {'Do this:' if num == 0 else '        '} {line}")
        if insight.evidence:
            out.append("   Evidence:")
            for line in insight.evidence[:6]:
                out.append(f"     - {line}")
        out.append("")

    out.append(_rule("-", w))
    out.append(f"{'Total realistically reducible':<44}"
               f"{_money(report.monthly_reducible, cur):>16} a month")
    out.append(f"{'':<44}{_money(report.annual_reducible, cur):>16} a year")
    consumption_month = report.reconciliation.consumption / report.period.months
    if consumption_month:
        pct = report.monthly_reducible / consumption_month * 100
        out.append(f"That is {pct:.0f}% of monthly consumption spend of "
                   f"{_money(consumption_month, cur)}.")
    out.append("Suggestions do not overlap: each transaction feeds at most one of them.")

    if context:
        out.append("")
        out.append("For context (no saving claimed):")
        for insight in context:
            out.append(f"  - {insight.title}")
            for line in _wrap(insight.detail, w - 6):
                out.append(f"    {line}")
    return out


def _quality(report: PeriodReport, w: int) -> list[str]:
    out = _heading("WORTH KNOWING ABOUT THESE NUMBERS", w)
    for note in report.data_quality:
        for num, line in enumerate(_wrap(note, w - 4)):
            out.append(("  - " if num == 0 else "    ") + line)
    return out


def _wrap(text: str, limit: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, max(30, limit)) or [""]


# --------------------------------------------------------------------------
# Smaller renderings used by other commands
# --------------------------------------------------------------------------

def _comparison_caveats(result: dict, periods: list[str]) -> list[str]:
    """Warn where a period is short, or its data stops early.

    A full month next to a half month reads as a collapse in spending when it is
    only a shorter window. Both causes are worth naming: the period itself being
    shorter, and the statements simply not covering all of it yet.
    """
    reports = result.get("reports") or []
    if not reports:
        return []
    notes: list[str] = []

    lengths = [r.period.days for r in reports]
    if max(lengths) - min(lengths) > 0.2 * max(lengths):
        notes.append("these periods are different lengths — " + ", ".join(
            f"{label} {days} days" for label, days in zip(periods, lengths)))

    partial = []
    for label, report in zip(periods, reports):
        if not report.daily:
            continue
        last = report.daily[-1][0]
        covered = (date.fromisoformat(last)
                   - date.fromisoformat(report.period.start)).days + 1
        if covered < report.period.days * 0.85:
            partial.append(f"{label} has data only to {last}")
    if partial:
        notes.append("; ".join(partial))

    if not notes:
        return []
    out = ["Note: " + notes[0] + "."]
    out += [f"      {note}." for note in notes[1:]]
    out.append("      Differences below partly reflect that, not only behaviour.")
    out.append("")
    return out


def render_comparison(result: dict, currency: str = "R", limit: int = 25) -> str:
    periods = result["periods"]
    w = 30 + 14 * len(periods) + 14
    lines = [_rule("=", w), _centre("PERIOD COMPARISON", w), _rule("=", w), ""]

    lines.extend(_comparison_caveats(result, periods))

    header = (f"{'Category':<28}" + "".join(f"{p[:12]:>14}" for p in periods)
              + f"{'Change':>14}")
    lines.append(header)
    lines.append(_rule("-", w))
    for row in result["rows"][:limit]:
        cells = "".join(f"{config.money(v, currency):>14}" for v in row["values"])
        arrow = "↑" if row["change"] > 0 else ("↓" if row["change"] < 0 else " ")
        lines.append(f"{row['category'][:28]:<28}{cells}"
                     f"{arrow + config.money(abs(row['change']), currency):>14}")
    return "\n".join(lines)


def render_slip_table(rows: list, currency: str = "R") -> str:
    if not rows:
        return "No slips stored yet."
    out = [f"{'ID':>5}  {'Date':<11}{'Merchant':<26}{'Total':>12}  {'Method':<8}"
           f"{'Status':<20}Txn"]
    out.append("-" * 96)
    for row in rows:
        total = config.money(float(row["total"]), currency) if row["total"] is not None else "-"
        out.append(
            f"{row['id']:>5}  {str(row['slip_date'] or '-'):<11}"
            f"{str(row['merchant'] or '-')[:26]:<26}{total:>12}  "
            f"{str(row['payment_method'] or '-'):<8}{str(row['status']):<20}"
            f"{row['matched_txn_id'] if row['matched_txn_id'] else '-'}"
        )
    return "\n".join(out)


def render_transactions(rows: list, currency: str = "R", limit: int = 200) -> str:
    if not rows:
        return "No transactions match."
    out = [f"{'ID':>6}  {'Date':<11}{'Amount':>13}  {'Category':<24}"
           f"{'Merchant':<22}Description"]
    out.append("-" * 118)
    for row in rows[:limit]:
        out.append(
            f"{row['id']:>6}  {row['txn_date']:<11}"
            f"{config.money(float(row['amount']), currency):>13}  "
            f"{str(row['category'] or '-')[:24]:<24}"
            f"{str(row['merchant'] or '-')[:22]:<22}{row['description'][:40]}"
        )
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more")
    return "\n".join(out)
