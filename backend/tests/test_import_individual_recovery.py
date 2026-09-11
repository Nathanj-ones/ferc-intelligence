"""Focused controls for the pinned one-ID complete-accession importer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools/import_individual_recovery.py"
SPEC = importlib.util.spec_from_file_location("import_individual_recovery", TOOL)
assert SPEC is not None and SPEC.loader is not None
IMPORTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IMPORTER
SPEC.loader.exec_module(IMPORTER)

from ferclib.http import SourceCache  # noqa: E402


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.recovery = root / "recovery"
        self.ledger = root / "ledger.json"
        self.rows = []
        self.list_rows = []
        for index in range(1, 11):
            attachment_id = f"ID-{index:02d}"
            name = f"file-{index:02d}.dat"
            content = bytes([index]) * index
            self.list_rows.append({
                "ID": attachment_id,
                "Accession_Number": IMPORTER.ACCESSION,
                "Orig_File_Name": name,
                "File_Size_Num": len(content),
                "MimeType": "application/octet-stream",
                "Availability_Mode": "P",
            })
        self.list_body = json.dumps({"DataList": self.list_rows, "ErrorList": []},
                                    sort_keys=True).encode()
        self.list_path = self._immutable("list.body", self.list_body)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, listed in enumerate(self.list_rows, start=1):
                archive.writestr(
                    f"{IMPORTER.ACCESSION}_{listed['Orig_File_Name']}",
                    bytes([index]) * index)
        self.zip_body = out.getvalue()
        for index, listed in enumerate(self.list_rows, start=1):
            body = self.zip_body if index == 7 else b'{"error":"retained"}'
            body_path = self._immutable(f"individual/{index:02d}.body", body)
            _payload, canonical, cache_url = IMPORTER.canonical_download_request(
                IMPORTER.ACCESSION, [listed["ID"]])
            self.rows.append({
                "schema": "official_ferc_individual_response_v1",
                "accession_number": IMPORTER.ACCESSION,
                "attachment_id": listed["ID"],
                "attachment_order": index,
                "availability_mode": "P",
                "file_name": listed["Orig_File_Name"],
                "listed_bytes": listed["File_Size_Num"],
                "request": {
                    "method": "POST",
                    "official_url": "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File",
                    "cache_url": cache_url,
                    "payload_bytes": len(canonical),
                    "payload_sha256": sha(canonical),
                    "credentials_or_cookies_sent": False,
                    "retry_count": 0,
                },
                "response": {
                    "body_path": str(body_path),
                    "bytes": len(body),
                    "sha256": sha(body),
                    "complete_body": True,
                    "network_error": None,
                    "http_status": 200 if index == 7 else 500,
                },
            })
        self._write_ledger()

    def _immutable(self, relative: str, body: bytes) -> Path:
        path = self.recovery / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o444)
        return path

    def _write_ledger(self):
        doc = {
            "schema": "official_ferc_individual_recovery_v1",
            "accession_number": IMPORTER.ACCESSION,
            "network_policy": "one request per public attachment; no retries/auth/cookies/bypass",
            "list_response": {"path": str(self.list_path), "bytes": len(self.list_body),
                              "sha256": sha(self.list_body)},
            "results": self.rows,
        }
        self.ledger.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        self.ledger_sha = sha(self.ledger.read_bytes())

    def execute(self, cache: Path, inventory: Path):
        with mock.patch.object(IMPORTER, "PINNED_COMPLETE_ZIP_BYTES", len(self.zip_body)), \
                mock.patch.object(IMPORTER, "PINNED_COMPLETE_ZIP_SHA256", sha(self.zip_body)):
            return IMPORTER.execute_import(
                self.ledger, self.recovery, cache, inventory,
                expected_ledger_sha256=self.ledger_sha)


class IndividualRecoveryImportTests(unittest.TestCase):
    def test_imports_only_verified_success_and_retains_failure_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp) / "fixture")
            cache_root = Path(tmp) / "cache"
            record = fixture.execute(cache_root, Path(tmp) / "runs.jsonl")
            self.assertEqual(record["status"], "complete")
            self.assertEqual((record["imported"], record["already_present_identical"]), (1, 0))
            self.assertEqual(len(record["verification"]["members"]), 10)
            self.assertEqual(len(record["verification"]["failed_observations"]), 9)
            cache = SourceCache(cache_root)
            self.assertEqual(len(cache._index), 1)
            success_url = fixture.rows[6]["request"]["cache_url"]
            self.assertEqual(cache.get(success_url)[0], fixture.zip_body)
            obj = cache._object_path(sha(fixture.zip_body))
            self.assertFalse(stat.S_IMODE(obj.stat().st_mode) & 0o222)

    def test_member_mismatch_fails_before_cache_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp) / "fixture")
            fixture.list_rows[0]["Orig_File_Name"] = "wrong.dat"
            fixture.list_body = json.dumps({"DataList": fixture.list_rows, "ErrorList": []},
                                           sort_keys=True).encode()
            fixture.list_path.chmod(0o644)
            fixture.list_path.write_bytes(fixture.list_body)
            fixture.list_path.chmod(0o444)
            fixture._write_ledger()
            cache_root = Path(tmp) / "cache"
            with mock.patch.object(IMPORTER, "PINNED_COMPLETE_ZIP_BYTES", len(fixture.zip_body)), \
                    mock.patch.object(IMPORTER, "PINNED_COMPLETE_ZIP_SHA256", sha(fixture.zip_body)):
                with self.assertRaises(IMPORTER.EvidenceVerificationError):
                    IMPORTER.execute_import(
                        fixture.ledger, fixture.recovery, cache_root, Path(tmp) / "runs.jsonl",
                        expected_ledger_sha256=fixture.ledger_sha)
            self.assertFalse((cache_root / "index.json").exists())

    def test_request_identity_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp) / "fixture")
            fixture.rows[6]["request"]["cache_url"] += "-tampered"
            fixture._write_ledger()
            with mock.patch.object(IMPORTER, "PINNED_COMPLETE_ZIP_BYTES", len(fixture.zip_body)), \
                    mock.patch.object(IMPORTER, "PINNED_COMPLETE_ZIP_SHA256", sha(fixture.zip_body)):
                with self.assertRaisesRegex(IMPORTER.EvidenceVerificationError,
                                            "request identity mismatch"):
                    IMPORTER.load_verified_plan(
                        fixture.ledger, fixture.recovery, fixture.ledger_sha)

    def test_existing_url_conflict_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp) / "fixture")
            cache_root = Path(tmp) / "cache"
            cache = SourceCache(cache_root)
            success_url = fixture.rows[6]["request"]["cache_url"]
            old = cache.put(success_url, b"different", "application/octet-stream", "eLibrary")
            with self.assertRaises(IMPORTER.CacheConflictError):
                fixture.execute(cache_root, Path(tmp) / "runs.jsonl")
            reopened = SourceCache(cache_root)
            self.assertEqual(reopened.get(success_url)[0], b"different")
            self.assertEqual(reopened.get(success_url)[1]["content_hash"], old["content_hash"])


if __name__ == "__main__":
    unittest.main()
