"""Description normalisation, fingerprints and fuzzy comparison."""

import unittest

from spendtrack import normalise


class TestCleanDescription(unittest.TestCase):
    def test_strips_card_mask_and_channel_prefix(self):
        result = normalise.clean_description(
            "CARD PURCHASE 4123****8891 CHECKERS HYPER SANDTON 02 JUN")
        self.assertEqual(result, "CHECKERS HYPER SANDTON")

    def test_strips_debit_order_prefix(self):
        self.assertEqual(
            normalise.clean_description("DEBIT ORDER DISCOVERY LIFE PREMIUM"),
            "DISCOVERY LIFE PREMIUM")

    def test_keeps_cash_withdrawal_wording(self):
        """Stripping this would leave only a suburb name."""
        result = normalise.clean_description("ATM CASH WITHDRAWAL SANDTON CITY 04 JUN")
        self.assertIn("CASH WITHDRAWAL", result.upper())

    def test_never_returns_empty(self):
        self.assertTrue(normalise.clean_description("CARD PURCHASE"))


class TestDescriptionKey(unittest.TestCase):
    def test_collapses_reference_numbers(self):
        a = normalise.description_key("DEBIT ORDER NETFLIX.COM 8829102")
        b = normalise.description_key("DEBIT ORDER NETFLIX.COM 8830011")
        self.assertEqual(a, b)
        self.assertEqual(a, "netflix")

    def test_ignores_card_number_changes(self):
        a = normalise.description_key("CARD PURCHASE 4123****8891 VIDA E CAFFE ROSEBANK")
        b = normalise.description_key("CARD PURCHASE 5199****2201 VIDA E CAFFE ROSEBANK")
        self.assertEqual(a, b)


class TestMatchText(unittest.TestCase):
    def test_keeps_words_rules_need(self):
        """description_key drops 'fee'; rule matching must not."""
        self.assertIn("fee", normalise.match_text("MONTHLY ACCOUNT FEE"))
        self.assertIn("com", normalise.match_text("APPLE.COM/BILL ICLOUD"))


class TestFingerprint(unittest.TestCase):
    def test_stable_for_identical_input(self):
        args = ("main", "2026-06-02", -1842.66, "checkers hyper sandton", 0)
        self.assertEqual(normalise.fingerprint(*args), normalise.fingerprint(*args))

    def test_ordinal_separates_genuine_repeats(self):
        base = ("main", "2026-06-09", -500.0, "betway")
        self.assertNotEqual(normalise.fingerprint(*base, 0),
                            normalise.fingerprint(*base, 1))

    def test_account_is_part_of_identity(self):
        self.assertNotEqual(
            normalise.fingerprint("main", "2026-06-02", -50.0, "coffee", 0),
            normalise.fingerprint("savings", "2026-06-02", -50.0, "coffee", 0))

    def test_amount_change_changes_fingerprint(self):
        self.assertNotEqual(
            normalise.fingerprint("main", "2026-06-02", -50.0, "coffee", 0),
            normalise.fingerprint("main", "2026-06-02", -50.01, "coffee", 0))


class TestSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(normalise.similarity("checkers", "checkers"), 1.0)

    def test_containment_scores_high(self):
        score = normalise.similarity("woolworths", "woolworths food hyde park")
        self.assertGreater(score, 0.8)

    def test_ocr_damage_still_matches(self):
        self.assertGreater(normalise.similarity("checkers hyper", "chekers hyper"), 0.85)

    def test_unrelated_scores_low(self):
        """Well below matching.MERCHANT_FLOOR, while real matches score 0.85+."""
        self.assertLess(normalise.similarity("checkers", "wesbank vehicle finance"), 0.30)
        self.assertLess(normalise.similarity("vida e caffe", "jones properties"), 0.30)

    def test_empty_is_zero(self):
        self.assertEqual(normalise.similarity("", "checkers"), 0.0)


if __name__ == "__main__":
    unittest.main()
