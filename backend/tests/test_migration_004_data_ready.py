"""Regression tests for the document scope-contract migration."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "ferclib" / "schema.sql"
MIGRATION_PATH = ROOT / "migrations" / "004_document_scope_contract_2026_09_10.py"
SPEC = importlib.util.spec_from_file_location("document_scope_migration_004", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MIGRATION)


def quiet_main(args: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = MIGRATION.main(args)
    return result, stdout.getvalue(), stderr.getvalue()


def create_database(path: pathlib.Path, *, unknown_metric: bool = False) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    rows = [
        ("obs-doc", "C000001", "lng_inspection", "eLibrary document", "as_of",
         "Sabine Pass terminal; inspection finding", None, "present", "elibrary_document",
         "document_extracted", "original", "pass"),
        ("obs-xbrl", "C000001", "gas_operating_revenues", "Form 2", "annual",
         "filing entity, whole entity", None, "present", "native_xbrl", "filed",
         "original", "pass"),
    ]
    if unknown_metric:
        rows.append(
            ("obs-unknown", "C000001", "not_a_registry_metric", "eLibrary document",
             "as_of", "unknown actual scope", None, "present", "elibrary_document",
             "document_extracted", "original", "pass")
        )
    con.executemany(
        "INSERT INTO observations(observation_id,entity_key,metric_id,source_regime,"
        "period_basis,scope,scope_rule,availability,origin,method,version_status,validation) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


class DocumentScopeMigration004Tests(unittest.TestCase):
    def test_dry_run_apply_and_idempotent_replay(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-004-") as td:
            root = pathlib.Path(td)
            db, report = root / "candidate.sqlite", root / "report.json"
            create_database(db)

            rc, output, error = quiet_main(["--db", str(db)])
            self.assertEqual(0, rc, error)
            dry = json.loads(output)
            self.assertEqual("ready", dry["result"])
            self.assertEqual(1, dry["before"]["scope_rule_to_backfill"])
            con = sqlite3.connect(db)
            self.assertIsNone(con.execute(
                "SELECT scope_rule FROM observations WHERE observation_id='obs-doc'"
            ).fetchone()[0])
            actual_scope = con.execute(
                "SELECT scope FROM observations WHERE observation_id='obs-doc'"
            ).fetchone()[0]
            con.close()

            rc, output, error = quiet_main(
                ["--db", str(db), "--apply", "--report", str(report)])
            self.assertEqual(0, rc, error)
            applied = json.loads(output)
            self.assertEqual("applied", applied["result"])
            self.assertEqual(1, applied["rows_updated"])
            self.assertEqual(applied, json.loads(report.read_text(encoding="utf-8")))
            con = sqlite3.connect(db)
            scope, rule = con.execute(
                "SELECT scope,scope_rule FROM observations WHERE observation_id='obs-doc'"
            ).fetchone()
            self.assertEqual(actual_scope, scope)
            self.assertEqual(MIGRATION.BY_ID["lng_inspection"].scope, rule)
            self.assertIsNone(con.execute(
                "SELECT scope_rule FROM observations WHERE observation_id='obs-xbrl'"
            ).fetchone()[0])
            con.close()

            rc, output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(0, rc, error)
            self.assertEqual("verified_noop", json.loads(output)["result"])

    def test_unknown_document_metric_refuses_without_partial_update(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-004-refuse-") as td:
            db = pathlib.Path(td) / "candidate.sqlite"
            create_database(db, unknown_metric=True)
            rc, output, _error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc)
            self.assertEqual("refused_unmapped_scope_contract", json.loads(output)["result"])
            con = sqlite3.connect(db)
            self.assertEqual(
                [("obs-doc", None), ("obs-unknown", None)],
                list(con.execute(
                    "SELECT observation_id,scope_rule FROM observations "
                    "WHERE source_regime='eLibrary document' ORDER BY observation_id")),
            )
            con.close()


if __name__ == "__main__":
    unittest.main()
