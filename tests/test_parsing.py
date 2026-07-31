"""Amount and date parsing — the shapes real bank exports actually use."""

import unittest
from datetime import date

from spendtrack import parsing
from spendtrack.parsing import ParseError


class TestParseAmount(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parsing.parse_amount("1234.56"), 1234.56)
        self.assertEqual(parsing.parse_amount("-99"), -99.0)
        self.assertEqual(parsing.parse_amount("0.00"), 0.0)

    def test_thousands_separators(self):
        self.assertEqual(parsing.parse_amount("1,234.56"), 1234.56)
        self.assertEqual(parsing.parse_amount("12,345,678.90"), 12345678.90)

    def test_decimal_comma(self):
        self.assertEqual(parsing.parse_amount("1234,56"), 1234.56)
        self.assertEqual(parsing.parse_amount("1.234,56"), 1234.56)

    def test_space_thousands(self):
        self.assertEqual(parsing.parse_amount("1 234,56"), 1234.56)
        self.assertEqual(parsing.parse_amount("1 234.56"), 1234.56)

    def test_currency_symbols(self):
        self.assertEqual(parsing.parse_amount("R1 842,66"), 1842.66)
        self.assertEqual(parsing.parse_amount("-R99.00"), -99.0)
        self.assertEqual(parsing.parse_amount("ZAR 450.00"), 450.0)

    def test_parentheses_are_negative(self):
        self.assertEqual(parsing.parse_amount("(450.00)"), -450.0)
        self.assertEqual(parsing.parse_amount("(R1,200.00)"), -1200.0)

    def test_trailing_minus(self):
        self.assertEqual(parsing.parse_amount("450.00-"), -450.0)

    def test_credit_debit_markers(self):
        self.assertEqual(parsing.parse_amount("450.00 Cr"), 450.0)
        self.assertEqual(parsing.parse_amount("450.00 Dr"), -450.0)
        self.assertEqual(parsing.parse_amount("1,000.00 CR"), 1000.0)

    def test_three_digit_group_is_not_decimal(self):
        # "1,234" is one thousand two hundred and thirty four, not 1.234
        self.assertEqual(parsing.parse_amount("1,234"), 1234.0)

    def test_rejects_junk(self):
        for value in ("", "   ", "-", "N/A", "abc"):
            with self.assertRaises(ParseError):
                parsing.parse_amount(value)


class TestParseDate(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(parsing.parse_date("2026-06-02"), date(2026, 6, 2))

    def test_day_first(self):
        self.assertEqual(parsing.parse_date("02/06/2026"), date(2026, 6, 2))
        self.assertEqual(parsing.parse_date("25-12-2026"), date(2026, 12, 25))

    def test_month_names(self):
        self.assertEqual(parsing.parse_date("02 Jun 2026"), date(2026, 6, 2))
        self.assertEqual(parsing.parse_date("2 June 2026"), date(2026, 6, 2))

    def test_rejects_junk(self):
        with self.assertRaises(ParseError):
            parsing.parse_date("Closing Balance")

    def test_format_detection_resolves_ambiguity(self):
        """A file-wide format keeps 01/02 from being read as January the second."""
        values = ["01/02/2026", "15/02/2026", "28/02/2026"]
        self.assertEqual(parsing.detect_date_format(values), "%d/%m/%Y")
        parsed = parsing.parse_dates(values)
        self.assertEqual(parsed[0], date(2026, 2, 1))
        self.assertEqual(parsed[2], date(2026, 2, 28))

    def test_ambiguous_when_all_days_low(self):
        """With nothing above 12, day-first wins because it is listed first."""
        self.assertEqual(parsing.detect_date_format(["01/02/2026"]), "%d/%m/%Y")


if __name__ == "__main__":
    unittest.main()
