"""Regression controls for deterministic filing-occurrence revision graphs."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from ferclib.staging import Staging


SOURCE = "eLibrary"
ENTITY = "C001088"
FORM = "Form 549B IOC"
SNAPSHOT = "2026-01-01"


def filing(filing_id, submitted_on, content_hash):
    return {
        "source_system": SOURCE,
        "filing_id": filing_id,
        "entity_key": ENTITY,
        "form": FORM,
        "reporting_year": 2026,
        "reporting_period": "Q1",
        "submitted_on": submitted_on,
        "filed_date": submitted_on,
        "snapshot_date": SNAPSHOT,
        "content_hash": content_hash,
        "is_canonical": 0,
        "canonical_reason": "provisional",
        "version_status": "unresolved",
        "supersedes_filing_id": None,
    }


class RevisionGraphRegressionTests(unittest.TestCase):
    def _replay(self, order, rows):
        temp = tempfile.TemporaryDirectory(prefix="ferc-revision-test-")
        self.addCleanup(temp.cleanup)
        staging = Staging(pathlib.Path(temp.name) / "test.sqlite")
        self.addCleanup(staging.close)
        staging.upsert("entities", [{
            "entity_key": ENTITY, "cid": ENTITY,
            "legal_name": "Synthetic revision filer",
        }], ["entity_key"])
        by_id = {row["filing_id"]: row for row in rows}
        for filing_id in order:
            row = dict(by_id[filing_id])
            status, supersedes = staging.classify_version(
                SOURCE, ENTITY, FORM, 2026, "Q1", filing_id,
                row["content_hash"], snapshot_date=SNAPSHOT,
                submitted_on=row["submitted_on"])
            row["version_status"] = status
            row["supersedes_filing_id"] = supersedes
            staging.write_filing_bundle(row)
            staging.normalize_revision_group(
                SOURCE, ENTITY, FORM, snapshot_date=SNAPSHOT,
                set_canonical=True)
        return [dict(r) for r in staging.query(
            "SELECT filing_id,version_status,supersedes_filing_id,is_canonical "
            "FROM filings ORDER BY submitted_on,filing_id")]

    def test_three_revisions_have_same_acyclic_graph_in_both_replay_orders(self):
        rows = [
            filing("20260102-5284", "2026-01-02", "hash-a"),
            filing("20260108-5091", "2026-01-08", "hash-b"),
            filing("20260219-5144", "2026-02-19", "hash-c"),
        ]
        forward = self._replay([r["filing_id"] for r in rows], rows)
        reverse = self._replay([r["filing_id"] for r in reversed(rows)], rows)
        self.assertEqual(forward, reverse)
        self.assertEqual(
            [(r["version_status"], r["supersedes_filing_id"], r["is_canonical"])
             for r in forward],
            [("original", None, 0),
             ("revised", "20260102-5284", 0),
             ("revised", "20260108-5091", 1)],
        )
        links = {r["filing_id"]: r["supersedes_filing_id"] for r in forward}
        for start in links:
            seen, current = set(), start
            while current:
                self.assertNotIn(current, seen, f"cycle from {start}: {seen}")
                seen.add(current)
                current = links.get(current)

    def test_identical_resubmission_is_retained_without_economic_revision(self):
        rows = [
            filing("first", "2026-01-02", "same-hash"),
            filing("same-bytes", "2026-01-08", "same-hash"),
            filing("changed", "2026-02-19", "different-hash"),
        ]
        result = self._replay(["changed", "same-bytes", "first"], rows)
        self.assertEqual(result[1]["version_status"], "identical_resubmission")
        self.assertIsNone(result[1]["supersedes_filing_id"])
        self.assertEqual(result[2]["version_status"], "revised")
        self.assertEqual(result[2]["supersedes_filing_id"], "same-bytes")

    def test_different_snapshot_is_not_in_the_revision_group(self):
        temp = tempfile.TemporaryDirectory(prefix="ferc-revision-snapshot-")
        self.addCleanup(temp.cleanup)
        staging = Staging(pathlib.Path(temp.name) / "test.sqlite")
        self.addCleanup(staging.close)
        staging.upsert("entities", [{
            "entity_key": ENTITY, "cid": ENTITY,
            "legal_name": "Synthetic revision filer",
        }], ["entity_key"])
        first = filing("q1", "2026-01-02", "a")
        second = filing("q2", "2026-04-02", "b")
        second["snapshot_date"] = "2026-04-01"
        staging.write_filing_bundle(first)
        staging.write_filing_bundle(second)
        staging.normalize_revision_group(
            SOURCE, ENTITY, FORM, snapshot_date=SNAPSHOT, set_canonical=True)
        other = dict(staging.query(
            "SELECT version_status,supersedes_filing_id,is_canonical FROM filings "
            "WHERE filing_id='q2'")[0])
        self.assertEqual(other, {
            "version_status": "unresolved",
            "supersedes_filing_id": None,
            "is_canonical": 0,
        })


if __name__ == "__main__":
    unittest.main()
