"""Regression tests for the captured Oil Pipeline Index source evidence.

The static Python table is not treated as its own authority.  These tests bind
every row to a captured official publication occurrence and prove that both
byte corruption and a self-consistent-but-semantically-wrong bundle fail before
the adapter writes anything.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import oil_index  # noqa: E402


class _Staging:
    def __init__(self):
        self.bundles = []

    def write_filing_bundle(self, filing, *, facts):
        self.bundles.append((filing, facts))


class _Context:
    def __init__(self):
        self.staging = _Staging()


class OilIndexOfficialSourceBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = oil_index.SOURCE_BUNDLE_ROOT
        cls.manifest_path = oil_index.SOURCE_MANIFEST_PATH
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    def _copy_selected(self, document):
        temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(temp.name) / "bundle"
        root.mkdir()
        manifest = copy.deepcopy(self.manifest)
        manifest["documents"] = [
            row for row in manifest["documents"]
            if row["fr_document"] == document]
        entry = manifest["documents"][0]
        for item in (entry["metadata"], entry["official_pdf"],
                     entry["extraction_text"],
                     manifest["supplemental_attempts"][0]):
            src = self.root / item["path"]
            dst = root / item["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Archive inputs are intentionally read-only.  Fault injection
            # mutates only this disposable copy, so do not carry the source
            # mode bit into the temporary fixture.
            shutil.copyfile(src, dst)
        for prior in manifest.get("binding_history", []):
            shutil.copyfile(self.root / prior["receipt"], root / prior["receipt"])
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        return temp, root, manifest

    def test_complete_bundle_is_valid_and_has_one_occurrence_per_table_row(self):
        self.assertEqual(oil_index.verify_source_bundle(), [])
        documents = self.manifest["documents"]
        self.assertEqual(len(documents), 31)
        self.assertEqual(
            {row["fr_document"] for row in documents},
            {row["fr_document"] for row in oil_index.INDEX_TABLE})
        self.assertEqual(len({row["fr_document"] for row in documents}), 31)
        self.assertFalse(self.manifest["runtime_network_required"])

    def test_every_http_200_claim_has_nonempty_hash_verified_captured_bytes(self):
        for document in self.manifest["documents"]:
            for kind in ("metadata", "official_pdf", "extraction_text"):
                artifact = document[kind]
                path = self.root / artifact["path"]
                body = path.read_bytes()
                self.assertEqual(artifact["http_status"], 200)
                self.assertTrue(body)
                self.assertEqual(artifact["bytes"], len(body))
                self.assertEqual(
                    artifact["sha256"], hashlib.sha256(body).hexdigest())
        # The adapter itself claims no network success; it points readers to the
        # per-response manifest instead of turning a citation into an HTTP claim.
        self.assertFalse(any(
            "HTTP 200" in str(attempt.get("outcome", ""))
            for attempt in oil_index.RETRIEVAL_ATTEMPTS))

    def test_authoritative_pdf_identity_and_extraction_text_are_both_retained(self):
        table = {row["fr_document"]: row for row in oil_index.INDEX_TABLE}
        for entry in self.manifest["documents"]:
            expected = table[entry["fr_document"]]
            self.assertTrue(entry["official_pdf"]["request_url"].startswith(
                "https://www.govinfo.gov/content/pkg/"))
            self.assertTrue((self.root / entry["official_pdf"]["path"])
                            .read_bytes().startswith(b"%PDF-"))
            text = (self.root / entry["extraction_text"]["path"])
            rendered = text.read_text(encoding="utf-8")
            self.assertIn(expected["factor"], rendered)
            self.assertTrue(oil_index._text_has_decimal(
                rendered, expected["index_change"]))
            self.assertEqual(entry["asserted_values"]["factor"],
                             expected["factor"])
            self.assertEqual(entry["consumer"]["source_table_version"],
                             oil_index.SOURCE_TABLE_VERSION)

    def test_no_notice_supported_change_is_suppressed(self):
        self.assertTrue(all(row["index_change"] for row in oil_index.INDEX_TABLE))
        changes = [row for row in oil_index.observations_for()[0]
                   if row["metric_id"] == oil_index.METRIC_CHANGE]
        change_facts = [row for row in oil_index.all_source_facts()
                        if row["concept_local"] == "PublishedIndexFigureChange"]
        # Thirty current intervals, plus the separately retained 2022 revision.
        self.assertEqual(len(changes), 30)
        self.assertEqual(len(change_facts), 31)
        self.assertEqual({row["interval_start"] for row in oil_index.INDEX_TABLE},
                         {row["period_start"] for row in changes})
        for entry in self.manifest["documents"]:
            self.assertIn(oil_index.METRIC_CHANGE,
                          entry["consumer"]["metrics"])
            self.assertTrue(entry["asserted_values"]["index_change"])

    def test_ferc_summary_403_is_captured_but_not_called_a_source_outage(self):
        attempt = self.manifest["supplemental_attempts"][0]
        body = (self.root / attempt["path"]).read_bytes()
        self.assertTrue(attempt["attempted"])
        self.assertEqual(attempt["http_status"], 403)
        self.assertEqual(attempt["classification"],
                         oil_index.CLASS_ACCESS_BLOCKED)
        self.assertEqual(hashlib.sha256(body).hexdigest(), attempt["sha256"])
        blocked = [row for row in oil_index.RETRIEVAL_ATTEMPTS
                   if row["classification"] == oil_index.CLASS_ACCESS_BLOCKED]
        self.assertEqual(len(blocked), 1)
        self.assertIn("NOT a FERC outage", blocked[0]["explicitly_not"])

    def test_source_occurrence_persists_pdf_url_and_bundle_versions(self):
        record = oil_index.filing_record(oil_index.INDEX_TABLE[0])
        self.assertIn("/pdf/97-13604.pdf", record["source_url"])
        self.assertIn(oil_index.SOURCE_BUNDLE_VERSION,
                      record["canonical_reason"])
        fact = oil_index.source_facts(oil_index.INDEX_TABLE[0])[0]
        self.assertIn(oil_index.SOURCE_TABLE_VERSION,
                      fact["taxonomy_version"])
        self.assertIn(oil_index.SOURCE_BUNDLE_VERSION,
                      fact["taxonomy_version"])

    def test_generated_evidence_report_pins_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "oil_source_evidence.json"
            oil_index.write_evidence(path)
            report = json.loads(path.read_text(encoding="utf-8"))
        manifest_bytes = self.manifest_path.read_bytes()
        self.assertEqual(report["source_manifest_sha256"],
                         hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual(report["source_manifest_bytes"], len(manifest_bytes))
        self.assertEqual(report["captured_document_occurrences"], 31)
        self.assertEqual(report["source_bundle_problems"], [])

    def test_corrupted_pdf_is_rejected(self):
        temp, root, manifest = self._copy_selected("2024-11147")
        self.addCleanup(temp.cleanup)
        entry = manifest["documents"][0]
        path = root / entry["official_pdf"]["path"]
        path.write_bytes(path.read_bytes() + b"tampered")
        problems = oil_index.verify_source_bundle(
            filing_ids=["2024-11147"], bundle_root=root)
        self.assertTrue(any("official_pdf SHA-256 mismatch" in value
                            for value in problems), problems)

    def test_rehashed_but_semantically_wrong_text_is_rejected(self):
        temp, root, manifest = self._copy_selected("2024-11147")
        self.addCleanup(temp.cleanup)
        entry = manifest["documents"][0]
        artifact = entry["extraction_text"]
        path = root / artifact["path"]
        body = path.read_bytes().replace(b"1.012647", b"9.999999")
        self.assertNotIn(b"1.012647", body)
        path.write_bytes(body)
        artifact["bytes"] = len(body)
        artifact["sha256"] = hashlib.sha256(body).hexdigest()
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        problems = oil_index.verify_source_bundle(
            filing_ids=["2024-11147"], bundle_root=root)
        self.assertIn("2024-11147: extraction text omits factor", problems)

    def test_rehashed_wrong_target_interval_is_rejected(self):
        temp, root, manifest = self._copy_selected("2024-11147")
        self.addCleanup(temp.cleanup)
        entry = manifest["documents"][0]
        artifact = entry["extraction_text"]
        path = root / artifact["path"]
        body = path.read_bytes().replace(b"2024", b"2094")
        self.assertNotIn(b"2024", body)
        path.write_bytes(body)
        artifact["bytes"] = len(body)
        artifact["sha256"] = hashlib.sha256(body).hexdigest()
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        problems = oil_index.verify_source_bundle(
            filing_ids=["2024-11147"], bundle_root=root)
        self.assertIn(
            "2024-11147: no operative passage supports target interval", problems)

    def test_missing_source_stops_retrieve_before_any_write(self):
        temp, root, manifest = self._copy_selected("2024-11147")
        self.addCleanup(temp.cleanup)
        path = root / manifest["documents"][0]["official_pdf"]["path"]
        path.unlink()
        ctx = _Context()
        with mock.patch.object(oil_index, "SOURCE_BUNDLE_ROOT", root):
            with self.assertRaisesRegex(RuntimeError,
                                        "official source bundle is invalid"):
                oil_index.retrieve(
                    ctx, oil_index.GLOBAL_RUN_ENTITY,
                    year_from=2024, year_to=2024)
        self.assertEqual(ctx.staging.bundles, [])

    def test_valid_retrieve_is_genuinely_offline(self):
        ctx = _Context()
        with mock.patch.object(
                socket, "create_connection",
                side_effect=AssertionError("network access attempted")):
            filings = oil_index.retrieve(
                ctx, oil_index.GLOBAL_RUN_ENTITY,
                year_from=2024, year_to=2024)
        self.assertEqual([row["filing_id"] for row in filings], ["2024-11147"])
        self.assertEqual(len(ctx.staging.bundles), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
