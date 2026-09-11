"""Build-B refusals must retain the exact comparison that failed."""

from __future__ import annotations

import unittest

from implementation import run_build_b_acceptance as build_b


class BuildBFailureEvidenceTests(unittest.TestCase):
    def test_mismatch_is_recorded_before_fail_closed_exception(self):
        record = {"comparisons": {}}
        mismatch = {
            "match": False,
            "mismatches": [{"table": "filings", "field": "normalized_sha256"}],
        }
        with self.assertRaisesRegex(build_b.BuildBRefused, "outside allowlist"):
            build_b._require_recorded_comparison(
                record, "database_build_a_build_b", mismatch,
                "outside allowlist")
        self.assertIs(mismatch, record["comparisons"]["database_build_a_build_b"])

    def test_valid_comparison_is_recorded_and_returned(self):
        record = {}
        result = {"match": True, "mismatches": []}
        returned = build_b._require_recorded_comparison(
            record, "publication_build_a_build_b", result, "must not raise")
        self.assertIs(result, returned)
        self.assertEqual(result, record["comparisons"]["publication_build_a_build_b"])


if __name__ == "__main__":
    unittest.main()
