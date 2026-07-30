"""Money handling.

All amounts are stored in the database as integer minor units (cents) to keep
arithmetic exact. Never use floats for money.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Characters that appear around numbers in bank exports but carry no value.
_CURRENCY_JUNK = re.compile(r"[^\d\-+.,()]")
_TRAILING_MINUS = re.compile(r"^([\d.,]+)-$")


class AmountParseError(ValueError):
    """Raised when a string cannot be interpreted as a monetary amount."""


def parse_amount(raw: str | int | float | Decimal | None) -> Decimal | None:
    """Parse a monetary amount from the many shapes banks export.

    Handles: "1 234,56", "1,234.56", "R1 234.56", "-45.00", "(45.00)",
    "45.00-", "1.234,56" (European), "" / None -> None.

    Returns a Decimal, or None for blank input. Negative results mean money
    out in the source document's own convention; callers decide the sign
    semantics for their column layout.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))

    s = str(raw).strip()
    if not s:
        return None

    # Strip currency symbols, letters, non-breaking spaces, thin spaces.
    s = s.replace(" ", " ").replace(" ", " ")
    s = _CURRENCY_JUNK.sub("", s)
    if not s:
        return None

    negative = False

    # Accounting-style parentheses: (45.00) means -45.00
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = s.replace("(", "").replace(")", "")

    # Trailing minus: 45.00-
    m = _TRAILING_MINUS.match(s)
    if m:
        negative = True
        s = m.group(1)

    if s.startswith("-"):
        negative = True
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]

    s = s.replace(" ", "")
    if not s:
        return None

    s = _normalise_separators(s)

    try:
        value = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise AmountParseError(f"cannot parse amount: {raw!r}") from exc

    return -value if negative else value


def _normalise_separators(s: str) -> str:
    """Reduce a digit string with , and . separators to a plain decimal.

    Decides which separator is the decimal point by position, since South
    African and European exports disagree with US ones.
    """
    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        # Whichever appears last is the decimal separator.
        if s.rindex(",") > s.rindex("."):
            return s.replace(".", "").replace(",", ".")
        return s.replace(",", "")

    if has_comma:
        # A single comma with 1-2 trailing digits is a decimal comma
        # ("1234,5" / "1234,56"). Anything else is a thousands separator
        # ("1,234" / "1,234,567").
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            return s.replace(",", ".")
        return s.replace(",", "")

    if has_dot:
        parts = s.split(".")
        if len(parts) > 2:
            # "1.234.567" - multiple groups can only be thousands separators.
            return s.replace(".", "")
        # A single dot is treated as a decimal point, always.
        #
        # "1.500" is genuinely ambiguous in isolation: 1500 in European notation,
        # 1.50 in ours. Guessing by digit-group length is worse than useless -
        # it silently turned "0.005" into 5.00 and would misread "12.500" by a
        # factor of a thousand. Since this function sees one value at a time with
        # no file context, the honest choice is the far more common convention,
        # and to let the importer catch the other case: a genuinely European
        # export is checked against its own running balance, where a 1000x error
        # collapses the agreement and raises a warning instead of passing
        # quietly. Such exports also almost always write the decimal comma
        # ("1.500,00"), which the both-separators branch above handles correctly.
        return s

    return s


def to_cents(value: Decimal | int | float | str | None) -> int | None:
    """Convert an amount to integer cents, rounding half-up."""
    if value is None:
        return None
    dec = value if isinstance(value, Decimal) else parse_amount(value)
    if dec is None:
        return None
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int | None) -> Decimal:
    """Convert integer cents back to a Decimal amount."""
    if cents is None:
        return Decimal("0.00")
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def fmt(cents: int | None, symbol: str = "R", *, signed: bool = False) -> str:
    """Format cents for display, e.g. 123456 -> 'R1 234.56'."""
    if cents is None:
        cents = 0
    negative = cents < 0
    whole, frac = divmod(abs(cents), 100)
    grouped = f"{whole:,}".replace(",", " ")
    body = f"{symbol}{grouped}.{frac:02d}"
    if negative:
        return f"-{body}"
    if signed:
        return f"+{body}"
    return body
