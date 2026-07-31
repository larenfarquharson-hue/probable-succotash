"""CSV shapes real banks emit: preambles, split columns, odd sign conventions."""

import tempfile
import unittest
from pathlib import Path

from spendtrack import csvimport
from spendtrack.parsing import ParseError


class ImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, content: str, name: str = "s.csv", **kwargs):
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding=kwargs.pop("encoding", "utf-8"))
        return csvimport.parse_file(path, **kwargs)


class TestHeaderDetection(ImportCase):
    def test_skips_bank_preamble(self):
        result = self.parse(
            "Statement of Account\n"
            "Account Number,1234567890\n"
            "Period,01 June 2026 to 30 June 2026\n"
            "\n"
            "Transaction Date,Description,Amount,Balance\n"
            "01/06/2026,CHECKERS,-500.00,19500.00\n"
        )
        self.assertEqual(result.headers[0], "Transaction Date")
        self.assertEqual(len(result.rows), 1)

    def test_drops_trailing_totals_row(self):
        result = self.parse(
            "Date,Description,Amount\n"
            "01/06/2026,CHECKERS,-500.00\n"
            "Closing Balance,,19500.00\n"
        )
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(len(result.skipped), 1)

    def test_handles_headerless_file(self):
        result = self.parse(
            "01/06/2026,CHECKERS HYPER,-500.00,19500.00\n"
            "02/06/2026,VIDA E CAFFE,-52.00,19448.00\n"
        )
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0].description, "CHECKERS HYPER")

    def test_refuses_a_file_with_no_usable_columns(self):
        with self.assertRaises(ParseError):
            self.parse("Notes\nnothing useful here\nor here\n")


class TestDelimiters(ImportCase):
    def test_semicolon(self):
        result = self.parse("Date;Description;Amount\n01/06/2026;CHECKERS;-500,00\n")
        self.assertEqual(result.delimiter, ";")
        self.assertEqual(result.rows[0].amount, -500.0)

    def test_tab(self):
        result = self.parse("Date\tDescription\tAmount\n01/06/2026\tCHECKERS\t-500.00\n")
        self.assertEqual(result.delimiter, "\t")
        self.assertEqual(len(result.rows), 1)

    def test_pipe(self):
        result = self.parse("Date|Description|Amount\n01/06/2026|CHECKERS|-500.00\n")
        self.assertEqual(result.delimiter, "|")


class TestSignConventions(ImportCase):
    def test_signed_amounts_confirmed_by_balance(self):
        result = self.parse(
            "Date,Description,Amount,Balance\n"
            "01/06/2026,SALARY,20000.00,20000.00\n"
            "02/06/2026,CHECKERS,-500.00,19500.00\n"
            "03/06/2026,COFFEE,-52.00,19448.00\n"
        )
        self.assertEqual(result.sign_convention, "signed")
        self.assertEqual(result.outflow_total, -552.0)
        self.assertEqual(result.inflow_total, 20000.0)

    def test_positive_means_outflow_detected_from_balance(self):
        """Some exports print every amount positive and let the balance say which way."""
        result = self.parse(
            "Date,Description,Amount,Balance\n"
            "02/06/2026,CHECKERS,500.00,19500.00\n"
            "03/06/2026,COFFEE,52.00,19448.00\n"
            "04/06/2026,FUEL,900.00,18548.00\n"
        )
        self.assertEqual(result.sign_convention, "positive_is_outflow")
        self.assertEqual(result.outflow_total, -1452.0)

    def test_split_debit_credit_columns(self):
        result = self.parse(
            "Date;Details;Debit;Credit\n"
            "01-06-2026;SALARY;;20000.00\n"
            "02-06-2026;CHECKERS;500.00;\n"
        )
        self.assertEqual(result.sign_convention, "split_columns")
        self.assertEqual(result.rows[0].amount, 20000.0)
        self.assertEqual(result.rows[1].amount, -500.0)

    def test_indicator_column(self):
        result = self.parse(
            "Date,Description,Amount,Transaction Type\n"
            "01/06/2026,SALARY,20000.00,Credit\n"
            "02/06/2026,CHECKERS,500.00,Debit\n"
        )
        self.assertEqual(result.rows[0].amount, 20000.0)
        self.assertEqual(result.rows[1].amount, -500.0)

    def test_explicit_override_wins(self):
        result = self.parse(
            "Date,Description,Amount\n02/06/2026,CHECKERS,500.00\n",
            positive_is="inflow")
        self.assertEqual(result.rows[0].amount, 500.0)


class TestFeeColumn(ImportCase):
    def test_fee_is_detected_and_negative(self):
        result = self.parse(
            "Date,Description,Amount,Fee\n"
            "02/06/2026,CHECKERS,-500.00,3.50\n"
            "03/06/2026,COFFEE,-52.00,\n"
        )
        self.assertEqual(result.rows[0].fee, -3.5)
        self.assertIsNone(result.rows[1].fee)


class TestEncodings(ImportCase):
    def test_bom_is_stripped(self):
        path = Path(self.tmp.name) / "bom.csv"
        path.write_text("Date,Description,Amount\n01/06/2026,CAFE,-52.00\n",
                        encoding="utf-8-sig")
        result = csvimport.parse_file(path)
        self.assertEqual(result.headers[0], "Date")

    def test_cp1252_falls_back_cleanly(self):
        path = Path(self.tmp.name) / "w.csv"
        path.write_bytes("Date,Description,Amount\n01/06/2026,CAF\xc9,-52.00\n"
                         .encode("cp1252"))
        result = csvimport.parse_file(path)
        self.assertEqual(len(result.rows), 1)


class TestMultipleDescriptionColumns(ImportCase):
    def test_parts_are_joined(self):
        result = self.parse(
            "Date,Type,Description,Amount\n"
            "01/06/2026,Card purchase,CHECKERS HYPER,-500.00\n"
        )
        self.assertIn("CHECKERS HYPER", result.rows[0].description)
        self.assertIn("Card purchase", result.rows[0].description)


class TestProfiles(ImportCase):
    def test_profile_pins_columns_by_name(self):
        result = self.parse(
            "When,What,Value\n01/06/2026,CHECKERS,-500.00\n",
            profile={"name": "t", "columns": {"date": "When", "description": ["What"],
                                              "amount": "Value"}})
        self.assertEqual(result.rows[0].amount, -500.0)
        self.assertEqual(result.rows[0].description, "CHECKERS")

    def test_profile_rejects_unknown_column(self):
        with self.assertRaises(ParseError):
            self.parse("Date,Description,Amount\n01/06/2026,X,-1.00\n",
                       profile={"name": "t", "columns": {"date": "Nope"}})

    def test_example_profile_is_valid_json(self):
        profile = csvimport.load_profile("example")
        self.assertIn("columns", profile)


if __name__ == "__main__":
    unittest.main()
