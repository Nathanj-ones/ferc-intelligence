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


if __name__ == "__main__":
    unittest.main()
