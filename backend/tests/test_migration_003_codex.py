"""Regression tests for the filing-entity association migration."""

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


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "ferclib" / "schema.sql"
MIGRATION_PATH = ROOT / "migrations/003_filing_entity_associations_2026_09_10.py"
SPEC = importlib.util.spec_from_file_location("filing_entity_migration_003", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MIGRATION)


def quiet_main(args: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = MIGRATION.main(args)
    return result, stdout.getvalue(), stderr.getvalue()


def create_database(path: pathlib.Path, *, legacy: bool) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    if legacy:
        con.execute("DROP TABLE filing_entities")
        con.execute("ALTER TABLE asset_dockets DROP COLUMN evidence_ref")

    con.executemany(
        "INSERT INTO entities(entity_key,cid,legal_name,parent,ticker,jurisdiction,note) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("C000001", "C000001", "First Pipeline LLC", "Parent A", "AAA", "FERC", "entity one"),
            ("C000002", "C000002", "Second Pipeline LLC", "Parent B", "BBB", "FERC", "entity two"),
        ],
    )
    con.executemany(
        "INSERT INTO assets(asset_id,ticker,display_name,template,authority,status,note) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("asset-one", "AAA", "First Pipeline", "interstate_gas", "NGA", "operating", "asset one"),
            ("asset-two", "BBB", "Second Pipeline", "liquids", "ICA", "operating", "asset two"),
        ],
    )
    con.executemany(
        "INSERT INTO asset_entity_map(asset_id,entity_key,mapping_scope,note) VALUES(?,?,?,?)",
        [
            ("asset-one", "C000001", "whole_entity", "mapping one"),
            ("asset-two", "C000002", "whole_entity", "mapping two"),
        ],
    )
    con.executemany(
        "INSERT INTO dockets(docket,prefix,note) VALUES(?,?,?)",
        [("CP24-1", "CP", "gas docket"), ("IS24-2", "IS", "oil docket")],
    )
    if legacy:
        con.executemany(
            "INSERT INTO asset_dockets(asset_id,docket,role) VALUES(?,?,?)",
            [("asset-one", "CP24-1", "certificate"),
             ("asset-two", "IS24-2", "rate")],
        )
    else:
        con.executemany(
            "INSERT INTO asset_dockets(asset_id,docket,role,evidence_ref) VALUES(?,?,?,?)",
            [("asset-one", "CP24-1", "certificate", "registry:asset-one"),
             ("asset-two", "IS24-2", "rate", "registry:asset-two")],
        )
    con.executemany(
        "INSERT INTO filings(source_system,filing_id,entity_key,form,accession_number,"
        "reporting_year,reporting_period,content_hash,is_canonical,version_status,"
        "data_origin,source_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("eLibrary", "20240101-5001", "C000001", "FERC order", "20240101-5001",
             2024, "event", "a" * 64, 1, "original", "document",
             "https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20240101-5001"),
            ("DataFERC", "form6-2024", "C000002", "Form 6", None,
             2024, "annual", "b" * 64, 0, "revised", "structured_bulk",
             "https://data.ferc.gov/form6-2024"),
        ],
    )
    if not legacy:
        con.executemany(
            "INSERT INTO filing_entities(source_system,filing_id,entity_key,"
            "association_role,facility_key,evidence_ref) VALUES(?,?,?,?,?,?)",
            [
                ("eLibrary", "20240101-5001", "C000001", "named_filer", "asset-one",
                 "accession:20240101-5001"),
                ("DataFERC", "form6-2024", "C000002", "source_entity", None,
                 "DataFERC:form6-2024"),
            ],
        )
    con.commit()
    con.close()


def rows(con: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row) for row in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


class FilingEntityMigration003Tests(unittest.TestCase):
    def test_legacy_dry_run_is_read_only_then_apply_preserves_rows_and_backfills_once(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-003-") as td:
            root = pathlib.Path(td)
            db = root / "legacy.sqlite"
            report = root / "applied.json"
            create_database(db, legacy=True)

            con = sqlite3.connect(db)
            preserved_tables = (
                "entities", "assets", "asset_entity_map", "dockets", "asset_dockets", "filings"
            )
            before = {table: rows(con, table) for table in preserved_tables}
            before_total_changes = con.total_changes
            con.close()

            rc, output, error = quiet_main(["--db", str(db)])
            self.assertEqual(0, rc, error)
            dry_run = json.loads(output)
            self.assertEqual("dry_run", dry_run["mode"])
            self.assertEqual("ready", dry_run["result"])
            self.assertEqual("unchanged", dry_run["database_commit"])
            self.assertTrue(dry_run["before"]["add_asset_dockets_evidence_ref"])
            self.assertTrue(dry_run["before"]["create_filing_entities"])
            self.assertEqual(2, dry_run["before"]["backfill_compatibility_associations"])

            con = sqlite3.connect(db)
            self.assertNotIn("evidence_ref", {
                row[1] for row in con.execute("PRAGMA table_info(asset_dockets)")
            })
            self.assertNotIn("filing_entities", {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            })
            self.assertEqual(before_total_changes, con.total_changes)
            self.assertEqual(before, {table: rows(con, table) for table in preserved_tables})
            con.close()

            rc, output, error = quiet_main(
                ["--db", str(db), "--apply", "--report", str(report)]
            )
            self.assertEqual(0, rc, error)
            applied = json.loads(output)
            self.assertEqual("apply", applied["mode"])
            self.assertEqual("applied", applied["result"])
            self.assertEqual("committed", applied["database_commit"])
            self.assertTrue(applied["after"]["already_current"])
            self.assertEqual(applied, json.loads(report.read_text(encoding="utf-8")))

            con = sqlite3.connect(db)
            self.assertEqual(before["entities"], rows(con, "entities"))
            self.assertEqual(before["assets"], rows(con, "assets"))
            self.assertEqual(before["asset_entity_map"], rows(con, "asset_entity_map"))
            self.assertEqual(before["dockets"], rows(con, "dockets"))
            self.assertEqual(before["filings"], rows(con, "filings"))
            self.assertEqual(
                [row + (None,) for row in before["asset_dockets"]],
                rows(con, "asset_dockets"),
            )
            associations = rows(con, "filing_entities")
            self.assertEqual(2, len(associations))
            self.assertEqual(
                {
                    ("eLibrary", "20240101-5001", "C000001"),
                    ("DataFERC", "form6-2024", "C000002"),
                },
                {row[:3] for row in associations},
            )
            self.assertEqual({"compatibility_anchor"}, {row[3] for row in associations})
            self.assertEqual({""}, {row[4] for row in associations})
            self.assertEqual(
                {"migration_003: legacy filings.entity_key; reviewed replay required"},
                {row[5] for row in associations},
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM filings").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM filing_entities").fetchone()[0],
            )
            self.assertEqual("ok", con.execute("PRAGMA quick_check").fetchone()[0])
            self.assertIsNone(con.execute("PRAGMA foreign_key_check").fetchone())
            con.close()

            rc, output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(0, rc, error)
            second = json.loads(output)
            self.assertEqual("already_current", second["mode"])
            self.assertEqual("verified_noop", second["result"])
            self.assertEqual("unchanged", second["database_commit"])
            con = sqlite3.connect(db)
            self.assertEqual(associations, rows(con, "filing_entities"))
            self.assertEqual(
                [row + (None,) for row in before["asset_dockets"]],
                rows(con, "asset_dockets"),
            )
            con.close()

    def test_current_database_dry_run_and_apply_are_verified_noops(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-003-current-") as td:
            db = pathlib.Path(td) / "current.sqlite"
            create_database(db, legacy=False)
            con = sqlite3.connect(db)
            before = {
                table: rows(con, table)
                for table in ("entities", "assets", "asset_dockets", "filings", "filing_entities")
            }
            self.assertTrue(MIGRATION.plan(con)["already_current"])
            con.close()

            rc, output, error = quiet_main(["--db", str(db)])
            self.assertEqual(0, rc, error)
            dry_run = json.loads(output)
            self.assertEqual("already_current", dry_run["result"])
            self.assertEqual("dry_run", dry_run["mode"])
            self.assertEqual("unchanged", dry_run["database_commit"])

            rc, output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(0, rc, error)
            apply = json.loads(output)
            self.assertEqual("already_current", apply["mode"])
            self.assertEqual("verified_noop", apply["result"])
            self.assertEqual("unchanged", apply["database_commit"])
            con = sqlite3.connect(db)
            self.assertEqual(before, {table: rows(con, table) for table in before})
            con.close()

    def test_unknown_asset_docket_shape_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-003-shape-") as td:
            db = pathlib.Path(td) / "unknown.sqlite"
            create_database(db, legacy=True)
            con = sqlite3.connect(db)
            con.execute("ALTER TABLE asset_dockets ADD COLUMN undeclared_owner TEXT")
            con.execute("UPDATE asset_dockets SET undeclared_owner='must survive refusal'")
            con.commit()
            before = rows(con, "asset_dockets")
            con.close()

            rc, output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc, (output, error))
            refused = json.loads(output)
            self.assertEqual("refused_unknown_shape", refused["result"])
            self.assertTrue(refused["before"]["problems"])
            con = sqlite3.connect(db)
            self.assertEqual(before, rows(con, "asset_dockets"))
            self.assertNotIn("evidence_ref", {
                row[1] for row in con.execute("PRAGMA table_info(asset_dockets)")
            })
            self.assertNotIn("filing_entities", {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            })
            con.close()

    def test_apply_error_rolls_back_column_table_index_and_backfill_together(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-003-rollback-") as td:
            db = pathlib.Path(td) / "legacy.sqlite"
            create_database(db, legacy=True)
            con = sqlite3.connect(db)
            before_asset_dockets = rows(con, "asset_dockets")
            before_filings = rows(con, "filings")
            con.close()

            with mock.patch.object(
                MIGRATION,
                "TABLE_SQL",
                "CREATE TABLE filing_entities this is deliberately invalid SQL",
            ):
                rc, _output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc)
            self.assertIn("migration failed: OperationalError", error)

            con = sqlite3.connect(db)
            self.assertEqual(before_asset_dockets, rows(con, "asset_dockets"))
            self.assertEqual(before_filings, rows(con, "filings"))
            self.assertNotIn("evidence_ref", {
                row[1] for row in con.execute("PRAGMA table_info(asset_dockets)")
            })
            objects = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
            self.assertNotIn("filing_entities", objects)
            self.assertNotIn("ix_filing_entities_entity", objects)
            self.assertEqual("ok", con.execute("PRAGMA quick_check").fetchone()[0])
            con.close()


if __name__ == "__main__":
    unittest.main()
