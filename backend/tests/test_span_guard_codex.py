"""Regression controls for occurrence-level document span support."""

from __future__ import annotations

import unittest

from ferclib import elibrary


class SpanGuardRegressionTests(unittest.TestCase):
    def test_numeric_zero_is_a_real_required_value(self):
        self.assertEqual(
            elibrary.require_span_support(
                "The reported adjustment is 0 Dth.", 0, what="adjustment"),
            "The reported adjustment is 0 Dth.",
        )
        self.assertTrue(elibrary.span_supports("The reported adjustment is 0.00.", "0"))

    def test_zero_does_not_match_inside_another_number(self):
        for passage in (
            "The reported adjustment is 10 Dth.",
            "The order was issued in 2026.",
            "Docket RP20-100 contains no stated adjustment.",
        ):
            with self.subTest(passage=passage):
                self.assertFalse(elibrary.span_supports(passage, "0"))
                with self.assertRaises(elibrary.SpanSupportError):
                    elibrary.require_span_support(passage, 0, what="adjustment")

    def test_formatted_number_matches_only_a_complete_numeric_token(self):
        self.assertTrue(elibrary.span_supports("Capacity is 1,250.00 Dth.", "1250"))
        self.assertFalse(elibrary.span_supports("Capacity is 12,500 Dth.", "1250"))

    def test_number_attached_to_a_recognised_filed_unit_is_still_a_complete_token(self):
        self.assertTrue(elibrary.span_supports(
            "Rate Schedule FTS 380,000MMBTU/d", "380,000"))
        self.assertTrue(elibrary.span_supports(
            "Maximum withdrawal 1,250.00Dth/day", "1250"))
        self.assertFalse(elibrary.span_supports(
            "Rate Schedule FTS 3,800,000MMBTU/d", "380,000"))
        self.assertFalse(elibrary.span_supports(
            "identifier X380000MMBTU/d", "380000"))

    def test_nil_and_blank_remain_constructor_errors(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                with self.assertRaises(elibrary.SpanSupportError):
                    elibrary.require_span_support("anything", value, what="value")


if __name__ == "__main__":
    unittest.main()
