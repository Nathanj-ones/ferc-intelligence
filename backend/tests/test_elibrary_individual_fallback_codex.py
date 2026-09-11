"""Regression controls for the official individual-file recovery route."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import tempfile
import unittest
import zipfile

from ferclib.elibrary import DOWNLOAD_URL, Elibrary
from ferclib.http import Client, FetchError, SourceCache, is_offline, set_offline


def archive(names: list[str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, name in enumerate(names):
            zf.writestr(name, f"member-{i}".encode())
    return out.getvalue()


class FakeClient:
    def __init__(self, responses=None, listing=None):
        self.responses = responses or {}
        self.listing = listing
        self.requests = []

    def post_json(self, url, payload, **kwargs):
        ids = tuple(payload["fileidLst"])
        self.requests.append(ids)
        value = self.responses.get(ids)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError(url, f"uncaptured {ids}")
        return value, {"content_hash": "a" * 64, "media_type": "application/octet-stream",
                       "byte_size": len(value), "source_url": url}

    def get(self, url, **kwargs):
        body = json.dumps(self.listing).encode()
        return body, {"content_hash": "b" * 64}


class IndividualDownloadFallbackTests(unittest.TestCase):
    def test_valid_full_request_remains_the_primary_route(self):
        ids = ["A", "B"]
        full = archive(["one.pdf", "two.rtf"])
        client = FakeClient({tuple(ids): full})
        blob, entry = Elibrary(client).download("20231229-5212", ids)
        self.assertEqual(blob, full)
        self.assertEqual(client.requests, [("A", "B")])
        self.assertNotIn("download_strategy", entry)

    def test_failed_bulk_uses_distinct_single_id_until_complete_zip_resolves(self):
        ids = ["A", "B", "C"]
        err = FetchError("official", "HTTP 500", status=500, attempts=1)
        complete = archive(["accession_a.pdf", "accession_b.xlsx", "accession_c.rtf"])
        client = FakeClient({tuple(ids): err, ("A",): err, ("B",): complete})
        blob, entry = Elibrary(client).download("20231229-5212", ids)
        self.assertEqual(blob, complete)
        self.assertEqual(client.requests, [("A", "B", "C"), ("A",), ("B",)])
        self.assertEqual(entry["requested_attachment_id"], "B")
        self.assertEqual(entry["declared_attachment_count"], 3)
        self.assertIn("single-ID", entry["download_strategy"])

    def test_partial_single_file_response_does_not_masquerade_as_full_package(self):
        ids = ["A", "B"]
        err = FetchError("official", "HTTP 500", status=500, attempts=1)
        partial = archive(["only-one.pdf"])
        client = FakeClient({tuple(ids): err, ("A",): b"%PDF raw", ("B",): partial})
        with self.assertRaises(FetchError) as caught:
            Elibrary(client).download("20231229-5212", ids)
        self.assertIn("no individual-ID response supplied a complete 2-attachment archive",
                      caught.exception.detail)
        self.assertIn("not a complete ZIP", caught.exception.detail)
        self.assertEqual(client.requests, [("A", "B"), ("A",), ("B",)])

    def test_fallback_must_match_official_names_and_sizes_when_available(self):
        ids = ["A", "B"]
        err = FetchError("official", "HTTP 500", status=500, attempts=1)
        wrong = archive(["20231229-5212_one.pdf", "20231229-5212_wrong.rtf"])
        right = archive(["20231229-5212_one.pdf", "20231229-5212_two.rtf"])
        expected = [
            {"attachment_id": "A", "file_name": "one.pdf",
             "listed_byte_size": len(b"member-0")},
            {"attachment_id": "B", "file_name": "two.rtf",
             "listed_byte_size": len(b"member-1")},
        ]
        client = FakeClient({tuple(ids): err, ("A",): wrong, ("B",): right})
        blob, entry = Elibrary(client).download(
            "20231229-5212", ids, expected_files=expected)
        self.assertEqual(blob, right)
        self.assertEqual(entry["requested_attachment_id"], "B")
        self.assertEqual(client.requests, [("A", "B"), ("A",), ("B",)])

    def test_unsafe_complete_archive_is_rejected_while_later_valid_control_passes(self):
        ids = ["A", "B"]
        err = FetchError("official", "HTTP 500", status=500, attempts=1)
        unsafe = archive(["../escape.pdf", "ok.rtf"])
        valid = archive(["safe.pdf", "safe.rtf"])
        client = FakeClient({tuple(ids): err, ("A",): unsafe, ("B",): valid})
        blob, entry = Elibrary(client).download("20231229-5212", ids)
        self.assertEqual(blob, valid)
        self.assertEqual(entry["requested_attachment_id"], "B")
        self.assertEqual(client.requests, [("A", "B"), ("A",), ("B",)])

    def test_file_list_retains_current_per_attachment_metadata(self):
        listing = {"DataList": [{
            "ID": "A", "Availability_Mode": "P", "MimeType": "application/pdf",
            "Accession_Number": "20231229-5212", "Description": "filing description",
            "Orig_File_Name": "Transmittal Letter.pdf",
            "FileDescription": "Transmittal Letter", "File_Size_Num": 366636,
        }], "ErrorList": []}
        rows = Elibrary(FakeClient(listing=listing)).file_list("20231229-5212")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_name"], "Transmittal Letter.pdf")
        self.assertEqual(rows[0]["file_description"], "Transmittal Letter")
        self.assertEqual(rows[0]["listed_byte_size"], 366636)
        self.assertEqual(rows[0]["filing_description"], "filing description")

    def test_offline_complete_zip_satisfies_only_its_bounded_fallback_probes(self):
        ids = ["A", "B", "C"]
        complete = archive(["one.pdf", "two.xlsx", "three.rtf"])
        expected = [
            {"attachment_id": value, "file_name": name,
             "listed_byte_size": len(f"member-{index}".encode())}
            for index, (value, name) in enumerate(zip(
                ids, ["one.pdf", "two.xlsx", "three.rtf"]))
        ]
        prior_offline = is_offline()
        self.addCleanup(set_offline, prior_offline)
        with tempfile.TemporaryDirectory(prefix="ferc-elibrary-offline-fallback-") as td:
            cache = SourceCache(pathlib.Path(td))
            client = Client(cache, offline=True)
            payload = {"FileType": "", "accession": "20231229-5212", "fileid": 0,
                       "FileIDAll": "", "fileidLst": ["B"], "Islegacy": False}
            request = json.dumps(payload, sort_keys=True).encode("utf-8")
            success_url = f"{DOWNLOAD_URL}#body={hashlib.sha256(request).hexdigest()[:32]}"
            cache.put(success_url, complete, "application/octet-stream", "eLibrary")

            blob, entry = Elibrary(client).download(
                "20231229-5212", ids, use_cache=True, expected_files=expected)

            self.assertEqual(complete, blob)
            self.assertEqual("B", entry["requested_attachment_id"])
            self.assertEqual([], client.cache_misses)
            self.assertEqual(2, len(client.satisfied_cache_misses))
            self.assertTrue(all(
                row["satisfied_by"]["content_hash"] == entry["content_hash"]
                for row in client.satisfied_cache_misses))

    def test_failed_offline_fallback_leaves_every_probe_unresolved(self):
        prior_offline = is_offline()
        self.addCleanup(set_offline, prior_offline)
        with tempfile.TemporaryDirectory(prefix="ferc-elibrary-offline-miss-") as td:
            client = Client(SourceCache(pathlib.Path(td)), offline=True)
            with self.assertRaises(FetchError):
                Elibrary(client).download("20231229-5212", ["A", "B"])
            self.assertEqual(3, len(client.cache_misses))
            self.assertEqual([], client.satisfied_cache_misses)


if __name__ == "__main__":
    unittest.main()
