"""Pinned Transco/TGP references are mandatory inputs, never silent skips."""

from __future__ import annotations

import hashlib
import pathlib
import tempfile
import unittest

import run_regressions


class ReferenceRegressionContractTests(unittest.TestCase):
    def test_shipped_reference_identities_are_exact(self):
        self.assertEqual(2, len(run_regressions.REFERENCES))
        for reference in run_regressions.REFERENCES:
            result = run_regressions.reference_identity(reference)
            self.assertEqual("VERIFIED", result["status"], result)
            self.assertEqual(reference["sha256"], result["sha256"])
            self.assertGreater(result["bytes"], 1_000_000)

    def test_absent_required_reference_is_not_a_skip(self):
        with tempfile.TemporaryDirectory(prefix="ferc-reference-missing-") as td:
            path = pathlib.Path(td) / "absent.csv"
            result = run_regressions.reference_identity(
                {"canonical": path, "sha256": "0" * 64})
        self.assertEqual("FIXTURE_MISSING", result["status"])
        self.assertNotEqual("SKIPPED", result["status"])

    def test_changed_reference_fails_closed_and_valid_control_passes(self):
        with tempfile.TemporaryDirectory(prefix="ferc-reference-mutated-") as td:
            path = pathlib.Path(td) / "reference.csv"
            original = b"metric_id,value\nfixture,1\n"
            path.write_bytes(original)
            expected = hashlib.sha256(original).hexdigest()
            reference = {"canonical": path, "sha256": expected}
            self.assertEqual("VERIFIED",
                             run_regressions.reference_identity(reference)["status"])
            path.write_bytes(original + b"mutation,2\n")
            result = run_regressions.reference_identity(reference)
        self.assertEqual("FIXTURE_IDENTITY_MISMATCH", result["status"])
        self.assertEqual(expected, result["expected_sha256"])
        self.assertNotEqual(expected, result["actual_sha256"])


if __name__ == "__main__":
    unittest.main()
