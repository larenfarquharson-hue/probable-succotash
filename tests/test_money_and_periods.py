from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from spendtracker.money import fmt, from_cents, parse_amount, to_cents
from spendtracker.periods import parse_period


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234.56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1 234.56", "1234.56"),
        ("1 234,56", "1234.56"),      # South African / European decimal comma
        ("1.234,56", "1234.56"),      # European thousands dot
        ("1.234.567,89", "1234567.89"),
        ("R1 234.56", "1234.56"),
        ("-45.00", "-45.00"),
        ("(45.00)", "-45.00"),        # accounting parentheses
        ("45.00-", "-45.00"),         # trailing minus
        ("+45.00", "45.00"),
        ("0.00", "0.00"),
        ("1,234", "1234"),            # comma as thousands, no decimals
        ("1234,5", "1234.5"),         # single trailing digit is a decimal comma
        # A single dot is always the decimal point. Guessing "European
        # thousands" from digit-group length turned 0.005 into 5.00.
        ("0.005", "0.005"),
        ("1.500", "1.500"),
        ("12.500", "12.500"),
        # Multiple dot groups are unambiguously thousands separators.
        ("1.234.567", "1234567"),
    ],
)
def test_parse_amount_formats(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "   ", None, "abc", "R"])
def test_parse_amount_blank_is_none(raw):
    assert parse_amount(raw) is None


def test_cents_round_trip_is_exact():
    assert to_cents("1234.56") == 123456
    assert to_cents("0.005") == 1          # half-up, not banker's rounding (0.5c -> 1c)
    assert to_cents("0.004") == 0
    assert to_cents("2.675") == 268        # the classic float-rounding trap
    assert to_cents("-19.99") == -1999
    assert from_cents(123456) == Decimal("1234.56")


def test_formatting_uses_space_thousands():
    assert fmt(123456) == "R1 234.56"
    assert fmt(-123456) == "-R1 234.56"
    assert fmt(0) == "R0.00"
    assert fmt(None) == "R0.00"
    assert fmt(50, "$") == "$0.50"


def test_amounts_never_lose_cents_when_summed():
    """The whole point of integer cents: 100 x 0.01 must be exactly 1.00."""
    total = sum(to_cents("0.01") for _ in range(100))
    assert total == 100
    assert from_cents(total) == Decimal("1.00")


class TestPeriods:
    today = date(2026, 7, 15)

    def test_month(self):
        p = parse_period("2026-03")
        assert (p.start, p.end) == (date(2026, 3, 1), date(2026, 3, 31))
        assert p.label == "March 2026"

    def test_february_leap_year(self):
        p = parse_period("2024-02")
        assert p.end == date(2024, 2, 29)

    def test_quarter(self):
        p = parse_period("2026-Q1")
        assert (p.start, p.end) == (date(2026, 1, 1), date(2026, 3, 31))

    def test_year(self):
        p = parse_period("2026")
        assert (p.start, p.end) == (date(2026, 1, 1), date(2026, 12, 31))

    def test_last_month(self):
        p = parse_period("last-month", today=self.today)
        assert (p.start, p.end) == (date(2026, 6, 1), date(2026, 6, 30))

    def test_relative_days(self):
        p = parse_period("last-90-days", today=self.today)
        assert p.end == self.today
        assert p.days == 90

    def test_explicit_range(self):
        p = parse_period("2026-01-01:2026-03-31")
        assert (p.start, p.end) == (date(2026, 1, 1), date(2026, 3, 31))

    def test_reversed_range_is_corrected(self):
        p = parse_period("2026-03-31:2026-01-01")
        assert p.start < p.end

    def test_all_means_no_period(self):
        assert parse_period("all") is None
        assert parse_period("") is None
        assert parse_period(None) is None

    def test_unparseable_raises_with_guidance(self):
        with pytest.raises(ValueError, match="last-90-days"):
            parse_period("sometime in march-ish")

    def test_contains(self):
        p = parse_period("2026-03")
        assert date(2026, 3, 15) in p
        assert date(2026, 4, 1) not in p
