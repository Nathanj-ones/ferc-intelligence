"""Negative and positive controls for the read-only review-workflow audit."""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "audit_review_workflow.py"
SPEC = importlib.util.spec_from_file_location("audit_review_workflow", TOOL)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load review workflow audit")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


DDL = """
CREATE TABLE observations (
 observation_id TEXT PRIMARY KEY, source_system TEXT, entity_key TEXT,
 filing_id TEXT, source_fact_id TEXT, metric_id TEXT, value_text TEXT,
 availability TEXT, validation TEXT, review_status TEXT, qa_flags TEXT
);
CREATE TABLE reviewed_source_annotations (
 source_system TEXT, entity_key TEXT, filing_id TEXT, source_fact_id TEXT,
 metric_id TEXT, filed_text TEXT, review_status TEXT, rationale TEXT,
 evidence_ref TEXT, evidence_hash TEXT, reviewer TEXT, reviewed_at TEXT,
 applied_count INTEGER
);
CREATE TABLE document_facts (review_state TEXT, confidence TEXT);
CREATE TABLE blockers (
 kind TEXT, human_decision_needed INTEGER, resolved_at TEXT
);
CREATE TABLE events (event_class TEXT, event_type TEXT, destination TEXT);
CREATE TABLE publication_generations (
 generation_id TEXT, code_snapshot TEXT, input_snapshot TEXT,
 database_identity TEXT, status TEXT, published_at TEXT
);
"""


class ReviewWorkflowAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ferc-review-audit-")
        self.addCleanup(self.temp.cleanup)
        self.database = pathlib.Path(self.temp.name) / "review.sqlite"
        connection = sqlite3.connect(self.database)
        connection.executescript(DDL)
        connection.execute(
            "INSERT INTO publication_generations VALUES(?,?,?,?,?,?)",
            ("generation-1", "code-1", "input-1", "database-1", "published",
             "2026-09-10T00:00:00Z"),
        )
        connection.executemany(
            "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("open", "xbrl", "C1", "F1", "S1", "m1", "999999",
                 "present", "source_anomaly_review", "open", "anomaly"),
                ("resolved", "xbrl", "C1", "F2", "S2", "m2", "bad date",
                 "present", "source_anomaly_review", "resolved", "date warning"),
                ("unit", "xbrl", "C1", "F3", "S3", "m3", "12",
                 "present", "unit_warning", "", "unit mismatch; unit mismatch"),
                ("scope", "xbrl", "C1", "F4", "S4", "m4", "13",
                 "present", "scope_incompatible", "", "scope differs"),
                ("blocked", "docs", "C1", "F5", "S5", "m5", "14",
                 "present", "blocked_ambiguity", "", "meaning unresolved"),
                ("pass", "xbrl", "C1", "F6", "S6", "m6", "15",
                 "present", "pass", "", ""),
            ],
        )
        annotations = [
            {
                "source_system": "xbrl", "entity_key": "C1", "filing_id": "F1",
                "source_fact_id": "S1", "metric_id": "m1", "filed_text": "999999",
                "review_status": "reviewed_open", "rationale": "requires review",
                "evidence_ref": "exact:F1:S1:m1", "reviewer": "test process",
                "reviewed_at": "2026-09-10", "applied_count": 1,
            },
            {
                "source_system": "xbrl", "entity_key": "C1", "filing_id": "F2",
                "source_fact_id": "S2", "metric_id": "m2", "filed_text": "bad date",
                "review_status": "reviewed_final", "rationale": "review complete",
                "evidence_ref": "exact:F2:S2:m2", "reviewer": "test process",
                "reviewed_at": "2026-09-10", "applied_count": 1,
            },
        ]
        for row in annotations:
            row["evidence_hash"] = audit.annotation_evidence_hash(row)
            connection.execute(
                "INSERT INTO reviewed_source_annotations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[key] for key in (
                    "source_system", "entity_key", "filing_id", "source_fact_id",
                    "metric_id", "filed_text", "review_status", "rationale",
                    "evidence_ref", "evidence_hash", "reviewer", "reviewed_at",
                    "applied_count",
                )),
            )
        connection.executemany(
            "INSERT INTO document_facts VALUES(?,?)",
            [("queued", "review_required:unit_unresolved"),
             ("reviewed", "verified_span")],
        )
        connection.executemany(
            "INSERT INTO blockers VALUES(?,?,?)",
            [("semantic", 1, None), ("source", 0, "2026-09-10T00:00:00Z")],
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?)",
            ("data_quality", "late_discovery", "data_review_queue"),
        )
        connection.commit()
        connection.close()

    def test_classifies_flags_without_treating_every_warning_as_review_task(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        report = audit.audit_database(self.database)
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after, "read-only audit changed the database")
        self.assertEqual("pass", report["integrity"]["status"])
        flags = report["observation_quality_flags"]
        self.assertEqual(5, flags["records"])
        self.assertEqual(1, flags["open_review_tasks"])
        self.assertEqual(1, flags["resolved_review_tasks"])
        self.assertEqual(3, flags["flags_without_workflow_state"])
        self.assertEqual(1, flags["duplicate_warning_records"])
        self.assertEqual(
            1,
            report["reviewed_source_annotations"]["mechanically_closed_observations"],
        )
        self.assertEqual(2, report["observation_review_workflow"]["records"])
        self.assertEqual(1, report["data_review_queue_events"][0]["records"])

    def test_tampered_annotation_hash_fails_closed(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE reviewed_source_annotations SET evidence_hash=? WHERE filing_id='F2'",
            ("0" * 64,),
        )
        connection.commit()
        connection.close()
        report = audit.audit_database(self.database)
        self.assertEqual("fail", report["integrity"]["status"])
        self.assertIn(
            "annotation_evidence_hash_mismatch",
            {problem["code"] for problem in report["integrity"]["problems"]},
        )

    def test_false_resolved_state_without_final_evidence_fails_closed(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE observations SET review_status='resolved' WHERE observation_id='unit'",
        )
        connection.commit()
        connection.close()
        report = audit.audit_database(self.database)
        self.assertEqual("fail", report["integrity"]["status"])
        self.assertIn(
            "resolved_observation_without_exact_final_annotation",
            {problem["code"] for problem in report["integrity"]["problems"]},
        )

    def test_workflow_state_on_unflagged_value_fails_closed(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE observations SET review_status='open' WHERE observation_id='pass'",
        )
        connection.commit()
        connection.close()
        report = audit.audit_database(self.database)
        self.assertEqual("fail", report["integrity"]["status"])
        self.assertIn(
            "review_status_on_non_flagged_observation",
            {problem["code"] for problem in report["integrity"]["problems"]},
        )


if __name__ == "__main__":
    unittest.main()
