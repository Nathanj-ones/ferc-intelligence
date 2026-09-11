"""Regression controls for deterministic retrieval/discovery timestamps.

These fixtures are deliberately synthetic.  They prove that an offline replay
uses the capture times persisted in the immutable cache, and that repeated
sightings of one source occurrence converge without borrowing timestamps from
another occurrence or from the replay wall clock.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters import elibrary_docs as docs  # noqa: E402
from adapters import lng  # noqa: E402
from ferclib import elibrary  # noqa: E402
from ferclib.staging import Staging  # noqa: E402


ACCESSION = "20260909-5099"
OTHER_ACCESSION = "20260909-5100"
SOURCE = "eLibrary"
ENTITY = "CID-R50-TIMESTAMP-FIXTURE"
FIRST_EARLY = "2026-09-09T01:02:03.123456+00:00"
FIRST_LATE = "2026-09-09T02:03:04.234567+00:00"
RETRIEVED_EARLY = "2026-09-09T03:04:05.345678+00:00"
RETRIEVED_LATE = "2026-09-10T04:05:06.456789+00:00"
EVENT_ENVELOPE_TIME = "2026-09-10T05:06:07+00:00"


def _search_payload(accession: str = ACCESSION) -> bytes:
    return json.dumps({
        "success": True,
        "searchHits": [{
            "acesssionNumber": accession,
            "description": "Synthetic R50 timestamp fixture",
            "filedDate": "09/09/2026",
            "availCode": "P",
            # Response-body lookalikes are not cache capture metadata.
            "first_seen_at": "1900-01-01T00:00:00+00:00",
            "retrieved_at": "1900-01-02T00:00:00+00:00",
        }],
    }).encode("utf-8")


def _search_page(accessions: list[str]) -> bytes:
    return json.dumps({
        "success": True,
        "searchHits": [{
            "acesssionNumber": accession,
            "description": f"Synthetic paged occurrence {accession}",
            "filedDate": "09/09/2026",
            "availCode": "P",
        } for accession in accessions],
    }).encode("utf-8")


class _FakeClient:
    def __init__(self, entry: dict):
        self.entry = dict(entry)
        self.calls = []

    def post_json(self, url, body, *, source_system, use_cache):
        self.calls.append((url, body, source_system, use_cache))
        return _search_payload(), dict(self.entry)


class _PagedFakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def post_json(self, url, body, *, source_system, use_cache):
        self.calls.append((url, body, source_system, use_cache))
        raw, entry = self.pages[body["curPage"]]
        return raw, dict(entry)


def _occurrence(*, first_seen_at, retrieved_at, marker: str) -> dict:
    return {
        "accession": ACCESSION,
        "avail_code": "P",
        "filed_date": "2026-09-09",
        "description": "Synthetic R50 duplicate occurrence",
        "first_seen_at": first_seen_at,
        "retrieved_at": retrieved_at,
        "marker": marker,
    }


class CachedSearchCaptureTimeTests(unittest.TestCase):
    def test_cached_search_propagates_both_index_times_exactly(self):
        client = _FakeClient({
            "first_seen_at": FIRST_EARLY,
            "last_seen_at": RETRIEVED_LATE,
        })

        hits = elibrary.Elibrary(client).search(
            docket="RP26-999", per_page=100, max_pages=1, use_cache=True)

        self.assertEqual(1, len(hits))
        self.assertEqual(FIRST_EARLY, hits[0]["first_seen_at"])
        self.assertEqual(RETRIEVED_LATE, hits[0]["retrieved_at"])
        self.assertEqual(1, len(client.calls))
        self.assertTrue(client.calls[0][3])

    def test_missing_index_times_stay_unknown_instead_of_using_replay_time(self):
        client = _FakeClient({})

        hit = elibrary.Elibrary(client).search(
            docket="RP26-999", per_page=100, max_pages=1, use_cache=True)[0]

        self.assertIsNone(hit["first_seen_at"])
        self.assertIsNone(hit["retrieved_at"])
        self.assertNotEqual("1900-01-01T00:00:00+00:00", hit["first_seen_at"])
        self.assertNotEqual("1900-01-02T00:00:00+00:00", hit["retrieved_at"])

    def test_paged_duplicate_accession_merges_cache_bounds_in_either_order(self):
        early_entry = {
            "first_seen_at": FIRST_EARLY,
            "last_seen_at": RETRIEVED_EARLY,
        }
        late_entry = {
            "first_seen_at": FIRST_LATE,
            "last_seen_at": RETRIEVED_LATE,
        }
        for first_entry, second_entry in ((early_entry, late_entry),
                                          (late_entry, early_entry)):
            with self.subTest(first_page_first_seen=first_entry["first_seen_at"]):
                client = _PagedFakeClient([
                    (_search_page([ACCESSION, "20260909-5101"]), first_entry),
                    (_search_page([ACCESSION, "20260909-5102"]), second_entry),
                ])
                hits = elibrary.Elibrary(client).search(
                    docket="RP26-999", per_page=2, max_pages=2, use_cache=True)
                repeated = next(hit for hit in hits if hit["accession"] == ACCESSION)
                self.assertEqual(FIRST_EARLY, repeated["first_seen_at"])
                self.assertEqual(RETRIEVED_LATE, repeated["retrieved_at"])
                self.assertEqual(2, len(client.calls))


class DuplicateOccurrenceCaptureTimeTests(unittest.TestCase):
    def test_forward_and_reverse_duplicates_converge_on_capture_extremes(self):
        early = _occurrence(
            first_seen_at=FIRST_EARLY,
            retrieved_at=RETRIEVED_EARLY,
            marker="early-left-fields",
        )
        late = _occurrence(
            first_seen_at=FIRST_LATE,
            retrieved_at=RETRIEVED_LATE,
            marker="late-left-fields",
        )
        snapshots = (dict(early), dict(late))

        for left, right in ((early, late), (late, early)):
            with self.subTest(left=left["marker"]):
                merged = elibrary._merge_occurrence_capture_times(left, right)
                self.assertIsNot(left, merged)
                self.assertEqual(FIRST_EARLY, merged["first_seen_at"])
                self.assertEqual(RETRIEVED_LATE, merged["retrieved_at"])
                self.assertEqual(left["marker"], merged["marker"])

                canonical, twins = elibrary.dedupe_availability([left, right])
                self.assertEqual([], twins)
                self.assertEqual(1, len(canonical))
                self.assertEqual(FIRST_EARLY, canonical[0]["first_seen_at"])
                self.assertEqual(RETRIEVED_LATE, canonical[0]["retrieved_at"])

        self.assertEqual(snapshots, (early, late), "merging must not mutate either input")

    def test_blank_duplicate_metadata_cannot_erase_a_known_capture_time(self):
        known = _occurrence(
            first_seen_at=FIRST_EARLY,
            retrieved_at=RETRIEVED_LATE,
            marker="known",
        )
        unknown = _occurrence(
            first_seen_at=None,
            retrieved_at="",
            marker="unknown",
        )

        for left, right in ((known, unknown), (unknown, known)):
            with self.subTest(left=left["marker"]):
                merged = elibrary._merge_occurrence_capture_times(left, right)
                self.assertEqual(FIRST_EARLY, merged["first_seen_at"])
                self.assertEqual(RETRIEVED_LATE, merged["retrieved_at"])

    def test_availability_twins_keep_their_distinct_capture_windows(self):
        public = _occurrence(
            first_seen_at=FIRST_EARLY,
            retrieved_at=RETRIEVED_EARLY,
            marker="public",
        )
        private = dict(public, accession=OTHER_ACCESSION, avail_code="N",
                       first_seen_at=FIRST_LATE, retrieved_at=RETRIEVED_LATE,
                       marker="private")

        canonical, twins = elibrary.dedupe_availability([private, public])

        self.assertEqual(1, len(canonical))
        self.assertEqual(1, len(twins))
        self.assertEqual(ACCESSION, canonical[0]["accession"])
        self.assertEqual(FIRST_EARLY, canonical[0]["first_seen_at"])
        self.assertEqual(RETRIEVED_EARLY, canonical[0]["retrieved_at"])
        self.assertEqual(OTHER_ACCESSION, twins[0]["accession"])
        self.assertEqual(FIRST_LATE, twins[0]["first_seen_at"])
        self.assertEqual(RETRIEVED_LATE, twins[0]["retrieved_at"])


class _BaselineStaging:
    def __init__(self, prior=FIRST_EARLY):
        self.prior = prior

    def query(self, sql, params):
        del params
        if "FROM filings" in sql:
            return [{"filing_id": ACCESSION, "first_seen_at": self.prior}]
        if "FROM checkpoints" in sql:
            return []
        raise AssertionError(f"unexpected NewsBaseline query: {sql}")


class NewsBaselineFirstSeenTests(unittest.TestCase):
    def setUp(self):
        self.baseline = elibrary.NewsBaseline(
            _BaselineStaging(), "synthetic-adapter", ENTITY, SOURCE)

    def test_prior_first_seen_survives_even_when_the_caller_has_no_now_value(self):
        self.assertEqual(FIRST_EARLY, self.baseline.first_seen(ACCESSION, None))

    def test_earlier_cache_capture_repairs_a_later_stored_first_seen(self):
        baseline = elibrary.NewsBaseline(
            _BaselineStaging(prior=FIRST_LATE),
            "synthetic-adapter", ENTITY, SOURCE)
        self.assertEqual(FIRST_EARLY, baseline.first_seen(ACCESSION, FIRST_EARLY))

    def test_unseen_occurrence_accepts_none_without_inventing_wall_clock_time(self):
        self.assertIsNone(self.baseline.first_seen("unseen-without-capture", None))
        self.assertEqual(
            FIRST_LATE,
            self.baseline.first_seen("unseen-with-explicit-capture", FIRST_LATE),
        )


class _CaptureStaging:
    def __init__(self):
        self.filing = None
        self.documents = None

    def write_filing_bundle(self, filing, *, documents, **_kwargs):
        self.filing = dict(filing)
        self.documents = [dict(row) for row in documents]


class _CaptureBaseline:
    def __init__(self):
        self.first_seen_calls = []

    def first_seen(self, filing_id, captured_at):
        self.first_seen_calls.append((filing_id, captured_at))
        return captured_at

    def route(self, _filing_id, _filed_date, *, version_status=""):
        del version_status
        return elibrary.ARCHIVE, 1, "synthetic timestamp fixture"


def _adapter_filing(*, captured: bool) -> dict:
    return {
        "accession": ACCESSION,
        "description": "Synthetic R50 captured eLibrary occurrence",
        "class_pairs": [],
        "class_types": [],
        "filed_date": "2026-09-09",
        "posted_date": "2026-09-09",
        "issued_date": "",
        "avail_code": "P",
        "dockets": ["CP11-72-000"],
        "docket_bases": ["CP11-72"],
        "availability_twins": [],
        "classification": {
            "label": "synthetic timestamp fixture",
            "stage": "other_submittal",
            "is_issuance": False,
        },
        "retrieval": "retrieved" if captured else "",
        "first_seen_at": FIRST_EARLY if captured else None,
        "retrieved_at": RETRIEVED_EARLY if captured else None,
        "document_retrieved_at": RETRIEVED_LATE if captured else None,
    }


class AdapterCaptureTimePropagationTests(unittest.TestCase):
    LNG_ENTITY = "NO-FERC-CID:Sabine Pass Liquefaction, LLC"

    def _persist_docs(self, *, captured: bool):
        staging = _CaptureStaging()
        baseline = _CaptureBaseline()
        ctx = SimpleNamespace(staging=staging)
        filing = _adapter_filing(captured=captured)
        entity = {"entity_key": ENTITY, "legal_name": "Synthetic R50 Entity"}
        with mock.patch.object(
                docs, "_now", side_effect=AssertionError(
                    "elibrary_docs._persist consulted wall clock for cached occurrence")):
            docs._persist(ctx, entity, filing, baseline=baseline)
        return staging, baseline

    def _persist_lng(self, *, captured: bool):
        staging = _CaptureStaging()
        baseline = _CaptureBaseline()
        ctx = SimpleNamespace(staging=staging)
        filing = _adapter_filing(captured=captured)
        cfg = lng.FACILITIES[self.LNG_ENTITY]
        entity = {
            "entity_key": self.LNG_ENTITY,
            "legal_name": lng.FACILITY_ENTITY_NAMES[self.LNG_ENTITY],
        }
        with mock.patch.object(
                lng, "_now", side_effect=AssertionError(
                    "lng._persist consulted wall clock for cached occurrence")):
            lng._persist(ctx, entity, cfg, filing, is_twin=False, baseline=baseline)
        return staging, baseline

    def test_both_document_adapters_persist_exact_cached_capture_times(self):
        for name, persist in (("elibrary_docs", self._persist_docs),
                              ("lng", self._persist_lng)):
            with self.subTest(adapter=name):
                staging, baseline = persist(captured=True)
                self.assertEqual(FIRST_EARLY, staging.filing["first_seen_at"])
                self.assertEqual(RETRIEVED_LATE, staging.filing["retrieved_at"])
                self.assertEqual(RETRIEVED_LATE, staging.documents[0]["retrieved_at"])
                self.assertEqual([(ACCESSION, FIRST_EARLY)], baseline.first_seen_calls)

    def test_both_document_adapters_leave_uncaptured_times_unknown(self):
        for name, persist in (("elibrary_docs", self._persist_docs),
                              ("lng", self._persist_lng)):
            with self.subTest(adapter=name):
                staging, baseline = persist(captured=False)
                self.assertIsNone(staging.filing["first_seen_at"])
                self.assertIsNone(staging.filing["retrieved_at"])
                self.assertIsNone(staging.documents[0]["retrieved_at"])
                self.assertEqual([(ACCESSION, None)], baseline.first_seen_calls)

    def test_event_builders_prefer_capture_then_use_explicit_run_envelope_fallback(self):
        cases = ((FIRST_EARLY, FIRST_EARLY), (None, EVENT_ENVELOPE_TIME))
        for captured_at, expected_at in cases:
            docs_now = (mock.patch.object(
                docs, "_now", side_effect=AssertionError(
                    "elibrary_docs._event ignored a captured first_seen_at"))
                if captured_at else
                mock.patch.object(docs, "_now", return_value=EVENT_ENVELOPE_TIME))
            with self.subTest(adapter="elibrary_docs", captured_at=captured_at):
                filing = _adapter_filing(captured=bool(captured_at))
                baseline = _CaptureBaseline()
                with docs_now:
                    event = docs._event(
                        ENTITY, filing, "operational_report", "operational",
                        [{"asset_id": "synthetic-asset"}], "synthetic", baseline)
                self.assertEqual(expected_at, event["first_seen_at"])
                self.assertEqual([(ACCESSION, expected_at)], baseline.first_seen_calls)

            lng_now = (mock.patch.object(
                lng, "_now", side_effect=AssertionError(
                    "lng._events ignored a captured first_seen_at"))
                if captured_at else
                mock.patch.object(lng, "_now", return_value=EVENT_ENVELOPE_TIME))
            with self.subTest(adapter="lng", captured_at=captured_at):
                filing = _adapter_filing(captured=bool(captured_at))
                filing["description"] = "Semi-Annual Operational Report for Sabine Pass"
                filing["version_status"] = "original"
                baseline = _CaptureBaseline()
                with lng_now:
                    events = lng._events(
                        self.LNG_ENTITY, lng.FACILITIES[self.LNG_ENTITY],
                        [filing], baseline)
                self.assertEqual(1, len(events))
                self.assertEqual(expected_at, events[0]["first_seen_at"])
                self.assertEqual([(ACCESSION, expected_at)], baseline.first_seen_calls)


def _filing(filing_id: str, *, first_seen_at, retrieved_at) -> dict:
    return {
        "source_system": SOURCE,
        "filing_id": filing_id,
        "entity_key": ENTITY,
        "form": "Synthetic R50 filing",
        "accession_number": filing_id,
        "content_hash": ("a" if filing_id == ACCESSION else "b") * 64,
        "is_canonical": 1,
        "canonical_reason": "synthetic timestamp determinism fixture",
        "version_status": "original",
        "data_origin": "document",
        "retrieved_at": retrieved_at,
        "first_seen_at": first_seen_at,
        "source_url": f"https://example.invalid/elibrary/{filing_id}",
    }


class StagingOccurrenceCaptureTimeTests(unittest.TestCase):
    def _replay(self, sightings: list[dict]) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory(prefix="ferc-r50-timestamps-") as temporary:
            store = Staging(pathlib.Path(temporary) / "staging.sqlite")
            try:
                store.upsert("entities", [{
                    "entity_key": ENTITY,
                    "cid": ENTITY,
                    "legal_name": "Synthetic R50 timestamp entity",
                }], ["entity_key"])
                for sighting in sightings:
                    store.write_filing_bundle(dict(sighting))

                # Negative control: a different source occurrence must retain
                # its own capture window rather than join ACCESSION's extrema.
                store.write_filing_bundle(_filing(
                    OTHER_ACCESSION,
                    first_seen_at=FIRST_LATE,
                    retrieved_at=RETRIEVED_EARLY,
                ))
                rows = {
                    row["filing_id"]: dict(row)
                    for row in store.query(
                        "SELECT filing_id, first_seen_at, retrieved_at FROM filings "
                        "WHERE source_system=? ORDER BY filing_id", (SOURCE,))
                }
                return rows[ACCESSION], rows[OTHER_ACCESSION]
            finally:
                store.close()

    def test_real_staging_upsert_converges_in_forward_and_reverse_order(self):
        early = _filing(
            ACCESSION,
            first_seen_at=FIRST_EARLY,
            retrieved_at=RETRIEVED_EARLY,
        )
        late = _filing(
            ACCESSION,
            first_seen_at=FIRST_LATE,
            retrieved_at=RETRIEVED_LATE,
        )

        forward, forward_other = self._replay([early, late])
        reverse, reverse_other = self._replay([late, early])

        expected = {
            "filing_id": ACCESSION,
            "first_seen_at": FIRST_EARLY,
            "retrieved_at": RETRIEVED_LATE,
        }
        self.assertEqual(expected, forward)
        self.assertEqual(expected, reverse)
        other_expected = {
            "filing_id": OTHER_ACCESSION,
            "first_seen_at": FIRST_LATE,
            "retrieved_at": RETRIEVED_EARLY,
        }
        self.assertEqual(other_expected, forward_other)
        self.assertEqual(other_expected, reverse_other)

    def test_missing_replay_metadata_does_not_erase_known_staging_times(self):
        known = _filing(
            ACCESSION,
            first_seen_at=FIRST_EARLY,
            retrieved_at=RETRIEVED_LATE,
        )
        unknown = _filing(ACCESSION, first_seen_at=None, retrieved_at="")

        for sightings in ([known, unknown], [unknown, known]):
            with self.subTest(first_write_has_capture=bool(sightings[0]["first_seen_at"])):
                occurrence, _other = self._replay(sightings)
                self.assertEqual(FIRST_EARLY, occurrence["first_seen_at"])
                self.assertEqual(RETRIEVED_LATE, occurrence["retrieved_at"])

    def test_listing_placeholder_converges_when_another_path_captures_occurrence(self):
        listing = {
            "document_id": f"{SOURCE}|{ACCESSION}|listing",
            "source_system": SOURCE,
            "filing_id": ACCESSION,
            "accession_number": ACCESSION,
            "attachment_id": "",
            "title": "Synthetic listing placeholder",
            "content_hash": "",
            "availability": "not_retrieved",
            "retrieved_at": None,
        }
        attachment = {
            "document_id": f"{SOURCE}|{ACCESSION}|attachment-1",
            "source_system": SOURCE,
            "filing_id": ACCESSION,
            "accession_number": ACCESSION,
            "attachment_id": "attachment-1",
            "title": "Synthetic retrieved attachment",
            "content_hash": "c" * 64,
            "availability": "retrieved",
            "retrieved_at": RETRIEVED_LATE,
        }
        failed_attachment = dict(
            attachment,
            document_id=f"{SOURCE}|{ACCESSION}|attachment-failed",
            attachment_id="attachment-failed",
            content_hash="",
            availability="failed",
            retrieved_at=None,
        )
        unknown = _filing(ACCESSION, first_seen_at=None, retrieved_at=None)
        known = _filing(
            ACCESSION,
            first_seen_at=FIRST_EARLY,
            retrieved_at=RETRIEVED_LATE,
        )

        for writes in (
            ((unknown, [listing, failed_attachment]), (known, [attachment])),
            ((known, [attachment]), (unknown, [listing, failed_attachment])),
        ):
            with self.subTest(listing_written_first=writes[0][0] is unknown):
                with tempfile.TemporaryDirectory(
                        prefix="ferc-r53-listing-capture-") as temporary:
                    store = Staging(pathlib.Path(temporary) / "staging.sqlite")
                    try:
                        store.upsert("entities", [{
                            "entity_key": ENTITY,
                            "cid": ENTITY,
                            "legal_name": "Synthetic R53 listing entity",
                        }], ["entity_key"])
                        for filing, documents in writes:
                            store.write_filing_bundle(
                                dict(filing), documents=[dict(row) for row in documents])
                        rows = {
                            row["document_id"]: dict(row)
                            for row in store.query(
                                "SELECT document_id,retrieved_at FROM documents "
                                "WHERE source_system=? AND filing_id=?",
                                (SOURCE, ACCESSION))
                        }
                    finally:
                        store.close()

                self.assertEqual(
                    RETRIEVED_LATE,
                    rows[f"{SOURCE}|{ACCESSION}|listing"]["retrieved_at"],
                )
                self.assertEqual(
                    RETRIEVED_LATE,
                    rows[f"{SOURCE}|{ACCESSION}|attachment-1"]["retrieved_at"],
                )
                self.assertIsNone(
                    rows[f"{SOURCE}|{ACCESSION}|attachment-failed"]["retrieved_at"],
                    "a real uncaptured attachment must not inherit the filing time",
                )


class IntegratedCapturedStateTests(unittest.TestCase):
    """The rebuilt candidate must apply the code repair to stored/exported data."""

    def test_every_elibrary_source_time_is_cache_backed_or_explicitly_unknown(self):
        database = pathlib.Path(os.environ.get(
            "FERC_ACCEPTANCE_REFERENCE_DB",
            str(ROOT / "staging" / "operating_assets.sqlite")))
        cache_root = pathlib.Path(os.environ.get(
            "FERC_SOURCE_CACHE", str(ROOT / "source_cache")))
        self.assertTrue(database.is_file(), f"candidate database absent: {database}")
        index_path = cache_root / "index.json"
        self.assertTrue(index_path.is_file(), f"frozen cache index absent: {index_path}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        first_times = {str(row.get("first_seen_at") or "") for row in index.values()}
        retrieved_times = {
            str(row.get("last_seen_at") or row.get("first_seen_at") or "")
            for row in index.values()
        }
        first_times.discard("")
        retrieved_times.discard("")

        connection = sqlite3.connect(
            f"file:{database}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            generic = [dict(row) for row in connection.execute(
                "SELECT filing_id,first_seen_at,retrieved_at FROM filings "
                "WHERE source_system='eLibrary' AND form='eLibrary document'")]
            documents = [dict(row) for row in connection.execute(
                "SELECT document_id,retrieved_at FROM documents "
                "WHERE source_system='eLibrary'")]
        finally:
            connection.close()

        self.assertEqual(4808, len(generic))
        self.assertEqual(5283, len(documents))
        self.assertEqual(31, sum(row["first_seen_at"] is None for row in generic))
        self.assertEqual(31, sum(row["retrieved_at"] is None for row in generic))
        self.assertEqual(31, sum(row["retrieved_at"] is None for row in documents))
        self.assertFalse([
            row for row in generic
            if row["first_seen_at"] is not None
            and row["first_seen_at"] not in first_times
        ])
        self.assertFalse([
            row for row in generic
            if row["retrieved_at"] is not None
            and row["retrieved_at"] not in retrieved_times
        ])
        self.assertFalse([
            row for row in documents
            if row["retrieved_at"] is not None
            and row["retrieved_at"] not in retrieved_times
        ])


if __name__ == "__main__":
    unittest.main()
