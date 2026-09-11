"""Regression controls for the official individual-file recovery route."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import tempfile
import unittest
import warnings
import zipfile

from ferclib.elibrary import (DOWNLOAD_URL, Elibrary,
                             reconcile_filing_content_hash,
                             stable_payload_fingerprint)
from ferclib.http import Client, FetchError, SourceCache, is_offline, set_offline


def archive(names: list[str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, name in enumerate(names):
            zf.writestr(name, f"member-{i}".encode())
    return out.getvalue()


def dated_archive(entries: list[tuple[str, bytes]], date_time: tuple[int, ...],
                  compression=zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in entries:
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.compress_type = compression
            zf.writestr(info, body)
    return out.getvalue()


class ExistingFiling:
    def __init__(self, content_hash: str):
        self.content_hash = content_hash

    def query(self, sql, params=()):
        assert "SELECT content_hash FROM filings" in sql
        assert params == ("eLibrary", "20231229-5212")
        return [{"content_hash": self.content_hash}]


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

    def test_successful_bulk_response_must_also_match_official_roster(self):
        ids = ["A", "B"]
        wrong = archive(["20231229-5212_one.pdf", "20231229-5212_wrong.rtf"])
        right = archive(["20231229-5212_one.pdf", "20231229-5212_two.rtf"])
        expected = [
            {"attachment_id": "A", "file_name": "one.pdf",
             "listed_byte_size": len(b"member-0")},
            {"attachment_id": "B", "file_name": "two.rtf",
             "listed_byte_size": len(b"member-1")},
        ]
        client = FakeClient({tuple(ids): wrong, ("A",): right})
        blob, entry = Elibrary(client).download(
            "20231229-5212", ids, expected_files=expected)
        self.assertEqual(blob, right)
        self.assertEqual(entry["requested_attachment_id"], "A")
        self.assertEqual(client.requests, [("A", "B"), ("A",)])

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


class ImmutableFilingPayloadTests(unittest.TestCase):
    ENTRIES = [("20231229-5212_letter.pdf", b"%PDF filing letter"),
               ("20231229-5212_appendix.xlsx", b"PK spreadsheet payload")]

    def test_wrapper_timestamp_compression_and_order_do_not_create_a_revision(self):
        old = dated_archive(self.ENTRIES, (2026, 9, 9, 13, 23, 0), zipfile.ZIP_STORED)
        fresh = dated_archive(
            list(reversed(self.ENTRIES)), (2026, 9, 11, 5, 30, 0),
            zipfile.ZIP_DEFLATED)
        self.assertNotEqual(hashlib.sha256(old).hexdigest(),
                            hashlib.sha256(fresh).hexdigest())
        self.assertEqual(stable_payload_fingerprint(old),
                         stable_payload_fingerprint(fresh))

        with tempfile.TemporaryDirectory(prefix="ferc-repacked-zip-") as td:
            cache = SourceCache(pathlib.Path(td))
            old_entry = cache.put("old", old, "application/octet-stream", "eLibrary")
            fresh_hash = hashlib.sha256(fresh).hexdigest()
            resolved, equivalent, byte_size = reconcile_filing_content_hash(
                ExistingFiling(old_entry["content_hash"]), cache, "eLibrary",
                "20231229-5212", fresh, fresh_hash)
            self.assertTrue(equivalent)
            self.assertEqual(old_entry["content_hash"], resolved)
            self.assertEqual(len(old), byte_size)

    def test_same_request_repack_keeps_both_raw_objects_in_the_manifest(self):
        old = dated_archive(self.ENTRIES, (2026, 9, 9, 13, 23, 0), zipfile.ZIP_STORED)
        fresh = dated_archive(self.ENTRIES, (2026, 9, 11, 5, 30, 0),
                              zipfile.ZIP_DEFLATED)
        with tempfile.TemporaryDirectory(prefix="ferc-repacked-history-") as td:
            cache = SourceCache(pathlib.Path(td))
            old_entry = cache.put("https://official.example/download#body=request", old,
                                  "application/octet-stream", "eLibrary")
            fresh_entry = cache.put("https://official.example/download#body=request", fresh,
                                    "application/octet-stream", "eLibrary")
            rows = cache.manifest_rows()
            self.assertEqual(
                {old_entry["content_hash"], fresh_entry["content_hash"]},
                {row["content_hash"] for row in rows},
            )
            self.assertTrue(any("historical raw response" in row["note"] for row in rows))
            self.assertTrue(all(
                row["cache_key"] == cache.url_key(row["source_url"]) for row in rows))
            for entry, expected in ((old_entry, old), (fresh_entry, fresh)):
                body = cache.get_content(entry["content_hash"])
                self.assertEqual(expected, body)
                self.assertEqual(len(expected), len(body))

    def test_member_change_or_rename_fails_closed_to_the_fresh_hash(self):
        old = dated_archive(self.ENTRIES, (2026, 9, 9, 13, 23, 0))
        variants = (
            [(self.ENTRIES[0][0], b"%PDF filing letteX"), self.ENTRIES[1]],
            [("20231229-5212_renamed.pdf", self.ENTRIES[0][1]), self.ENTRIES[1]],
        )
        with tempfile.TemporaryDirectory(prefix="ferc-changed-zip-") as td:
            cache = SourceCache(pathlib.Path(td))
            old_entry = cache.put("old", old, "application/octet-stream", "eLibrary")
            for entries in variants:
                fresh = dated_archive(entries, (2026, 9, 11, 5, 30, 0))
                fresh_hash = hashlib.sha256(fresh).hexdigest()
                with self.subTest(entries=entries):
                    resolved, equivalent, byte_size = reconcile_filing_content_hash(
                        ExistingFiling(old_entry["content_hash"]), cache, "eLibrary",
                        "20231229-5212", fresh, fresh_hash)
                    self.assertFalse(equivalent)
                    self.assertEqual(fresh_hash, resolved)
                    self.assertEqual(len(fresh), byte_size)

    def test_missing_or_tampered_prior_object_never_bypasses_the_guard(self):
        old = dated_archive(self.ENTRIES, (2026, 9, 9, 13, 23, 0))
        fresh = dated_archive(self.ENTRIES, (2026, 9, 11, 5, 30, 0))
        fresh_hash = hashlib.sha256(fresh).hexdigest()
        with tempfile.TemporaryDirectory(prefix="ferc-missing-prior-") as td:
            cache = SourceCache(pathlib.Path(td))
            missing_hash = hashlib.sha256(old).hexdigest()
            resolved, equivalent, byte_size = reconcile_filing_content_hash(
                ExistingFiling(missing_hash), cache, "eLibrary", "20231229-5212",
                fresh, fresh_hash)
            self.assertEqual((fresh_hash, False, len(fresh)),
                             (resolved, equivalent, byte_size))

            entry = cache.put("old", old, "application/octet-stream", "eLibrary")
            cache._object_path(entry["content_hash"]).write_bytes(b"tampered")
            resolved, equivalent, byte_size = reconcile_filing_content_hash(
                ExistingFiling(entry["content_hash"]), cache, "eLibrary",
                "20231229-5212", fresh, fresh_hash)
            self.assertEqual((fresh_hash, False, len(fresh)),
                             (resolved, equivalent, byte_size))

    def test_unsafe_or_duplicate_archive_has_no_stable_payload_identity(self):
        unsafe = dated_archive([("../escape.pdf", b"x")],
                               (2026, 9, 11, 5, 30, 0))
        duplicate = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as zf:
                zf.writestr("same.pdf", b"one")
                zf.writestr("same.pdf", b"two")
        self.assertIsNone(stable_payload_fingerprint(unsafe))
        self.assertIsNone(stable_payload_fingerprint(duplicate.getvalue()))

    def test_claimed_fresh_hash_must_match_the_downloaded_bytes(self):
        fresh = dated_archive(self.ENTRIES, (2026, 9, 11, 5, 30, 0))
        with tempfile.TemporaryDirectory(prefix="ferc-hash-invariant-") as td:
            with self.assertRaisesRegex(ValueError, "hash does not match bytes"):
                reconcile_filing_content_hash(
                    ExistingFiling("a" * 64), SourceCache(pathlib.Path(td)),
                    "eLibrary", "20231229-5212", fresh, "b" * 64)


if __name__ == "__main__":
    unittest.main()
