"""Focused tests for the one-shot MountainWest capacity evidence capture."""

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


ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CAPTURE = _load(
    "capture_capacity_20260226_5162",
    ROOT / "implementation_logs/input_recovery/capture_capacity_20260226_5162.py")
IMPORTER = _load(
    "import_official_recovery_capacity_test",
    ROOT / "tools/import_official_recovery.py")


class FakeResponse:
    def __init__(self, body: bytes, media_type: str):
        self._body = body
        self.status = 200
        self.reason = "OK"
        self.headers = {
            "Content-Type": media_type,
            "Content-Length": str(len(body)),
            "X-Ignored-Secret-Like-Header": "must-not-be-recorded",
        }

    def read(self, count: int = -1) -> bytes:
        return self._body if count < 0 else self._body[:count]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, request.data, timeout))
        if not self.responses:
            raise AssertionError("capture made more HTTP attempts than authorised")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _list_bytes(*, file_name=CAPTURE.FILE_NAME) -> bytes:
    return json.dumps({
        "DataList": [{
            "ID": CAPTURE.ATTACHMENT_ID,
            "Accession_Number": CAPTURE.ACCESSION,
            "Availability_Mode": "P",
            "Availability_Code": "Public",
            "Orig_File_Name": file_name,
            "File_Size_Num": CAPTURE.LISTED_FILE_BYTES,
            "MimeType": "application/pdf",
            "Description": "Capacity Report",
        }],
        "ErrorList": [],
    }, sort_keys=True).encode("utf-8")


def _attachment_zip() -> bytes:
    pdf = b"%PDF-1.7\n" + b"x" * (CAPTURE.LISTED_FILE_BYTES - len(b"%PDF-1.7\n"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{CAPTURE.ACCESSION}_{CAPTURE.FILE_NAME}", pdf)
    return output.getvalue()


def _install_pinned_search_evidence(candidate: Path) -> None:
    source_object = (
        ROOT / "source_cache/objects" / CAPTURE.SEARCH_OBJECT_SHA256[:2]
        / CAPTURE.SEARCH_OBJECT_SHA256
    )
    body = source_object.read_bytes()
    assert hashlib.sha256(body).hexdigest() == CAPTURE.SEARCH_OBJECT_SHA256
    target = (
        candidate / "source_cache/objects" / CAPTURE.SEARCH_OBJECT_SHA256[:2]
        / CAPTURE.SEARCH_OBJECT_SHA256
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    index = {
        CAPTURE.SEARCH_CACHE_KEY: {
            "content_hash": CAPTURE.SEARCH_OBJECT_SHA256,
            "byte_size": CAPTURE.SEARCH_OBJECT_BYTES,
            "source_url": CAPTURE.SEARCH_SOURCE_URL,
            "source_system": "eLibrary",
        },
    }
    (candidate / "source_cache/index.json").write_text(
        json.dumps(index), encoding="utf-8")


class Capacity202602265162CaptureTests(unittest.TestCase):
    def test_exact_get_then_one_post_produces_importable_immutable_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _install_pinned_search_evidence(root)
            list_body = _list_bytes()
            attachment_body = _attachment_zip()
            opener = FakeOpener([
                FakeResponse(list_body, "application/json; charset=utf-8"),
                FakeResponse(attachment_body, "application/octet-stream"),
            ])
            result = CAPTURE.capture(root, opener=opener)

            self.assertTrue(result["summary"]["complete_validated_capture"])
            self.assertEqual([call[0] for call in opener.calls], ["GET", "POST"])
            self.assertEqual(opener.calls[0][1], CAPTURE.LIST_URL)
            self.assertEqual(opener.calls[1][1], CAPTURE.DOWNLOAD_URL)
            payload = json.loads(opener.calls[1][2].decode("utf-8"))
            self.assertEqual(payload["fileidLst"], [CAPTURE.ATTACHMENT_ID])
            self.assertEqual(payload["accession"], CAPTURE.ACCESSION)

            layout = CAPTURE.Layout.for_candidate(root)
            for path in (
                layout.list_body, layout.list_headers, layout.attachment_body,
                layout.attachment_headers, layout.get_marker, layout.post_marker,
                layout.results, layout.hashes,
            ):
                self.assertTrue(path.is_file(), path)
                self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o222, path)
            self.assertNotIn(
                "X-Ignored-Secret-Like-Header", layout.list_headers.read_text())

            result_sha = hashlib.sha256(layout.results.read_bytes()).hexdigest()
            hashes_sha = hashlib.sha256(layout.hashes.read_bytes()).hexdigest()
            plan = IMPORTER.load_verified_plan(
                layout.results, layout.hashes, layout.recovery,
                expected_results_sha256=result_sha,
                expected_hash_index_sha256=hashes_sha,
                expected_accessions=frozenset({CAPTURE.ACCESSION}),
            )
            self.assertEqual(len(plan.entries), 2)
            self.assertEqual(plan.blockers, ())

            record = IMPORTER.execute_import(
                results_path=layout.results,
                hash_index_path=layout.hashes,
                recovery_root=layout.recovery,
                cache_root=root / "cache",
                run_inventory=root / "cache_imports.jsonl",
                expected_results_sha256=result_sha,
                expected_hash_index_sha256=hashes_sha,
                expected_accessions=frozenset({CAPTURE.ACCESSION}),
            )
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["imported"], 2)

    def test_list_identity_mismatch_prevents_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            _install_pinned_search_evidence(Path(temporary))
            opener = FakeOpener([
                FakeResponse(_list_bytes(file_name="plausible-but-wrong.pdf"),
                             "application/json"),
            ])
            result = CAPTURE.capture(Path(temporary), opener=opener)
            self.assertEqual([call[0] for call in opener.calls], ["GET"])
            self.assertFalse(result["summary"]["complete_validated_capture"])
            self.assertIn("file name", result["summary"]["list_validation_error"])
            self.assertIsNone(result["results"][0]["attachment_response"])

    def test_network_error_is_one_attempt_and_never_promotes_to_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            _install_pinned_search_evidence(Path(temporary))
            opener = FakeOpener([OSError("synthetic offline network denial")])
            result = CAPTURE.capture(Path(temporary), opener=opener)
            self.assertEqual([call[0] for call in opener.calls], ["GET"])
            self.assertFalse(result["summary"]["complete_validated_capture"])
            response = result["results"][0]["list_response"]
            self.assertEqual(response["network_error"]["type"], "OSError")
            self.assertIsNone(response["raw_body_path"])

    def test_existing_attempt_marker_refuses_all_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = CAPTURE.Layout.for_candidate(root)
            layout.get_marker.parent.mkdir(parents=True)
            layout.get_marker.write_text("attempt already began", encoding="utf-8")
            opener = FakeOpener([])
            with self.assertRaisesRegex(FileExistsError, "refusing every repeated request"):
                CAPTURE.capture(root, opener=opener)
            self.assertEqual(opener.calls, [])

    def test_malformed_attachment_is_retained_but_not_marked_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            _install_pinned_search_evidence(Path(temporary))
            opener = FakeOpener([
                FakeResponse(_list_bytes(), "application/json"),
                FakeResponse(b'{"error":"not a filing"}', "application/json"),
            ])
            result = CAPTURE.capture(Path(temporary), opener=opener)
            self.assertEqual([call[0] for call in opener.calls], ["GET", "POST"])
            self.assertFalse(result["summary"]["complete_validated_capture"])
            self.assertIn("neither PDF nor ZIP",
                          result["summary"]["attachment_validation_error"])
            layout = CAPTURE.Layout.for_candidate(Path(temporary))
            self.assertEqual(layout.attachment_body.read_bytes(),
                             b'{"error":"not a filing"}')

    def test_missing_search_anchor_fails_before_request_or_attempt_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opener = FakeOpener([])
            with self.assertRaisesRegex(ValueError, "source-cache index"):
                CAPTURE.capture(root, opener=opener)
            self.assertEqual(opener.calls, [])
            self.assertFalse(CAPTURE.Layout.for_candidate(root).get_marker.exists())


if __name__ == "__main__":
    unittest.main()
