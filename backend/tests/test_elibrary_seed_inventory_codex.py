"""Regressions for the versioned eLibrary seed and per-run input identity.

Synthetic state stays in temporary directories.  The one real-file assertion
binds the deliberately shipped 62-row seed; it never reads worker discovery.
"""

from __future__ import annotations

import csv
import hashlib
import pathlib
import tempfile
import unittest
from unittest import mock

from adapters import elibrary_docs as docs
from ferclib.ledger import input_digest
import run


ROOT = pathlib.Path(__file__).resolve().parents[1]
SEED_SHA256 = "b4e767d3906ed096a5404aeff8db6c1f3504994c2b71db8fac5e36d1baff6032"


class ELibrarySeedContract(unittest.TestCase):

    def test_seed_is_versioned_packaged_input_with_exact_identity(self):
        expected = (ROOT / "inputs" / "official_ferc" / "elibrary" /
                    "rate_proceedings_v1.csv")
        self.assertEqual(expected, docs.SEED_CSV)
        raw = expected.read_bytes()
        self.assertEqual(25939, len(raw))
        self.assertEqual(SEED_SHA256, hashlib.sha256(raw).hexdigest())
        with expected.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(62, len(rows))
        self.assertEqual(
            {"C000434", "C000654", "C000662", "C001049"},
            {row["cid"] for row in rows})

    def test_missing_seed_fails_instead_of_silently_changing_dockets(self):
        with tempfile.TemporaryDirectory(prefix="ferc-missing-elibrary-seed-") as td:
            absent = pathlib.Path(td) / "rate_proceedings_v1.csv"
            with mock.patch.object(docs, "SEED_CSV", absent):
                with self.assertRaisesRegex(FileNotFoundError, "mandatory versioned"):
                    docs._seed_dockets("C000654")

    def test_curated_seed_retains_transco_and_liquids_history(self):
        transco, transco_rows = docs._seed_dockets("C000654")
        liquids, liquids_rows = docs._seed_dockets("C001049")
        self.assertEqual(["RP25-1189", "RP24-1035", "RP18-1126"], transco[:3])
        self.assertEqual(["IS26-546", "IS24-804", "IS24-810"], liquids[:3])
        self.assertTrue(transco_rows)
        self.assertTrue(liquids_rows)

    def test_seed_bytes_participate_in_resume_config_digest(self):
        with tempfile.TemporaryDirectory(prefix="ferc-config-digest-") as td:
            root = pathlib.Path(td)
            (root / "ferclib").mkdir()
            (root / "inputs" / "official_ferc" / "elibrary").mkdir(parents=True)
            (root / "config" / "annotations").mkdir(parents=True)
            (root / "ferclib" / "registry.py").write_text("registry\n")
            universe = root / "config" / "universe.csv"
            universe.write_text("entity_key\nC1\n")
            annotations = root / "config" / "annotations"
            (annotations / "MANIFEST.json").write_text("{}\n")
            seed = root / "inputs" / "official_ferc" / "elibrary" / \
                "rate_proceedings_v1.csv"
            seed.write_text("cid,docket\nC1,RP1-1\n")
            lng_seed = root / "inputs" / "official_ferc" / "elibrary" / \
                "lng_facility_sources_v1.csv"
            lng_seed.write_text("entity_key,accession\nC1,20250101-0001\n")
            with (mock.patch.object(run, "HERE", root),
                  mock.patch.object(run, "UNIVERSE", universe),
                  mock.patch.object(run, "ANNOTATIONS_DIR", annotations),
                  mock.patch.object(run, "_CONFIG_DIGEST", None)):
                before = run.config_digest()
                run._CONFIG_DIGEST = None
                seed.write_text("cid,docket\nC1,RP1-2\n")
                after_rate_change = run.config_digest()
                run._CONFIG_DIGEST = None
                lng_seed.write_text("entity_key,accession\nC1,20250101-0002\n")
                after_lng_change = run.config_digest()
            self.assertNotEqual(before, after_rate_change)
            self.assertNotEqual(after_rate_change, after_lng_change)


class ELibraryRunInputIdentity(unittest.TestCase):

    class Staging:
        def __init__(self):
            self.filing = None

        @staticmethod
        def classify_version(*_args, **_kwargs):
            return "original", None

        def write_filing_bundle(self, row, **_kwargs):
            self.filing = dict(row)

    class Context:
        def __init__(self):
            self.staging = ELibraryRunInputIdentity.Staging()

    def test_persist_returns_complete_occurrence_shape_for_append_only_inventory(self):
        context = self.Context()
        entity = {"entity_key": "C000654"}
        filing = {
            "accession": "20260825-5125",
            "filed_date": "2026-08-25",
            "posted_date": "2026-08-25",
            "issued_date": "2026-08-25",
            "description": "synthetic tariff occurrence",
            "classification": {"label": "tariff filing"},
            "content_hash": "a" * 64,
            "is_twin": False,
            "dockets": ["RP26-1091-000"],
        }
        docs._persist(context, entity, filing)
        expected = {
            "source_system": "eLibrary",
            "filing_id": "20260825-5125",
            "accession_number": "20260825-5125",
            "submitted_on": "2026-08-25",
            "form": "eLibrary document",
            "reporting_year": 2026,
            "reporting_period": "as_of",
            "snapshot_date": None,
            "is_canonical": 1,
        }
        self.assertEqual(expected, {key: filing.get(key) for key in expected})
        self.assertEqual(expected["filing_id"], context.staging.filing["filing_id"])

        changed = dict(filing, accession_number="20260826-5000",
                       filing_id="20260826-5000")
        self.assertNotEqual(input_digest([filing]), input_digest([changed]))


if __name__ == "__main__":
    unittest.main()
