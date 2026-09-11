"""Focused controls for the immutable official-recovery cache importer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = ROOT / "tools/import_official_recovery.py"
SPEC = importlib.util.spec_from_file_location("import_official_recovery", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
IMPORTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IMPORTER
SPEC.loader.exec_module(IMPORTER)

from ferclib.http import SourceCache  # noqa: E402


SUCCESS_ACCESSION = "20250102-5121"
FAILED_ACCESSION = "20231229-5212"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity(path: Path) -> dict:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": sha256_bytes(data)}


class RecoveryFixture:
    """Two-accession capture: one complete response and one retained HTTP 500."""

    def __init__(self, root: Path):
        self.root = root
        self.recovery = root / "recovery"
        self.log = root / "log"
        self.results = self.log / "OFFICIAL_FERC_RECOVERY_RESULTS.json"
        self.hash_index = self.log / "OFFICIAL_FERC_RECOVERY_HASHES.json"
        self.files = []
        self.success_ids = ["SUCCESS-PDF", "SUCCESS-TAB"]
        self.failed_ids = ["FAIL-PDF", "FAIL-XLS"]
        self.success_attachment = b"PK\x03\x04synthetic-success-zip"
        self.failed_attachment = b'"DownloadP8File failed while error in calling P8Download dll"'
        self._build()

    def _immutable_file(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o444)
        self.files.append(path)
        return path

    def _response(self, accession: str, kind: str, body: bytes, status: int,
                  media_type: str) -> dict:
        body_path = self._immutable_file(
            self.recovery / f"{kind}_responses/{accession}.body", body)
        headers = json.dumps([{"name": "Content-Type", "value": media_type}],
                             sort_keys=True).encode("utf-8")
        headers_path = self._immutable_file(
            self.recovery / f"{kind}_responses/{accession}.headers.json", headers)
        url = (IMPORTER.OFFICIAL_LIST_PREFIX + accession
               if kind == "list" else IMPORTER.OFFICIAL_DOWNLOAD_URL)
        return {
            "accession": accession,
            "request_kind": kind,
            "attempted_this_run": True,
            "attempt_started_utc": "2026-09-09T00:00:00+00:00",
            "attempt_ended_utc": "2026-09-09T00:00:01+00:00",
            "method": "GET" if kind == "list" else "POST",
            "official_url": url,
            "final_url": url,
            "redirects": [],
            "http_status": status,
            "http_reason": "OK" if status == 200 else "Internal Server Error",
            "network_error": None,
            "body_capture_limitation": None,
            "response_media_type": media_type,
            "content_disposition": None,
            "response_bytes": len(body),
            "response_sha256": sha256_bytes(body),
            "response_sha256_is_complete": True,
            "raw_body_path": str(body_path),
            "raw_headers_path": str(headers_path),
            "credentials_or_cookies_sent": False,
        }

    @staticmethod
    def _list_body(accession: str, ids) -> bytes:
        return json.dumps({
            "DataList": [
                {"ID": attachment_id, "Accession_Number": accession,
                 "Availability_Mode": "P", "MimeType": "application/octet-stream"}
                for attachment_id in ids
            ],
            "ErrorList": [],
        }, sort_keys=True).encode("utf-8")

    def _row(self, accession: str, ids, attachment_body: bytes,
             attachment_status: int) -> dict:
        list_body = self._list_body(accession, ids)
        return {
            "accession_number": accession,
            "inventory_url": IMPORTER.OFFICIAL_LIST_PREFIX + accession,
            "classification_before_recovery": "mandatory executable prerequisite",
            "consumers": ["test consumer"],
            "list_response": self._response(
                accession, "list", list_body, 200, "application/json; charset=utf-8"),
            "parsed_file_list": {
                "json_parsed": True,
                "data_list_count": len(ids),
                "error_list": [],
                "attachment_ids": list(ids),
                "attachment_metadata": [],
                "parse_error": None,
            },
            "attachment_response": self._response(
                accession, "attachment", attachment_body, attachment_status,
                "application/octet-stream" if attachment_status == 200
                else "application/json; charset=utf-8"),
            "attachment_inspection": None,
        }

    def _build(self) -> None:
        self.log.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "official_ferc_recovery_results_v1",
            "evidence_lane": "synthetic focused test",
            "network_policy": "no test network",
            "completed_utc": "2026-09-09T00:00:02+00:00",
            "results": [
                self._row(SUCCESS_ACCESSION, self.success_ids,
                          self.success_attachment, 200),
                self._row(FAILED_ACCESSION, self.failed_ids,
                          self.failed_attachment, 500),
            ],
            "summary": {"accessions": 2, "list_http_200": 2, "attachment_http_200": 1},
        }
        self.results.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        indexed = {str(self.results): identity(self.results)}
        indexed.update({str(path): identity(path) for path in self.files})
        index_doc = {
            "schema": "official_ferc_recovery_hash_index_v1",
            "self_excluded": True,
            "files": indexed,
        }
        self.hash_index.write_text(json.dumps(index_doc, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
        self.results_sha256 = sha256_bytes(self.results.read_bytes())
        self.hash_index_sha256 = sha256_bytes(self.hash_index.read_bytes())

    @property
    def expected_accessions(self):
        return frozenset({SUCCESS_ACCESSION, FAILED_ACCESSION})

    def execute(self, cache: Path, inventory: Path):
        return IMPORTER.execute_import(
            results_path=self.results,
            hash_index_path=self.hash_index,
            recovery_root=self.recovery,
            cache_root=cache,
            run_inventory=inventory,
            expected_results_sha256=self.results_sha256,
            expected_hash_index_sha256=self.hash_index_sha256,
            expected_accessions=self.expected_accessions,
        )


class ImportOfficialRecoveryTests(unittest.TestCase):
    def test_imports_only_200_responses_and_appends_explicit_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = RecoveryFixture(root / "fixture")
            cache_root = root / "cache"
            inventory = root / "runs/imports.jsonl"

            first = fixture.execute(cache_root, inventory)
            self.assertEqual(first["status"], "complete_with_blockers")
            self.assertEqual(first["imported"], 3)  # two list GETs + one successful POST
            self.assertEqual(first["already_present_identical"], 0)
            self.assertEqual(len(first["blockers"]), 1)
            blocker = first["blockers"][0]
            self.assertEqual((blocker["accession"], blocker["response_kind"],
                              blocker["http_status"]),
                             (FAILED_ACCESSION, "attachment", 500))
            self.assertIn("not proof of unavailability", blocker["disposition"])

            cache = SourceCache(cache_root)
            success_list_url = IMPORTER.OFFICIAL_LIST_PREFIX + SUCCESS_ACCESSION
            failed_list_url = IMPORTER.OFFICIAL_LIST_PREFIX + FAILED_ACCESSION
            _payload, _blob, success_post_url = IMPORTER.canonical_download_request(
                SUCCESS_ACCESSION, fixture.success_ids)
            _payload, _blob, failed_post_url = IMPORTER.canonical_download_request(
                FAILED_ACCESSION, fixture.failed_ids)
            self.assertIsNotNone(cache.get(success_list_url))
            self.assertIsNotNone(cache.get(failed_list_url))
            self.assertEqual(cache.get(success_post_url)[0], fixture.success_attachment)
            self.assertIsNone(cache.get(failed_post_url), "HTTP-500 body must not be imported")
            self.assertEqual(len(cache._index), 3)
            for entry in cache._index.values():
                obj = cache._object_path(entry["content_hash"])
                self.assertEqual(stat.S_IMODE(obj.stat().st_mode), 0o444)

            rows = [json.loads(line) for line in inventory.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], first["run_id"])

            second = fixture.execute(cache_root, inventory)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["already_present_identical"], 3)
            rows = [json.loads(line) for line in inventory.read_text().splitlines()]
            self.assertEqual(len(rows), 2, "each run must append, never overwrite")
            self.assertNotEqual(rows[0]["run_id"], rows[1]["run_id"])

    def test_conflicting_url_aborts_before_any_new_import_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = RecoveryFixture(root / "fixture")
            cache_root = root / "cache"
            inventory = root / "imports.jsonl"
            cache = SourceCache(cache_root)
            conflict_url = IMPORTER.OFFICIAL_LIST_PREFIX + SUCCESS_ACCESSION
            cache.put(conflict_url, b"pre-existing-conflicting-bytes",
                      "application/json", IMPORTER.SOURCE_SYSTEM)

            with self.assertRaises(IMPORTER.CacheConflictError):
                fixture.execute(cache_root, inventory)
            after = SourceCache(cache_root)
            self.assertEqual(after.get(conflict_url)[0], b"pre-existing-conflicting-bytes")
            self.assertEqual(len(after._index), 1, "preflight must prevent partial imports")
            rows = [json.loads(line) for line in inventory.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "blocked_cache_conflict")
            self.assertEqual(rows[0]["imported"], 0)
            self.assertEqual(len(rows[0]["conflicts"]), 1)

    def test_result_index_and_body_tampering_each_fail_closed(self):
        cases = ("results", "index", "body")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = RecoveryFixture(root / "fixture")
                original_results_sha = fixture.results_sha256
                original_index_sha = fixture.hash_index_sha256
                if case == "results":
                    fixture.results.write_bytes(fixture.results.read_bytes() + b" ")
                elif case == "index":
                    fixture.hash_index.write_bytes(fixture.hash_index.read_bytes() + b" ")
                else:
                    body_path = Path(json.loads(fixture.results.read_text())["results"][0]
                                     ["list_response"]["raw_body_path"])
                    body_path.chmod(0o644)
                    body_path.write_bytes(body_path.read_bytes() + b"tampered")
                    body_path.chmod(0o444)
                with self.assertRaises(IMPORTER.EvidenceVerificationError):
                    IMPORTER.execute_import(
                        results_path=fixture.results,
                        hash_index_path=fixture.hash_index,
                        recovery_root=fixture.recovery,
                        cache_root=root / "cache",
                        run_inventory=root / "runs.jsonl",
                        expected_results_sha256=original_results_sha,
                        expected_hash_index_sha256=original_index_sha,
                        expected_accessions=fixture.expected_accessions,
                    )
                self.assertFalse((root / "cache").exists(),
                                 "evidence must verify before SourceCache is opened")
                self.assertFalse((root / "runs.jsonl").exists())

    def test_malformed_existing_cache_index_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = RecoveryFixture(root / "fixture")
            cache_root = root / "cache"
            cache_root.mkdir()
            index = cache_root / "index.json"
            original = b'{"truncated":'
            index.write_bytes(original)
            inventory = root / "imports.jsonl"

            with self.assertRaises(IMPORTER.CacheConflictError):
                fixture.execute(cache_root, inventory)
            self.assertEqual(index.read_bytes(), original)
            self.assertFalse((cache_root / "objects").exists())
            rows = [json.loads(line) for line in inventory.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "blocked_cache_conflict")
            self.assertEqual(rows[0]["imported"], 0)

    def test_run_inventory_cannot_collide_with_cache_or_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = RecoveryFixture(root / "fixture")
            cache_root = root / "cache"
            with self.assertRaises(IMPORTER.RecoveryImportError):
                fixture.execute(cache_root, cache_root / "index.json")
            self.assertFalse(cache_root.exists())
            with self.assertRaises(IMPORTER.RecoveryImportError):
                fixture.execute(cache_root, fixture.results)
            self.assertFalse(cache_root.exists())

    def test_download_cache_url_is_the_client_post_json_canonical_key(self):
        ids = ["SUCCESS-PDF", "SUCCESS-TAB"]
        payload, blob, cache_url = IMPORTER.canonical_download_request(SUCCESS_ACCESSION, ids)
        self.assertEqual(payload, {
            "FileType": "", "accession": SUCCESS_ACCESSION, "fileid": 0,
            "FileIDAll": "", "fileidLst": ids, "Islegacy": False,
        })
        self.assertEqual(
            blob,
            b'{"FileIDAll": "", "FileType": "", "Islegacy": false, '
            b'"accession": "20250102-5121", "fileid": 0, '
            b'"fileidLst": ["SUCCESS-PDF", "SUCCESS-TAB"]}',
        )
        self.assertEqual(
            cache_url,
            "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
            "#body=b65b03f8f337a7854f448de1588c413a",
        )

    def test_apply_flag_is_required_before_any_cache_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    IMPORTER.main(["--cache-root", str(cache),
                                   "--run-inventory", str(Path(tmp) / "runs.jsonl")])
            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(cache.exists())

    def test_cli_custom_capture_requires_and_passes_exact_complete_contract(self):
        result_hash = "1" * 64
        index_hash = "2" * 64
        record = {
            "status": "complete", "run_id": "run-one", "imported": 2,
            "already_present_identical": 0, "blockers": [],
            "append_only_inventory": "/tmp/example-inventory.jsonl",
        }
        argv = [
            "--results", "/tmp/results.json", "--hash-index", "/tmp/hashes.json",
            "--recovery-root", "/tmp/recovery", "--cache-root", "/tmp/cache",
            "--run-inventory", "/tmp/runs.jsonl", "--apply",
            "--expected-results-sha256", result_hash,
            "--expected-hash-index-sha256", index_hash,
            "--expected-accession", "20260226-5162",
        ]
        with mock.patch.object(IMPORTER, "execute_import", return_value=record) as execute:
            self.assertEqual(0, IMPORTER.main(argv))
        kwargs = execute.call_args.kwargs
        self.assertEqual(result_hash, kwargs["expected_results_sha256"])
        self.assertEqual(index_hash, kwargs["expected_hash_index_sha256"])
        self.assertEqual(frozenset({"20260226-5162"}), kwargs["expected_accessions"])

        incomplete = argv[:-2]
        with mock.patch.object(IMPORTER, "execute_import") as refused, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            IMPORTER.main(incomplete)
        self.assertEqual(2, raised.exception.code)
        refused.assert_not_called()


if __name__ == "__main__":
    unittest.main()
