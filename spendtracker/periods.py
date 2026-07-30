"""Period parsing and helpers.

Accepts the shorthands people actually type: "2026-03", "2026-Q1", "2026",
"last-month", "this-month", "last-90-days", "ytd", or an explicit
"2026-01-01:2026-03-31".
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    def __contains__(self, d: date) -> bool:
        return self.start <= d <= self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def months(self) -> float:
        """Approximate length in months, for per-month averages."""
        return max(self.days / 30.44, 1 / 30.44)

    def as_iso(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


def month_period(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    start, end = date(year, month, 1), date(year, month, last)
    return Period(start, end, start.strftime("%B %Y"))


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def parse_period(text: str | None, *, today: date | None = None) -> Period | None:
    """Interpret a period shorthand. Returns None for empty/"all"."""
    today = today or date.today()
    if not text:
        return None
    s = text.strip().lower()
    if s in ("", "all", "everything", "all-time", "alltime"):
        return None

    if s in ("this-month", "thismonth", "current-month"):
        return month_period(today.year, today.month)
    if s in ("last-month", "lastmonth", "previous-month"):
        prev = date(today.year, today.month, 1) - timedelta(days=1)
        return month_period(prev.year, prev.month)
    if s in ("this-year", "thisyear"):
        return Period(date(today.year, 1, 1), date(today.year, 12, 31), str(today.year))
    if s in ("last-year", "lastyear"):
        y = today.year - 1
        return Period(date(y, 1, 1), date(y, 12, 31), str(y))
    if s == "ytd":
        return Period(date(today.year, 1, 1), today, f"{today.year} year to date")

    m = re.fullmatch(r"last[-\s]?(\d+)[-\s]?(day|days|week|weeks|month|months)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("day"):
            start = today - timedelta(days=n - 1)
        elif unit.startswith("week"):
            start = today - timedelta(weeks=n)
        else:
            start = add_months(today, -n)
        return Period(start, today, f"last {n} {unit}")

    m = re.fullmatch(r"(\d{4})[-/]?q([1-4])", s)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        start_month = (q - 1) * 3 + 1
        start = date(year, start_month, 1)
        end = month_end(date(year, start_month + 2, 1))
        return Period(start, end, f"Q{q} {year}")

    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", s)
    if m:
        return month_period(int(m.group(1)), int(m.group(2)))

    m = re.fullmatch(r"(\d{4})", s)
    if m:
        year = int(m.group(1))
        return Period(date(year, 1, 1), date(year, 12, 31), str(year))

    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?::|\.\.|to|--)\s*(\d{4}-\d{2}-\d{2})", s)
    if m:
        start, end = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        if end < start:
            start, end = end, start
        return Period(start, end, f"{start.isoformat()} to {end.isoformat()}")

    raise ValueError(
        f"could not understand period {text!r}. Try 2026-03, 2026-Q1, 2026, "
        "last-month, last-90-days, ytd, or 2026-01-01:2026-03-31"
    )


def months_between(start: date, end: date) -> list[Period]:
    """One Period per calendar month touched by [start, end]."""
    out: list[Period] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        out.append(month_period(cursor.year, cursor.month))
        cursor = add_months(cursor, 1).replace(day=1)
    return out
