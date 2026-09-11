"""Regression controls for stable blocker identity and lifecycle timestamps."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from ferclib.staging import Staging


class BlockerLifecycleDeterminismTests(unittest.TestCase):
    def test_same_identity_preserves_original_open_time_and_updates_detail(self):
        with tempfile.TemporaryDirectory(prefix="ferc-blocker-lifecycle-") as td:
            store = Staging(pathlib.Path(td) / "staging.sqlite")
            self.addCleanup(store.close)
            with mock.patch("ferclib.staging.now", return_value="2026-09-09T01:00:00+00:00"):
                blocker_id = store.open_blocker(
                    "ioc", "source", "first summary", scope="C1:2024",
                    key="stable-question", attempts="1", exact_error="first")

            # A resolution is an ordinary lifecycle transition. Reopening the
            # same question must clear it without rewriting when it first arose.
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE blockers SET resolved_at=? WHERE blocker_id=?",
                    ("2026-09-09T02:00:00+00:00", blocker_id))
            with mock.patch("ferclib.staging.now", return_value="2026-09-10T03:00:00+00:00"):
                repeated = store.open_blocker(
                    "ioc", "source", "updated summary", scope="C1:2024",
                    key="stable-question", attempts="2", exact_error="second")

            self.assertEqual(blocker_id, repeated)
            rows = store.query("SELECT * FROM blockers WHERE blocker_id=?", (blocker_id,))
            self.assertEqual(1, len(rows))
            row = rows[0]
            self.assertEqual("2026-09-09T01:00:00+00:00", row["opened_at"])
            self.assertIsNone(row["resolved_at"])
            self.assertEqual("updated summary", row["summary"])
            self.assertEqual("2", row["attempts"])
            self.assertEqual("second", row["exact_error"])

    def test_distinct_stable_key_creates_a_distinct_blocker(self):
        with tempfile.TemporaryDirectory(prefix="ferc-blocker-key-") as td:
            store = Staging(pathlib.Path(td) / "staging.sqlite")
            self.addCleanup(store.close)
            with mock.patch("ferclib.staging.now", return_value="2026-09-09T01:00:00+00:00"):
                first = store.open_blocker(
                    "ioc", "source", "same prose", scope="C1", key="question-a")
            with mock.patch("ferclib.staging.now", return_value="2026-09-10T01:00:00+00:00"):
                second = store.open_blocker(
                    "ioc", "source", "same prose", scope="C1", key="question-b")
            self.assertNotEqual(first, second)
            self.assertEqual(2, store.query("SELECT COUNT(*) AS n FROM blockers")[0]["n"])

    def test_exact_key_resolution_does_not_close_a_same_scope_sibling(self):
        with tempfile.TemporaryDirectory(prefix="ferc-blocker-resolve-") as td:
            store = Staging(pathlib.Path(td) / "staging.sqlite")
            self.addCleanup(store.close)
            target = store.open_blocker(
                "ioc", "execution", "runner unit failed", scope="C1",
                key="unit-failure", exact_error="[fixture] runner failure")
            sibling = store.open_blocker(
                "ioc", "source", "adapter search failed", scope="C1",
                key="adapter-search", exact_error="[fixture] source failure")

            self.assertEqual(0, store.resolve_blocker(
                "ioc", "C1", key="not-the-target"))
            with mock.patch("ferclib.staging.now",
                            return_value="2026-09-10T04:00:00+00:00"):
                self.assertEqual(1, store.resolve_blocker(
                    "ioc", "C1", key="unit-failure"))

            rows = {row["blocker_id"]: row for row in store.query(
                "SELECT blocker_id,resolved_at FROM blockers WHERE scope='C1'")}
            self.assertEqual("2026-09-10T04:00:00+00:00",
                             rows[target]["resolved_at"])
            self.assertIsNone(rows[sibling]["resolved_at"])


if __name__ == "__main__":
    unittest.main()
