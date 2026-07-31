"""Tolerant parsing of the dates and money values banks put in CSV exports."""

from __future__ import annotations

import re
from datetime import date, datetime

# Formats tried in order. A format only wins if it parses *every* date in the
# file, which is how ambiguous 01/02/2026 gets resolved without guessing.
DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d",
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%m/%d/%Y", "%m-%d-%Y",
    "%d/%m/%y", "%d-%m-%y", "%m/%d/%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y",
    "%b %d %Y", "%B %d %Y", "%b %d, %Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S",
]

_NBSP = " "
_CURRENCY = re.compile(r"[Rr]\s?(?=[\d(.,-])|[$€£]|\bZAR\b|\bzar\b")
_TRAILING_MARKER = re.compile(r"\b(cr|dr|db)\b\.?\s*$", re.I)
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


class ParseError(ValueError):
    """Raised when a value cannot be interpreted at all."""


def parse_date(value: str) -> date:
    """Parse one date value, trying every known format."""
    text = (value or "").strip().strip('"')
    if not text:
        raise ParseError("empty date")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Excel-style serial numbers (days since 1899-12-30).
    if _NUMERIC.match(text):
        serial = float(text)
        if 20000 < serial < 80000:
            from datetime import timedelta
            return date(1899, 12, 30) + timedelta(days=int(serial))
    raise ParseError(f"unrecognised date: {value!r}")


def detect_date_format(values: list[str]) -> str | None:
    """Find the single format that parses every supplied value.

    Returns None when no one format covers them all (callers then fall back to
    per-value parsing, which is more permissive but can misread 01/02).
    """
    candidates = [v.strip().strip('"') for v in values if v and v.strip()]
    if not candidates:
        return None
    for fmt in DATE_FORMATS:
        try:
            for value in candidates:
                datetime.strptime(value, fmt)
        except ValueError:
            continue
        return fmt
    return None


def parse_dates(values: list[str]) -> list[date]:
    """Parse a whole column, using a file-wide format when one exists."""
    fmt = detect_date_format(values)
    out: list[date] = []
    for value in values:
        text = (value or "").strip().strip('"')
        if fmt:
            out.append(datetime.strptime(text, fmt).date())
        else:
            out.append(parse_date(text))
    return out


def parse_amount(value: str) -> float:
    """Parse a money value from any of the shapes banks emit.

    Handles: R1 234,56 / 1,234.56 / (450.00) / 450.00- / 450.00 Cr / -R99.
    """
    if value is None:
        raise ParseError("empty amount")
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().strip('"').replace(_NBSP, " ")
    if not text or text in {"-", "--", "N/A", "n/a", "nil"}:
        raise ParseError("empty amount")

    negative = False
    credit_marker = False

    marker = _TRAILING_MARKER.search(text)
    if marker:
        if marker.group(1).lower() == "cr":
            credit_marker = True
        else:
            negative = True
        text = text[: marker.start()].strip()

    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = _CURRENCY.sub("", text).strip()

    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    if text.startswith("+"):
        text = text[1:].strip()

    text = text.replace(" ", "")
    if not text:
        raise ParseError(f"no digits in amount: {value!r}")

    # Decide which separator is the decimal point.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")   # 1.234,56
        else:
            text = text.replace(",", "")                     # 1,234.56
    elif "," in text:
        head, _, tail = text.rpartition(",")
        # A 1-2 digit tail is a decimal comma; 3 digits is a thousands group.
        if len(tail) in (1, 2) and head:
            text = f"{head.replace(',', '')}.{tail}"
        else:
            text = text.replace(",", "")

    try:
        number = float(text)
    except ValueError as exc:
        raise ParseError(f"unrecognised amount: {value!r}") from exc

    if negative:
        number = -abs(number)
    if credit_marker:
        number = abs(number)
    return number


def try_amount(value: str) -> float | None:
    try:
        return parse_amount(value)
    except (ParseError, TypeError):
        return None


def looks_like_date(value: str) -> bool:
    try:
        parse_date(value)
        return True
    except ParseError:
        return False


def looks_like_amount(value: str) -> bool:
    return try_amount(value) is not None
