"""Regression controls for package-aligned input snapshot identity.

The full package carries one 109 MB official response at its content-addressed
cache path and omits a byte-identical capture-tree alias.  These fixtures use a
small synthetic body but exercise the production hash/size/index/provenance
checks before that sole alias can be excluded from the snapshot.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run as pipeline  # noqa: E402


ALIAS_REL = (
    "inputs/official_ferc_recovery/individual_responses/20231229-5212/"
    "07_F19520DA-2360-C302-8619-8CB6CB100000.body")
RESULTS_REL = (
    "implementation_logs/input_recovery/"
    "OFFICIAL_FERC_INDIVIDUAL_20231229-5212.json")
LEDGER_REL = "implementation_logs/input_recovery/cache_import_runs.jsonl"
ACCESSION = "20231229-5212"
ATTACHMENT = "F19520DA-2360-C302-8619-8CB6CB100000"
SOURCE_URL = (
    "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
    "#body=synthetic-packaging-regression")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class InputSnapshotPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ferc-input-snapshot-packaging-")
        self.base = pathlib.Path(self.temporary.name)
        self.body = b"synthetic exact official response\x00for package identity\n"
        self.body_hash = _sha(self.body)
        self.cache_rel = f"objects/{self.body_hash[:2]}/{self.body_hash}"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write(path: pathlib.Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def _make_root(self, label: str, *, include_alias: bool) -> tuple[pathlib.Path, dict]:
        root = self.base / label

        # Every production input subtree receives a stable marker.  The
        # unrelated marker is used to prove that no general missing-file waiver
        # was introduced while canonicalising the one exact alias.
        files = {
            "config/universe.csv": b"entity_key\nC-SYNTHETIC\n",
            "config/annotations/MANIFEST.json": b"{}\n",
            "config/run_plan.json": b'{"schema":"synthetic-run-plan"}\n',
            "inputs/day3/input.txt": b"day3\n",
            "inputs/audit_baseline/input.txt": b"audit\n",
            "inputs/official_ferc/unrelated.txt": b"must remain visible\n",
            "inputs/official_ferc_recovery/other.body": b"other recovery\n",
            "inputs/official_ferc_capacity_20260226_5162/input.txt": b"capacity\n",
            "inputs/official_ferc_elibrary_search_recovery_20260909/input.txt": b"search\n",
            "inputs/reference_regressions/input.txt": b"history\n",
        }
        for relative, raw in files.items():
            self._write(root / relative, raw)

        if include_alias:
            self._write(root / ALIAS_REL, self.body)
        self._write(root / "source_cache" / self.cache_rel, self.body)

        cache_key = hashlib.sha256(SOURCE_URL.encode("utf-8")).hexdigest()
        index = {
            cache_key: {
                "content_hash": self.body_hash,
                "cache_path": self.cache_rel,
                "byte_size": len(self.body),
                "source_system": "eLibrary",
                "source_url": SOURCE_URL,
            }
        }
        self._write(
            root / "source_cache/index.json",
            (json.dumps(index, sort_keys=True) + "\n").encode("utf-8"))

        results = {
            "schema": "official_ferc_individual_retrieval_v1",
            "results": [{
                "accession_number": ACCESSION,
                "attachment_id": ATTACHMENT,
                "request": {"cache_url": SOURCE_URL},
                "response": {
                    "http_status": 200,
                    "complete_body": True,
                    "bytes": len(self.body),
                    "sha256": self.body_hash,
                },
            }],
        }
        results_raw = (json.dumps(results, sort_keys=True) + "\n").encode("utf-8")
        self._write(root / RESULTS_REL, results_raw)

        ledger = {
            "schema": "official_recovery_cache_import_run_v1",
            "status": "complete",
            "results_identity": {
                "bytes": len(results_raw), "sha256": _sha(results_raw)},
            "entries": [{
                "accession": ACCESSION,
                "attachment_ids": [ATTACHMENT],
                "action": "imported",
                "http_status": 200,
                "cache_url": SOURCE_URL,
                "byte_size": len(self.body),
                "content_sha256": self.body_hash,
            }],
        }
        ledger_raw = (json.dumps(ledger, sort_keys=True) + "\n").encode("utf-8")
        self._write(root / LEDGER_REL, ledger_raw)

        declaration = {
            "alias_path": ALIAS_REL,
            "cache_path": self.cache_rel,
            "sha256": self.body_hash,
            "bytes": len(self.body),
            "accession": ACCESSION,
            "attachment_id": ATTACHMENT,
            "source_url": SOURCE_URL,
            "results_path": RESULTS_REL,
            "results_sha256": _sha(results_raw),
            "results_bytes": len(results_raw),
            "ledger_path": LEDGER_REL,
            "ledger_sha256": _sha(ledger_raw),
            "ledger_bytes": len(ledger_raw),
        }
        return root, declaration

    @contextlib.contextmanager
    def _candidate(self, root: pathlib.Path, declaration: dict):
        with mock.patch.object(pipeline, "HERE", root), \
                mock.patch.object(pipeline, "SOURCE_CACHE", root / "source_cache"), \
                mock.patch.object(pipeline, "UNIVERSE", root / "config/universe.csv"), \
                mock.patch.object(
                    pipeline, "ANNOTATIONS_DIR", root / "config/annotations"), \
                mock.patch.object(pipeline, "RUN_PLAN", root / "config/run_plan.json"), \
                mock.patch.object(pipeline, "_REDUNDANT_INPUT_ALIAS", declaration):
            yield

    def _snapshot(self, root: pathlib.Path, declaration: dict) -> str:
        with self._candidate(root, declaration):
            return pipeline._input_snapshot()

    def test_build_a_alias_and_packaged_canonical_object_have_same_snapshot(self):
        build_a, declaration = self._make_root("build-a", include_alias=True)
        packaged, packaged_declaration = self._make_root(
            "packaged", include_alias=False)

        self.assertEqual(declaration, packaged_declaration)
        self.assertEqual(
            self._snapshot(build_a, declaration),
            self._snapshot(packaged, packaged_declaration))

    def test_tampered_alias_is_refused_before_exclusion(self):
        root, declaration = self._make_root("tampered-alias", include_alias=True)
        (root / ALIAS_REL).write_bytes(self.body + b"tampered")

        with self.assertRaisesRegex(RuntimeError, "capture alias identity mismatch"):
            self._snapshot(root, declaration)

    def test_missing_or_tampered_canonical_identity_is_refused(self):
        cases = ("missing-object", "missing-index", "changed-index")
        for case in cases:
            with self.subTest(case=case):
                root, declaration = self._make_root(case, include_alias=False)
                if case == "missing-object":
                    (root / "source_cache" / self.cache_rel).unlink()
                    pattern = "canonical cache object.*absent"
                elif case == "missing-index":
                    (root / "source_cache/index.json").unlink()
                    pattern = "source-cache index.*absent"
                else:
                    index_path = root / "source_cache/index.json"
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    next(iter(index.values()))["source_system"] = "not-eLibrary"
                    index_path.write_text(
                        json.dumps(index, sort_keys=True) + "\n", encoding="utf-8")
                    pattern = "index does not bind"
                with self.assertRaisesRegex(RuntimeError, pattern):
                    self._snapshot(root, declaration)

    def test_tampered_retained_provenance_is_refused(self):
        cases = (RESULTS_REL, LEDGER_REL)
        for relative in cases:
            with self.subTest(relative=relative):
                root, declaration = self._make_root(
                    "provenance-" + pathlib.Path(relative).name, include_alias=False)
                with (root / relative).open("ab") as handle:
                    handle.write(b"tampered\n")
                with self.assertRaisesRegex(RuntimeError, "provenance.*identity mismatch"):
                    self._snapshot(root, declaration)

    def test_unrelated_omitted_input_changes_snapshot(self):
        complete, declaration = self._make_root("complete", include_alias=True)
        incomplete, incomplete_declaration = self._make_root(
            "unrelated-omitted", include_alias=False)
        (incomplete / "inputs/official_ferc/unrelated.txt").unlink()

        self.assertNotEqual(
            self._snapshot(complete, declaration),
            self._snapshot(incomplete, incomplete_declaration))


if __name__ == "__main__":
    unittest.main()
