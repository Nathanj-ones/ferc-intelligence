"""Regression controls for annotation metadata, status and atomic bookkeeping."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest

import seed_annotations
from ferclib import xbrl_adapter
from ferclib.staging import Staging


def annotation():
    return {
        "source_system": "eCollection_XBRL",
        "entity_key": "CID",
        "filing_id": "filing-1",
        "source_fact_id": "fact-1",
        "metric_id": "metric-1",
        "filed_text": "999999",
        "review_status": "reviewed_open",
        "rationale": "preserve as filed; review remains required",
        "evidence_ref": "eCollection_XBRL|filing=filing-1|fact=fact-1|metric=metric-1",
        "evidence_hash": "a" * 64,
        "reviewer": "unattributed process 2026-09-07",
        "reviewed_at": "2026-09-07",
        "applied_count": 0,
    }


def observation(value="999999"):
    return {
        "observation_id": "obs-fixed-grain",
        "entity_key": "CID",
        "metric_id": "metric-1",
        "source_regime": "Form 2-A",
        "period_basis": "quarter",
        "period_start": "2025-10-01",
        "period_end": "2025-12-31",
        "reporting_year": 2025,
        "reporting_period": "Q4",
        "scope": "FERC filing entity CID; consolidated source context",
        "scope_rule": "same filing entity",
        "unit": "Dth",
        "value_text": value,
        "value_num": float(value),
        "availability": "present",
        "origin": "native_xbrl",
        "method": "filed",
        "version_status": "original",
        "validation": "source_anomaly_review",
        "source_system": "eCollection_XBRL",
        "filing_id": "filing-1",
        "source_fact_id": "fact-1",
        "qa_flags": "reviewed source anomaly; value preserved as filed",
        "review_status": "open",
    }


class AnnotationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ferc-annotation-test-")
        self.addCleanup(self.temp.cleanup)
        self.db = Staging(pathlib.Path(self.temp.name) / "test.sqlite")
        self.addCleanup(self.db.close)

    def _commit(self, obs):
        return self.db.commit_unit(
            adapter="gas_xbrl", entity_key="CID", metric_ids=["metric-1"],
            year_from=2025, year_to=2025, observations=[obs], edges=[])

    def test_seed_does_not_zero_count_and_swap_reconciles_atomically(self):
        row = annotation()
        self.db.write_reviewed_annotations([row])
        report = self._commit(observation())
        self.assertEqual(report["annotation_matches"], 1)
        self.assertEqual(self.db.query(
            "SELECT applied_count FROM reviewed_source_annotations")[0][0], 1)

        # A later run seeds before it publishes. That must leave last-good
        # bookkeeping intact until the observation transaction commits.
        self.db.write_reviewed_annotations([{**row, "applied_count": 0}])
        self.assertEqual(self.db.query(
            "SELECT applied_count FROM reviewed_source_annotations")[0][0], 1)

        report = self._commit(observation("888888"))
        self.assertEqual(report["annotation_matches"], 0)
        self.assertEqual(self.db.query(
            "SELECT applied_count FROM reviewed_source_annotations")[0][0], 0)

    def test_reviewed_final_maps_to_resolved_while_open_stays_open(self):
        self.assertEqual(xbrl_adapter._annotation_review_status("reviewed_final"),
                         "resolved")
        self.assertEqual(xbrl_adapter._annotation_review_status("reviewed_resolved"),
                         "resolved")
        self.assertEqual(xbrl_adapter._annotation_review_status("reviewed_open"), "open")

    def test_bundled_metadata_is_complete_and_recomputable(self):
        rows, manifest = seed_annotations.load_bundle()
        self.assertEqual(manifest["annotation_set_version"], "1.1.0")
        self.assertEqual(len(rows), 7)
        for row in rows:
            identity = {k: row[k] for k in (
                "entity_key", "filed_text", "filing_id", "metric_id",
                "source_fact_id", "source_system")}
            digest = hashlib.sha256(json.dumps(
                identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(row["evidence_hash"], digest)
            self.assertTrue(row["evidence_ref"])
            self.assertRegex(row["reviewed_at"], r"^20\d{2}-\d{2}-\d{2}$")

    def test_run_plan_annotation_identity_query_matches_empty_database_schema(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        plan = json.loads((root / "config/run_plan.json").read_text(encoding="utf-8"))
        step = next(s for s in plan["steps"] if s["id"] == "load_review_annotations")
        query = step["outputs"][0]["sqlite_query"]["sql"]
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((root / "ferclib/schema.sql").read_text(encoding="utf-8"))
            con.execute(query).fetchall()
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
