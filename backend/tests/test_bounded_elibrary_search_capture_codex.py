"""Paired controls for the bounded official eLibrary search importer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    path = ROOT / "implementation/import_bounded_elibrary_search_capture.py"
    spec = importlib.util.spec_from_file_location("bounded_elibrary_import_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


IMPORTER = _load_module()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return encoded


def _cache_index_bytes(value: object) -> bytes:
    return json.dumps(value, indent=0, sort_keys=True).encode("utf-8")


def _make_bundle(base: Path, dockets=None):
    recovery = base / "recovery"
    source_cache = recovery / "source_cache"
    dockets = list(dockets or sorted(IMPORTER.EXPECTED_DOCKETS))
    rows = []
    source_index = {}
    for position, docket in enumerate(dockets, 1):
        payload = IMPORTER._expected_payload(docket)
        payload_blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        payload_hash = _sha(payload_blob)
        cache_url = "%s#body=%s" % (IMPORTER.OFFICIAL_SEARCH_URL,
                                     payload_hash[:32])
        accession = "2026090%d-%04d" % (position, 5000 + position)
        response = {
            "searchHits": [{
                "acesssionNumber": accession,
                "docketNumbers": [docket + "-000"],
                "description": "synthetic official-response fixture",
            }],
            "totalHits": 1,
            "numHits": 1,
            "success": True,
            "errorMessage": None,
            "searchResultId": None,
        }
        body = json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8")
        content_hash = _sha(body)
        cache_path = "objects/%s/%s" % (content_hash[:2], content_hash)
        object_path = source_cache / cache_path
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(body)
        key = _sha(cache_url.encode("utf-8"))
        source_index[key] = {
            "byte_size": len(body),
            "cache_path": cache_path,
            "content_hash": content_hash,
            "fetch_count": 1,
            "first_seen_at": "2026-09-09T22:00:00+00:00",
            "last_seen_at": "2026-09-09T22:00:00+00:00",
            "media_type": "application/json",
            "source_system": "eLibrary",
            "source_url": cache_url,
        }
        rows.append({
            "accessions": [accession],
            "cache_url": cache_url,
            "docket": docket,
            "media_type": "application/json",
            "method": "POST",
            "num_hits": 1,
            "official_url": IMPORTER.OFFICIAL_SEARCH_URL,
            "request_payload": payload,
            "request_payload_bytes": len(payload_blob),
            "request_payload_sha256": payload_hash,
            "response_bytes": len(body),
            "response_sha256": content_hash,
            "success": True,
            "total_hits": 1,
        })
    capture = {
        "network_policy": "synthetic bounded fixture; no network",
        "requests_made": len(rows),
        "retrieved_utc": "2026-09-09T22:00:01+00:00",
        "rows": rows,
        "schema": IMPORTER.CAPTURE_SCHEMA,
    }
    capture_path = recovery / "CAPTURE.json"
    capture_bytes = _write_json(capture_path, capture)
    index_path = source_cache / "index.json"
    index_bytes = _cache_index_bytes(source_index)
    index_path.write_bytes(index_bytes)
    return {
        "recovery": recovery,
        "source_cache": source_cache,
        "capture_path": capture_path,
        "index_path": index_path,
        "capture_sha": _sha(capture_bytes),
        "index_sha": _sha(index_bytes),
        "capture": capture,
        "source_index": source_index,
    }


def _execute(bundle, target: Path, inventory: Path):
    return IMPORTER.execute_import(
        capture_path=bundle["capture_path"],
        source_index_path=bundle["index_path"],
        source_cache_root=bundle["source_cache"],
        recovery_root=bundle["recovery"],
        target_cache_root=target,
        run_inventory=inventory,
        expected_capture_sha256=bundle["capture_sha"],
        expected_source_index_sha256=bundle["index_sha"],
    )


class BoundedELibrarySearchCaptureTests(unittest.TestCase):
    def test_valid_import_preserves_unrelated_entry_and_all_response_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            target = base / "target"
            target.mkdir()
            unrelated_body = b'{"kept":true}'
            unrelated_hash = _sha(unrelated_body)
            unrelated_url = "https://example.invalid/already-captured"
            unrelated_key = _sha(unrelated_url.encode("utf-8"))
            unrelated_path = target / "objects" / unrelated_hash[:2] / unrelated_hash
            unrelated_path.parent.mkdir(parents=True)
            unrelated_path.write_bytes(unrelated_body)
            unrelated_entry = {
                "byte_size": len(unrelated_body),
                "cache_path": "objects/%s/%s" % (unrelated_hash[:2], unrelated_hash),
                "content_hash": unrelated_hash,
                "fetch_count": 7,
                "first_seen_at": "2020-01-01T00:00:00+00:00",
                "last_seen_at": "2020-01-02T00:00:00+00:00",
                "media_type": "application/json",
                "source_system": "test",
                "source_url": unrelated_url,
            }
            (target / "index.json").write_bytes(
                _cache_index_bytes({unrelated_key: unrelated_entry}))
            inventory = base / "runs.jsonl"

            record = _execute(bundle, target, inventory)
            self.assertEqual("complete", record["status"])
            self.assertEqual(3, record["imported"])
            self.assertEqual(0, record["already_present_identical"])
            target_index = json.loads((target / "index.json").read_text())
            self.assertEqual(unrelated_entry, target_index[unrelated_key])
            self.assertEqual(4, len(target_index))
            for row in record["entries"]:
                self.assertEqual(200, row["http_status"])
                self.assertTrue(row["success"])
                self.assertEqual("application/json", row["media_type"])
                self.assertEqual("imported", row["action"])
                path = target / target_index[row["cache_key"]]["cache_path"]
                self.assertEqual(row["content_hash"], _sha(path.read_bytes()))
            lines = inventory.read_text().splitlines()
            self.assertEqual(1, len(lines))
            self.assertEqual("complete", json.loads(lines[0])["status"])

    def test_object_tamper_is_rejected_before_target_creation_and_is_inventoried(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            first = next(iter(bundle["source_index"].values()))
            object_path = bundle["source_cache"] / first["cache_path"]
            object_path.write_bytes(object_path.read_bytes() + b"tamper")
            target = base / "target"
            inventory = base / "runs.jsonl"
            with self.assertRaisesRegex(IMPORTER.EvidenceVerificationError,
                                        "response object identity mismatch"):
                _execute(bundle, target, inventory)
            self.assertFalse(target.exists())
            row = json.loads(inventory.read_text().splitlines()[0])
            self.assertEqual("failed_evidence_verification", row["status"])
            self.assertEqual(0, row["imported"])

    def test_capture_hash_tamper_is_rejected_even_when_json_remains_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            document = json.loads(bundle["capture_path"].read_text())
            document["network_policy"] = "silently changed"
            _write_json(bundle["capture_path"], document)
            with self.assertRaisesRegex(IMPORTER.EvidenceVerificationError,
                                        "capture ledger SHA-256 mismatch"):
                _execute(bundle, base / "target", base / "runs.jsonl")

    def test_semantic_docket_tamper_is_rejected_with_newly_pinned_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base, dockets=["IS26-587", "RP26-1091", "RP26-9999"])
            with self.assertRaisesRegex(IMPORTER.EvidenceVerificationError,
                                        "docket population"):
                _execute(bundle, base / "target", base / "runs.jsonl")

    def test_conflicting_url_preflights_before_any_verified_object_is_imported(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            target = base / "target"
            target.mkdir()
            desired_key, desired_entry = next(iter(bundle["source_index"].items()))
            conflicting_body = b'{"success":true,"different":true}'
            conflicting_hash = _sha(conflicting_body)
            conflicting_rel = "objects/%s/%s" % (conflicting_hash[:2], conflicting_hash)
            conflicting_path = target / conflicting_rel
            conflicting_path.parent.mkdir(parents=True)
            conflicting_path.write_bytes(conflicting_body)
            conflict_entry = dict(desired_entry)
            conflict_entry.update({
                "content_hash": conflicting_hash,
                "byte_size": len(conflicting_body),
                "cache_path": conflicting_rel,
            })
            initial_index = _cache_index_bytes({desired_key: conflict_entry})
            (target / "index.json").write_bytes(initial_index)
            inventory = base / "runs.jsonl"

            with self.assertRaises(IMPORTER.CacheConflictError):
                _execute(bundle, target, inventory)
            self.assertEqual(initial_index, (target / "index.json").read_bytes())
            imported_hashes = {entry["content_hash"] for entry in bundle["source_index"].values()}
            resident = {path.name for path in (target / "objects").glob("*/*")}
            self.assertTrue(imported_hashes.isdisjoint(resident))
            row = json.loads(inventory.read_text().splitlines()[0])
            self.assertEqual("blocked_cache_conflict", row["status"])
            self.assertEqual(0, row["imported"])

    def test_corrupt_unrelated_target_object_blocks_complete_cache_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            target = base / "target"
            target.mkdir()
            expected_body = b"expected"
            digest = _sha(expected_body)
            url = "https://example.invalid/existing"
            key = _sha(url.encode("utf-8"))
            path = target / "objects" / digest[:2] / digest
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt")
            entry = {
                "byte_size": len(expected_body),
                "cache_path": "objects/%s/%s" % (digest[:2], digest),
                "content_hash": digest,
                "fetch_count": 1,
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "last_seen_at": "2026-09-09T00:00:00+00:00",
                "media_type": "application/json",
                "source_system": "test",
                "source_url": url,
            }
            original_index = _cache_index_bytes({key: entry})
            (target / "index.json").write_bytes(original_index)
            with self.assertRaisesRegex(IMPORTER.CacheConflictError, "conflicting"):
                _execute(bundle, target, base / "runs.jsonl")
            self.assertEqual(original_index, (target / "index.json").read_bytes())

    def test_source_object_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            first = next(iter(bundle["source_index"].values()))
            object_path = bundle["source_cache"] / first["cache_path"]
            retained = base / "retained-object.json"
            retained.write_bytes(object_path.read_bytes())
            object_path.unlink()
            object_path.symlink_to(retained)
            with self.assertRaisesRegex(IMPORTER.EvidenceVerificationError, "symlink"):
                _execute(bundle, base / "target", base / "runs.jsonl")

    def test_second_run_is_cache_idempotent_and_inventory_is_append_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = _make_bundle(base)
            target = base / "target"
            inventory = base / "runs.jsonl"
            first = _execute(bundle, target, inventory)
            index_after_first = (target / "index.json").read_bytes()
            object_modes = {path: path.stat().st_mode
                            for path in (target / "objects").glob("*/*")}
            second = _execute(bundle, target, inventory)
            self.assertEqual(3, first["imported"])
            self.assertEqual(0, second["imported"])
            self.assertEqual(3, second["already_present_identical"])
            self.assertEqual(index_after_first, (target / "index.json").read_bytes())
            self.assertEqual(object_modes, {path: path.stat().st_mode
                                            for path in (target / "objects").glob("*/*")})
            lines = [json.loads(line) for line in inventory.read_text().splitlines()]
            self.assertEqual(2, len(lines))
            self.assertNotEqual(lines[0]["run_id"], lines[1]["run_id"])
            self.assertEqual([3, 0], [line["imported"] for line in lines])
            self.assertEqual([0, 3],
                             [line["already_present_identical"] for line in lines])


if __name__ == "__main__":
    unittest.main()
