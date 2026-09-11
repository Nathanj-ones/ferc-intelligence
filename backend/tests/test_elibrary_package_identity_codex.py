"""Regression controls for eLibrary accession-package document identity."""

from __future__ import annotations

import pathlib
import tempfile
import types
import unittest
from unittest import mock

from adapters import elibrary_docs, lng
from ferclib import elibrary
from ferclib.staging import FilingEntityConflict, Staging


ACCESSION = "20260101-5000"
PACKAGE_HASH = "a" * 64
ENTITY = "C-PACKAGE"


class _FakeElibrary:
    public = [
        {"attachment_id": "first-id", "avail_code": "P"},
        {"attachment_id": "second-id", "avail_code": "P"},
    ]

    def file_list(self, _accession):
        return [dict(row) for row in self.public]

    def download(self, _accession, _ids, *, expected_files):
        self.expected_files = expected_files
        return b"synthetic-package", {
            "content_hash": PACKAGE_HASH,
            "media_type": "application/octet-stream",
            "byte_size": 123,
            "first_seen_at": "2026-09-11T01:00:00+00:00",
            "last_seen_at": "2026-09-11T02:00:00+00:00",
        }

    @staticmethod
    def extract_text(data, _name):
        return data.decode(), "fixture_text", "yes"


class _CaptureStaging:
    def __init__(self):
        self.documents = []
        self.bundle_options = {}
        self.resolved = []

    @staticmethod
    def is_done(*_args):
        return False

    @staticmethod
    def checkpoint(*_args, **_kwargs):
        return None

    def resolve_blockers(self, adapter, scope):
        self.resolved.append((adapter, scope))
        return 0

    @staticmethod
    def classify_version(*_args):
        return "original", None

    @staticmethod
    def query(*_args, **_kwargs):
        return []

    def write_filing_bundle(self, _filing, *, documents, **kwargs):
        self.documents = [dict(row) for row in documents]
        self.bundle_options = dict(kwargs)


def _context():
    return types.SimpleNamespace(
        staging=_CaptureStaging(), cache=object(), force=False,
        log=lambda *_args, **_kwargs: None,
    )


def _filing():
    return {
        "accession": ACCESSION,
        "description": "Synthetic multi-file eLibrary filing",
        "class_pairs": [],
        "class_types": [],
        "filed_date": "2026-01-01",
        "posted_date": "2026-01-01",
        "issued_date": "",
        "avail_code": "P",
        "dockets": [],
        "docket_bases": [],
        "availability_twins": [],
        "classification": {
            "label": "synthetic filing",
            "stage": "other_submittal",
            "is_issuance": False,
        },
    }


class AdapterPackageIdentityTests(unittest.TestCase):
    MEMBERS = [
        ("first.pdf", b"short", "pdf"),
        ("second.pdf", b"substantially longer selected text", "pdf"),
    ]

    def _fetch(self, module, ctx, filing):
        fake = _FakeElibrary()
        with mock.patch.object(elibrary, "members", return_value=self.MEMBERS), \
                mock.patch.object(
                    elibrary, "reconcile_filing_content_hash",
                    return_value=(PACKAGE_HASH, False, 123)):
            if module is elibrary_docs:
                module._fetch_document(ctx, fake, ENTITY, filing)
            else:
                module._fetch_document(ctx, fake, ENTITY, filing)

    def test_rate_adapter_models_the_download_as_one_package(self):
        ctx, filing = _context(), _filing()
        self._fetch(elibrary_docs, ctx, filing)
        self.assertEqual("second.pdf", filing["selected_member_name"])
        self.assertEqual("partial", filing["text_layer"])

        elibrary_docs._persist(
            ctx, {"entity_key": ENTITY, "legal_name": "Package Fixture"}, filing)
        document = ctx.staging.documents[0]
        self.assertEqual(f"eLibrary|{ACCESSION}|package", document["document_id"])
        self.assertEqual("", document["attachment_id"])
        self.assertEqual(
            f"eLibrary accession {ACCESSION} package (2 public files)",
            document["title"],
        )
        self.assertEqual((123, PACKAGE_HASH),
                         (document["byte_size"], document["content_hash"]))
        self.assertTrue(ctx.staging.bundle_options["replace_documents"])

    def test_lng_adapter_uses_the_same_package_contract(self):
        ctx, filing = _context(), _filing()
        self._fetch(lng, ctx, filing)
        entity_key, cfg = next(iter(lng.FACILITIES.items()))
        entity = {"entity_key": entity_key,
                  "legal_name": lng.FACILITY_ENTITY_NAMES[entity_key]}
        association = [{
            "entity_key": entity_key,
            "association_role": "source_entity",
            "facility_key": cfg["facility"],
            "evidence_ref": "fixture",
        }]
        with mock.patch.object(lng, "_filing_entity_associations",
                               return_value=association):
            lng._persist(ctx, entity, cfg, filing, is_twin=False)
        document = ctx.staging.documents[0]
        self.assertEqual(f"eLibrary|{ACCESSION}|package", document["document_id"])
        self.assertEqual("", document["attachment_id"])
        self.assertNotIn("second.pdf", document["title"])
        self.assertEqual("partial", document["text_layer"])
        self.assertTrue(ctx.staging.bundle_options["replace_documents"])


def _filing_row():
    return {
        "source_system": "eLibrary",
        "filing_id": ACCESSION,
        "entity_key": ENTITY,
        "form": "eLibrary document",
        "content_hash": PACKAGE_HASH,
        "is_canonical": 1,
    }


def _document(document_id: str, content_hash: str):
    return {
        "document_id": document_id,
        "source_system": "eLibrary",
        "filing_id": ACCESSION,
        "accession_number": ACCESSION,
        "attachment_id": "legacy-first-id" if document_id.endswith("legacy") else "",
        "title": "legacy selected member" if document_id.endswith("legacy")
        else f"eLibrary accession {ACCESSION} package (2 public files)",
        "media_type": "application/octet-stream",
        "byte_size": 123,
        "content_hash": content_hash,
        "availability": "retrieved",
    }


class PackageReplacementTests(unittest.TestCase):
    OLD = f"eLibrary|{ACCESSION}|legacy"
    NEW = f"eLibrary|{ACCESSION}|package"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ferc-package-identity-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Staging(pathlib.Path(self.temporary.name) / "staging.sqlite")
        self.addCleanup(self.store.close)
        self.store.upsert("entities", [{
            "entity_key": ENTITY,
            "cid": ENTITY,
            "legal_name": "Package Fixture",
        }], ["entity_key"])
        self.store.write_filing_bundle(
            _filing_row(), documents=[_document(self.OLD, PACKAGE_HASH)])
        self.store.upsert("document_facts", [{
            "document_fact_id": "fact-package",
            "document_id": self.OLD,
            "source_system": "eLibrary",
            "filing_id": ACCESSION,
            "entity_key": ENTITY,
            "assertion_type": "fixture",
            "scope_note": "fixture",
            "verbatim_span": "fixture",
            "extraction_method": "fixture",
            "content_hash": PACKAGE_HASH,
        }], ["document_fact_id"])
        self.store.upsert("observations", [{
            "observation_id": "obs-package",
            "entity_key": ENTITY,
            "metric_id": "fixture",
            "source_regime": "eLibrary document",
            "period_basis": "as_of",
            "scope": "fixture",
            "availability": "present",
            "origin": "filed",
            "method": "reported",
            "version_status": "original",
            "validation": "not_yet_validated",
            "document_id": self.OLD,
        }], ["observation_id"])
        self.store.upsert("events", [{
            "event_id": "event-package",
            "event_class": "rate",
            "event_type": "fixture",
            "headline": "fixture",
            "destination": "filing_archive",
            "document_id": self.OLD,
            "first_seen_at": "2026-09-11T01:00:00+00:00",
            "is_backfill": 0,
        }], ["event_id"])

    def test_replacement_rewires_references_and_is_idempotent(self):
        for _attempt in range(2):
            self.store.write_filing_bundle(
                _filing_row(), documents=[_document(self.NEW, PACKAGE_HASH)],
                replace_documents=True)
        documents = self.store.query(
            "SELECT document_id FROM documents WHERE filing_id=?",
            (ACCESSION,))
        self.assertEqual([self.NEW], [row["document_id"] for row in documents])
        for table in ("document_facts", "observations", "events"):
            row = self.store.query(
                f"SELECT document_id FROM {table} WHERE document_id=?", (self.NEW,))
            self.assertEqual(1, len(row), table)

    def test_different_package_hash_rolls_back_without_deleting_legacy_row(self):
        with self.assertRaisesRegex(FilingEntityConflict, "differs"):
            self.store.write_filing_bundle(
                _filing_row(), documents=[_document(self.NEW, "b" * 64)],
                replace_documents=True)
        documents = self.store.query(
            "SELECT document_id,content_hash FROM documents WHERE filing_id=?",
            (ACCESSION,))
        self.assertEqual([(self.OLD, PACKAGE_HASH)],
                         [(row["document_id"], row["content_hash"])
                          for row in documents])

    def test_malformed_package_contract_rolls_back_without_rewiring(self):
        mutations = {
            "wrong document ID": lambda row: row.update(
                document_id=f"eLibrary|{ACCESSION}|foreign"),
            "wrong source": lambda row: row.update(source_system="Other"),
            "wrong filing": lambda row: row.update(filing_id="20260101-9999"),
            "wrong accession": lambda row: row.update(
                accession_number="20260101-9999"),
            "attachment-shaped package": lambda row: row.update(
                attachment_id="one-member-id"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                replacement = _document(self.NEW, PACKAGE_HASH)
                mutate(replacement)
                with self.assertRaises(FilingEntityConflict):
                    self.store.write_filing_bundle(
                        _filing_row(), documents=[replacement],
                        replace_documents=True)
                self.assertEqual(
                    [self.OLD],
                    [row["document_id"] for row in self.store.query(
                        "SELECT document_id FROM documents WHERE filing_id=?",
                        (ACCESSION,))],
                )
                for table in ("document_facts", "observations", "events"):
                    self.assertEqual(1, len(self.store.query(
                        f"SELECT document_id FROM {table} WHERE document_id=?",
                        (self.OLD,))), table)


if __name__ == "__main__":
    unittest.main()
