"""Slip parsing: JSON input, OCR text, and the validation that catches errors."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from spendtrack import slips

CHECKERS_SLIP = """
CHECKERS HYPER
SANDTON CITY
VAT REG NO 4130105207
TEL 011 883 4400
TAX INVOICE
02/06/2026  17:42
Till 07  Op 214

FULL CREAM MILK 2L        65.98 A
2 x 32.99
BREAD WHITE LOAF          21.99 A
CHICKEN BREAST 1KG       129.99 A

SUBTOTAL                 217.96
VAT @ 15%                 28.43
TOTAL                    217.96
CARD                     217.96
VISA DEBIT  ****8891
CHANGE                     0.00

THANK YOU FOR SHOPPING
"""

CASH_SLIP = """
OCEAN BASKET SANDTON
05-06-2026 19:55
CALAMARI PLATTER   378.00
SOFT DRINKS         64.00
TIP                 38.00
TOTAL              480.00
CASH               500.00
CHANGE              20.00
"""


class TestParseSlipText(unittest.TestCase):
    def test_reads_a_card_slip(self):
        slip = slips.parse_slip_text(CHECKERS_SLIP, "IMG_1.jpg")
        self.assertEqual(slip.merchant, "CHECKERS HYPER")
        self.assertEqual(slip.slip_date, date(2026, 6, 2))
        self.assertEqual(slip.slip_time, "17:42")
        self.assertEqual(slip.total, 217.96)
        self.assertEqual(slip.tax, 28.43)
        self.assertEqual(slip.payment_method, slips.PAYMENT_CARD)
        self.assertEqual(slip.card_last4, "8891")
        self.assertEqual(slip.image_path, "IMG_1.jpg")

    def test_line_items_with_quantities(self):
        slip = slips.parse_slip_text(CHECKERS_SLIP)
        descriptions = [i.description for i in slip.items]
        self.assertIn("FULL CREAM MILK 2L", descriptions)
        milk = next(i for i in slip.items if "MILK" in i.description)
        self.assertEqual(milk.line_total, 65.98)
        self.assertEqual(milk.qty, 2.0)
        self.assertEqual(milk.unit_price, 32.99)

    def test_totals_and_vat_are_not_treated_as_items(self):
        slip = slips.parse_slip_text(CHECKERS_SLIP)
        for item in slip.items:
            self.assertNotIn("TOTAL", item.description.upper())
            self.assertNotIn("VAT", item.description.upper())
            self.assertNotIn("CHANGE", item.description.upper())

    def test_cash_slip_is_recognised_as_cash(self):
        slip = slips.parse_slip_text(CASH_SLIP)
        self.assertEqual(slip.payment_method, slips.PAYMENT_CASH)
        self.assertEqual(slip.total, 480.00)
        self.assertEqual(slip.slip_date, date(2026, 6, 5))

    def test_tendered_amount_is_not_mistaken_for_the_total(self):
        """R500 was handed over, but R480 was spent."""
        slip = slips.parse_slip_text(CASH_SLIP)
        self.assertEqual(slip.total, 480.00)

    def test_empty_text_does_not_crash(self):
        slip = slips.parse_slip_text("")
        self.assertIsNone(slip.total)
        self.assertTrue(slip.problems())


class TestSlipValidation(unittest.TestCase):
    def test_flags_missing_fields(self):
        slip = slips.Slip()
        problems = slip.problems()
        self.assertIn("no total", problems)
        self.assertIn("no date", problems)
        self.assertIn("no merchant", problems)

    def test_flags_items_that_do_not_add_up(self):
        slip = slips.Slip(
            merchant="Shop", slip_date=date(2026, 6, 2), total=1000.0,
            items=[slips.SlipItem("thing", line_total=100.0)])
        self.assertTrue(any("line items sum" in p for p in slip.problems()))

    def test_accepts_items_within_tolerance(self):
        slip = slips.Slip(
            merchant="Shop", slip_date=date(2026, 6, 2), total=100.0,
            items=[slips.SlipItem("a", line_total=60.0),
                   slips.SlipItem("b", line_total=40.0)])
        self.assertEqual(slip.problems(), [])


class TestSlipJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data) -> Path:
        path = Path(self.tmp.name) / "slip.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_single_object(self):
        loaded = slips.load_json_file(self.write(slips.SLIP_JSON_TEMPLATE))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].total, 1842.66)
        self.assertEqual(loaded[0].card_last4, "8891")

    def test_list_of_objects(self):
        loaded = slips.load_json_file(self.write([
            {"merchant": "A", "date": "2026-06-01", "total": 10},
            {"merchant": "B", "date": "2026-06-02", "total": 20},
        ]))
        self.assertEqual([s.merchant for s in loaded], ["A", "B"])

    def test_slips_wrapper_key(self):
        loaded = slips.load_json_file(self.write(
            {"slips": [{"merchant": "A", "date": "2026-06-01", "total": 10}]}))
        self.assertEqual(len(loaded), 1)

    def test_alternative_key_spellings(self):
        loaded = slips.load_json_file(self.write({
            "store": "Woolworths", "purchase_date": "02/06/2026",
            "grand_total": "R1 234,56", "vat": "161.03", "tender": "Visa debit",
            "line_items": [{"name": "milk", "amount": 32.99}],
        }))
        slip = loaded[0]
        self.assertEqual(slip.merchant, "Woolworths")
        self.assertEqual(slip.slip_date, date(2026, 6, 2))
        self.assertEqual(slip.total, 1234.56)
        self.assertEqual(slip.tax, 161.03)
        self.assertEqual(slip.payment_method, slips.PAYMENT_CARD)
        self.assertEqual(slip.items[0].line_total, 32.99)

    def test_totals_are_stored_as_positive(self):
        loaded = slips.load_json_file(self.write(
            {"merchant": "A", "date": "2026-06-01", "total": -50}))
        self.assertEqual(loaded[0].total, 50.0)


class TestContentHash(unittest.TestCase):
    def base(self, **kwargs) -> slips.Slip:
        values = {"merchant": "Checkers Hyper", "slip_date": date(2026, 6, 2),
                  "slip_time": "17:42", "total": 500.0}
        values.update(kwargs)
        return slips.Slip(**values)

    def test_same_purchase_hashes_the_same(self):
        self.assertEqual(self.base().content_hash(), self.base().content_hash())

    def test_photo_filename_does_not_affect_identity(self):
        """Two photos of one slip are one slip."""
        self.assertEqual(
            self.base(image_path="a.jpg").content_hash(),
            self.base(image_path="b.jpg").content_hash())

    def test_different_time_is_a_different_purchase(self):
        self.assertNotEqual(self.base().content_hash(),
                            self.base(slip_time="19:10").content_hash())

    def test_different_total_is_a_different_purchase(self):
        self.assertNotEqual(self.base().content_hash(),
                            self.base(total=501.0).content_hash())


class TestPaymentMethod(unittest.TestCase):
    def test_recognises_methods(self):
        cases = {
            "cash": slips.PAYMENT_CASH, "Kontant": slips.PAYMENT_CASH,
            "Visa Debit": slips.PAYMENT_CARD, "mastercard": slips.PAYMENT_CARD,
            "SnapScan": slips.PAYMENT_CARD, "Apple Pay": slips.PAYMENT_CARD,
            "EFT": slips.PAYMENT_EFT, "PayShap": slips.PAYMENT_EFT,
            "": slips.PAYMENT_UNKNOWN, "bartered": slips.PAYMENT_UNKNOWN,
        }
        for value, expected in cases.items():
            self.assertEqual(slips.normalise_payment_method(value), expected, value)


if __name__ == "__main__":
    unittest.main()
