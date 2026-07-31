"""Categorisation: specificity ordering, merchant labelling, user overrides."""

import json
import tempfile
import unittest
from pathlib import Path

from spendtrack import categorise, taxonomy


class TestBuiltInRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = categorise.Categoriser()

    def assertCategory(self, description, expected, merchant=None):
        result = self.cat.classify(description, -100.0)
        self.assertEqual(result.category, expected, f"{description!r} -> {result.category}")
        if merchant is not None:
            self.assertEqual(result.merchant, merchant, description)

    def test_groceries(self):
        self.assertCategory("CARD PURCHASE 4123****8891 CHECKERS HYPER SANDTON",
                            "Groceries", "Checkers Hyper")
        self.assertCategory("POS PURCHASE WOOLWORTHS FOOD HYDE PARK", "Groceries")
        self.assertCategory("PICK N PAY MIDRAND", "Groceries", "Pick n Pay")

    def test_bank_fees_survive_normalisation(self):
        """description_key drops 'fee'; rule matching uses text that keeps it."""
        self.assertCategory("MONTHLY ACCOUNT FEE", "Bank Charges & Fees")
        self.assertCategory("UNSUCCESSFUL DEBIT ORDER FEE", "Bank Charges & Fees")
        self.assertCategory("CASH WITHDRAWAL FEE", "Bank Charges & Fees")

    def test_interest_is_separate_from_fees(self):
        self.assertCategory("DEBIT INTEREST CHARGED", "Interest & Penalties")

    def test_cash_withdrawal_is_not_swallowed_by_prefix_stripping(self):
        self.assertCategory("ATM CASH WITHDRAWAL SANDTON CITY 04 JUN",
                            taxonomy.CASH, "Cash")

    def test_more_specific_pattern_wins(self):
        """'checkers sixty60' must beat the bare 'checkers'."""
        result = self.cat.classify("CHECKERS SIXTY60 DELIVERY", -100.0)
        self.assertEqual(result.merchant, "Checkers Sixty60")
        self.assertIn("premium", result.flags)

    def test_tops_at_spar_is_alcohol_not_groceries(self):
        self.assertCategory("TOPS AT SPAR SANDTON", "Alcohol & Tobacco")

    def test_income_is_recognised(self):
        result = self.cat.classify("SALARY IGNITION GROUP PTY LTD", 42000.0)
        self.assertEqual(result.category, "Income")
        self.assertEqual(taxonomy.get(result.category).kind, "income")

    def test_refunds_are_their_own_kind(self):
        result = self.cat.classify("REFUND TAKEALOT.COM", 500.0)
        self.assertEqual(taxonomy.get(result.category).kind, "refund")

    def test_transfers_are_marked_internal(self):
        result = self.cat.classify("IB TRANSFER TO SAVINGS", -1000.0)
        self.assertTrue(result.is_internal)

    def test_gambling(self):
        self.assertCategory("BETWAY ZA", "Gambling & Betting", "Betway")
        self.assertCategory("HOLLYWOODBETS DURBAN", "Gambling & Betting")

    def test_delivery_platforms(self):
        self.assertCategory("UBER EATS HELP.UBER.COM", "Food Delivery", "Uber Eats")
        self.assertCategory("MR D FOOD DELIVERY", "Food Delivery", "Mr D Food")

    def test_ride_hailing_not_confused_with_eats(self):
        self.assertCategory("UBER TRIP HELP.UBER.COM", "Ride Hailing", "Uber")

    def test_savings_are_not_spending(self):
        result = self.cat.classify("IB PAYMENT TO EASYEQUITIES ZAR WALLET", -3000.0)
        self.assertEqual(taxonomy.get(result.category).kind, "saving")

    def test_unknown_falls_back(self):
        self.assertCategory("ZZQQ WIDGET EMPORIUM 88", taxonomy.UNCATEGORISED)

    def test_merchant_aliases_are_applied(self):
        self.assertCategory("NANDOS SANDTON", "Takeaways & Fast Food", "Nando's")
        self.assertCategory("APPLE.COM/BILL ICLOUD", "Apps & Software", "Apple")
        self.assertCategory("MULTICHOICE DSTV PREMIUM", "Streaming & Subscriptions",
                            "DStv (MultiChoice)")

    def test_generic_pattern_derives_merchant_from_description(self):
        """'vehicle finance' names no shop, so the description supplies the brand."""
        result = self.cat.classify("IB PAYMENT TO WESBANK VEHICLE FINANCE", -5480.0)
        self.assertEqual(result.category, "Vehicle Finance")
        self.assertEqual(result.merchant, "WesBank")

    def test_forecourt_brand_resolves_to_the_parent(self):
        result = self.cat.classify("SHELL ULTRA CITY MIDRAND", -1015.40)
        self.assertEqual(result.category, "Transport & Fuel")
        self.assertEqual(result.merchant, "Shell")

    def test_card_number_changes_do_not_change_the_answer(self):
        a = self.cat.classify("CARD PURCHASE 4123****8891 VIDA E CAFFE", -52.0)
        b = self.cat.classify("CARD PURCHASE 5199****2201 VIDA E CAFFE", -52.0)
        self.assertEqual(a.category, b.category)
        self.assertEqual(a.merchant, b.merchant)


class TestUserRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rules.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_rule_beats_a_built_in_one(self):
        self.path.write_text(json.dumps({"rules": [{
            "id": "mine", "category": "Housing & Rent", "merchant": "Landlord",
            "patterns": ["checkers"],
        }]}), encoding="utf-8")
        cat = categorise.build(self.path)
        result = cat.classify("CHECKERS HYPER SANDTON", -500.0)
        self.assertEqual(result.category, "Housing & Rent")
        self.assertEqual(result.merchant, "Landlord")

    def test_regex_patterns(self):
        self.path.write_text(json.dumps({"rules": [{
            "id": "rx", "category": "Pets", "patterns": ["re:^woef\\s+\\d+"],
        }]}), encoding="utf-8")
        cat = categorise.build(self.path)
        self.assertEqual(cat.classify("WOEF 1234 PET SHOP", -100.0).category, "Pets")

    def test_category_discretion_override(self):
        self.path.write_text(json.dumps({
            "rules": [], "categories": {"Groceries": {"discretion": 0.9}}}),
            encoding="utf-8")
        categorise.build(self.path)
        self.assertEqual(taxonomy.get("Groceries").discretion, 0.9)
        taxonomy.apply_overrides({"Groceries": {"discretion": 0.20}})   # restore

    def test_broken_rules_file_is_reported_clearly(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            categorise.build(self.path)

    def test_template_is_valid(self):
        written = categorise.write_template(self.path)
        data = json.loads(written.read_text(encoding="utf-8"))
        self.assertIn("rules", data)
        categorise.build(written)   # must not raise


class TestTaxonomy(unittest.TestCase):
    def test_every_category_has_a_sane_discretion(self):
        for name, cat in taxonomy.CATEGORIES.items():
            self.assertGreaterEqual(cat.discretion, 0.0, name)
            self.assertLessEqual(cat.discretion, 1.0, name)

    def test_kinds_are_known(self):
        allowed = {"spend", "transfer", "saving", "debt", "income", "refund", "unknown"}
        for name, cat in taxonomy.CATEGORIES.items():
            self.assertIn(cat.kind, allowed, name)

    def test_every_rule_names_a_known_category(self):
        from spendtrack import rules_default
        for rule in rules_default.flatten():
            self.assertIn(rule["category"], taxonomy.CATEGORIES, rule["id"])

    def test_unknown_category_gets_a_safe_default(self):
        cat = taxonomy.get("Something Invented")
        self.assertEqual(cat.kind, "spend")
        self.assertGreater(cat.discretion, 0.0)


if __name__ == "__main__":
    unittest.main()
