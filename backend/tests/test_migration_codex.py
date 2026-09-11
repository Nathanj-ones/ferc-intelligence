"""Migration and empty-database regressions for the integrated repair."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from ferclib.staging import Staging


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "ferclib" / "schema.sql"
MIGRATION_PATH = ROOT / "migrations/002_integrated_repair_2026_09_09.py"
SPEC = importlib.util.spec_from_file_location("integrated_repair_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MIGRATION)


def clean_database(path: pathlib.Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()


def legacy_database(path: pathlib.Path) -> None:
    """A clean-schema database downgraded to the audited pre-002 shape."""
    clean_database(path)
    con = sqlite3.connect(path)
    for table in MIGRATION.TABLE_SQL:
        con.execute(f'DROP TABLE "{table}"')
    con.execute("ALTER TABLE observations DROP COLUMN scope_rule")
    # A constrained column cannot be dropped/re-added in place on populated
    # SQLite. Recreate the known old manifest shape used by the audited DB.
    con.executescript("""
        ALTER TABLE source_manifest RENAME TO source_manifest_current;
        CREATE TABLE source_manifest (
          cache_key     TEXT PRIMARY KEY,
          source_system TEXT NOT NULL,
          source_url    TEXT NOT NULL,
          media_type    TEXT,
          byte_size     INTEGER,
          first_seen_at TEXT NOT NULL,
          last_seen_at  TEXT NOT NULL,
          fetch_count   INTEGER NOT NULL DEFAULT 1,
          note          TEXT
        );
        DROP TABLE source_manifest_current;
    """)
    con.execute(
        "INSERT INTO observations(observation_id,entity_key,metric_id,source_regime,"
        "period_basis,scope,unit,value_text,value_num,availability,origin,method,"
        "version_status,validation,qa_flags,review_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("o1", "CID", "metric", "Form 2", "annual", "whole legal entity", "USD",
         "10", 10.0, "present", "native", "filed", "original", "pass", "keep-qa",
         "reviewed"))
    con.execute(
        "INSERT INTO source_manifest(cache_key,source_system,source_url,media_type,"
        "byte_size,first_seen_at,last_seen_at,fetch_count,note) VALUES(?,?,?,?,?,?,?,?,?)",
        ("a" * 64, "eLibrary", "https://elibrary.ferc.gov/id", "application/pdf", 2,
         "2026-09-07T00:00:00+00:00", "2026-09-07T00:00:00+00:00", 3,
         "legacy row must survive"))
    con.executemany(
        "INSERT INTO filings(source_system,filing_id,entity_key,form,accession_number,"
        "reporting_year,reporting_period,period_start,period_end,filed_date,posted_date,"
        "issued_date,effective_date,submitted_on,snapshot_date,acceptance_status,"
        "taxonomy_version,schema_ref,content_hash,is_canonical,canonical_reason,"
        "version_status,supersedes_filing_id,data_origin,retrieved_at,first_seen_at,"
        "source_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("DataFERC", "target", "CID", "Form 549D", "acc", 2025, "annual",
             "2025-01-01", "2025-12-31", "2026-01-01", "2026-01-02", None, None,
             "2026-01-03", "2026-09-07T06:36:03.232Z", "accepted", "tax", "schema",
             "b" * 64, 1, "current", "original", None, "structured_bulk",
             "2026-09-07T06:36:03.232Z", "2026-09-07T06:36:03.232Z", "https://ferc.gov/target"),
            ("DataFERC", "other-form", "CID", "Form 2", None, 2025, "annual",
             None, None, None, None, None, None, None, "2026-08-01", None, None, None,
             None, 0, None, None, None, None, None, None, None),
            ("eLibrary", "other-source", "CID", "Form 549D", None, 2025, "annual",
             None, None, None, None, None, None, None, "2026-08-02", None, None, None,
             None, 0, None, None, None, None, None, None, None),
            ("DataFERC", "case-mismatch", "CID", "Form 549d", None, 2025, "annual",
             None, None, None, None, None, None, None, "2026-08-03", None, None, None,
             None, 0, None, None, None, None, None, None, None),
        ])
    con.commit()
    con.close()


def quiet_main(args) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = MIGRATION.main(args)
    return rc, stdout.getvalue(), stderr.getvalue()


class IntegratedMigrationTests(unittest.TestCase):
    def test_existing_state_upgrade_is_constrained_data_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-") as td:
            root = pathlib.Path(td)
            db = root / "legacy.sqlite"
            first = root / "first.json"
            second = root / "second.json"
            legacy_database(db)
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            target_before = dict(con.execute(
                "SELECT * FROM filings WHERE filing_id='target'").fetchone())
            controls_before = [tuple(r) for r in con.execute(
                "SELECT source_system,filing_id,snapshot_date FROM filings "
                "WHERE filing_id!='target' ORDER BY filing_id")]
            manifest_before = tuple(con.execute(
                "SELECT cache_key,source_system,source_url,media_type,byte_size,"
                "first_seen_at,last_seen_at,fetch_count,note FROM source_manifest").fetchone())
            con.close()

            rc, _out, err = quiet_main(
                ["--db", str(db), "--apply", "--report", str(first)])
            self.assertEqual(0, rc, err)
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            self.assertEqual(("whole legal entity", "10", None, "keep-qa", "reviewed"),
                             tuple(con.execute(
                                 "SELECT scope,value_text,scope_rule,qa_flags,review_status "
                                 "FROM observations WHERE observation_id='o1'").fetchone()))
            manifest_after = con.execute(
                "SELECT cache_key,content_hash,source_system,source_url,media_type,byte_size,"
                "first_seen_at,last_seen_at,fetch_count,note FROM source_manifest").fetchone()
            self.assertEqual("a" * 64, manifest_after[1])
            self.assertEqual(manifest_before, tuple(manifest_after[:1]) + tuple(manifest_after[2:]))
            content_info = next(r for r in con.execute("PRAGMA table_info(source_manifest)")
                                if r[1] == "content_hash")
            self.assertEqual(("TEXT", 1, None, 0),
                             (content_info[2], content_info[3], content_info[4], content_info[5]))

            target_after = dict(con.execute(
                "SELECT * FROM filings WHERE filing_id='target'").fetchone())
            self.assertIsNone(target_after["snapshot_date"])
            target_before["snapshot_date"] = None
            self.assertEqual(target_before, target_after)
            controls_after = [tuple(r) for r in con.execute(
                "SELECT source_system,filing_id,snapshot_date FROM filings "
                "WHERE filing_id!='target' ORDER BY filing_id")]
            self.assertEqual(controls_before, controls_after)
            counts_before = {name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                             for name in MIGRATION.TABLE_SQL}
            plan_after_first = MIGRATION.plan(con)
            self.assertTrue(plan_after_first["already_current"])
            self.assertIn("observations", plan_after_first["column_order_differences"])
            self.assertEqual("ok", con.execute("PRAGMA quick_check").fetchone()[0])
            con.close()

            rc, _out, err = quiet_main(
                ["--db", str(db), "--apply", "--report", str(second)])
            self.assertEqual(0, rc, err)
            report = json.loads(second.read_text())
            self.assertEqual("already_current", report["mode"])
            self.assertEqual("unchanged", report["database_commit"])
            con = sqlite3.connect(db)
            counts_after = {name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                            for name in MIGRATION.TABLE_SQL}
            self.assertEqual(counts_before, counts_after)
            self.assertEqual(0, con.execute(
                "SELECT COUNT(*) FROM filings WHERE source_system='DataFERC' "
                "AND form='Form 549D' AND snapshot_date IS NOT NULL").fetchone()[0])
            self.assertEqual("ok", con.execute("PRAGMA quick_check").fetchone()[0])
            self.assertIsNone(con.execute("PRAGMA foreign_key_check").fetchone())
            con.close()

    def test_failure_rolls_back_columns_tables_manifest_and_timestamp_together(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-rollback-") as td:
            db = pathlib.Path(td) / "legacy.sqlite"
            legacy_database(db)
            broken = dict(MIGRATION.TABLE_SQL)
            broken["run_input_inventory"] = "CREATE TABLE broken syntax"
            with mock.patch.object(MIGRATION, "TABLE_SQL", broken):
                rc, _out, _err = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc)
            con = sqlite3.connect(db)
            self.assertNotIn("scope_rule", {r[1] for r in con.execute(
                "PRAGMA table_info(observations)")})
            self.assertNotIn("content_hash", {r[1] for r in con.execute(
                "PRAGMA table_info(source_manifest)")})
            self.assertFalse({"run_unit_status", "unit_commits"} & {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            self.assertEqual("2026-09-07T06:36:03.232Z", con.execute(
                "SELECT snapshot_date FROM filings WHERE filing_id='target'").fetchone()[0])
            self.assertEqual(("whole legal entity", "10"), con.execute(
                "SELECT scope,value_text FROM observations").fetchone())
            con.close()

    def test_same_named_wrong_table_and_index_are_refused_before_writes(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-shape-") as td:
            db = pathlib.Path(td) / "wrong.sqlite"
            legacy_database(db)
            con = sqlite3.connect(db)
            # Same table name/columns, but status has an undeclared default.
            con.execute(MIGRATION.TABLE_SQL["publication_generations"].replace(
                "status TEXT NOT NULL", "status TEXT NOT NULL DEFAULT 'published'"))
            con.execute(MIGRATION.TABLE_SQL["run_unit_status"])
            con.execute(MIGRATION.TABLE_SQL["unit_commits"].replace(
                "committed_at TEXT NOT NULL,",
                "committed_at TEXT NOT NULL CHECK(length(committed_at)>0),"))
            con.execute("CREATE INDEX ix_run_unit_status_current ON "
                        "run_unit_status(state,run_id,adapter,entity_cid)")
            con.commit()
            problems = MIGRATION.plan(con)["problems"]
            self.assertTrue(any(p["object"] == "publication_generations" for p in problems))
            self.assertTrue(any(p["object"] == "unit_commits" for p in problems))
            self.assertTrue(any(p["object"] == "ix_run_unit_status_current" for p in problems))
            con.close()
            rc, _out, err = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc)
            self.assertIn("refused", err)
            con = sqlite3.connect(db)
            self.assertNotIn("scope_rule", {r[1] for r in con.execute(
                "PRAGMA table_info(observations)")})
            self.assertEqual("2026-09-07T06:36:03.232Z", con.execute(
                "SELECT snapshot_date FROM filings WHERE filing_id='target'").fetchone()[0])
            con.close()

    def test_nonempty_invalid_claimed_content_hash_is_not_replaced_from_cache_key(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-bad-hash-") as td:
            db = pathlib.Path(td) / "bad-hash.sqlite"
            legacy_database(db)
            con = sqlite3.connect(db)
            con.execute("ALTER TABLE source_manifest ADD COLUMN content_hash TEXT")
            con.execute("UPDATE source_manifest SET content_hash='not-a-sha256'")
            con.commit()
            self.assertTrue(any(p["kind"] == "unrecoverable_values"
                                for p in MIGRATION.plan(con)["problems"]))
            con.close()
            rc, _out, err = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc)
            self.assertIn("refused", err)
            con = sqlite3.connect(db)
            self.assertEqual("not-a-sha256", con.execute(
                "SELECT content_hash FROM source_manifest").fetchone()[0])
            self.assertNotIn("scope_rule", {r[1] for r in con.execute(
                "PRAGMA table_info(observations)")})
            con.close()

    def test_staging_detects_silent_drift_before_schema_script_and_names_migration_002(self):
        with tempfile.TemporaryDirectory(prefix="ferc-staging-drift-") as td:
            db = pathlib.Path(td) / "legacy.sqlite"
            legacy_database(db)
            with self.assertRaisesRegex(sqlite3.OperationalError,
                                        "002_integrated_repair_2026_09_09.py"):
                Staging(db)
            con = sqlite3.connect(db)
            self.assertNotIn("scope_rule", {r[1] for r in con.execute(
                "PRAGMA table_info(observations)")})
            self.assertNotIn("run_unit_status", {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
            con.close()

    def test_report_failure_distinguishes_committed_database_from_missing_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-report-") as td:
            db = pathlib.Path(td) / "legacy.sqlite"
            legacy_database(db)
            with mock.patch.object(MIGRATION, "write_report",
                                   side_effect=OSError("injected sidecar failure")):
                rc, output, error = quiet_main(
                    ["--db", str(db), "--apply", "--report", str(pathlib.Path(td) / "x.json")])
            self.assertEqual(MIGRATION.EXIT_REPORT_UNAVAILABLE, rc)
            self.assertIn('"database_commit": "committed"', output)
            self.assertIn("database state COMMITTED", error)
            self.assertNotIn("rolled back", error.lower())
            con = sqlite3.connect(db)
            self.assertTrue(MIGRATION.plan(con)["already_current"])
            self.assertIsNone(con.execute(
                "SELECT snapshot_date FROM filings WHERE filing_id='target'").fetchone()[0])
            con.close()

    def test_empty_database_path_has_the_complete_current_schema(self):
        with tempfile.TemporaryDirectory(prefix="ferc-empty-schema-") as td:
            db = pathlib.Path(td) / "empty.sqlite"
            staging = Staging(db)
            self.assertTrue(MIGRATION.plan(staging.con)["already_current"])
            self.assertEqual({}, staging.schema_column_order_differences)
            self.assertIn("scope_rule", {r[1] for r in staging.query(
                "PRAGMA table_info(observations)")})
            content = next(r for r in staging.query("PRAGMA table_info(source_manifest)")
                           if r["name"] == "content_hash")
            self.assertEqual(1, content["notnull"])
            self.assertIsNone(content["dflt_value"])
            self.assertEqual(0, staging.query("SELECT COUNT(*) n FROM observations")[0]["n"])
            staging.close()

    def test_migrated_column_order_is_explicit_and_staging_accepts_it(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-order-") as td:
            db = pathlib.Path(td) / "legacy.sqlite"
            legacy_database(db)
            rc, _out, err = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(0, rc, err)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                staging = Staging(db)
            self.assertIn("observations", staging.schema_column_order_differences)
            self.assertIn("accepted named-column order difference", stderr.getvalue())
            staging.close()


if __name__ == "__main__":
    unittest.main()
