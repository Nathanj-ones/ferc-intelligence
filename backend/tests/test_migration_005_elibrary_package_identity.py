"""Regression tests for the eLibrary package-document identity migration."""

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
MIGRATION_PATH = (
    ROOT / "migrations" / "005_elibrary_package_document_identity_2026_09_11.py"
)
SPEC = importlib.util.spec_from_file_location(
    "elibrary_package_document_migration_005", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MIGRATION)


def quiet_main(args: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = MIGRATION.main(args)
    return result, stdout.getvalue(), stderr.getvalue()


def rows(con: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row) for row in con.execute(
        f'SELECT * FROM "{table}" ORDER BY rowid')]


def _filing(accession: str, form: str, digest: str) -> tuple:
    return (
        "eLibrary", accession, "C000001", form, accession, 2026, "as_of",
        digest, 1, "original", "document",
        f"https://elibrary.ferc.gov/eLibrary/filelist?accession_number={accession}",
    )


def _document(accession: str, suffix: str, digest: str, *, title: str,
              attachment_id: str, byte_size: int = 1234,
              text_layer: str = "yes", availability: str = "retrieved") -> tuple:
    return (
        f"eLibrary|{accession}|{suffix}", "eLibrary", accession, accession,
        attachment_id, title, "Filing", "application/octet-stream", byte_size,
        digest, None, text_layer, availability, "2026-09-10T12:00:00+00:00",
        f"https://elibrary.ferc.gov/eLibrary/filelist?accession_number={accession}",
    )


def create_database(path: pathlib.Path, *, two_legacy: bool = False) -> dict[str, str]:
    digests = {
        "legacy": "a" * 64,
        "current": "b" * 64,
        "ioc": "c" * 64,
        "capacity": "d" * 64,
        "second": "e" * 64,
    }
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO entities(entity_key,cid,legal_name) VALUES('C000001','C000001',"
        "'Example Pipeline LLC')")
    filings = [
        _filing("20260101-5001", "eLibrary document", digests["legacy"]),
        _filing("20260102-5002", "eLibrary document", digests["current"]),
        _filing("20260103-5003", "eLibrary document", ""),
        _filing("20260104-5004", "Form 549B IOC", digests["ioc"]),
        _filing("20260105-5005", "Form 549B Capacity", digests["capacity"]),
    ]
    if two_legacy:
        filings.append(
            _filing("20260106-5006", "eLibrary document", digests["second"]))
    con.executemany(
        "INSERT INTO filings(source_system,filing_id,entity_key,form,accession_number,"
        "reporting_year,reporting_period,content_hash,is_canonical,version_status,"
        "data_origin,source_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        filings,
    )
    documents = [
        _document(
            "20260101-5001", "ATTACHMENT-FIRST", digests["legacy"],
            title="20260101-5001_selected-second-member.pdf",
            attachment_id="ATTACHMENT-FIRST", byte_size=9876,
        ),
        _document(
            "20260101-5001", "listing", "", title="Metadata-only listing",
            attachment_id="", byte_size=0, text_layer="unknown",
            availability="not_retrieved",
        ),
        _document(
            "20260102-5002", "package", digests["current"],
            title="eLibrary accession 20260102-5002 package", attachment_id="",
            byte_size=4567, text_layer="partial",
        ),
        _document(
            "20260103-5003", "listing", "", title="Metadata-only listing",
            attachment_id="", byte_size=0, text_layer="unknown",
            availability="not_retrieved",
        ),
        _document(
            "20260104-5004", "IOC-ATTACHMENT", digests["ioc"],
            title="Exact IOC attachment.tab", attachment_id="IOC-ATTACHMENT",
            byte_size=2222,
        ),
        _document(
            "20260105-5005", "CAPACITY-ATTACHMENT", digests["capacity"],
            title="Exact capacity attachment.pdf",
            attachment_id="CAPACITY-ATTACHMENT", byte_size=3333,
        ),
    ]
    if two_legacy:
        documents.append(_document(
            "20260106-5006", "ANOTHER-FIRST", digests["second"],
            title="another-selected-member.pdf", attachment_id="ANOTHER-FIRST",
            byte_size=6789,
        ))
    con.executemany(
        "INSERT INTO documents(document_id,source_system,filing_id,accession_number,"
        "attachment_id,title,class_type,media_type,byte_size,content_hash,cache_path,"
        "text_layer,availability,retrieved_at,source_url) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        documents,
    )

    legacy_id = "eLibrary|20260101-5001|ATTACHMENT-FIRST"
    con.execute(
        "INSERT INTO observations(observation_id,entity_key,metric_id,source_regime,"
        "period_basis,scope,availability,origin,method,version_status,validation,"
        "source_system,filing_id,document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("obs-package", "C000001", "lng_inspection", "eLibrary document", "as_of",
         "facility", "present", "elibrary_document", "document_extracted",
         "original", "pass", "eLibrary", "20260101-5001", legacy_id),
    )
    con.execute(
        "INSERT INTO document_facts(document_fact_id,document_id,source_system,filing_id,"
        "entity_key,assertion_type,scope_note,verbatim_span,extraction_method,content_hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("dfact-package", legacy_id, "eLibrary", "20260101-5001", "C000001",
         "test_assertion", "accession package", "filed words", "pdf_text_span",
         digests["legacy"]),
    )
    con.execute(
        "INSERT INTO events(event_id,event_class,event_type,headline,destination,"
        "source_system,filing_id,document_id,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("evt-package", "authority", "order", "Example order", "filing_archive",
         "eLibrary", "20260101-5001", legacy_id, "2026-09-10T12:00:00+00:00"),
    )
    if two_legacy:
        second_id = "eLibrary|20260106-5006|ANOTHER-FIRST"
        con.execute(
            "INSERT INTO events(event_id,event_class,event_type,headline,destination,"
            "source_system,filing_id,document_id,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("evt-second", "authority", "order", "Second order", "filing_archive",
             "eLibrary", "20260106-5006", second_id,
             "2026-09-10T12:00:00+00:00"),
        )
    con.commit()
    con.close()
    return digests


class ELibraryPackageDocumentMigration005Tests(unittest.TestCase):
    def test_dry_run_apply_rewires_every_reference_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-005-") as temporary:
            root = pathlib.Path(temporary)
            db, report = root / "candidate.sqlite", root / "report.json"
            digests = create_database(db)
            con = sqlite3.connect(db)
            preserved_counts = {
                table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("filings", "documents", "observations",
                              "document_facts", "events")
            }
            protected = {
                accession: [tuple(row) for row in con.execute(
                    "SELECT * FROM documents WHERE filing_id=?", (accession,)
                ).fetchall()]
                for accession in ("20260103-5003", "20260104-5004", "20260105-5005")
            }
            con.close()

            rc, output, error = quiet_main(["--db", str(db)])
            self.assertEqual(0, rc, error)
            dry = json.loads(output)
            self.assertEqual("ready", dry["result"])
            self.assertEqual("unchanged", dry["database_commit"])
            self.assertEqual(1, dry["before"]["legacy_documents_to_migrate"])
            self.assertEqual(1, dry["before"]["current_package_documents"])
            self.assertEqual(2, dry["before"]["listing_documents_protected"])
            self.assertEqual(
                {"observations": 1, "document_facts": 1, "events": 1},
                dry["before"]["references_to_rewire"],
            )
            con = sqlite3.connect(db)
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM documents WHERE document_id=?",
                ("eLibrary|20260101-5001|ATTACHMENT-FIRST",),
            ).fetchone())
            con.close()

            rc, output, error = quiet_main(
                ["--db", str(db), "--apply", "--report", str(report)])
            self.assertEqual(0, rc, error)
            applied = json.loads(output)
            self.assertEqual("applied", applied["result"])
            self.assertEqual("committed", applied["database_commit"])
            self.assertEqual(1, applied["rows_migrated"])
            self.assertEqual(applied, json.loads(report.read_text(encoding="utf-8")))

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            package_id = "eLibrary|20260101-5001|package"
            package = con.execute(
                "SELECT * FROM documents WHERE document_id=?", (package_id,)
            ).fetchone()
            self.assertIsNotNone(package)
            self.assertEqual("", package["attachment_id"])
            self.assertEqual(
                "eLibrary accession 20260101-5001 package", package["title"])
            self.assertEqual(digests["legacy"], package["content_hash"])
            self.assertEqual(9876, package["byte_size"])
            self.assertEqual("unknown", package["text_layer"])
            self.assertIsNone(con.execute(
                "SELECT 1 FROM documents WHERE document_id=?",
                ("eLibrary|20260101-5001|ATTACHMENT-FIRST",),
            ).fetchone())
            listing_id = "eLibrary|20260101-5001|listing"
            self.assertEqual(
                [(listing_id, "", "")],
                [tuple(row) for row in con.execute(
                    "SELECT document_id,attachment_id,content_hash FROM documents "
                    "WHERE document_id=?", (listing_id,))],
            )
            for table in ("observations", "document_facts", "events"):
                self.assertEqual(
                    [(package_id,)],
                    [tuple(row) for row in con.execute(
                        f'SELECT document_id FROM "{table}" WHERE filing_id=?',
                        ("20260101-5001",),
                    )],
                )
            self.assertEqual(
                preserved_counts,
                {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                 for table in preserved_counts},
            )
            for accession, expected in protected.items():
                self.assertEqual(
                    expected,
                    [tuple(row) for row in con.execute(
                        "SELECT * FROM documents WHERE filing_id=?", (accession,)
                    ).fetchall()],
                )
            current_before = "eLibrary|20260102-5002|package"
            self.assertEqual(
                "partial",
                con.execute("SELECT text_layer FROM documents WHERE document_id=?",
                            (current_before,)).fetchone()[0],
            )
            self.assertEqual("ok", con.execute("PRAGMA quick_check").fetchone()[0])
            self.assertIsNone(con.execute("PRAGMA foreign_key_check").fetchone())
            con.close()

            rc, output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(0, rc, error)
            second = json.loads(output)
            self.assertEqual("verified_noop", second["result"])
            self.assertEqual("unchanged", second["database_commit"])

    def test_ambiguous_or_mismatched_package_state_is_refused_without_writes(self):
        mutations = {
            "hash_mismatch": (
                "UPDATE documents SET content_hash=? WHERE filing_id='20260101-5001'",
                ("f" * 64,),
            ),
            "second_hashed_document": (
                "INSERT INTO documents(document_id,source_system,filing_id,accession_number,"
                "attachment_id,title,byte_size,content_hash,text_layer,availability) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("eLibrary|20260101-5001|SECOND", "eLibrary", "20260101-5001",
                 "20260101-5001", "SECOND", "second.pdf", 100, "a" * 64,
                 "yes", "retrieved"),
            ),
            "missing_hashed_document": (
                "DELETE FROM documents WHERE document_id='eLibrary|20260102-5002|package'",
                (),
            ),
            "accession_mismatch": (
                "UPDATE documents SET accession_number='20260101-9999' "
                "WHERE document_id='eLibrary|20260101-5001|ATTACHMENT-FIRST'",
                (),
            ),
            "document_fact_hash_mismatch": (
                "UPDATE document_facts SET content_hash=? "
                "WHERE document_id='eLibrary|20260101-5001|ATTACHMENT-FIRST'",
                ("f" * 64,),
            ),
        }
        for label, (statement, params) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="ferc-migration-005-refuse-") as temporary:
                db = pathlib.Path(temporary) / "candidate.sqlite"
                create_database(db)
                con = sqlite3.connect(db)
                con.execute(statement, params)
                con.commit()
                before = {
                    table: rows(con, table)
                    for table in ("documents", "document_facts")
                }
                con.close()

                rc, output, error = quiet_main(["--db", str(db), "--apply"])
                self.assertEqual(2, rc, error)
                refused = json.loads(output)
                self.assertEqual("refused_ambiguous_package_identity", refused["result"])
                self.assertTrue(refused["before"]["problems"])
                con = sqlite3.connect(db)
                self.assertEqual(
                    before,
                    {table: rows(con, table) for table in before},
                )
                con.close()

    def test_blank_document_fact_hash_is_filled_during_apply(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-005-fact-") as temporary:
            db = pathlib.Path(temporary) / "candidate.sqlite"
            digests = create_database(db)
            con = sqlite3.connect(db)
            con.execute(
                "UPDATE document_facts SET content_hash='' "
                "WHERE document_id='eLibrary|20260101-5001|ATTACHMENT-FIRST'")
            con.execute(
                "INSERT INTO document_facts(document_fact_id,document_id,source_system,"
                "filing_id,entity_key,assertion_type,scope_note,verbatim_span,"
                "extraction_method,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("dfact-current-package", "eLibrary|20260102-5002|package",
                 "eLibrary", "20260102-5002", "C000001", "test_assertion",
                 "accession package", "filed words", "pdf_text_span", ""),
            )
            con.commit()
            con.close()

            rc, output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(0, rc, error)
            applied = json.loads(output)
            self.assertEqual(2, applied["document_fact_hashes_filled"])
            con = sqlite3.connect(db)
            self.assertEqual(
                digests["legacy"],
                con.execute(
                    "SELECT content_hash FROM document_facts "
                    "WHERE document_fact_id='dfact-package'").fetchone()[0],
            )
            self.assertEqual(
                digests["current"],
                con.execute(
                    "SELECT content_hash FROM document_facts "
                    "WHERE document_fact_id='dfact-current-package'").fetchone()[0],
            )
            con.close()

    def test_injected_mid_apply_error_rolls_back_documents_and_references(self):
        with tempfile.TemporaryDirectory(prefix="ferc-migration-005-rollback-") as temporary:
            db = pathlib.Path(temporary) / "candidate.sqlite"
            create_database(db, two_legacy=True)
            con = sqlite3.connect(db)
            before = {
                table: rows(con, table)
                for table in ("documents", "observations", "document_facts", "events")
            }
            con.close()

            original = MIGRATION._rewrite_document
            calls = 0

            def fail_after_first(con, row):
                nonlocal calls
                original(con, row)
                calls += 1
                if calls == 1:
                    raise RuntimeError("injected failure after a complete first rewrite")

            with mock.patch.object(MIGRATION, "_rewrite_document", fail_after_first):
                rc, _output, error = quiet_main(["--db", str(db), "--apply"])
            self.assertEqual(2, rc)
            self.assertIn("injected failure", error)
            con = sqlite3.connect(db)
            self.assertEqual(before, {table: rows(con, table) for table in before})
            self.assertEqual("ok", con.execute("PRAGMA quick_check").fetchone()[0])
            self.assertIsNone(con.execute("PRAGMA foreign_key_check").fetchone())
            con.close()


if __name__ == "__main__":
    unittest.main()
