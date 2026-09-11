"""Fail-closed tests for the Build-A final implementation-record generator.

All databases, captures, logs, export generations and mutations are synthetic
and contained in ``TemporaryDirectory``.  No live project data is opened.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock

from adapters import ioc, lng
from implementation import build_final_release_records as records


ROOT = pathlib.Path(__file__).resolve().parents[1]


class _QueryView:
    """Minimal read-only-shaped adapter for validators that consume `.query()`."""

    def __init__(self, con):
        self.con = con

    def query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()


def _raw_json(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _ident(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_raw_json(value))


def _write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=list(columns), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(out.getvalue(), encoding="utf-8")


_TEST_ALIAS_BODY = b"synthetic official-FERC response retained canonically\n"
_TEST_ALIAS_SHA256 = hashlib.sha256(_TEST_ALIAS_BODY).hexdigest()
_TEST_ALIAS_SOURCE_URL = (
    "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
    "#body=synthetic-finalizer-alias")
_TEST_ALIAS_ACCESSION = "20990101-0001"
_TEST_ALIAS_ATTACHMENT = "SYNTHETIC-FINALIZER-ATTACHMENT"
_TEST_ALIAS_RESULTS = {
    "results": [{
        "accession_number": _TEST_ALIAS_ACCESSION,
        "attachment_id": _TEST_ALIAS_ATTACHMENT,
        "request": {"cache_url": _TEST_ALIAS_SOURCE_URL},
        "response": {
            "http_status": 200,
            "complete_body": True,
            "bytes": len(_TEST_ALIAS_BODY),
            "sha256": _TEST_ALIAS_SHA256,
        },
    }],
}
_TEST_ALIAS_RESULTS_RAW = _raw_json(_TEST_ALIAS_RESULTS)
_TEST_ALIAS_RESULTS_IDENTITY = _ident(_TEST_ALIAS_RESULTS_RAW)
_TEST_ALIAS_LEDGER = {
    "status": "complete",
    "results_identity": _TEST_ALIAS_RESULTS_IDENTITY,
    "entries": [{
        "accession": _TEST_ALIAS_ACCESSION,
        "attachment_ids": [_TEST_ALIAS_ATTACHMENT],
        "action": "imported",
        "http_status": 200,
        "cache_url": _TEST_ALIAS_SOURCE_URL,
        "byte_size": len(_TEST_ALIAS_BODY),
        "content_sha256": _TEST_ALIAS_SHA256,
    }],
}
_TEST_ALIAS_LEDGER_RAW = (
    json.dumps(_TEST_ALIAS_LEDGER, sort_keys=True, separators=(",", ":")) + "\n"
).encode("utf-8")
TEST_REDUNDANT_INPUT_ALIAS = {
    "alias_path": (
        "inputs/official_ferc_recovery/individual_responses/20990101-0001/"
        "01_SYNTHETIC-FINALIZER-ATTACHMENT.body"),
    "cache_path": "objects/%s/%s" % (
        _TEST_ALIAS_SHA256[:2], _TEST_ALIAS_SHA256),
    "sha256": _TEST_ALIAS_SHA256,
    "bytes": len(_TEST_ALIAS_BODY),
    "accession": _TEST_ALIAS_ACCESSION,
    "attachment_id": _TEST_ALIAS_ATTACHMENT,
    "source_url": _TEST_ALIAS_SOURCE_URL,
    "results_path": (
        "implementation_logs/input_recovery/"
        "OFFICIAL_FERC_INDIVIDUAL_20990101-0001.json"),
    "results_sha256": _TEST_ALIAS_RESULTS_IDENTITY["sha256"],
    "results_bytes": _TEST_ALIAS_RESULTS_IDENTITY["bytes"],
    "ledger_path": "implementation_logs/input_recovery/cache_import_runs.jsonl",
    "ledger_sha256": hashlib.sha256(_TEST_ALIAS_LEDGER_RAW).hexdigest(),
    "ledger_bytes": len(_TEST_ALIAS_LEDGER_RAW),
}


def _add_redundant_input_fixture(root, index, *, include_alias=True):
    root = pathlib.Path(root)
    declaration = TEST_REDUNDANT_INPUT_ALIAS
    canonical = root / "source_cache" / declaration["cache_path"]
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(_TEST_ALIAS_BODY)
    results = root / declaration["results_path"]
    results.parent.mkdir(parents=True, exist_ok=True)
    results.write_bytes(_TEST_ALIAS_RESULTS_RAW)
    ledger = root / declaration["ledger_path"]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(_TEST_ALIAS_LEDGER_RAW)
    if include_alias:
        alias = root / declaration["alias_path"]
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.write_bytes(_TEST_ALIAS_BODY)
    cache_key = hashlib.sha256(_TEST_ALIAS_SOURCE_URL.encode("utf-8")).hexdigest()
    index[cache_key] = {
        "content_hash": _TEST_ALIAS_SHA256,
        "cache_path": declaration["cache_path"],
        "byte_size": len(_TEST_ALIAS_BODY),
        "source_system": "eLibrary",
        "source_url": _TEST_ALIAS_SOURCE_URL,
        "media_type": "application/octet-stream",
    }


def _make_snapshot_fixture(root, *, include_alias=True):
    root = pathlib.Path(root)
    index = {}
    _add_redundant_input_fixture(root, index, include_alias=include_alias)
    _write_json(root / "source_cache" / "index.json", index)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "universe.csv").write_bytes(b"entity_key\nC-SYNTHETIC\n")
    (root / "config" / "run_plan.json").write_bytes(b"{}\n")
    unrelated = root / "inputs" / "official_ferc" / "unrelated.body"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"unrelated input must remain visible\n")
    return unrelated


def _add_dependency_capture_fixture(root, index):
    """Create an independent compact analogue of the 11-response cache delta."""
    root = pathlib.Path(root)
    capture_dir = (root / "inputs" /
                   "official_ferc_elibrary_dependency_recovery_20260910")
    capture_dir.mkdir(parents=True, exist_ok=True)
    before_raw = _raw_json(index)
    before_path = capture_dir / "source_cache_index_before.json"
    before_path.write_bytes(before_raw)
    expected_units = [
        {"entity_key": "C000630", "dockets": ["IS26-390", "IS26-294"]},
        {"entity_key": "C001031", "dockets": ["RP26-488"]},
        {"entity_key": "C001089", "dockets": ["RP25-1075"]},
        {"entity_key": "C001506", "dockets": ["RP26-397"]},
    ]
    rows = []

    def add_response(kind, url, raw, media_type, **extra):
        digest = hashlib.sha256(raw).hexdigest()
        cache_rel = pathlib.Path("objects") / digest[:2] / digest
        cache_path = root / "source_cache" / cache_rel
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        index[key] = {
            "byte_size": len(raw), "cache_path": cache_rel.as_posix(),
            "content_hash": digest, "source_url": url,
            "source_system": "eLibrary", "media_type": media_type,
        }
        record = {
            "cache_key": key, "source_url": url,
            "method": "POST" if "#body=" in url else "GET",
            "source_system": "eLibrary", "retrieved_utc": "2026-09-10T00:00:00Z",
            "media_type": media_type,
            "response": {"bytes": len(raw), "sha256": digest,
                         "cache_path": "source_cache/" + cache_rel.as_posix()},
            "kind": kind,
        }
        record.update(extra)
        rows.append(record)
        return record

    for unit in expected_units:
        for docket in unit["dockets"]:
            body = {"docket": docket, "window": ["2015-01-01", "2026-12-31"]}
            request_raw = json.dumps(body, sort_keys=True).encode("utf-8")
            request_hash = hashlib.sha256(request_raw).hexdigest()
            url = ("https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/"
                   "AdvancedSearch#body=" + request_hash[:32])
            accession = "2026%02d01-%04d" % (len(rows) + 1, 7000 + len(rows))
            response = _raw_json({
                "success": True,
                "searchHits": [{"acesssionNumber": accession}],
                "numHits": 1, "totalHits": 1,
            })
            add_response(
                "elibrary_advanced_search_response", url, response,
                "application/json", entity_key=unit["entity_key"],
                entity_name="Synthetic " + unit["entity_key"], docket=docket,
                request={"content_type": "application/json", "body": body,
                         "bytes": len(request_raw), "sha256": request_hash},
                response_accessions=[accession], required_by_failed_build=True,
                mandatory_for_clean_retry=True, dependency_role="direct_v4_failure")

    file_accessions = ["20260601-7101", "20260701-7102", "20260801-7103"]
    file_rows = []
    for number, accession in enumerate(file_accessions):
        attachment_id = "SYNTHETIC-ATTACHMENT-%d" % number
        list_raw = _raw_json({
            "DataList": [{"ID": attachment_id, "Availability_Mode": "P"}],
            "ErrorList": [],
        })
        file_row = add_response(
            "elibrary_file_list_response",
            "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/GetFileListFromP8/" +
            accession,
            list_raw, "application/json", accession_number=accession,
            request={"body": None, "bytes": 0, "sha256": None},
            public_attachment_ids=[attachment_id], listed_files=1, error_list=[],
            required_by_failed_build=(number == 0), mandatory_for_clean_retry=True,
            dependency_role=("direct_v4_failure" if number == 0
                             else "transitive_replay_dependency"))
        file_rows.append((file_row, attachment_id))

    for number, ((file_row, attachment_id), accession) in enumerate(
            zip(file_rows, file_accessions)):
        body = {"FileIDAll": "", "FileType": "", "Islegacy": False,
                "accession": accession, "fileid": 0, "fileidLst": [attachment_id]}
        request_raw = json.dumps(body, sort_keys=True).encode("utf-8")
        request_hash = hashlib.sha256(request_raw).hexdigest()
        url = ("https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
               "#body=" + request_hash[:32])
        attachment_raw = ("synthetic official attachment " + accession).encode("utf-8")
        attachment = add_response(
            "elibrary_attachment_response", url, attachment_raw,
            "application/pdf", accession_number=accession,
            request_strategy="all_public_ids", attachment_ids=[attachment_id],
            request={"content_type": "application/json", "body": body,
                     "bytes": len(request_raw), "sha256": request_hash},
            response_container={"container": "single_object", "crc_clean": None,
                                "members": []},
            required_by_failed_build=False, mandatory_for_clean_retry=True,
            dependency_role="transitive_replay_dependency")
        file_row["download_cache_key"] = attachment["cache_key"]

    after_raw = _raw_json(index)
    direct_keys = sorted(value["cache_key"] for value in rows
                         if value["required_by_failed_build"])
    log_rel = pathlib.Path(
        "implementation_logs/input_recovery/build_a_v4_dependency_capture.log")
    log_path = root / log_rel
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"synthetic bounded capture: run status: complete; failed=  0\n")
    capture = {
        "schema": "official_ferc_elibrary_dependency_capture_v1",
        "as_of_date": "2026-09-07", "retrieval_method": "synthetic bounded fixture",
        "source_cache_before": {"entries": len(json.loads(before_raw)),
                                "bytes": len(before_raw),
                                "sha256": hashlib.sha256(before_raw).hexdigest()},
        "source_cache_after": {"entries": len(index), "bytes": len(after_raw),
                               "sha256": hashlib.sha256(after_raw).hexdigest()},
        "preexisting_entries_changed": 0, "preexisting_entries_removed": 0,
        "new_entries": 11, "required_failed_build_keys": direct_keys,
        "mandatory_clean_retry_keys": sorted(value["cache_key"] for value in rows),
        "capture_log": {"path": log_rel.as_posix(), **_ident(log_path.read_bytes())},
        "rows": sorted(rows, key=lambda value: value["cache_key"]),
    }
    capture_path = capture_dir / "CAPTURE.json"
    _write_json(capture_path, capture)
    input_id = "elibrary-dependency-capture:build-a-v4:20260910"
    return ({
        "input_id": input_id,
        "input_kind": "elibrary_dependency_capture_bundle",
        "adapter": "elibrary_docs", "source_system": "FERC eLibrary Web API",
        "classification": "mandatory executable prerequisite",
        "classification_basis": "clean replay exact cache dependency",
        "repair_discovery": "clean replay exposed exact missing requests",
        "entities": [{"entity_key": value["entity_key"]} for value in expected_units],
        "consumers": ["current-universe retrieval", "offline Build B"],
        "capture_manifest": {"path": str(capture_path.relative_to(root)),
                             "schema": capture["schema"], "new_entries": 11,
                             "required_failed_build_keys": 6,
                             "preexisting_entries_changed": 0,
                             "preexisting_entries_removed": 0,
                             **_ident(capture_path.read_bytes())},
        "pre_capture_cache_index": {"path": str(before_path.relative_to(root)),
                                    "entries": len(json.loads(before_raw)),
                                    **_ident(before_raw)},
        "capture_log": {"path": log_rel.as_posix(), **_ident(log_path.read_bytes())},
        "required_cache_keys": direct_keys, "expected_units": expected_units,
        "disposition": "pending_build_a",
        "residual_limitation": "synthetic fixture boundary",
        "evidence": [str(capture_path.relative_to(root))],
    }, expected_units)


class Fixture:
    def __init__(self, base):
        self.base = pathlib.Path(base)
        self.root = self.base / "candidate"
        self.audit = self.base / "audit"
        self.logs = self.root / "implementation_logs" / "final"
        self.output = self.root / "implementation" / "final"
        self.db = self.root / "staging" / "operating_assets.sqlite"
        self.ledger_path = self.root / "implementation" / "REPAIR_LEDGER_DRAFT.json"
        self.exception_path = self.root / "implementation" / "EXCEPTION_DRAFT.json"
        self.input_path = self.root / "implementation" / "INPUT_DRAFT.json"
        self.related_path = self.root / "implementation" / "RELATED_DRAFT.json"
        self.additional_input_path = (
            self.root / "implementation" / "ADDITIONAL_INPUT_DRAFT.json")
        self.validation = self.root / "verification" / "validation_results.json"
        self.receipt = self.root / "publication_receipt.json"
        self.test_manifest = self.logs / "test_runs.json"
        self.issue_ids = ["A%02d" % i for i in range(1, 23)] + [
            "C%02d" % i for i in range(1, 43)]
        self.exception_ids = ["blk-%016x" % i for i in range(1, 35)]
        self.input_accessions = ["2025%02d01-%04d" % (i, 5000 + i)
                                 for i in range(1, 10)] + [
            "20251001-5010", "20251101-5011", "20251201-5012", "20260101-5013"]
        self.input_ids = ["elibrary:" + a for a in self.input_accessions]
        self.related_ids = sorted(records.RELATED_FINDING_IDS)
        self.additional_input_id = "elibrary:20260226-5162"
        self.dependency_input_id = \
            "elibrary-dependency-capture:build-a-v4:20260910"
        self.search_input_ids = sorted(
            records.ADDITIONAL_INPUT_IDS - {
                self.additional_input_id, self.dependency_input_id})
        self._make()

    def args(self):
        return [
            "--root", str(self.root), "--db", str(self.db),
            "--output-dir", str(self.output), "--logs-dir", str(self.logs),
            "--audit-dir", str(self.audit),
            "--test-run-manifest", str(self.test_manifest),
            "--validation", str(self.validation),
            "--publication-receipt", str(self.receipt),
            "--ledger-draft", str(self.ledger_path),
            "--exception-draft", str(self.exception_path),
            "--input-draft", str(self.input_path),
            "--related-findings-draft", str(self.related_path),
            "--additional-inputs-draft", str(self.additional_input_path),
        ]

    def _make(self):
        self.root.mkdir(parents=True)
        self.audit.mkdir()
        self.logs.mkdir(parents=True)
        self._make_config_and_drafts()
        self._make_database()
        self._make_crosswalks()
        self._make_exports()
        self._make_validation_and_tests()
        self._make_audit()

    def _make_config_and_drafts(self):
        # Use the frozen universe bytes because R28 independently checks the
        # one reviewed CP15-161 authority row whose evidence lives there.
        universe = (ROOT / "config" / "universe.csv").read_bytes()
        universe_path = self.root / "config" / "universe.csv"
        universe_path.parent.mkdir(parents=True)
        universe_path.write_bytes(universe)
        source_identity = {
            "fixture": {"path": "config/universe.csv", **_ident(universe)}}

        ledger_rows = []
        for issue_id in self.issue_ids:
            ledger_row = {
                "issue_id": issue_id,
                "source_row_type": ("original_audit_A01_A22"
                                    if issue_id.startswith("A") else "additional_claimed_issue"),
                "original_issue_ids": [issue_id] if issue_id.startswith("A") else ["A01"],
                "severity": "P1", "audit_claim": {"title": "historical claim " + issue_id},
                "affected_component_or_data": ["component"],
                "root_cause": "specific reproduced cause",
                "targeted_test": "test_exact_control",
                "current_disposition": ("still_open" if issue_id == "A17"
                                        else "fixed_pending_build"),
                "evidence": {"audit": ["audit.json"], "candidate": ["test.py"]},
                "audit_claim_preserved": True,
            }
            if issue_id == "C01":
                ledger_row["repair_counterevidence"] = {
                    "claim_boundary": "exact rows rebut one classification only",
                    "facts": ["dfact-one", "dfact-two"],
                }
            ledger_rows.append(ledger_row)
        ledger = {
            "schema": "ferc_integrated_repair_ledger_draft_v1", "draft": True,
            "counts": {"rows": 64}, "source_identities": source_identity,
            "rows": ledger_rows,
        }
        _write_json(self.ledger_path, ledger)

        exception_rows = []
        exception_accessions = ["20240223-5073", "20240223-5075"] + [
            "2024%02d01-%04d" % (((i - 2) % 12) + 1, 6000 + i)
            for i in range(2, 34)]
        self.exception_accessions = exception_accessions
        for i, (exception_id, accession) in enumerate(
                zip(self.exception_ids, exception_accessions)):
            capacity = i < 2
            exception_rows.append({
                "original_exception_id": exception_id,
                "related_original_issue_ids": ["A17" if capacity else "A02"],
                "related_final_finding_ids": ["R12"],
                "occurrence_or_obligation": {
                    "scope": ("capacity:" if capacity else "ioc:") + accession,
                    "accession_number": accession,
                    "adapter": "capacity" if capacity else "ioc",
                    "kind": "source",
                    "original_summary": "specific source exception",
                    "obligation_level_effect": "one retained obligation",
                },
                "audit_claim": {"summary": "original unclaimed audit record"},
                "root_cause": "specific source-format cause",
                "current_repair_action": "replay exact captured source",
                "current_candidate_state": "code implemented",
                "current_audited_output": {},
                "targeted_test_gate": "exact source replay",
                "disposition": "open_capability_gap" if capacity else "pending_build_a",
                "residual_limitation": "source-specific boundary",
                "evidence": [{"path": "config/universe.csv", **_ident(universe)}],
            })
        exceptions = {
            "schema": "integrated_repair_exception_dispositions_draft_v1",
            "draft": True, "counts": {"rows": 34},
            "source_identities": source_identity, "rows": exception_rows,
        }
        _write_json(self.exception_path, exceptions)

        self.index = {}
        _add_redundant_input_fixture(self.root, self.index)
        input_rows = []
        for i, (input_id, accession) in enumerate(zip(self.input_ids, self.input_accessions)):
            list_raw = ("list " + accession).encode()
            attachment_raw = ("attachment " + accession).encode()
            raw_rel = pathlib.Path("inputs/official_ferc_recovery") / (accession + ".body")
            raw_path = self.root / raw_rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(attachment_raw)
            list_url = "https://elibrary.ferc.gov/list/" + accession
            attachment_url = "https://elibrary.ferc.gov/download#" + accession
            for url, raw, media in ((list_url, list_raw, "application/json"),
                                    (attachment_url, attachment_raw,
                                     "application/octet-stream")):
                digest = hashlib.sha256(raw).hexdigest()
                cache_rel = pathlib.Path("objects") / digest[:2] / digest
                cache_path = self.root / "source_cache" / cache_rel
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(raw)
                key = hashlib.sha256(url.encode()).hexdigest()
                self.index[key] = {
                    "byte_size": len(raw), "cache_path": cache_rel.as_posix(),
                    "content_hash": digest, "source_url": url,
                    "source_system": "eLibrary", "media_type": media,
                }
            input_rows.append({
                "input_id": input_id, "accession_number": accession,
                "adapter": "ioc", "source_system": "FERC eLibrary Web API",
                "classification": "mandatory executable prerequisite",
                "classification_basis": "final audit absent-input population",
                "audit_claim": {"request_url": list_url},
                "entities": [{"entity_key": "C%06d" % (i + 100)}],
                "consumers": ["IOC adapter", "source-linked export"],
                "official_list_capture": {"url": list_url, "http_status": 200,
                                          **_ident(list_raw)},
                "official_attachment_capture": {
                    "official_url": attachment_url, "raw_path": raw_rel.as_posix(),
                    **_ident(attachment_raw)},
                "cache_import": {"list_cache_url": list_url,
                                 "attachment_cache_url": attachment_url,
                                 "status": "imported and confirmed"},
                "consumer_application_status": "pending Build A",
                "disposition": "pending_build_a",
                "evidence": [{"path": "config/universe.csv", **_ident(universe)}],
            })
        self.a17_sources = []
        for accession, entity, expected in (
                ("20240223-5073", "Cadeville Gas Storage LLC",
                 (("estimated_peak_day_delivery_capacity", "420", "MMcf/day", "approximately"),
                  ("estimated_total_storage_capacity", "23.7", "Bcf", "estimated"))),
                ("20240223-5075", "Monroe Gas Storage Company, LLC",
                 (("estimated_peak_day_delivery_capacity", "465", "MMcf/day", "approximately"),
                  ("estimated_total_storage_capacity", "11.96", "Bcf", "estimated")))):
            source_raw = ("%PDF-1.4 synthetic reviewed source " + accession).encode()
            source_sha = hashlib.sha256(source_raw).hexdigest()
            cache_rel = pathlib.Path("objects") / source_sha[:2] / source_sha
            cache_path = self.root / "source_cache" / cache_rel
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(source_raw)
            url = "https://elibrary.ferc.gov/a17/" + accession
            self.index[hashlib.sha256(url.encode()).hexdigest()] = {
                "byte_size": len(source_raw), "cache_path": cache_rel.as_posix(),
                "content_hash": source_sha, "source_url": url,
                "source_system": "eLibrary", "media_type": "application/pdf",
            }
            self.a17_sources.append({
                "accession": accession, "entity": entity,
                "source_object_sha256": source_sha, "source_bytes": len(source_raw),
                "pages": 1, "text_layer": False,
                "render_sha256": hashlib.sha256(("render " + accession).encode()).hexdigest(),
                "reviewed_by": "independent visual review", "ocr_used": False,
                "expected": [{"kind": kind, "value_text": value, "unit": unit,
                              "qualifier": qualifier}
                             for kind, value, unit, qualifier in expected],
                "required_qualifiers": ["BASE GAS", "OFF-SYSTEM",
                                        "reasonably representative operating assumptions"],
            })
        index_path = self.root / "source_cache" / "index.json"
        _write_json(index_path, self.index)
        inputs = {
            "schema": "integrated_repair_final_input_inventory_draft_v1",
            "draft": True, "counts": {"rows": 13},
            "source_identities": source_identity,
            "population_reconciliation": {"older": 28, "later": 7, "final": 13},
            "rows": input_rows,
        }
        _write_json(self.input_path, inputs)

        capacity_input_root = ROOT / "inputs" / "official_ferc_capacity_20260226_5162"
        capacity_list_raw = (capacity_input_root / "list_response.body").read_bytes()
        capacity_attachment_raw = (
            capacity_input_root / "attachment_response.body").read_bytes()
        self.capacity_attachment_hash = hashlib.sha256(
            capacity_attachment_raw).hexdigest()
        self.asserted_capacity_hash = \
            "983e6a1c12a2f15934d99a2149c7a18e3dbbedf145f0848b9844dc4467dac5eb"
        if self.capacity_attachment_hash != self.asserted_capacity_hash \
                or len(capacity_attachment_raw) != 173287:
            raise AssertionError("required capacity fixture identity changed")
        capacity_raw_rel = pathlib.Path(
            "inputs/official_ferc_capacity_20260226_5162/attachment_response.body")
        capacity_raw_path = self.root / capacity_raw_rel
        capacity_raw_path.parent.mkdir(parents=True, exist_ok=True)
        capacity_raw_path.write_bytes(capacity_attachment_raw)
        capacity_list_url = (
            "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/"
            "GetFileListFromP8/20260226-5162")
        capacity_attachment_url = (
            "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
            "#body=5cbdf764071370d4d42599158922ca68")
        for url, raw, media in (
                (capacity_list_url, capacity_list_raw, "application/json"),
                (capacity_attachment_url, capacity_attachment_raw,
                 "application/octet-stream")):
            digest = hashlib.sha256(raw).hexdigest()
            cache_rel = pathlib.Path("objects") / digest[:2] / digest
            cache_path = self.root / "source_cache" / cache_rel
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(raw)
            self.index[hashlib.sha256(url.encode()).hexdigest()] = {
                "byte_size": len(raw), "cache_path": cache_rel.as_posix(),
                "content_hash": digest, "source_url": url,
                "source_system": "eLibrary", "media_type": media,
            }
        _write_json(index_path, self.index)
        related_rows = []
        for issue_id in self.related_ids:
            related_rows.append({
                "issue_id": issue_id,
                "relationship_to_final_audit":
                    "not_in_final_audit_discovered_during_repair",
                "original_issue_ids": ["A02"], "severity": "P1",
                "repair_discovery": "specific repair-time defect",
                "affected_component_or_data": ["component"],
                "root_cause": "specific repair-time root cause",
                "targeted_test": "test exact related control",
                "target_test_ids": ["tests.synthetic.CompleteSuite.test_case_000"],
                "current_disposition": "fixed_pending_build",
                "residual_limitation": "package boundary remains separate",
                "evidence": ["tests/test_synthetic.py"],
            })
        _write_json(self.related_path, {
            "schema": "ferc-related-repair-findings-draft-v1", "draft": True,
            "counts": {"rows": len(related_rows)},
            "source_identities": source_identity, "rows": related_rows,
        })
        additional_rows = [{
            "input_id": self.additional_input_id,
            "accession_number": "20260226-5162", "adapter": "capacity",
            "source_system": "FERC eLibrary Web API",
            "classification": "mandatory executable prerequisite",
            "classification_basis": "specific repair-time source prerequisite",
            "repair_discovery": "exact filer boundary exposed missing bytes",
            "entities": [{"entity_key": "C001088"}],
            "consumers": ["source facts/observations/lineage",
                          "document/filing archive", "coverage export"],
            "official_list_capture": {
                "url": capacity_list_url, "http_status": 200,
                **_ident(capacity_list_raw)},
            "official_attachment_capture": {
                "official_url": capacity_attachment_url.split("#", 1)[0],
                "raw_path": capacity_raw_rel.as_posix(),
                **_ident(capacity_attachment_raw)},
            "cache_import": {"list_cache_url": capacity_list_url,
                             "attachment_cache_url": capacity_attachment_url,
                             "status": "imported and confirmed"},
            "disposition": "pending_build_a",
            "evidence": ["tests/test_synthetic.py"],
        }]

        source_search_root = (
            ROOT / "inputs" / "official_ferc_elibrary_search_recovery_20260909")
        fixture_search_root = (
            self.root / "inputs" / "official_ferc_elibrary_search_recovery_20260909")
        for source in source_search_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_search_root)
            target = fixture_search_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        capture_bundle = json.loads((fixture_search_root / "CAPTURE.json").read_text())
        applications = {
            "IS26-587": [{"accession_number": "20260731-5000",
                           "entity_key": "C001049", "filing_docket": "IS26-587-000",
                           "as_of_relation": "on_or_before_as_of"}],
            "RP26-1091": [{"accession_number": "20260909-5053",
                            "as_of_relation": "after_as_of"},
                           {"accession_number": "20260825-5125",
                            "entity_key": "C000654", "filing_docket": "RP26-1091-000",
                            "as_of_relation": "on_or_before_as_of"}],
            "RP26-981": [{"accession_number": "20260721-5070",
                           "entity_key": "C000654", "filing_docket": "RP26-981-000",
                           "as_of_relation": "on_or_before_as_of"}],
        }
        for captured in capture_bundle["rows"]:
            response_sha = captured["response_sha256"]
            response_raw = (fixture_search_root / "source_cache" / "objects" /
                            response_sha[:2] / response_sha).read_bytes()
            cache_rel = pathlib.Path("objects") / response_sha[:2] / response_sha
            cache_target = self.root / "source_cache" / cache_rel
            cache_target.parent.mkdir(parents=True, exist_ok=True)
            cache_target.write_bytes(response_raw)
            cache_url = captured["cache_url"]
            self.index[hashlib.sha256(cache_url.encode()).hexdigest()] = {
                "byte_size": len(response_raw), "cache_path": cache_rel.as_posix(),
                "content_hash": response_sha, "source_url": cache_url,
                "source_system": "eLibrary", "media_type": "application/json",
            }
            docket = captured["docket"]
            request_sha = captured["request_payload_sha256"]
            additional_rows.append({
                "input_id": "elibrary-search:%s:%s" % (docket, request_sha),
                "input_kind": "elibrary_advanced_search_response",
                "docket": docket, "adapter": "elibrary_docs",
                "source_system": "FERC eLibrary Web API",
                "classification": "evidence required to reproduce existing assertion",
                "classification_basis": "exact repair-time search response evidence",
                "repair_discovery": "clean Build A exposed the exact request",
                "entities": [{"entity_key": "C001049" if docket.startswith("IS")
                              else "C000654"}],
                "consumers": ["repair diagnosis", "point-in-time occurrence check"],
                "official_search_capture": {
                    "method": "POST", "official_url": captured["official_url"],
                    "cache_url": cache_url,
                    "request_payload_bytes": captured["request_payload_bytes"],
                    "request_payload_sha256": request_sha,
                    "bytes": captured["response_bytes"],
                    "sha256": response_sha,
                    "retrieved_utc": capture_bundle["retrieved_utc"],
                    "http_status_basis":
                        "bounded_capture_v1_successful_response_contract",
                    "accessions": captured["accessions"],
                    "raw_path": str((fixture_search_root / "source_cache" / "objects" /
                                     response_sha[:2] / response_sha).relative_to(self.root)),
                },
                "expected_occurrences": applications[docket],
                "disposition": "pending_build_a",
                "residual_limitation": "repair evidence, not final-plan selected input",
                "evidence": ["tests/test_synthetic.py"],
            })
        # Lightweight but complete 15-route taxonomy freeze for the R37
        # final-record gate.  Each object is independent of the implementation
        # under test and the whole-index hash is frozen only after all routes
        # have been installed.
        self.taxonomy_pins = []
        taxonomy_slugs = {
            "Form 2": "form2", "Form 2A": "form2a",
            "Form 3Q Gas": "form3q-gas", "Form 6": "form6",
            "Form 6Q": "form6q",
        }
        for form in sorted(records.TAXONOMY_PIN_FORMS):
            for year in sorted(records.TAXONOMY_PIN_YEARS):
                version = "%d-04-01" % year
                url = ("https://eCollection.ferc.gov/taxonomy/%s/%s/entry.xsd" %
                       (taxonomy_slugs[form], version))
                raw = ("<schema form=%r version=%r/>\n" % (form, version)).encode()
                content_hash = hashlib.sha256(raw).hexdigest()
                cache_key = hashlib.sha256(url.encode()).hexdigest()
                cache_rel = pathlib.Path("objects") / content_hash[:2] / content_hash
                cache_path = self.root / "source_cache" / cache_rel
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(raw)
                self.index[cache_key] = {
                    "byte_size": len(raw), "cache_path": cache_rel.as_posix(),
                    "content_hash": content_hash, "source_url": url,
                    "source_system": "taxonomy", "media_type": "application/xml",
                }
                self.taxonomy_pins.append({
                    "form": form, "reporting_year": year,
                    "taxonomy_version": version,
                    "evidence_ref": url + "#sha256=" + content_hash,
                    "cache_key": cache_key,
                    "retrieved_at": "2026-09-07T00:00:00+00:00",
                })
        dependency_row, self.dependency_expected_units = \
            _add_dependency_capture_fixture(self.root, self.index)
        additional_rows.append(dependency_row)
        _write_json(index_path, self.index)
        self.source_cache_index_identity = _ident(index_path.read_bytes())
        taxonomy_pins_path = self.root / "config" / "taxonomy_pins.json"
        _write_json(taxonomy_pins_path, {
            "schema": "ferc_taxonomy_pins_v1", "coverage_window": [2024, 2026],
            "as_of": "2026-09-07",
            "source_cache_index_sha256_at_freeze":
                self.source_cache_index_identity["sha256"],
            "policy": "synthetic exact-freeze acceptance fixture",
            "pins": self.taxonomy_pins,
        })
        self.taxonomy_pins_identity = _ident(taxonomy_pins_path.read_bytes())
        _write_json(self.additional_input_path, {
            "schema": "ferc-additional-inputs-discovered-draft-v1", "draft": True,
            "counts": {"rows": len(additional_rows)},
            "source_identities": source_identity,
            "rows": additional_rows,
        })

        annotations = []
        for i in range(7):
            fayetteville = i < 4
            annotations.append({
                "source_system": "eCollection_XBRL",
                "entity_key": "C001012" if fayetteville else "C000654",
                "filing_id": "306632" if fayetteville else "F%d" % i,
                "source_fact_id": "fact-%d" % i,
                "metric_id": "metric-%d" % i,
                "filed_text": "999999" if fayetteville else "reviewed",
                "review_status": "reviewed_open",
                "rationale": "specific source review warning",
                "evidence_ref": "source-ref-%d" % i,
                "evidence_hash": hashlib.sha256(("ann%d" % i).encode()).hexdigest(),
                "reviewer": "review process", "reviewed_at": "2026-09-07",
                "applied_count": 0,
            })
        self.annotations = annotations
        _write_json(self.root / "config" / "annotations" /
                    "reviewed_source_annotations.json",
                    {"annotation_set_version": "1.1.0", "annotations": annotations})

        # Minimal files spanning the exact production code/input snapshot sets.
        for rel in ("run.py", "build_crosswalk.py", "build_field_status.py",
                    "exporters.py", "validate.py", "ferclib/synthetic.py",
                    "adapters/capacity.py", "migrations/001.sql",
                    "ferclib/image_ocr.py", "tools/vision_ocr.swift",
                    "tests/test_synthetic.py", "acceptance/test_synthetic.py"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic fixture for %s\n" % rel, encoding="utf-8")
        for rel in ("inputs/day3/identity.txt", "inputs/audit_baseline/identity.txt"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rel + "\n", encoding="utf-8")

        _write_csv(
            self.root / "config" / "metric_registry.csv", ("metric_id", "adapter"),
            ([{"metric_id": metric_id, "adapter": "lng"}
              for metric_id in sorted(records.LNG_NAMED_FILER_METRICS)] +
             [{"metric_id": metric_id, "adapter": "ioc"}
              for metric_id in (
                  "ioc_affiliate_share", "ioc_contracted_storage_quantity",
                  "ioc_expiry_profile", "ioc_firm_transport_mdq",
                  "ioc_identity_coverage", "ioc_mdq_change", "ioc_points",
                  "ioc_top5_shipper_concentration")]))

        seed_source = (ROOT / "inputs" / "official_ferc" / "elibrary" /
                       "rate_proceedings_v1.csv")
        seed_path = (self.root / "inputs" / "official_ferc" / "elibrary" /
                     "rate_proceedings_v1.csv")
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed_path.write_bytes(seed_source.read_bytes())
        seed_identity = _ident(seed_path.read_bytes())

        lng_seed_source = (ROOT / "inputs" / "official_ferc" / "elibrary" /
                           "lng_facility_sources_v1.csv")
        lng_seed_path = (self.root / "inputs" / "official_ferc" / "elibrary" /
                         "lng_facility_sources_v1.csv")
        lng_seed_path.write_bytes(lng_seed_source.read_bytes())
        lng_seed_identity = _ident(lng_seed_path.read_bytes())

        plan = {
            "schema": "ferc_operating_assets_run_plan_v2", "cache_only": True,
            "built_at": "2026-09-09T18:02:08+00:00",
            "required_inputs": [
                {"path": "config/universe.csv", **_ident(universe)},
                {"path": "inputs/official_ferc/elibrary/rate_proceedings_v1.csv",
                 **seed_identity},
                {"path": "inputs/official_ferc/elibrary/lng_facility_sources_v1.csv",
                 **lng_seed_identity},
                {"path": "source_cache/index.json",
                 **self.source_cache_index_identity},
                {"path": "config/taxonomy_pins.json",
                 **self.taxonomy_pins_identity},
            ],
            "steps": [{
                "id": "build_current_universe",
                "inputs": [
                    "config/universe.csv",
                    "inputs/official_ferc/elibrary/rate_proceedings_v1.csv",
                    "inputs/official_ferc/elibrary/lng_facility_sources_v1.csv",
                ],
            }, {
                "id": "generate_and_measure_coverage",
                "depends_on": ["build_current_universe"],
                "inputs": ["config/taxonomy_pins.json", "source_cache/index.json"],
                "outputs": [{"name": "coverage generation evidence",
                             "path": "@output/verification/coverage_generation.json"}],
            }],
        }
        for declaration in (
                dependency_row["capture_manifest"],
                dependency_row["pre_capture_cache_index"],
                dependency_row["capture_log"]):
            plan["required_inputs"].append({
                "path": declaration["path"], "bytes": declaration["bytes"],
                "sha256": declaration["sha256"]})
        plan["steps"][0]["inputs"].append(
            dependency_row["capture_manifest"]["path"])
        _write_json(self.root / "config" / "run_plan.json", plan)
        review_fixture_path = (self.root / "evidence" / "test_fixtures" /
                               "reviewed_image_sources.json")
        _write_json(review_fixture_path, {
            "schema": "ferc-reviewed-image-sources-v1",
            "provenance": {"method": "independently inspected complete page image",
                           "limitation": "must not be called a fresh visual review"},
            "sources": self.a17_sources,
        })
        gate_sources = []
        for fixed in self.a17_sources:
            source_sha = fixed["source_object_sha256"]
            source_path = self.root / "source_cache" / "objects" / source_sha[:2] / source_sha
            gate_sources.append({
                "accession": fixed["accession"], "entity": fixed["entity"],
                "status": "verified",
                "source_object": {"path": str(source_path), "bytes": fixed["source_bytes"],
                                  "sha256": source_sha, "pdf_magic_valid": True},
                "pipeline_extractor": {"pages": 1, "rows": 0, "text_layer": False},
                "automatic_ocr": {"pages": 1, "rows": 12,
                                  "credential_environment_forwarded": False},
                "reviewed_transcription": {
                    "manual_review": True, "ocr_used": True,
                    "ocr_checked_against_manual_review": True,
                    "figures": fixed["expected"]},
                "page_1_render": {"sha256": fixed["render_sha256"], "width": 1530,
                                  "height": 1980, "signature_valid": True,
                                  "crc_valid": True, "render_retained": False},
            })
        gate = {
            "schema": "ferc-a17-reviewed-image-gate-v1", "status": "verified",
            "errors": [],
            "summary": {"required_sources": 2, "verified_sources": 2,
                        "failed_sources": 0, "error_count": 0},
            "claim_boundary": {"network_used": False, "ocr_used": True,
                               "automatic_image_value_extraction_claimed": True,
                               "renders_retained": False},
            "inputs": {
                "source_cache_index": {"path": str(index_path),
                                       **_ident(index_path.read_bytes())},
                "review_fixture": {"path": str(review_fixture_path),
                                   **_ident(review_fixture_path.read_bytes())},
                "capacity_adapter": {"path": str(self.root / "adapters/capacity.py"),
                                     **_ident((self.root / "adapters/capacity.py").read_bytes())},
                "image_ocr_module": {"path": str(self.root / "ferclib/image_ocr.py"),
                                    **_ident((self.root / "ferclib/image_ocr.py").read_bytes())},
                "vision_ocr_source": {"path": str(self.root / "tools/vision_ocr.swift"),
                                     **_ident((self.root / "tools/vision_ocr.swift").read_bytes())},
            },
            "sources": gate_sources,
        }
        _write_json(self.root / "verification" / "a17_reviewed_image_gate.json", gate)

    def _insert(self, con, table, row):
        cols = set(r[1] for r in con.execute('PRAGMA table_info("%s")' % table))
        row = {k: v for k, v in row.items() if k in cols}
        con.execute('INSERT INTO "%s"(%s) VALUES(%s)' % (
            table, ",".join('"%s"' % k for k in row),
            ",".join("?" for _ in row)), tuple(row.values()))

    def _observation(self, con, observation_id, accession, entity, source_system="eLibrary",
                     filing_id=None, fact_id=None, metric=None, value="1", warning=False):
        self._insert(con, "observations", {
            "observation_id": observation_id, "entity_key": entity,
            "metric_id": metric or observation_id, "source_regime": "Form 549B IOC",
            "period_basis": "snapshot", "instant_date": "2026-01-01",
            "reporting_year": 2026, "reporting_period": "Q1",
            "scope": "legal entity", "scope_rule": "actual scope required",
            "unit": "Dth/day", "value_text": value, "value_num": float(value),
            "availability": "present", "origin": "source_native",
            "method": "reported", "version_status": "original", "validation": "pass",
            "source_system": source_system, "filing_id": filing_id or accession,
            "source_fact_id": fact_id or ("fact-" + observation_id),
            "accession_number": accession, "qa_flags": "review warning" if warning else "",
            "review_status": "reviewed_open" if warning else "",
            "first_seen_at": "2026-09-09T00:00:00+00:00",
            "updated_at": "2026-09-09T00:00:00+00:00",
        })

    def _make_filing_association_gate_data(self, con):
        """Add a compact valid population for the R28/R29 Build-A gates."""
        with (ROOT / "config" / "universe.csv").open(newline="", encoding="utf-8") as f:
            universe = {row["asset_id"]: row for row in csv.DictReader(f)}
        authority_rows = lng.reviewed_asset_docket_rows(ROOT / "config" / "universe.csv")
        inserted_entities = set()
        inserted_assets = set()
        for authority in authority_rows:
            source = universe[authority["asset_id"]]
            entity = source["entity_key"]
            if entity not in inserted_entities:
                self._insert(con, "entities", {
                    "entity_key": entity,
                    "cid": entity if entity.startswith("C") else None,
                    "local_key": None if entity.startswith("C") else entity,
                    "legal_name": source["entity_name"], "parent": source["parent"],
                    "ticker": source["ticker"],
                })
                inserted_entities.add(entity)
            if source["asset_id"] not in inserted_assets:
                self._insert(con, "assets", {
                    "asset_id": source["asset_id"], "ticker": source["ticker"],
                    "display_name": source["display_name"],
                    "template": source["template"],
                    "group_key": source.get("group_key") or source["asset_id"],
                    "status": source["status"],
                })
                self._insert(con, "asset_entity_map", {
                    "asset_id": source["asset_id"], "entity_key": entity,
                    "mapping_scope": source["mapping_scope"],
                })
                inserted_assets.add(source["asset_id"])
            self._insert(con, "asset_dockets", authority)

        missing_lng = {
            "20070216-3045": (
                "NO-FERC-CID:Gulf LNG Energy, LLC", "gulf-lng",
                ("CP06-12", "CP06-13", "CP06-14")),
            "20160601-4008": (
                "NO-FERC-CID:Elba Liquefaction Company, L.L.C.", "elba",
                ("CP14-103", "CP14-115")),
            "20120416-3033": (
                "NO-FERC-CID:Sabine Pass Liquefaction, LLC", "sabine-pass",
                ("CP11-72",)),
            "20141230-3043": (
                "NO-FERC-CID:Corpus Christi Liquefaction, LLC", "corpus-christi",
                ("CP12-507",)),
            "20041221-3094": (
                "NO-FERC-CID:Sabine Pass LNG, L.P.", "sabine-pass",
                ("CP04-47",)),
            "20070920-3066": ("C000039", "elba", ("CP06-470",)),
        }
        for accession, (entity, _facility, dockets) in missing_lng.items():
            content_hash = hashlib.sha256(("lng:" + accession).encode()).hexdigest()
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": entity, "form": "eLibrary document",
                "accession_number": accession, "reporting_year": 2026,
                "reporting_period": "as_of", "content_hash": content_hash,
                "is_canonical": 1, "version_status": "original",
            })
            self._insert(con, "documents", {
                "document_id": "lng-document-" + accession,
                "source_system": "eLibrary", "filing_id": accession,
                "accession_number": accession, "availability": "retrieved",
                "byte_size": 1000, "content_hash": content_hash,
            })
            for docket in dockets:
                self._insert(con, "filing_dockets", {
                    "source_system": "eLibrary", "filing_id": accession,
                    "docket": docket,
                })
            self._observation(con, "lng-required-" + accession, accession, entity)

        correct_submissions = {
            "20240903-5081": ("C010176", ("IS24-802",)),
            "20260522-5206": ("C008985", ("IS26-292",)),
            "20260522-5237": ("C000832", ("IS26-296",)),
            "20240918-5014": ("C000629", ("IS24-804",)),
            "20260414-5238": ("C000231", ("RP26-999",)),
            "20260522-5194": ("C000606", ("IS26-291",)),
            "20201103-5131": ("C004609", ("IS20-999",)),
            # Synthetic target-owned occurrences exercise both restored docket
            # contracts without pretending they are source identities.
            "SYNTHETIC-C005518-IS23-573": ("C005518", ("IS23-573",)),
            "SYNTHETIC-C005518-IS25-632": ("C005518", ("IS25-632",)),
            "SYNTHETIC-C000606-IS26-220": ("C000606", ("IS26-220",)),
        }
        for accession, (entity, dockets) in correct_submissions.items():
            content_hash = hashlib.sha256(("rate:" + accession).encode()).hexdigest()
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": entity, "form": "eLibrary document",
                "accession_number": accession, "reporting_year": 2026,
                "reporting_period": "as_of", "content_hash": content_hash,
                "is_canonical": 1, "version_status": "original",
            })
            self._insert(con, "documents", {
                "document_id": "rate-document-" + accession,
                "source_system": "eLibrary", "filing_id": accession,
                "accession_number": accession, "availability": "retrieved",
                "byte_size": 1000, "content_hash": content_hash,
            })
            for docket in dockets:
                self._insert(con, "filing_dockets", {
                    "source_system": "eLibrary", "filing_id": accession,
                    "docket": docket,
                })
        self._observation(
            con, "correct-oneok-ngl-5194", "20260522-5194", "C000606")

        commission_entities = (
            "C000606", "C000629", "C000832", "C001049", "C004609",
            "C008985", "C010798",
        )
        commission_accession = "20251029-3064"
        self._insert(con, "filings", {
            "source_system": "eLibrary", "filing_id": commission_accession,
            "entity_key": commission_entities[0], "form": "eLibrary document",
            "accession_number": commission_accession, "reporting_year": 2025,
            "reporting_period": "as_of", "content_hash": hashlib.sha256(
                commission_accession.encode()).hexdigest(),
            "is_canonical": 1, "version_status": "original",
        })
        self._insert(con, "documents", {
            "document_id": "commission-document-" + commission_accession,
            "source_system": "eLibrary", "filing_id": commission_accession,
            "accession_number": commission_accession,
            "availability": "retrieved", "byte_size": 1000,
            "content_hash": hashlib.sha256(commission_accession.encode()).hexdigest(),
        })
        for number in range(1, 10):
            self._insert(con, "filing_dockets", {
                "source_system": "eLibrary", "filing_id": commission_accession,
                "docket": "IS25-%03d" % number,
            })

        # A valid cross-entity LNG observation proves that the finalizer does
        # not obtain a pass by rejecting every shared occurrence.
        shared_accession = "20260813-5004"
        shared_anchor = "C000039"
        shared_target = "NO-FERC-CID:Elba Liquefaction Company, L.L.C."
        self._insert(con, "filings", {
            "source_system": "eLibrary", "filing_id": shared_accession,
            "entity_key": shared_anchor, "form": "eLibrary document",
            "accession_number": shared_accession, "reporting_year": 2026,
            "reporting_period": "as_of", "content_hash": hashlib.sha256(
                shared_accession.encode()).hexdigest(),
            "is_canonical": 1, "version_status": "original",
        })
        self._insert(con, "documents", {
            "document_id": "shared-lng-document", "source_system": "eLibrary",
            "filing_id": shared_accession, "accession_number": shared_accession,
            "availability": "retrieved", "byte_size": 1000,
            "content_hash": hashlib.sha256(shared_accession.encode()).hexdigest(),
        })
        self._insert(con, "filing_dockets", {
            "source_system": "eLibrary", "filing_id": shared_accession,
            "docket": "CP14-103",
        })
        self._observation(
            con, "shared-elba-operating", shared_accession, shared_target,
            metric="lng_operational_report")

        # Exact production-shaped R36 population.  These deterministic IDs are
        # acceptance records only; the adapter still derives them from source
        # occurrences in a real build.
        r36_filings = {
            "20161012-3036": {
                "anchor": "NO-FERC-CID:Sabine Pass LNG, L.P.",
                "target": "NO-FERC-CID:Sabine Pass Liquefaction, LLC",
                "facility": "sabine-pass", "dockets": ("CP11-72",),
            },
            "20241121-3047": {
                "anchor": "C000039",
                "target": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
                "facility": "elba", "dockets": ("CP23-375", "CP23-375-000"),
            },
            "20241122-3097": {
                "anchor": "C000039",
                "target": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
                "facility": "elba", "dockets": ("CP23-375", "CP23-375-000"),
            },
            "20140310-5158": {
                "anchor": "C000039",
                "target": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
                "facility": "elba", "dockets": ("CP14-103",),
            },
            "20110131-5063": {
                "anchor": "NO-FERC-CID:Sabine Pass LNG, L.P.",
                "target": "NO-FERC-CID:Sabine Pass Liquefaction, LLC",
                "facility": "sabine-pass", "dockets": ("CP11-72",),
            },
            "20230428-5621": {
                "anchor": "C000039",
                "target": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
                "facility": "elba", "dockets": ("CP14-103", "CP23-375"),
            },
        }
        for accession, spec in r36_filings.items():
            content_hash = hashlib.sha256(("r36:" + accession).encode()).hexdigest()
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": spec["anchor"], "form": "eLibrary document",
                "accession_number": accession,
                "reporting_year": int(accession[:4]), "reporting_period": "as_of",
                "content_hash": content_hash, "is_canonical": 1,
                "version_status": "original",
                "retrieved_at": ("2026-09-07T11:37:50+00:00"
                                 if accession == "20241121-3047" else None),
            })
            for docket in spec["dockets"]:
                self._insert(con, "filing_dockets", {
                    "source_system": "eLibrary", "filing_id": accession,
                    "docket": docket,
                })
            retrieved = accession == "20241121-3047"
            document_id = (
                "eLibrary|20241121-3047|34D684F7-4D15-C02F-94DB-934F79000000"
                if retrieved else "eLibrary|%s|listing" % accession)
            self._insert(con, "documents", {
                "document_id": document_id, "source_system": "eLibrary",
                "filing_id": accession, "accession_number": accession,
                "attachment_id": ("34D684F7-4D15-C02F-94DB-934F79000000"
                                  if retrieved else ""),
                "availability": "retrieved" if retrieved else "not_retrieved",
                "byte_size": 1000 if retrieved else None,
                "content_hash": content_hash if retrieved else None,
                "retrieved_at": ("2026-09-07T11:37:50+00:00"
                                 if retrieved else None),
            })
            if retrieved:
                self._insert(con, "documents", {
                    "document_id": "eLibrary|20241121-3047|listing",
                    "source_system": "eLibrary", "filing_id": accession,
                    "accession_number": accession, "attachment_id": "",
                    "availability": "not_retrieved", "content_hash": None,
                    "retrieved_at": "2026-09-07T11:37:50+00:00",
                })

        r36_rows = {
            "obs-3f6a3dceba0be24e00757ab2dfe9e01b":
                ("20161012-3036", "lng_status_authorised", "2016-10-12", "original"),
            "obs-46bac3c37b38fc21dc86d98ff48ee868":
                ("20241121-3047", "lng_liquefaction_capacity", "2024-11-21", "original"),
            "obs-5901fa2a4130d36afd0ff21200404a33":
                ("20241122-3097", "lng_material_order", "2024-11-22", "original"),
            "obs-9a232e39e00dcb2d4db28be69b8ed199":
                ("20241121-3047", "lng_liquefaction_capacity", "2024-11-21", "superseded"),
            "obs-dcd1fb5a7fe5a9cfcb3dd69a07e64caa":
                ("20140310-5158", "lng_status_requested", "2014-03-10", "original"),
            "obs-dfb107168e7dbd978a6daaa98d00cdb1":
                ("20110131-5063", "lng_status_requested", "2011-01-31", "original"),
            "obs-e00c7ed8756114bf836a0e1086366774":
                ("20230428-5621", "lng_status_requested", "2023-04-28", "original"),
            "obs-e7409e9a31f26e658e839fd1d41cf67e":
                ("20241121-3047", "lng_material_order", "2024-11-21", "original"),
        }
        for observation_id, (accession, metric, instant, version) in r36_rows.items():
            spec = r36_filings[accession]
            self._observation(
                con, observation_id, accession, spec["target"], metric=metric)
            document_id = (
                "eLibrary|20241121-3047|34D684F7-4D15-C02F-94DB-934F79000000"
                if accession == "20241121-3047"
                else "eLibrary|%s|listing" % accession)
            con.execute(
                "UPDATE observations SET instant_date=?,version_status=?,document_id=?,"
                "scope=? WHERE observation_id=?",
                (instant, version, document_id,
                 "R36 exact occurrence " + observation_id, observation_id))

        for number, (entity, metric) in enumerate(sorted(records.R36_ROUTE_PAIRS), 1):
            slot_id = "slot-r36-%02d" % number
            self._insert(con, "coverage_expected", {
                "slot_id": slot_id, "entity_key": entity, "template": "lng",
                "metric_id": metric, "source_regime": "eLibrary document",
                "period_basis": "event", "scope": "reviewed facility route",
                "unit_rule": "status-or-filed-unit", "requirement": "REQUIRED",
                "requirement_evidence": "official reviewed LNG filing route",
                "frozen_at": "2026-09-09T18:02:08+00:00",
                "frozen_run_id": "coverage-r36-fixture", "slot_state": "slot_open",
                "source_health": "ok",
                "denominator_origin": "document_adapter_anchor",
            })
            self._insert(con, "coverage_measured", {
                "slot_id": slot_id, "run_id": "coverage-r36-fixture",
                "outcome": "missing", "populated": 0, "source_matched": 0,
                "validated": 0, "in_review": 0, "candidates_refused": 0,
            })

        # filing_entities has entity foreign keys even though legacy filings did
        # not. Populate every synthetic legal identity before relationships.
        filing_entities = {str(row[0]) for row in con.execute(
            "SELECT DISTINCT entity_key FROM filings")}
        filing_entities.update(commission_entities)
        filing_entities.update((shared_anchor, shared_target))
        existing_entities = {str(row[0]) for row in con.execute(
            "SELECT entity_key FROM entities")}
        for entity in sorted(filing_entities - existing_entities):
            self._insert(con, "entities", {
                "entity_key": entity,
                "cid": entity if entity.startswith("C") else None,
                "local_key": None if entity.startswith("C") else entity,
                "legal_name": "Synthetic legal entity " + entity,
            })

        special_submitters = set(correct_submissions) | set(r36_filings)
        for filing in con.execute(
                "SELECT source_system,filing_id,entity_key FROM filings "
                "ORDER BY source_system,filing_id"):
            filing_id = str(filing["filing_id"])
            if filing_id == commission_accession:
                continue
            role = "named_filer" if filing_id in special_submitters else "source_entity"
            facility = ""
            if filing_id in missing_lng:
                role = "named_filer"
                facility = missing_lng[filing_id][1]
            elif filing_id in r36_filings:
                role = "named_filer"
                facility = r36_filings[filing_id]["facility"]
            self._insert(con, "filing_entities", {
                "source_system": filing["source_system"], "filing_id": filing_id,
                "entity_key": filing["entity_key"], "association_role": role,
                "facility_key": facility,
                "evidence_ref": "synthetic occurrence evidence:" + filing_id,
            })
        for entity in commission_entities:
            self._insert(con, "filing_entities", {
                "source_system": "eLibrary", "filing_id": commission_accession,
                "entity_key": entity, "association_role": "commission_docket_subject",
                "facility_key": "",
                "evidence_ref": "synthetic Commission order evidence:" + entity,
            })
        # Replace the generic shared-anchor role, then add its second named filer.
        con.execute(
            "UPDATE filing_entities SET association_role='named_filer',facility_key='elba' "
            "WHERE source_system='eLibrary' AND filing_id=? AND entity_key=?",
            (shared_accession, shared_anchor))
        self._insert(con, "filing_entities", {
            "source_system": "eLibrary", "filing_id": shared_accession,
            "entity_key": shared_target, "association_role": "named_filer",
            "facility_key": "elba", "evidence_ref": "synthetic shared Elba evidence",
        })
        for accession, spec in r36_filings.items():
            self._insert(con, "filing_entities", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": spec["target"], "association_role": "named_filer",
                "facility_key": spec["facility"],
                "evidence_ref": "synthetic exact R36 occurrence:" + accession,
            })

    def _make_ioc_r38_r39_gate_data(self, con):
        """Exact sparse Build-A records for the IOC validator repair gates.

        R38 binds the shipped validator to an actual three-character point code
        occurrence.  R39 preserves the two blank item-yh source rows and the
        file-level census, while deliberately omitting the defective blank-code
        per-code observation and population.  The fixture's file-level
        population contains the two source rows relevant to the regression;
        ``candidate_count`` retains the production occurrence's complete count.
        """
        extended_accession = "20250808-5173"
        extended_observation = "obs-65199973861961ad89dda4a2f43be6f5"
        extended_population = "pop-4637ebf709b91d5a98cb86dffd9945d9"
        self._insert(con, "filings", {
            "source_system": "eLibrary", "filing_id": extended_accession,
            "entity_key": "C000255", "form": "Form 549B IOC",
            "accession_number": extended_accession, "reporting_year": 2025,
            "reporting_period": "Q3", "snapshot_date": "2025-07-01",
            "content_hash": hashlib.sha256(
                ("r38:" + extended_accession).encode()).hexdigest(),
            "is_canonical": 1, "version_status": "revised",
            "data_origin": "structured_bulk",
        })
        self._insert(con, "source_facts", {
            "source_system": "eLibrary", "filing_id": extended_accession,
            "source_fact_id": "P00005", "document_order": 4,
            "concept_qname": "ferc549b:P", "concept_local": "ioc_point_record",
            "context_id": "contract0000", "unit_id": "T", "unit_text": "Dth",
            "value_as_filed": (
                "P\t130\tCHANDELEUR SOUND 51\t95\t936700\tZONE 3\t0\t\t\t\t\t\t"),
            "is_nil": 0, "period_class": "snapshot", "instant": "2025-07-01",
            "current_or_prior": "current", "typed_dims_json": json.dumps({
                "footnote_ids": "", "line": 5, "point_code": "130",
                "point_id": "936700", "point_name": "CHANDELEUR SOUND 51",
                "qualifier": "95", "storage_qty": None, "storage_qty_text": "",
                "transport_qty": 0.0, "transport_qty_text": "0", "zone": "ZONE 3",
            }, sort_keys=True),
            "taxonomy_version": "no_version_indicator_in_format",
        })
        self._insert(con, "observations", {
            "observation_id": extended_observation, "entity_key": "C000255",
            "metric_id": "ioc_points", "source_regime": "Form 549B IOC",
            "period_basis": "snapshot", "instant_date": "2025-07-01",
            "reporting_year": 2025, "reporting_period": "Q3",
            "period_label": "2025Q3 snapshot 2025-07-01",
            "scope": ("location detail only | point code 130 "
                      "(code not in the Form 549B manual's table)"),
            "unit": "codes",
            "value_text": "130 (code not in the Form 549B manual's table)",
            "availability": "present", "origin": "elibrary_document",
            "method": "filed", "version_status": "revised",
            "validation": "scope_incompatible", "source_system": "eLibrary",
            "filing_id": extended_accession, "accession_number": extended_accession,
            "candidate_count": 1, "selector": "ioc_p_records",
            "qa_flags": ("CATEGORICAL: this snapshot carries the NAESB point code 130; "
                         "the 1 record that carries it is candidate_count"),
        })
        self._insert(con, "lineage_populations", {
            "population_id": extended_population,
            "observation_id": extended_observation,
            "source_system": "eLibrary", "source_table": "source_facts",
            "filing_ids": json.dumps([extended_accession]),
            "inclusion_rule": ("P records of this filing occurrence whose item yh "
                               "(point code) is 130 -- code not in the Form 549B "
                               "manual's table"),
            "exclusion_rule": ("the other 2016 P record(s) carry a different point "
                               "code; S8 and S9 are segment endpoints and are never "
                               "folded into the receipt code M2"),
            "row_count": 1, "candidate_count": 2017, "excluded_count": 2016,
            "member_key": "filing_id:source_fact_id",
            "member_digest": (
                "f4010d74ac6a8849ae3b8fd3f7dbab525cf2919bc5e0d6cae33ac6d6b0d5d336"),
            "members_sample": json.dumps([extended_accession + ":P00005"]),
            "aggregate_unit": "codes",
            "note": json.dumps({
                "detail": None, "purpose": "point_records::130",
                "schema": "ioc_population_note_v1",
            }, sort_keys=True, separators=(",", ":")),
            "created_at": "2026-09-09T00:00:00Z",
        })
        self._insert(con, "lineage_edges", {
            "observation_id": extended_observation, "input_order": 1,
            "input_role": "population", "operator_sign": "+", "coefficient": 1,
            "input_source_system": "eLibrary",
            "input_filing_id": extended_accession, "input_concept": "ioc_points",
            "input_period": "2025-07-01", "input_value": "1",
            "input_unit": "codes", "input_population_id": extended_population,
        })
        self._insert(con, "lineage_edges", {
            "observation_id": extended_observation, "input_order": 2,
            "input_role": "contributing_row", "operator_sign": "+",
            "coefficient": 1, "input_source_system": "eLibrary",
            "input_filing_id": extended_accession,
            "input_source_fact_id": "P00005", "input_context_id": "contract0000",
            "input_concept": "ioc_points", "input_period": "2025-07-01",
            "input_value": "130", "input_unit": "codes",
            "input_population_id": extended_population,
        })

        blank_accession = "20251001-5149"
        head_observation = "obs-afaef0b47b3e803331ec404a0c7d6469"
        head_population = "pop-a434fa603aab5f5b5334f75104c094b2"
        self._insert(con, "filings", {
            "source_system": "eLibrary", "filing_id": blank_accession,
            "entity_key": "C000654", "form": "Form 549B IOC",
            "accession_number": blank_accession, "reporting_year": 2025,
            "reporting_period": "Q4", "snapshot_date": "2025-10-01",
            "content_hash": hashlib.sha256(
                ("r39:" + blank_accession).encode()).hexdigest(),
            "is_canonical": 1, "version_status": "original",
            "data_origin": "structured_bulk",
        })
        blank_members = []
        # The gate deliberately requires the complete 1,821-row occurrence,
        # not merely the two rows involved in the defect.  Nonblank fixture
        # codes are categorical stand-ins; the two audited blank identities and
        # their original filed text are exact.
        nonblank_index = 0
        for line in range(1, 1822):
            fact_id = "P%05d" % line
            context = "contract%04d" % ((line - 1) // 4)
            is_blank = fact_id in {"P01406", "P01457"}
            if is_blank:
                point_code = ""
            else:
                nonblank_index += 1
                point_code = ("S9" if nonblank_index <= 1086 else
                              "S8" if nonblank_index <= 1666 else "WR")
            self._insert(con, "source_facts", {
                "source_system": "eLibrary", "filing_id": blank_accession,
                "source_fact_id": fact_id, "document_order": line - 1,
                "concept_qname": "ferc549b:P",
                "concept_local": "ioc_point_record", "context_id": context,
                "unit_id": "T", "unit_text": "Dth",
                "value_as_filed": (
                    "P\t\t\t\t\t\t\t\t\t\t\t\t\t" if is_blank else
                    "P\t%s\tSYNTHETIC POINT\t95\t%d\tZONE\t0\t\t\t\t\t\t" %
                    (point_code, line)),
                "is_nil": 0, "period_class": "snapshot", "instant": "2025-10-01",
                "current_or_prior": "current", "typed_dims_json": json.dumps({
                    "footnote_ids": "", "line": line, "point_code": point_code,
                    "point_id": "" if is_blank else str(line),
                    "point_name": "" if is_blank else "SYNTHETIC POINT",
                    "qualifier": "" if is_blank else "95",
                    "storage_qty": None, "storage_qty_text": "",
                    "transport_qty": None if is_blank else 0.0,
                    "transport_qty_text": "" if is_blank else "0",
                    "zone": "" if is_blank else "ZONE",
                }, sort_keys=True),
                "taxonomy_version": "no_version_indicator_in_format",
            })
            blank_members.append(blank_accession + ":" + fact_id)
        self._insert(con, "observations", {
            "observation_id": head_observation, "entity_key": "C000654",
            "metric_id": "ioc_points", "source_regime": "Form 549B IOC",
            "period_basis": "snapshot", "instant_date": "2025-10-01",
            "reporting_year": 2025, "reporting_period": "Q4",
            "period_label": "2025Q4 snapshot 2025-10-01",
            "scope": "location detail only", "unit": "codes",
            "value_text": "S9=1086, S8=580, WR=153",
            "availability": "present", "origin": "elibrary_document",
            "method": "filed", "version_status": "original",
            "validation": "scope_incompatible", "source_system": "eLibrary",
            "filing_id": blank_accession, "accession_number": blank_accession,
            "candidate_count": 1821, "selector": "ioc_p_records",
            "qa_flags": ("1821 P records retained for location detail, by NAESB "
                         "point code: S9=1086, S8=580, WR=153; 2 P record(s) have "
                         "blank item yh; they remain in source_facts and the file-level "
                         "population but are not promoted to a present point-code "
                         "observation; every P record is retained in source_facts"),
        })
        self._insert(con, "lineage_populations", {
            "population_id": head_population, "observation_id": head_observation,
            "source_system": "eLibrary", "source_table": "source_facts",
            "filing_ids": json.dumps([blank_accession]),
            "inclusion_rule": ("every P (point) record of this filing occurrence, "
                               "whatever its NAESB point code"),
            "exclusion_rule": ("no P record is dropped; D, A, F, H and unclassified "
                               "rows are not point records and never contribute"),
            "row_count": 1821, "candidate_count": 1821, "excluded_count": 0,
            "member_key": "filing_id:source_fact_id",
            "member_digest": hashlib.sha256(
                "\n".join(sorted(blank_members)).encode()).hexdigest(),
            "members_sample": json.dumps(sorted(blank_members)[:25]),
            "aggregate_unit": "codes",
            "note": json.dumps({
                "detail": None, "purpose": "point_records",
                "schema": "ioc_population_note_v1",
            }, sort_keys=True, separators=(",", ":")),
            "created_at": "2026-09-09T00:00:00Z",
        })
        self._insert(con, "lineage_edges", {
            "observation_id": head_observation, "input_order": 1,
            "input_role": "population", "operator_sign": "+", "coefficient": 1,
            "input_source_system": "eLibrary", "input_filing_id": blank_accession,
            "input_concept": "ioc_points", "input_period": "2025-10-01",
            "input_value": "1821", "input_unit": "codes",
            "input_population_id": head_population,
        })

    def _make_ioc_r40_gate_data(self, con):
        """Exact repaired zero-point census rows exposed by the v8 replay.

        Each Fayetteville occurrence contains its native header but no P facts.
        The file-head observation therefore remains an explicit ``source_blank``
        with a numeric zero candidate census, not a present zero-valued metric.
        """
        rows = (
            ("20240102-5176", "obs-4952218c315d35d13923a19928873a83",
             "pop-bb8b4359313d73868e270683227f4d68", 2024, "Q1",
             "2024-01-01", "2024-01-02",
             "90bb481e3374e4ff6d40448c7afe4cda8ac189589b2c90abda6cfc2fe889d78a"),
            ("20240401-5209", "obs-3dc9e7eae0b8008049165c259339ab49",
             "pop-eafab6ea046d9e8f6ec28f12b4adc012", 2024, "Q2",
             "2024-04-01", "2024-04-01",
             "9c281970a7044abe08e0693508b377d5f76d4bc37a49dbbbbfa83aa858fac792"),
            ("20240701-5063", "obs-5857c0d81b6cb9521ba5903b5db99dd8",
             "pop-2f598b9085ae87cce49c094dfa05521e", 2024, "Q3",
             "2024-07-01", "2024-07-01",
             "afcc62ca8f8396118a213e398169898d87ef415c9d396e0d12a93665ddd25f76"),
            ("20241001-5081", "obs-63b68edeb06f8bf92458b22d2a5f3714",
             "pop-88a0e1954b9c67673b9d2c20e2c79ae8", 2024, "Q4",
             "2024-10-01", "2024-10-01",
             "e38bc53d52786511b10876b56df41493d3bb1f3e458e0751cba1805dfabb7df3"),
            ("20250102-5082", "obs-ede690fc9f63f1783e6ca01662170c29",
             "pop-a2c6f27116184d8ebd7e7de3d61676ff", 2025, "Q1",
             "2025-01-01", "2025-01-02",
             "791b68d0d6e8f48a3635e3b853871dba39856bc29137d5317849b4aeed485e30"),
            ("20250401-5104", "obs-f2df879fa747f7ad19a16d1616be6a68",
             "pop-0e9ef18bcb1a2b7ee2a035373da36d4c", 2025, "Q2",
             "2025-04-01", "2025-04-01",
             "09f3a6b56eac66fe0ea876ed938152f4ddb17b0ac1d49f0762eb6e14b0f02061"),
            ("20250701-5084", "obs-df8b7ff849d23fa438e54956688dcee9",
             "pop-43500ebb4d38040fc83ae0e781e7b721", 2025, "Q3",
             "2025-07-01", "2025-07-01",
             "e4d150d4ec7ea8bb63d6915134a458743d4ddbced846f7151ecf2e76b65e92b4"),
            ("20251001-5072", "obs-6b2945622d0bb9d54a8674605fbac5ed",
             "pop-97d98c1550b448e38ad982e8e55f2a87", 2025, "Q4",
             "2025-10-01", "2025-10-01",
             "b877bde80a69340ef6f5cd42033e779df8a858767496ad4f3f38a8d24c4d76c9"),
            ("20260102-5094", "obs-4ff7e05520dfb22f9254e998b947fdd2",
             "pop-589dcaa239777db28161a515d99f3001", 2026, "Q1",
             "2026-01-01", "2026-01-02",
             "58937c59840100ca8e933607bc7b75b70aea5aabe0c88bf57069b1f5226e4e0d"),
            ("20260331-5115", "obs-de4107482f3e49981de6facee9157cae",
             "pop-b8eeb1e4051dcc884fbb831b212cf9c3", 2026, "Q2",
             "2026-04-01", "2026-03-31",
             "5cf00ad1b26e392654eb3c71d229dca0fda81dbc8f93ec298c57278d3c6b4e1c"),
            ("20260701-5148", "obs-dcb182f69c8a58abd3e3b63982dd78f4",
             "pop-fd8eb70a925b2e3360a6d9758785a9bc", 2026, "Q3",
             "2026-07-01", "2026-07-01",
             "8c8732b7616aad3ad6a15e903ab6570efb2dfdd0d35dbe317262e1061369c443"),
        )
        empty_digest = hashlib.sha256(b"").hexdigest()
        for (accession, observation_id, population_id, year, quarter,
             snapshot_date, filed_date, content_hash) in rows:
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": "C001012", "form": "Form 549B IOC",
                "accession_number": accession, "reporting_year": year,
                "reporting_period": quarter, "snapshot_date": snapshot_date,
                "filed_date": filed_date, "content_hash": content_hash,
                "is_canonical": 1, "version_status": "original",
                "data_origin": "document",
            })
            self._insert(con, "source_facts", {
                "source_system": "eLibrary", "filing_id": accession,
                "source_fact_id": "H00001", "document_order": 0,
                "concept_qname": "ferc549b:H",
                "concept_local": "ioc_header_record", "context_id": "file",
                "unit_id": "B", "unit_text": "MMBtu",
                "value_as_filed": (
                    "H\tFAYETTEVILLE EXPRESS PIPELINE LLC\tC001012\t%s\tO\t%s"
                    "\tB\tB\tBlair Lichtenwalter (713) 989-2605" %
                    (filed_date, snapshot_date)),
                "is_nil": 0, "period_class": "snapshot", "instant": snapshot_date,
                "current_or_prior": "current",
                "typed_dims_json": json.dumps({
                    "entity_gate": {
                        "accession": accession, "header_cid": "C001012",
                        "header_name": "FAYETTEVILLE EXPRESS PIPELINE LLC",
                        "requested_entity": "C001012", "result": "accepted",
                    },
                    "original_revised": "O", "pipeline_id": "C001012",
                    "pipeline_name": "FAYETTEVILLE EXPRESS PIPELINE LLC",
                    "report_date": filed_date, "snapshot_date": snapshot_date,
                    "uom_storage": "MMBtu", "uom_transport": "MMBtu",
                }, sort_keys=True),
                "taxonomy_version": "no_version_indicator_in_format",
            })
            self._insert(con, "observations", {
                "observation_id": observation_id, "entity_key": "C001012",
                "metric_id": "ioc_points", "source_regime": "Form 549B IOC",
                "period_basis": "snapshot", "instant_date": snapshot_date,
                "reporting_year": year, "reporting_period": quarter,
                "period_label": "%d%s snapshot %s" % (year, quarter, snapshot_date),
                "scope": "location detail only",
                "availability": "source_blank", "origin": "elibrary_document",
                "method": "filed", "version_status": "original",
                "validation": "pass", "source_system": "eLibrary",
                "filing_id": accession, "accession_number": accession,
                "candidate_count": 0, "selector": "ioc_p_records",
                "missing_reason": "this snapshot carries no P (point) records",
                "qa_flags": (
                    "as-of date is header item e (first day of the calendar quarter) = "
                    "%s; eLibrary filedDate %s; header indicator O; units read from "
                    "header item f/g, never assumed" % (snapshot_date, filed_date)),
            })
            self._insert(con, "lineage_populations", {
                "population_id": population_id, "observation_id": observation_id,
                "source_system": "eLibrary", "source_table": "source_facts",
                "filing_ids": json.dumps([accession]),
                "inclusion_rule": "P (point) records of this filing occurrence",
                "exclusion_rule": (
                    "no row was excluded; there was nothing to exclude"),
                "row_count": 0, "candidate_count": 0, "excluded_count": 0,
                "member_key": "filing_id:source_fact_id",
                "member_digest": empty_digest, "members_sample": json.dumps([]),
                "empty_reason": (
                    "the filing WAS retrieved and parsed and its 0 D records were "
                    "examined; it carries no P record at all. The filer published no "
                    "location detail, which is not a count of zero points on the system"),
                "note": json.dumps({
                    "detail": None, "purpose": "point_records",
                    "schema": "ioc_population_note_v1",
                }, sort_keys=True, separators=(",", ":")),
                "created_at": "2026-09-09T00:00:00Z",
            })
            self._insert(con, "lineage_edges", {
                "observation_id": observation_id, "input_order": 1,
                "input_role": "population", "operator_sign": "+",
                "coefficient": 1, "input_source_system": "eLibrary",
                "input_filing_id": accession, "input_concept": "ioc_points",
                "input_period": snapshot_date, "input_value": "0",
                "input_population_id": population_id,
            })

    def _make_w5_horizon_counterevidence(self, con):
        for fact_id, accession in sorted(
                records.W5_HORIZON_ATTACHED_UNIT_FACTS.items()):
            content_hash = hashlib.sha256(("horizon:" + accession).encode()).hexdigest()
            document_id = "eLibrary|%s|horizon-fixture" % accession
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": "C000226", "form": "Form 549B Capacity",
                "accession_number": accession, "reporting_year": int(accession[:4]),
                "reporting_period": "annual", "content_hash": content_hash,
                "is_canonical": 1, "version_status": "original",
            })
            self._insert(con, "documents", {
                "document_id": document_id, "source_system": "eLibrary",
                "filing_id": accession, "accession_number": accession,
                "attachment_id": "horizon-fixture", "media_type": "application/pdf",
                "byte_size": 1000, "content_hash": content_hash,
                "availability": "retrieved",
            })
            self._insert(con, "document_facts", {
                "document_fact_id": fact_id, "document_id": document_id,
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": "C000226", "assertion_type": "reported_capacity",
                "metric_id": "cap_reported_capacity", "value_text": "380,000",
                "value_num": 380000, "unit": "MMBTU/day",
                "scope_note": "Horizon Pipeline firm transportation service",
                "verbatim_span": "Firm Transportation Service FTS 380,000MMBTU/d",
                "extraction_method": "pdf_text_span", "content_hash": content_hash,
                "confidence": "verified_span", "review_state": "reviewed",
            })

    def _make_capacity_semantic_gate_data(self, con):
        """Exact R44 blocked-capacity population plus matched slot evidence."""
        occurrences = {
            "20250225-5101": ("C001685", 2024),
            "20250227-5034": ("C000584", 2024),
            "20260202-5047": ("C001506", 2025),
        }
        documents = {}
        for accession, (entity, reporting_year) in occurrences.items():
            content_hash = hashlib.sha256(
                ("r44-capacity:" + accession).encode()).hexdigest()
            document_id = "eLibrary|%s|r44-capacity-fixture" % accession
            documents[accession] = (document_id, content_hash)
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": entity, "form": "Form 549B Capacity",
                "accession_number": accession,
                "reporting_year": reporting_year,
                "reporting_period": "annual", "content_hash": content_hash,
                "is_canonical": 1, "version_status": "original",
            })
            self._insert(con, "documents", {
                "document_id": document_id, "source_system": "eLibrary",
                "filing_id": accession, "accession_number": accession,
                "attachment_id": "r44-capacity-fixture",
                "media_type": "application/pdf", "byte_size": 1000,
                "content_hash": content_hash, "availability": "retrieved",
            })

        arlington_document, arlington_hash = documents["20250225-5101"]
        for number in range(1, 19):
            fact_id = "r44-arlington-fact-%02d" % number
            self._insert(con, "document_facts", {
                "document_fact_id": fact_id,
                "document_id": arlington_document,
                "source_system": "eLibrary", "filing_id": "20250225-5101",
                "source_fact_id": fact_id, "entity_key": "C001685",
                "assertion_type": "reported_capacity",
                "metric_id": "cap_reported_capacity",
                "value_text": "ambiguous capacity row %02d" % number,
                "value_num": None, "unit": "Dth/day",
                "scope_note": "Arlington capacity row %02d" % number,
                "verbatim_span": "cleaned ambiguous span %02d" % number,
                "extraction_method": "pdf_text_span",
                "content_hash": arlington_hash,
                "confidence": "blocked_ambiguity", "review_state": "queued",
            })

        blocked_rows = [
            ("capacity-r44-arlington-head", "20250225-5101", "C001685",
             "Arlington report head", None),
        ]
        blocked_rows.extend(
            ("capacity-r44-arlington-%02d" % number, "20250225-5101",
             "C001685", "Arlington capacity row %02d" % number,
             "r44-arlington-fact-%02d" % number)
            for number in range(1, 19))
        blocked_rows.extend([
            ("capacity-r44-c000584", "20250227-5034", "C000584",
             "C000584 report head", None),
            ("capacity-r44-c001506-head", "20260202-5047", "C001506",
             "C001506 report head", None),
            ("capacity-r44-c001506-detail", "20260202-5047", "C001506",
             "C001506 capacity detail", "r44-c001506-fact-01"),
        ])
        for observation_id, accession, entity, scope, fact_id in blocked_rows:
            document_id, _ = documents[accession]
            self._insert(con, "observations", {
                "observation_id": observation_id, "entity_key": entity,
                "metric_id": "cap_reported_capacity",
                "source_regime": "Form 549B Capacity",
                "period_basis": "as_of",
                "instant_date": "%d-12-31" % occurrences[accession][1],
                "reporting_year": occurrences[accession][1],
                "reporting_period": "annual", "scope": scope,
                "scope_rule": "capacity definition must be unambiguous",
                "unit": "Dth/day", "value_text": "interpretation blocked",
                "value_num": None, "availability": "interpretation_blocked",
                "origin": "source_native", "method": "reported",
                "version_status": "original", "validation": "blocked_ambiguity",
                "source_system": "eLibrary", "filing_id": accession,
                "source_fact_id": fact_id, "document_id": document_id,
                "accession_number": accession,
                "qa_flags": "capacity_definition_ambiguous",
                "review_status": "reviewed_open",
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "updated_at": "2026-09-09T00:00:00+00:00",
            })

        # The pinned C000584 occurrence also has twelve decoded candidates:
        # eleven clean numeric rows and one numeric row whose definition still
        # requires review.  Keep them so the finalizer proves the exact
        # 34-observation occurrence population, not only the 22 null gates.
        c000584_document, _ = documents["20250227-5034"]
        for number in range(1, 12):
            self._insert(con, "observations", {
                "observation_id": "capacity-r44-c000584-present-%02d" % number,
                "entity_key": "C000584", "metric_id": "cap_reported_capacity",
                "source_regime": "Form 549B Capacity", "period_basis": "as_of",
                "instant_date": "2024-12-31", "reporting_year": 2024,
                "reporting_period": "annual",
                "scope": "C000584 decoded capacity row %02d" % number,
                "scope_rule": "reported capacity definition",
                "unit": "Dth/day", "value_text": str(1000 + number),
                "value_num": str(1000 + number), "availability": "present",
                "origin": "source_native", "method": "reported",
                "version_status": "original", "validation": "pass",
                "source_system": "eLibrary", "filing_id": "20250227-5034",
                "document_id": c000584_document,
                "accession_number": "20250227-5034", "qa_flags": "",
                "review_status": "reviewed_final",
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "updated_at": "2026-09-09T00:00:00+00:00",
            })
        self._insert(con, "observations", {
            "observation_id": "capacity-r44-c000584-blocked-numeric",
            "entity_key": "C000584", "metric_id": "cap_reported_capacity",
            "source_regime": "Form 549B Capacity", "period_basis": "as_of",
            "instant_date": "2024-12-31", "reporting_year": 2024,
            "reporting_period": "annual",
            "scope": "C000584 decoded but ambiguous capacity row",
            "scope_rule": "capacity definition must be unambiguous",
            "unit": "Dth/day", "value_text": "2048", "value_num": "2048",
            "availability": "present", "origin": "source_native",
            "method": "reported", "version_status": "original",
            "validation": "blocked_ambiguity", "source_system": "eLibrary",
            "filing_id": "20250227-5034", "document_id": c000584_document,
            "accession_number": "20250227-5034",
            "qa_flags": "capacity_definition_ambiguous",
            "review_status": "reviewed_open",
            "first_seen_at": "2026-09-09T00:00:00+00:00",
            "updated_at": "2026-09-09T00:00:00+00:00",
        })

        matched_slots = (
            ("slot-r44-arlington", "C001685", "gas_storage",
             "2024-12-31", "Arlington report head",
             "capacity-r44-arlington-head"),
            ("slot-r44-c001506", "C001506", "interstate_gas",
             "2025-12-31", "C001506 report head",
             "capacity-r44-c001506-head"),
        )
        for slot_id, entity, template, instant, scope, observation_id in matched_slots:
            self._insert(con, "coverage_expected", {
                "slot_id": slot_id, "entity_key": entity, "template": template,
                "metric_id": "cap_reported_capacity",
                "source_regime": "Form 549B Capacity",
                "period_basis": "as_of", "instant_date": instant,
                "reporting_year": int(instant[:4]), "reporting_period": "annual",
                "scope": scope, "unit_rule": "Dth/day",
                "requirement": "REQUIRED",
                "requirement_evidence": "complete official capacity filing",
                "frozen_at": "2026-09-09T00:00:00Z",
                "frozen_run_id": "coverage-r44-fixture",
                "due_date": "2026-03-01", "slot_state": "overdue",
                "source_health": "ok", "denominator_origin": "calendar",
            })
            self._insert(con, "coverage_measured", {
                "slot_id": slot_id, "run_id": "coverage-r44-fixture",
                "observation_id": observation_id,
                "outcome": "interpretation_blocked", "populated": 0,
                "source_matched": 1, "validated": 0, "in_review": 1,
                "reason": "capacity definition remains ambiguous",
                "candidates_refused": 0,
            })

    def _make_database(self):
        self.db.parent.mkdir(parents=True)
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        con.executescript((ROOT / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
        # Inputs: leave the thirteenth captured input intentionally unapplied.
        for i, accession in enumerate(self.input_accessions[:-1]):
            entity = "C%06d" % (i + 100)
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": entity, "form": "IOC", "accession_number": accession,
                "reporting_year": 2026, "reporting_period": "Q1",
                "content_hash": hashlib.sha256(accession.encode()).hexdigest(),
                "is_canonical": 1, "version_status": "original",
            })
            self._observation(con, "input-obs-%02d" % i, accession, entity)
        capacity_accession = "20260226-5162"
        capacity_fact = "P02R0010"
        self._insert(con, "filings", {
            "source_system": "eLibrary", "filing_id": capacity_accession,
            "entity_key": "C001088", "form": "Form 549B Capacity",
            "accession_number": capacity_accession, "reporting_year": 2024,
            "reporting_period": "annual", "content_hash": self.asserted_capacity_hash,
            "is_canonical": 1, "version_status": "original",
        })
        self._insert(con, "documents", {
            "document_id": "eLibrary|20260226-5162|fixture",
            "source_system": "eLibrary", "filing_id": capacity_accession,
            "accession_number": capacity_accession, "byte_size": 173287,
            "content_hash": self.asserted_capacity_hash,
            "availability": "retrieved", "media_type": "application/pdf",
        })
        self._insert(con, "source_facts", {
            "source_system": "eLibrary", "filing_id": capacity_accession,
            "source_fact_id": capacity_fact, "concept_local": "capacity_report_row",
            "value_as_filed": "Subscribed Capacity 2,508,300 Dth/d", "is_nil": 0,
        })
        self._observation(
            con, "capacity-5162-observation", capacity_accession, "C001088",
            fact_id=capacity_fact, metric="cap_reported_capacity", value="2508300")
        con.execute(
            "UPDATE observations SET source_regime='Form 549B Capacity' "
            "WHERE observation_id='capacity-5162-observation'")
        self._insert(con, "lineage_edges", {
            "observation_id": "capacity-5162-observation", "input_order": 0,
            "input_role": "basis", "input_source_system": "eLibrary",
            "input_filing_id": capacity_accession,
            "input_source_fact_id": capacity_fact,
        })
        self._insert(con, "lineage_populations", {
            "population_id": "pop-capacity-5162",
            "observation_id": "capacity-5162-observation",
            "source_system": "eLibrary", "source_table": "source_facts",
            "filing_ids": json.dumps([capacity_accession]),
            "inclusion_rule": "exact filing occurrence and supported row",
            "exclusion_rule": "exclude other owners and occurrences",
            "row_count": 1, "candidate_count": 1, "excluded_count": 0,
            "member_key": "source_fact_id", "member_digest": "a" * 64,
            "members_sample": json.dumps([capacity_fact]),
            "aggregate_value": "2508300", "aggregate_unit": "Dth/d",
            "created_at": "2026-09-09T00:00:00Z",
        })

        elibrary_run = "run-elibrary-fixture"
        self._insert(con, "runs", {
            "run_id": elibrary_run,
            "started_at": "2026-09-09T00:00:00Z",
            "finished_at": "2026-09-09T00:10:00Z",
            "mode": "universe",
            "scope_json": json.dumps({"offline": True, "as_of": "2026-09-07"}),
            "registry_version": "fixture", "code_version": "fixture",
            "status": "complete", "note": "synthetic complete replay",
        })
        messages = [
            ("C000654", "C000654: proceedings reported for ['RP25-1189', "
             "'RP24-1035', 'RP18-1126'] (of 3 located)"),
            ("C001049", "C001049: proceedings reported for ['IS26-546', "
             "'IS24-804', 'IS24-810'] (of 3 located)"),
            ("C000654", "C000654: excluded 1 filing occurrence(s) dated after "
             "the pinned as-of boundary 2026-09-07: 20260909-5053"),
        ]
        for seq, (entity, message) in enumerate(messages, 1):
            self._insert(con, "run_log", {
                "run_id": elibrary_run, "seq": seq,
                "ts": "2026-09-09T00:00:00Z",
                "level": "warn" if "excluded" in message else "info",
                "adapter": "elibrary_docs", "entity_cid": entity,
                "message": message,
            })
        search_occurrences = [
            ("20260731-5000", "C001049", "IS26-587-000"),
            ("20260825-5125", "C000654", "RP26-1091-000"),
            ("20260721-5070", "C000654", "RP26-981-000"),
        ]
        for order, (accession, entity, docket) in enumerate(search_occurrences, 1):
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": accession,
                "entity_key": entity, "form": "eLibrary document",
                "accession_number": accession, "reporting_year": 2026,
                "reporting_period": "as_of", "filed_date": accession[:4] + "-01-01",
                "is_canonical": 1, "version_status": "original",
            })
            self._insert(con, "filing_dockets", {
                "source_system": "eLibrary", "filing_id": accession,
                "docket": docket,
            })
            self._insert(con, "documents", {
                "document_id": "search-doc-" + accession,
                "source_system": "eLibrary", "filing_id": accession,
                "accession_number": accession, "availability": "not_retrieved",
            })
            self._insert(con, "run_input_inventory", {
                "run_id": elibrary_run, "adapter": "elibrary_docs",
                "entity_cid": entity, "input_order": order,
                "source_system": "eLibrary", "filing_id": accession,
                "accession_number": accession, "submitted_on": "2026-01-01",
                "form": "eLibrary document", "reporting_year": 2026,
                "reporting_period": "as_of", "version_status": "original",
                "is_canonical": 1, "observed_at": "2026-09-09T00:00:00Z",
            })
        for unit_number, unit in enumerate(self.dependency_expected_units, 1):
            entity = unit["entity_key"]
            scope_key = "2024:2026:dependency-%d" % unit_number
            self._insert(con, "checkpoints", {
                "adapter": "elibrary_docs", "entity_cid": entity,
                "scope_key": scope_key, "state": "done", "attempts": 1,
                "last_run_id": elibrary_run, "updated_at": "2026-09-09T00:09:00Z",
            })
            self._insert(con, "unit_commits", {
                "run_id": elibrary_run, "adapter": "elibrary_docs",
                "entity_cid": entity, "scope_key": scope_key,
                "identity_json": json.dumps({"year_from": 2024, "year_to": 2026}),
                "input_digest": hashlib.sha256(entity.encode()).hexdigest(),
                "observation_count": 0, "edge_count": 0,
                "document_fact_count": 0, "committed_at": "2026-09-09T00:09:00Z",
            })
            self._insert(con, "run_unit_status", {
                "run_id": elibrary_run, "adapter": "elibrary_docs",
                "entity_cid": entity, "scope_key": scope_key, "seq": 1,
                "state": "done", "detail": "synthetic atomic commit",
                "recorded_at": "2026-09-09T00:09:00Z",
            })
            for docket_number, docket in enumerate(unit["dockets"], 1):
                accession = "2026%02d01-%04d" % (
                    unit_number, 8000 + unit_number * 10 + docket_number)
                self._insert(con, "filings", {
                    "source_system": "eLibrary", "filing_id": accession,
                    "entity_key": entity, "form": "eLibrary document",
                    "accession_number": accession, "reporting_year": 2026,
                    "reporting_period": "as_of", "filed_date": "2026-01-01",
                    "content_hash": hashlib.sha256(accession.encode()).hexdigest(),
                    "is_canonical": 1, "version_status": "original",
                })
                self._insert(con, "filing_dockets", {
                    "source_system": "eLibrary", "filing_id": accession,
                    "docket": docket + "-000",
                })
                self._insert(con, "documents", {
                    "document_id": "dependency-doc-" + accession,
                    "source_system": "eLibrary", "filing_id": accession,
                    "accession_number": accession, "availability": "retrieved",
                    "byte_size": 100, "content_hash": hashlib.sha256(
                        accession.encode()).hexdigest(),
                })
        for i, accession in enumerate(self.exception_accessions):
            entity = "C%06d" % (i + 500)
            self._insert(con, "filings", {
                "source_system": "eLibrary", "filing_id": "exc-" + accession,
                "entity_key": entity, "form": "Capacity" if i < 2 else "IOC",
                "accession_number": accession, "reporting_year": 2024,
                "reporting_period": "Q1",
                "content_hash": hashlib.sha256(("exc" + accession).encode()).hexdigest(),
                "is_canonical": 1, "version_status": "original",
            })
            self._observation(con, "exc-obs-%02d" % i, accession, entity,
                              filing_id="exc-" + accession)
            if i < 2:
                self._insert(con, "documents", {
                    "document_id": "doc-" + accession, "source_system": "eLibrary",
                    "filing_id": "exc-" + accession, "accession_number": accession,
                    "media_type": "application/pdf", "text_layer": "no",
                    "availability": "retrieved",
                })

        for i, ann in enumerate(self.annotations):
            self._insert(con, "reviewed_source_annotations", ann)
            self._observation(
                con, "ann-obs-%d" % i, None, ann["entity_key"],
                source_system=ann["source_system"], filing_id=ann["filing_id"],
                fact_id=ann["source_fact_id"], metric=ann["metric_id"],
                value=ann["filed_text"] if i < 4 else "1", warning=i < 4)

        self._insert(con, "lineage_edges", {
            "observation_id": "input-obs-00", "input_order": 0,
            "input_role": "basis", "input_source_system": "eLibrary",
            "input_filing_id": self.input_accessions[0],
            "input_source_fact_id": "fact-input-obs-00",
            "input_observation_id": "input-obs-00",
        })
        self._insert(con, "events", {
            "event_id": "event-1", "entity_key": "C000100", "event_class": "data_quality",
            "event_type": "source_update", "headline": "captured source",
            "destination": "filing_archive", "first_seen_at": "2026-09-09T00:00:00Z",
            "is_backfill": 0, "comparison_basis": "filing identity",
            "confidence_note": "exact source",
        })
        self._insert(con, "document_facts", {
            "document_fact_id": "document-fact-1", "document_id": "doc-20240223-5073",
            "entity_key": "C000500", "assertion_type": "capacity",
            "scope_note": "named storage facility under reviewed operating assumptions",
            "verbatim_span": "approximately 420 MMcf/day",
            "extraction_method": "reviewed_page_image_transcription",
        })
        self._insert(con, "source_manifest", {
            "cache_key": "a" * 64, "content_hash": "b" * 64,
            "source_system": "eLibrary", "source_url": "https://ferc.example/source",
            "byte_size": 1, "first_seen_at": "2026-09-09T00:00:00Z",
            "last_seen_at": "2026-09-09T00:00:00Z", "fetch_count": 1,
        })
        self._insert(con, "applicability", {
            "form": "Form 2", "taxonomy_version": "2026", "concept_local": "Revenue",
            "in_form": "yes", "evidence": "official taxonomy",
        })
        self._insert(con, "coverage_expected", {
            "slot_id": "slot-1", "entity_key": "C000100", "template": "interstate_gas",
            "metric_id": "input-obs-00", "source_regime": "Form 2",
            "period_basis": "annual", "scope": "legal entity", "unit_rule": "USD",
            "requirement": "REQUIRED", "requirement_evidence": "official form",
            "frozen_at": "2026-09-09T00:00:00Z", "frozen_run_id": "coverage-1",
            "due_date": "2026-04-30", "slot_state": "overdue",
            "source_health": "ok", "denominator_origin": "calendar",
        })
        self._insert(con, "coverage_measured", {
            "slot_id": "slot-1", "run_id": "coverage-1",
            "observation_id": "input-obs-00", "outcome": "populated_validated",
            "populated": 1, "source_matched": 1, "validated": 1,
            "in_review": 0, "candidates_refused": 0,
        })
        self._insert(con, "coverage_expected", {
            "slot_id": "slot-capacity-5162", "entity_key": "C001088",
            "template": "interstate_gas", "metric_id": "cap_reported_capacity",
            "source_regime": "Form 549B Capacity", "period_basis": "snapshot",
            "scope": "legal entity C001088", "unit_rule": "Dth/d",
            "requirement": "REQUIRED", "requirement_evidence": "official filing",
            "frozen_at": "2026-09-09T00:00:00Z", "frozen_run_id": "coverage-1",
            "due_date": "2026-03-01", "slot_state": "due_and_open",
            "source_health": "ok", "denominator_origin": "calendar",
        })
        self._insert(con, "coverage_measured", {
            "slot_id": "slot-capacity-5162", "run_id": "coverage-1",
            "observation_id": "capacity-5162-observation",
            "outcome": "populated_validated", "populated": 1,
            "source_matched": 1, "validated": 1, "in_review": 0,
            "candidates_refused": 0,
        })
        self.field_keys = []
        for i in range(165):
            key = ("interstate_gas", "field-%03d" % i)
            self.field_keys.append(key)
        self.field_keys += [("interstate_gas", "split-a"),
                            ("interstate_gas", "split-b"),
                            ("interstate_gas", "new-field")]
        for template, field_id in self.field_keys:
            self._insert(con, "field_status", {
                "template": template, "field_id": field_id, "metric_id": field_id,
                "adapter": "gas_xbrl", "implementation": "implemented",
                "outcome": "implemented_retrieved_validated", "evidence": "Build A",
                "blocker": "", "adapter_implemented": 1, "has_data_in_template": 1,
                "validated_in_template": 1,
            })
        for i in range(146):
            self._insert(con, "requirements_crosswalk", {
                "source_doc": "source", "source_row": "row-%03d" % i,
                "disposition": "carried", "metric_id": "metric-%03d" % i,
                "template": "interstate_gas", "note": "mapped",
            })

        exercised_pin = next(
            pin for pin in self.taxonomy_pins
            if pin["form"] == "Form 2" and pin["reporting_year"] == 2024)
        taxonomy_url, taxonomy_hash = exercised_pin["evidence_ref"].rsplit(
            "#sha256=", 1)
        self._insert(con, "filings", {
            "source_system": "eCollection_XBRL", "filing_id": "r37-form2-2024",
            "entity_key": "C-R37", "form": "Form 2",
            "reporting_year": 2024, "reporting_period": "annual",
            "content_hash": hashlib.sha256(b"r37 filing").hexdigest(),
            "is_canonical": 1, "version_status": "original",
        })
        self._insert(con, "taxonomy_sources", {
            "form": "Form 2", "taxonomy_version": "2024-04-01",
            "artefact": "entry_point", "url": taxonomy_url,
            "content_hash": taxonomy_hash, "retrieved": 1,
            "note": "synthetic exercised R37 route",
        })
        self._make_ioc_r38_r39_gate_data(con)
        self._make_ioc_r40_gate_data(con)
        self._make_w5_horizon_counterevidence(con)
        self._make_capacity_semantic_gate_data(con)
        self._make_filing_association_gate_data(con)
        con.commit()
        # Convert WAL state to a closed, hashable file for the finalizer.
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()

    def _make_crosswalks(self):
        req_rows = [{"source_doc": "source", "source_row": "row-%03d" % i,
                     "disposition": "carried"} for i in range(146)]
        _write_csv(self.root / "config" / "requirements_crosswalk.csv",
                   ("source_doc", "source_row", "disposition"), req_rows)
        field_rows = []
        for i in range(165):
            field_rows.append({"source_doc": "audit", "source_row": "audit-%03d" % i,
                               "template": "interstate_gas", "disposition": "carried",
                               "successors": "field-%03d" % i})
        field_rows.append({"source_doc": "audit", "source_row": "audit-165",
                           "template": "interstate_gas", "disposition": "split",
                           "successors": "split-a;split-b"})
        field_rows.append({"source_doc": "registry", "source_row": "new",
                           "template": "interstate_gas", "disposition": "new",
                           "successors": "new-field"})
        _write_csv(self.root / "config" / "field_crosswalk_166.csv",
                   ("source_doc", "source_row", "template", "disposition", "successors"),
                   field_rows)

    def _table_rows(self, con, table, columns):
        return [{c: "" if v is None else v for c, v in zip(columns, row)}
                for row in con.execute("SELECT %s FROM %s" %
                                       (",".join(columns), table))]

    def _make_exports(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        exports = self.root / "exports"
        files = {}
        for name, spec in records.OUTPUT_CONTRACT.items():
            columns = list(spec.columns)
            rows = []
            if name == "canonical_observations.csv":
                db_rows = [dict(r) for r in con.execute(
                    "SELECT o.*,e.legal_name FROM observations o "
                    "LEFT JOIN entities e ON e.entity_key=o.entity_key")]
                rows = []
                for r in db_rows:
                    out = {c: r.get(c, "") for c in columns}
                    out["value"] = r.get("value_text", "")
                    display_unit, display_scale = records.DISPLAY_CONTRACT.get(
                        r.get("metric_id"), (r.get("unit") or "", 1.0))
                    out["display_unit"] = display_unit
                    out["display_scale"] = display_scale
                    number = r.get("value_num")
                    out["display_value"] = ("" if number in (None, "") else
                                            ("%.6f" % (float(number) * display_scale))
                                            .rstrip("0").rstrip("."))
                    rows.append(out)
            elif name in ("quarterly_key_metrics.csv", "annual_key_metrics.csv"):
                rows = []
            elif name == "coverage_by_slot.csv":
                if "observation_id" not in columns:
                    columns.append("observation_id")
                rows = [dict(r) for r in con.execute(
                    "SELECT e.*,m.observation_id,m.outcome,m.populated,m.source_matched,"
                    "m.validated,m.in_review,m.candidates_refused "
                    "FROM coverage_expected e JOIN coverage_measured m USING(slot_id)")]
            elif name == "field_status.csv":
                rows = [dict(r) for r in con.execute("SELECT * FROM field_status")]
            elif name == "reviewed_source_annotations.csv":
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM reviewed_source_annotations")]
            elif name == "lineage_edges.csv":
                # The real exporter emits ``l.*``.  The fixed artifact contract
                # lists only consumer-required columns, but the IOC gate also
                # binds the persisted population identity carried by the real
                # export.
                if "input_population_id" not in columns:
                    columns.append("input_population_id")
                rows = [dict(r) for r in con.execute("SELECT * FROM lineage_edges")]
            elif name == "filing_inventory.csv":
                rows = [dict(r) for r in con.execute(
                    "SELECT f.*,"
                    "(SELECT group_concat(entity_key, '|') FROM "
                    " (SELECT entity_key FROM filing_entities fe "
                    "  WHERE fe.source_system=f.source_system "
                    "  AND fe.filing_id=f.filing_id ORDER BY entity_key)) "
                    "AS associated_entity_keys,"
                    "(SELECT group_concat(entity_key || ':' || association_role, '|') FROM "
                    " (SELECT entity_key,association_role FROM filing_entities fe "
                    "  WHERE fe.source_system=f.source_system "
                    "  AND fe.filing_id=f.filing_id ORDER BY entity_key)) "
                    "AS associated_entity_roles FROM filings f")]
            elif name == "documents.csv":
                rows = [dict(r) for r in con.execute("SELECT * FROM documents")]
            elif name == "document_facts.csv":
                rows = [dict(r) for r in con.execute("SELECT * FROM document_facts")]
                # The real exporter emits d.*; keep the compact fixture faithful
                # to the occurrence/provenance fields used by the W5 gate.
                for column in ("source_system", "filing_id", "value_text", "unit",
                               "scope_note", "content_hash"):
                    if column not in columns:
                        columns.append(column)
            elif name == "events.csv":
                rows = [dict(r) for r in con.execute("SELECT * FROM events")]
            elif name == "blockers.csv":
                rows = [dict(r) for r in con.execute("SELECT * FROM blockers")]
            elif name == "applicability.csv":
                rows = [dict(r) for r in con.execute("SELECT * FROM applicability")]
                columns = list(rows[0]) if rows else []
            elif name == "source_manifest.csv":
                rows = [{"cache_key": key,
                         **{column: value.get(column, "") for column in columns
                            if column != "cache_key"}, "note": ""}
                        for key, value in self.index.items()]
            elif name == "coverage_by_entity.csv":
                rows = [dict(r) for r in con.execute(
                    "SELECT o.entity_key,COUNT(*) observations,"
                    "SUM(o.availability='present') populated,"
                    "SUM(o.validation='pass' AND o.availability='present') validated "
                    "FROM observations o LEFT JOIN entities e ON e.entity_key=o.entity_key "
                    "LEFT JOIN asset_entity_map m ON m.entity_key=o.entity_key "
                    "LEFT JOIN assets a ON a.asset_id=m.asset_id GROUP BY o.entity_key")]
            elif name == "coverage_by_metric.csv":
                rows = [dict(r) for r in con.execute(
                    "SELECT metric_id,source_regime,COUNT(*) observations,"
                    "SUM(availability='present') populated,"
                    "SUM(validation='pass' AND availability='present') validated "
                    "FROM observations GROUP BY metric_id,source_regime")]
            else:
                rows = [{c: "1" for c in columns}]
            _write_csv(exports / name, columns, rows)
            files[name] = (exports / name).read_bytes()

        coverage = records._coverage_counts(con)
        adapter_group = {
            "group": "fixture_all_adapters",
            "all_slots": coverage["expected_total"],
            "core_slots": coverage["expected_core"],
            "core_due_slots": coverage["expected_core_due"],
            "populated": coverage["core_populated"],
            "validated": coverage["core_validated"],
            "in_review": coverage["core_in_review"],
            "populated_pct": round(
                100.0 * coverage["core_populated"] / coverage["expected_core"], 4),
            "validated_pct": round(
                100.0 * coverage["core_validated"] / coverage["expected_core"], 4),
        }
        field_rows = [dict(r) for r in con.execute("SELECT * FROM field_status")]
        json_values = {
            "coverage_statistics.json": coverage,
            "coverage_groups.json": {
                "by_template": records._coverage_group_rows(con, "template"),
                "by_adapter": [adapter_group],
                "by_metric": records._coverage_group_rows(con, "metric_id")},
            "field_status_summary.json": {
                "field_rows": 168, "distinct_metrics": 168,
                "by_outcome": {"implemented_retrieved_validated": 168},
                "by_template": {"interstate_gas": {"implemented_retrieved_validated": 168}},
                "rows_with_implemented_adapter": 168,
                "rows_with_local_data": 168,
                "rows_validated_locally": 168,
                "readiness_basis": "within template, adapter, eligibility, period and quality gate",
            },
        }
        for name, value in json_values.items():
            raw = _raw_json(value)
            (exports / name).write_bytes(raw)
            files[name] = raw

        identities = {name: _ident(raw) for name, raw in sorted(files.items())}
        snapshots = records._candidate_snapshots(self.root)
        metadata = {"code_snapshot": snapshots["code_snapshot"],
                    "input_snapshot": snapshots["input_snapshot"],
                    "database_identity": records._database_semantic_identity(con)}
        generation_id = hashlib.sha256(json.dumps(
            {"kind": "consumer_exports", "files": identities, "metadata": metadata},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        generation_rel = pathlib.Path(".generations") / "consumer_exports" / generation_id
        generation = self.root / generation_rel
        generation.mkdir(parents=True)
        receipt = {"generation_id": generation_id, "kind": "consumer_exports",
                   "generation_path": generation_rel.as_posix(), "files": identities,
                   "metadata": metadata}
        for name, raw in files.items():
            path = generation / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        _write_json(generation / "GENERATION_MANIFEST.json", receipt)
        _write_json(self.receipt, receipt)
        con.execute(
            "INSERT INTO publication_generations(generation_id,run_id,code_snapshot,"
            "input_snapshot,database_identity,manifest_json,status,published_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (generation_id, "run-build-a", metadata["code_snapshot"],
             metadata["input_snapshot"], metadata["database_identity"],
             json.dumps(receipt, sort_keys=True), "published", "2026-09-09T18:02:08Z"))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()

    def _make_validation_and_tests(self):
        quality_rows = []
        for observation_id, expected in sorted(
                records.R36_NAMED_FILER_OBSERVATIONS.items()):
            quality_rows.append({
                "observation_id": observation_id,
                "entity_key": expected["entity_key"],
                "metric_id": expected["metric_id"],
                "source_system": "eLibrary", "filing_id": expected["filing_id"],
                "source_health": "ok", "evidence_kind": expected["evidence_kind"],
                "entity_association": (
                    "reviewed_shared_lng_facility:named_filer:" +
                    ",".join(expected["common_dockets"])),
                "entity_association_ok": True,
                "document_filing_association_ok": True,
                "current_version": expected["current_version"],
                "unit_contract_ok": expected["unit_contract_ok"],
                "unit_gate": expected["unit_gate"],
                "included_in_coverage_denominator": False,
                "classification": expected["classification"],
            })
        coverage_path = self.root / "verification" / "coverage_generation.json"
        _write_json(coverage_path, {
            "grid": {"fixture": True}, "statistics": {"fixture": True},
            "document_occurrence_quality": {
                "schema": "ferc-document-occurrence-quality-v1",
                "coverage_window": [2024, 2026],
                "summary": {"route_anchors": 5,
                            "occurrence_or_status_rows": len(quality_rows)},
                "rows": quality_rows,
            },
        })
        coverage_identity = _ident(coverage_path.read_bytes())
        plan_identity = _ident(
            (self.root / "config" / "run_plan.json").read_bytes())
        publication = json.loads(self.receipt.read_text())
        code_snapshot = publication["metadata"]["code_snapshot"]
        _write_json(self.root / "verification" / "replay_step_status.json", {
            "schema": "ferc_replay_step_status_v2",
            "active_plan_sha256": plan_identity["sha256"],
            "steps": {
                "generate_and_measure_coverage": {
                    "step": 2, "status": "complete", "exit_code": 0,
                    "input_digest": "1" * 64, "output_digest": "2" * 64,
                    "input_identity": {
                        "code_snapshot": code_snapshot,
                        "inputs": [
                            {"name": "config/taxonomy_pins.json",
                             "path": str(self.root / "config" / "taxonomy_pins.json"),
                             **self.taxonomy_pins_identity},
                            {"name": "source_cache/index.json",
                             "path": str(self.root / "source_cache" / "index.json"),
                             **self.source_cache_index_identity},
                        ],
                    },
                    "outputs": [{
                        "name": "coverage generation evidence",
                        "path": "verification/coverage_generation.json",
                        **coverage_identity,
                    }],
                },
            },
        })
        _write_json(self.validation, {
            "summary": {"PASS": 3, "FAIL": 0, "ERROR": 0, "SKIPPED": 0},
            "results": [
                {"check": "database integrity", "group": "database", "status": "PASS",
                 "detail": "ok"},
                {"check": "semantic controls", "group": "semantic", "status": "PASS",
                 "detail": "ok"},
                {"check": ("IOC aggregates redraw from their persisted source "
                           "populations"),
                 "group": "lineage", "status": "PASS",
                "detail": ("2 present IOC observations pass full persisted redraw; "
                            "foreign-adapter populations are outside this validator")},
            ],
        })
        replay_path = self.root / "verification" / "replay_step_status.json"
        replay = json.loads(replay_path.read_text())
        receipt_identity = _ident(self.receipt.read_bytes())
        validation_identity = _ident(self.validation.read_bytes())
        publication_output_digest = "3" * 64
        replay["steps"]["publish_consumer_generation"] = {
            "step": 9, "status": "complete", "exit_code": 0,
            "input_digest": "2" * 64,
            "output_digest": publication_output_digest,
            "input_identity": {"code_snapshot": code_snapshot, "inputs": []},
            "outputs": [{"name": "complete consumer publication boundary",
                         "path": "publication_receipt.json", **receipt_identity}],
        }
        replay["steps"]["validate_integrated_generation"] = {
            "step": 10, "status": "complete", "exit_code": 0,
            "input_digest": "4" * 64, "output_digest": "5" * 64,
            "input_identity": {
                "code_snapshot": code_snapshot,
                "dependencies": {
                    "publish_consumer_generation": publication_output_digest},
                "inputs": [{"name": "@output/publication_receipt.json",
                            "path": "publication_receipt.json", **receipt_identity}],
            },
            "outputs": [{"name": "complete validation results",
                         "path": "verification/validation_results.json",
                         **validation_identity}],
        }
        _write_json(replay_path, replay)
        test_ids = ["tests.synthetic.CompleteSuite.test_case_%03d" % i
                    for i in range(400)]
        lines = ["test_case_%03d (%s) ... %s" %
                 (i, test_id, "skipped 'optional unavailable reference'"
                  if i == 399 else "ok")
                 for i, test_id in enumerate(test_ids)]
        log = (("\n".join(lines) +
                "\n\nRan 400 tests in 1.000s\n\nOK (skipped=1)\n").encode())
        log_path = self.logs / "complete.log"
        log_path.write_bytes(log)
        manifest = {
            "schema": "ferc-final-test-run-manifest-v2",
            "tested_tree_snapshot": records._test_tool_snapshot(self.root),
            "issue_test_evidence": {issue_id: [test_ids[0]]
                                    for issue_id in self.issue_ids + self.related_ids},
            "exception_test_evidence": {exception_id: [test_ids[0]]
                                        for exception_id in self.exception_ids},
            "runs": [{
                "run_id": "complete-python314", "kind": "complete_suite",
                "acceptance_role": "acceptance", "complete_suite": True,
                "log": "complete.log", **_ident(log),
                "command": ["python3.14", "-B", "-m", "unittest", "discover", "-v"],
                "interpreter": "CPython 3.14.7", "exit_code": 0,
                "counts": {"collected": 400, "executed": 400, "pass": 399,
                           "fail": 0, "error": 0, "skip": 1, "xfail": 0,
                           "xpass": 0},
                "skip_details": [{"test": test_ids[-1],
                                  "classification": "optional unavailable reference",
                                  "reason": "not a required candidate capability",
                                  "required": False}],
                "xfail_details": [],
            }],
        }
        _write_json(self.test_manifest, manifest)

    def _make_audit(self):
        _write_json(self.audit / "CODEX_REAUDIT_ISSUES.json", {
            "draft": False,
            "rows": [{"issue_id": i,
                      "source_row_type": ("original_audit_A01_A22"
                                          if i.startswith("A") else "additional_claimed_issue")}
                     for i in self.issue_ids],
        })
        _write_json(self.audit / "EXCEPTION_RECONCILIATION_34.json", {
            "rows": [{"blocker_id": i} for i in self.exception_ids]})
        _write_json(self.audit / "MISSING_INPUT_IMPACT.json", {
            "unique_requests": [{"accession_number": a,
                                 "present_in_current_and_shipped_full_cache_index": False}
                                for a in self.input_accessions]})
        _write_json(self.audit / "EXACT_COVERAGE_COUNT_EXTRACTS.json", {
            "coverage_headline": {"total": 26191, "core": 25304,
                                  "core_populated": 21183, "core_validated": 20563}})
        _write_json(self.audit / "FIELD_RECONCILIATION_SUMMARY.json", {
            "key_reconciliation": {"historical_rows": 166}})
        names = ("CODEX_REAUDIT_ISSUES.json", "EXCEPTION_RECONCILIATION_34.json",
                 "MISSING_INPUT_IMPACT.json", "EXACT_COVERAGE_COUNT_EXTRACTS.json",
                 "FIELD_RECONCILIATION_SUMMARY.json")
        _write_json(self.audit / "EVIDENCE_INDEX.json", {
            "entries": [{"path": name, **_ident((self.audit / name).read_bytes())}
                        for name in names]})
        for draft_path, audit_name, label in (
                (self.ledger_path, "CODEX_REAUDIT_ISSUES.json", "final_audit_issues"),
                (self.exception_path, "EXCEPTION_RECONCILIATION_34.json",
                 "final_audit_exceptions"),
                (self.input_path, "MISSING_INPUT_IMPACT.json", "final_audit_inputs")):
            draft = json.loads(draft_path.read_text())
            audit_path = self.audit / audit_name
            draft["source_identities"][label] = {
                "path": str(audit_path), **_ident(audit_path.read_bytes())}
            _write_json(draft_path, draft)

    def rewrite_publication(self):
        """Rebuild the receipt/generation after intentional export mutations."""
        old = json.loads(self.receipt.read_text())
        files = {name: (self.root / "exports" / name).read_bytes()
                 for name in old["files"]}
        identities = {name: _ident(raw) for name, raw in sorted(files.items())}
        metadata = old["metadata"]
        generation_id = hashlib.sha256(json.dumps(
            {"kind": "consumer_exports", "files": identities, "metadata": metadata},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rel = pathlib.Path(".generations") / "consumer_exports" / generation_id
        generation = self.root / rel
        generation.mkdir(parents=True)
        receipt = {"generation_id": generation_id, "kind": "consumer_exports",
                   "generation_path": rel.as_posix(), "files": identities,
                   "metadata": metadata}
        for name, raw in files.items():
            (generation / name).write_bytes(raw)
        _write_json(generation / "GENERATION_MANIFEST.json", receipt)
        _write_json(self.receipt, receipt)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM publication_generations")
        con.execute("INSERT INTO publication_generations VALUES(?,?,?,?,?,?,?,?)",
                    (generation_id, "run", metadata["code_snapshot"],
                     metadata["input_snapshot"], metadata["database_identity"],
                     json.dumps(receipt), "published", "2026-09-09T18:02:08Z"))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()

    def rewrite_validation_boundary(self):
        """Record a successful synthetic step-10 rerun after republishing."""
        status_path = self.root / "verification" / "replay_step_status.json"
        status = json.loads(status_path.read_text())
        receipt_identity = _ident(self.receipt.read_bytes())
        validation_identity = _ident(self.validation.read_bytes())
        receipt = json.loads(self.receipt.read_text())
        code_snapshot = receipt["metadata"]["code_snapshot"]
        publish_digest = hashlib.sha256(
            ("synthetic-publish:" + receipt_identity["sha256"]).encode()).hexdigest()
        publish = status["steps"]["publish_consumer_generation"]
        publish["status"], publish["exit_code"] = "complete", 0
        publish["output_digest"] = publish_digest
        publish["outputs"] = [{
            "name": "complete consumer publication boundary",
            "path": "publication_receipt.json", **receipt_identity}]
        validate = status["steps"]["validate_integrated_generation"]
        validate["status"], validate["exit_code"] = "complete", 0
        validate["input_identity"] = {
            "code_snapshot": code_snapshot,
            "dependencies": {"publish_consumer_generation": publish_digest},
            "inputs": [{"name": "@output/publication_receipt.json",
                        "path": "publication_receipt.json", **receipt_identity}],
        }
        validate["outputs"] = [{
            "name": "complete validation results",
            "path": "verification/validation_results.json", **validation_identity}]
        _write_json(status_path, status)


class FinalReleaseRecordsTests(unittest.TestCase):
    def setUp(self):
        # Keep the finalizer's production declaration exact while allowing the
        # synthetic suite to exercise the same algorithm without copying the
        # 109 MB real official response into every TemporaryDirectory.
        declaration_patch = mock.patch.object(
            records, "REDUNDANT_INPUT_ALIAS", TEST_REDUNDANT_INPUT_ALIAS)
        declaration_patch.start()
        self.addCleanup(declaration_patch.stop)

    def test_canonical_input_snapshot_matches_build_a_alias_and_packaged_absence(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-alias-present-") as a_td, \
                tempfile.TemporaryDirectory(prefix="ferc-final-alias-absent-") as b_td:
            a_root = pathlib.Path(a_td)
            b_root = pathlib.Path(b_td)
            _make_snapshot_fixture(a_root, include_alias=True)
            _make_snapshot_fixture(b_root, include_alias=False)
            a_gate = records._verified_redundant_input_alias(a_root)
            b_gate = records._verified_redundant_input_alias(b_root)
            self.assertTrue(a_gate["alias_present"])
            self.assertFalse(b_gate["alias_present"])
            self.assertEqual(
                records._candidate_snapshots(a_root)["input_snapshot"],
                records._candidate_snapshots(b_root)["input_snapshot"])

    def test_canonical_input_snapshot_refuses_tampered_alias_cache_or_provenance(self):
        mutations = (
            "alias", "canonical", "index", "results_provenance",
            "ledger_provenance", "missing_canonical", "missing_index",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-alias-tamper-") as td:
                root = pathlib.Path(td)
                _make_snapshot_fixture(root)
                declaration = TEST_REDUNDANT_INPUT_ALIAS
                if mutation == "alias":
                    (root / declaration["alias_path"]).write_bytes(b"tampered alias\n")
                elif mutation == "canonical":
                    (root / "source_cache" / declaration["cache_path"]).write_bytes(
                        b"tampered canonical object\n")
                elif mutation == "index":
                    index_path = root / "source_cache" / "index.json"
                    index = json.loads(index_path.read_text())
                    cache_key = hashlib.sha256(
                        _TEST_ALIAS_SOURCE_URL.encode("utf-8")).hexdigest()
                    index[cache_key]["content_hash"] = "0" * 64
                    _write_json(index_path, index)
                elif mutation == "results_provenance":
                    (root / declaration["results_path"]).write_bytes(
                        _TEST_ALIAS_RESULTS_RAW + b"changed\n")
                elif mutation == "ledger_provenance":
                    (root / declaration["ledger_path"]).write_bytes(
                        _TEST_ALIAS_LEDGER_RAW + b"changed\n")
                elif mutation == "missing_canonical":
                    (root / "source_cache" / declaration["cache_path"]).unlink()
                else:
                    (root / "source_cache" / "index.json").unlink()
                with self.assertRaises(records.FinalRecordsRefused):
                    records._candidate_snapshots(root)

    def test_canonical_input_snapshot_keeps_unrelated_omissions_visible(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-alias-complete-") as a_td, \
                tempfile.TemporaryDirectory(prefix="ferc-final-alias-omitted-") as b_td:
            a_root = pathlib.Path(a_td)
            b_root = pathlib.Path(b_td)
            _make_snapshot_fixture(a_root, include_alias=True)
            omitted = _make_snapshot_fixture(b_root, include_alias=False)
            complete = records._candidate_snapshots(a_root)["input_snapshot"]
            packaged = records._candidate_snapshots(b_root)["input_snapshot"]
            self.assertEqual(complete, packaged)
            omitted.unlink()
            self.assertNotEqual(
                complete,
                records._candidate_snapshots(b_root)["input_snapshot"])

    def test_verbose_log_parser_normalises_supported_python_runtime_formats(self):
        cases = {
            "python314": (
                "test_exact (tests.synthetic.RuntimeCase.test_exact) ... ok\n\n"
                "Ran 1 test in 0.001s\n\nOK\n"),
            "python39": (
                "test_exact (tests.synthetic.RuntimeCase) ... ok\n\n"
                "Ran 1 test in 0.001s\n\nOK\n"),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                parsed = records._parse_test_log(body.encode("utf-8"), label)
                self.assertEqual(
                    {"tests.synthetic.RuntimeCase.test_exact": "pass"},
                    parsed["results"])

        inconsistent = (
            "test_exact (tests.synthetic.RuntimeCase.test_other) ... ok\n\n"
            "Ran 1 test in 0.001s\n\nOK\n")
        with self.assertRaisesRegex(records.FinalRecordsRefused, "inconsistent"):
            records._parse_test_log(inconsistent.encode("utf-8"), "inconsistent")

    def test_verbose_log_parser_handles_descriptions_and_captured_output_without_guessing(self):
        body = (
            "test_documented (tests.synthetic.RuntimeCase.test_documented)\n"
            "Documented behavior. ... ok\n"
            "test_noisy (tests.synthetic.RuntimeCase) ... emitted diagnostics\n"
            "a fixture traceback that is data, not a runner result\n"
            "ok\n\n----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.002s\n\nOK\n"
        )
        parsed = records._parse_test_log(body.encode("utf-8"), "captured")
        self.assertEqual(2, parsed["counts"]["executed"])
        self.assertEqual(2, parsed["counts"]["pass"])
        self.assertTrue(parsed["counts"]["result_success"])

        duplicate = body.replace(
            "a fixture traceback that is data, not a runner result\nok\n",
            "ok\na fixture traceback that is data, not a runner result\nok\n")
        with self.assertRaisesRegex(records.FinalRecordsRefused,
                                    "2 unambiguous per-test outcome"):
            records._parse_test_log(duplicate.encode("utf-8"), "ambiguous")

    def test_declared_historical_boundary_can_record_an_omitted_old_artifact(self):
        with tempfile.TemporaryDirectory(prefix="ferc-historical-identity-") as td:
            root = pathlib.Path(td)
            current = root / "current.txt"
            current.write_bytes(b"current")
            doc = {"source_identities": {
                "current": {"path": "current.txt", **_ident(b"current")},
                "candidate_artifacts_at_draft_boundary": {
                    "old_log": {"path": "old.log", "bytes": 12,
                                "sha256": "a" * 64}}}}
            identities = records._validate_source_identities(
                doc, root, root, "fixture")
            statuses = {row["declared_path"]: row["status"] for row in identities}
            self.assertEqual("identity_verified", statuses["current.txt"])
            self.assertEqual("historical_artifact_not_carried_in_candidate",
                             statuses["old.log"])

    def test_historical_cache_boundary_binds_preserved_index_not_mutable_current_path(self):
        with tempfile.TemporaryDirectory(prefix="ferc-cache-boundary-") as td:
            root = pathlib.Path(td)
            current = root / "source_cache" / "index.json"
            preserved = (root / "inputs" /
                         "official_ferc_elibrary_dependency_recovery_20260910" /
                         "source_cache_index_before.json")
            current.parent.mkdir(parents=True)
            preserved.parent.mkdir(parents=True)
            before = b'{"before":"index"}\n'
            after = b'{"after":"append-only index"}\n'
            current.write_bytes(after)
            preserved.write_bytes(before)
            identity = _ident(before)

            mutable_doc = {"source_identities": {
                "candidate_cache_index_at_draft_boundary": {
                    "path": "source_cache/index.json", **identity}}}
            with self.assertRaisesRegex(
                    records.FinalRecordsRefused, "identity mismatch"):
                records._validate_source_identities(
                    mutable_doc, root, root, "final input inventory")

            preserved_doc = {"source_identities": {
                "candidate_cache_index_at_draft_boundary": {
                    "path": str(preserved.relative_to(root)), **identity}}}
            verified = records._validate_source_identities(
                preserved_doc, root, root, "final input inventory")
            self.assertEqual(1, len(verified))
            self.assertEqual("identity_verified", verified[0]["status"])
            self.assertNotEqual(_ident(after), identity)

            preserved.write_bytes(before + b"tampered\n")
            with self.assertRaisesRegex(
                    records.FinalRecordsRefused, "identity mismatch"):
                records._validate_source_identities(
                    preserved_doc, root, root, "final input inventory")

    def test_capacity_repair_discovery_requires_exact_applied_build_a_occurrence(self):
        def finalize(fixture, con):
            related = json.loads(fixture.related_path.read_text())
            tests = {"issue_test_evidence": {
                issue_id: ["tests.synthetic.CompleteSuite.test_case_000"]
                for issue_id in fixture.related_ids}}
            additional = [{"input_id": fixture.additional_input_id,
                           "acceptance_result": "accepted_for_candidate"}]
            additional.extend({
                "input_id": input_id,
                "acceptance_result":
                    "accepted_as_repair_evidence_not_execution_prerequisite",
            } for input_id in fixture.search_input_ids)
            next(row for row in additional
                 if row["input_id"].startswith("elibrary-search:RP26-1091:"))[
                     "build_a_application"] = {
                         "occurrences": [{"accession_number": "20260909-5053",
                                          "accepted": True}],
                     }
            additional.append({
                "input_id": fixture.dependency_input_id,
                "acceptance_result": "accepted_for_candidate",
                "capture": {"new_entries": 11,
                            "required_failed_build_keys": ["x"] * 6},
                "build_a_application": {
                    "units": [{"accepted": True} for _ in range(4)]},
            })
            return records._related_repair_records(
                fixture.root, con, related, tests,
                {"generation_id": "a" * 64}, additional)

        with tempfile.TemporaryDirectory(prefix="ferc-related-capacity-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = finalize(fixture, con)
            con.close()
            target = next(row for row in rows if row["issue_id"] ==
                          "R23-CAPACITY-FILER-IDENTITY-COLLISION")
            self.assertTrue(target["evidence"]["build_a_gate"]["accepted"])

        mutations = {
            "wrong_filer": ("UPDATE filings SET entity_key='C001087' "
                            "WHERE accession_number='20260226-5162'"),
            "wrong_hash": ("UPDATE documents SET content_hash='bad' "
                           "WHERE accession_number='20260226-5162'"),
            "missing_population": ("DELETE FROM lineage_populations "
                                   "WHERE population_id='pop-capacity-5162'"),
        }
        for label, statement in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="ferc-related-capacity-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                con.execute(statement)
                con.commit()
                with self.assertRaisesRegex(records.FinalRecordsRefused,
                                            "failed Build A"):
                    finalize(fixture, con)
                con.close()

    def test_search_response_inputs_and_related_elibrary_gates_require_exact_application(self):
        def finalize(fixture, con):
            additional = json.loads(fixture.additional_input_path.read_text())
            claims = {
                row["input_id"]: {
                    "status": "not_in_final_independent_audit",
                    "relationship": "discovered_during_repair",
                }
                for row in additional["rows"]}
            input_rows, _ = records._additional_input_records(
                fixture.root, con, additional, claims)
            related = json.loads(fixture.related_path.read_text())
            tests = {"issue_test_evidence": {
                issue_id: ["tests.synthetic.CompleteSuite.test_case_000"]
                for issue_id in fixture.related_ids}}
            related_rows = records._related_repair_records(
                fixture.root, con, related, tests,
                {"generation_id": "a" * 64}, input_rows)
            return input_rows, related_rows

        with tempfile.TemporaryDirectory(prefix="ferc-related-elibrary-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            inputs, findings = finalize(fixture, con)
            con.close()
            search = [row for row in inputs if row.get("input_kind") ==
                      "elibrary_advanced_search_response"]
            self.assertEqual(3, len(search))
            self.assertTrue(all(row["build_a_application"]["accepted"] for row in search))
            gated = {row["issue_id"]: row["evidence"]["build_a_gate"]
                     for row in findings}
            for issue_id in ("R25-ELIBRARY-AS-OF-FUTURE-OCCURRENCE-LEAK",
                             "R26-ELIBRARY-HIDDEN-PROCEEDING-SEED",
                             "R27-ELIBRARY-RUN-INPUT-IDENTITY"):
                self.assertTrue(gated[issue_id]["accepted"])

        # The bounded response is diagnostic evidence in the final plan, not a
        # replay input.  When it was never consumed, the database must not
        # invent a warning claiming that it was; the explicit non-selection
        # boundary plus the production filter regression is the valid control.
        with tempfile.TemporaryDirectory(
                prefix="ferc-related-elibrary-diagnostic-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            con.execute("DELETE FROM run_log WHERE message LIKE '%20260909-5053%'")
            con.commit()
            inputs, findings = finalize(fixture, con)
            con.close()
            rp = next(row for row in inputs
                      if row.get("docket") == "RP26-1091")
            future = next(item for item in rp["build_a_application"]["occurrences"]
                          if item["accession_number"] == "20260909-5053")
            self.assertEqual("diagnostic_response_not_selected_by_run_plan",
                             future["exclusion_evidence"])
            self.assertTrue(rp["build_a_application"]["run_plan_boundary"]["accepted"])
            r25 = next(row for row in findings
                       if row["issue_id"] ==
                       "R25-ELIBRARY-AS-OF-FUTURE-OCCURRENCE-LEAK")
            self.assertTrue(r25["evidence"]["build_a_gate"]["accepted"])

        with tempfile.TemporaryDirectory(
                prefix="ferc-related-elibrary-selected-without-warning-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            con.execute("DELETE FROM run_log WHERE message LIKE '%20260909-5053%'")
            con.commit()
            plan_path = fixture.root / "config" / "run_plan.json"
            plan = json.loads(plan_path.read_text())
            plan["steps"][0]["inputs"].append(
                "inputs/official_ferc_elibrary_search_recovery_20260909/CAPTURE.json")
            _write_json(plan_path, plan)
            with self.assertRaisesRegex(
                    records.FinalRecordsRefused, "selected as execution inputs"):
                finalize(fixture, con)
            con.close()

        mutations = ("future_leak", "missing_prior_docket", "blank_inventory",
                     "missing_seed_declaration", "tampered_response_index")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-related-elibrary-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "future_leak":
                    fixture._insert(con, "filings", {
                        "source_system": "eLibrary", "filing_id": "20260909-5053",
                        "entity_key": "C000654", "form": "eLibrary document",
                        "accession_number": "20260909-5053", "reporting_year": 2026,
                        "reporting_period": "as_of", "is_canonical": 1,
                        "version_status": "original",
                    })
                elif mutation == "missing_prior_docket":
                    con.execute("DELETE FROM filing_dockets WHERE filing_id='20260825-5125'")
                elif mutation == "blank_inventory":
                    con.execute("UPDATE run_input_inventory SET filing_id=NULL "
                                "WHERE adapter='elibrary_docs'")
                elif mutation == "missing_seed_declaration":
                    plan_path = fixture.root / "config" / "run_plan.json"
                    plan = json.loads(plan_path.read_text())
                    plan["required_inputs"] = [item for item in plan["required_inputs"]
                                               if "rate_proceedings" not in item["path"]]
                    _write_json(plan_path, plan)
                else:
                    index_path = fixture.root / "source_cache" / "index.json"
                    index = json.loads(index_path.read_text())
                    search_key = next(key for key, value in index.items()
                                      if "AdvancedSearch" in value.get("source_url", ""))
                    index[search_key]["content_hash"] = "0" * 64
                    _write_json(index_path, index)
                con.commit()
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused,
                        "failed Build A|absent/mismatched|hash-declared"):
                    finalize(fixture, con)
                con.close()

    def test_dependency_capture_bundle_requires_exact_delta_and_completed_units(self):
        def finalize(fixture, con):
            additional = json.loads(fixture.additional_input_path.read_text())
            claims = {
                row["input_id"]: {
                    "status": "not_in_final_independent_audit",
                    "relationship": "discovered_during_repair",
                }
                for row in additional["rows"]}
            records_out, _ = records._additional_input_records(
                fixture.root, con, additional, claims)
            return next(row for row in records_out
                        if row["input_id"] == fixture.dependency_input_id)

        with tempfile.TemporaryDirectory(prefix="ferc-dependency-capture-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            record = finalize(fixture, con)
            con.close()
            self.assertEqual("accepted_for_candidate", record["acceptance_result"])
            self.assertEqual(11, record["capture"]["new_entries"])
            self.assertEqual(6, len(record["capture"]["required_failed_build_keys"]))
            self.assertEqual(4, len(record["build_a_application"]["units"]))
            self.assertTrue(all(unit["accepted"]
                                for unit in record["build_a_application"]["units"]))

        mutations = (
            "transitive_not_mandatory", "prior_index_rewrite", "missing_object",
            "request_binding", "missing_commit", "missing_docket_document",
            "missing_plan_declaration",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-dependency-capture-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                additional = json.loads(fixture.additional_input_path.read_text())
                declared = next(row for row in additional["rows"]
                                if row["input_id"] == fixture.dependency_input_id)
                capture_path = fixture.root / declared["capture_manifest"]["path"]
                capture = json.loads(capture_path.read_text())
                if mutation == "transitive_not_mandatory":
                    target = next(row for row in capture["rows"]
                                  if not row["required_by_failed_build"])
                    target["mandatory_for_clean_retry"] = False
                    _write_json(capture_path, capture)
                    declared["capture_manifest"].update(
                        _ident(capture_path.read_bytes()))
                    _write_json(fixture.additional_input_path, additional)
                elif mutation == "prior_index_rewrite":
                    before_path = fixture.root / declared["pre_capture_cache_index"]["path"]
                    before = json.loads(before_path.read_text())
                    key = sorted(before)[0]
                    before[key]["media_type"] = "tampered/type"
                    _write_json(before_path, before)
                    declared["pre_capture_cache_index"].update(
                        _ident(before_path.read_bytes()))
                    _write_json(fixture.additional_input_path, additional)
                elif mutation == "missing_object":
                    target = capture["rows"][0]
                    (fixture.root / target["response"]["cache_path"]).unlink()
                elif mutation == "request_binding":
                    target = next(row for row in capture["rows"]
                                  if row["kind"] == "elibrary_advanced_search_response")
                    target["request"]["body"]["docket"] = "WRONG-DOCKET"
                    _write_json(capture_path, capture)
                    declared["capture_manifest"].update(
                        _ident(capture_path.read_bytes()))
                    _write_json(fixture.additional_input_path, additional)
                elif mutation == "missing_commit":
                    con.execute("DELETE FROM unit_commits WHERE adapter='elibrary_docs' "
                                "AND entity_cid='C000630'")
                    con.commit()
                elif mutation == "missing_docket_document":
                    con.execute("DELETE FROM documents WHERE document_id LIKE "
                                "'dependency-doc-%' AND filing_id IN ("
                                "SELECT filing_id FROM filing_dockets "
                                "WHERE docket='RP26-397-000')")
                    con.commit()
                else:
                    plan_path = fixture.root / "config" / "run_plan.json"
                    plan = json.loads(plan_path.read_text())
                    capture_rel = declared["capture_manifest"]["path"]
                    plan["required_inputs"] = [
                        item for item in plan["required_inputs"]
                        if item.get("path") != capture_rel]
                    _write_json(plan_path, plan)
                with self.assertRaises(records.FinalRecordsRefused):
                    finalize(fixture, con)
                con.close()

    @staticmethod
    def _finalize_related_fixture(fixture, con):
        related = json.loads(fixture.related_path.read_text())
        tests = {"issue_test_evidence": {
            issue_id: ["tests.synthetic.CompleteSuite.test_case_000"]
            for issue_id in fixture.related_ids}}
        additional = [{"input_id": fixture.additional_input_id,
                       "acceptance_result": "accepted_for_candidate"}]
        additional.extend({
            "input_id": input_id,
            "acceptance_result":
                "accepted_as_repair_evidence_not_execution_prerequisite",
        } for input_id in fixture.search_input_ids)
        next(row for row in additional
             if row["input_id"].startswith("elibrary-search:RP26-1091:"))[
                 "build_a_application"] = {
                     "occurrences": [{"accession_number": "20260909-5053",
                                      "accepted": True}],
                 }
        additional.append({
            "input_id": fixture.dependency_input_id,
            "acceptance_result": "accepted_for_candidate",
            "capture": {"new_entries": 11,
                        "required_failed_build_keys": ["x"] * 6},
            "build_a_application": {
                "units": [{"accepted": True} for _ in range(4)]},
        })
        return records._related_repair_records(
            fixture.root, con, related, tests,
            {"generation_id": "a" * 64}, additional)

    def test_r44_capacity_semantic_gate_rejects_status_and_coverage_regressions(self):
        mutations = {
            "availability_regression": (
                "UPDATE observations SET availability='unverified_availability' "
                "WHERE observation_id='capacity-r44-arlington-head'"),
            "coverage_regression": (
                "UPDATE coverage_measured SET outcome='applicability_unknown' "
                "WHERE slot_id='slot-r44-arlington'"),
        }
        for label, statement in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="ferc-related-r44-negative-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                try:
                    # Paired valid control: the unchanged exact fixture passes
                    # the same production final-record gate before injection.
                    rows = self._finalize_related_fixture(fixture, con)
                    r44 = next(row for row in rows if row["issue_id"] ==
                               "R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY")
                    self.assertTrue(r44["evidence"]["build_a_gate"]["accepted"])
                    con.execute(statement)
                    con.commit()
                    with self.assertRaisesRegex(
                            records.FinalRecordsRefused,
                            "capacity_semantic_status"):
                        self._finalize_related_fixture(fixture, con)
                finally:
                    con.close()

    def test_r53_shared_listing_timestamp_gate_accepts_valid_and_rejects_stale(self):
        with tempfile.TemporaryDirectory(prefix="ferc-related-r53-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            try:
                # Paired valid control: filing, listing placeholder and real
                # attachment agree on the captured occurrence time.
                rows = self._finalize_related_fixture(fixture, con)
                finding = next(row for row in rows if row["issue_id"] ==
                               "R53-SHARED-OCCURRENCE-LISTING-TIMESTAMP-CONVERGENCE")
                gate = finding["evidence"]["build_a_gate"]
                self.assertTrue(gate["accepted"])
                self.assertEqual([], gate["checks"]
                                 ["inconsistent_listing_placeholders"])

                # Reintroduce the v21 defect without changing the schema.
                con.execute(
                    "UPDATE documents SET retrieved_at=NULL WHERE document_id=?",
                    ("eLibrary|20241121-3047|listing",))
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused, "R53.*failed Build A"):
                    self._finalize_related_fixture(fixture, con)
            finally:
                con.close()

    def test_r28_lng_routing_and_occurrence_associations_are_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="ferc-r28-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = self._finalize_related_fixture(fixture, con)
            con.close()
            finding = next(row for row in rows if row["issue_id"] ==
                           "R28-LNG-ROUTING-AUTHORITY-ASSOCIATIONS")
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual(44, gate["checks"]["authority_rows"])
            self.assertEqual(64, gate["checks"]["seed_rows"])
            self.assertEqual(6, len(gate["checks"]["required_occurrences"]))
            self.assertEqual(
                9, gate["checks"]["filing_association_integrity"]
                ["cross_entity_document_observations"])

        mutations = (
            "tampered_seed", "undeclared_step", "missing_authority",
            "missing_required_occurrence", "compatibility_anchor",
            "missing_shared_relation", "wrong_shared_facility",
            "association_export_dropped",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r28-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "tampered_seed":
                    seed = (fixture.root / "inputs" / "official_ferc" / "elibrary" /
                            "lng_facility_sources_v1.csv")
                    seed.write_bytes(seed.read_bytes() + b"\n")
                elif mutation == "undeclared_step":
                    plan_path = fixture.root / "config" / "run_plan.json"
                    plan = json.loads(plan_path.read_text())
                    plan["steps"] = []
                    _write_json(plan_path, plan)
                elif mutation == "missing_authority":
                    con.execute("DELETE FROM asset_dockets WHERE asset_id="
                                "'kmi-elba-liquefaction' AND docket='CP14-103'")
                elif mutation == "missing_required_occurrence":
                    con.execute("DELETE FROM filings WHERE source_system='eLibrary' "
                                "AND filing_id='20160601-4008'")
                elif mutation == "compatibility_anchor":
                    con.execute("UPDATE filing_entities "
                                "SET association_role='compatibility_anchor' "
                                "WHERE source_system='eLibrary' "
                                "AND filing_id='20070216-3045'")
                elif mutation == "missing_shared_relation":
                    con.execute("DELETE FROM filing_entities "
                                "WHERE source_system='eLibrary' "
                                "AND filing_id='20260813-5004' AND entity_key LIKE "
                                "'NO-FERC-CID:Elba Liquefaction%'")
                elif mutation == "wrong_shared_facility":
                    con.execute("UPDATE filing_entities SET facility_key='sabine-pass' "
                                "WHERE source_system='eLibrary' "
                                "AND filing_id='20260813-5004' AND entity_key LIKE "
                                "'NO-FERC-CID:Elba Liquefaction%'")
                else:
                    export_path = fixture.root / "exports" / "filing_inventory.csv"
                    header, rows, _ = records._csv_rows(export_path, "fixture export")
                    target = next(row for row in rows
                                  if row["filing_id"] == "20260813-5004")
                    target["associated_entity_roles"] = ""
                    _write_csv(export_path, header, rows)
                con.commit()
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused,
                        "R28.*failed Build A|versioned LNG routing/source index.*mismatch"):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_r29_sibling_contamination_gate_preserves_valid_occurrences(self):
        with tempfile.TemporaryDirectory(prefix="ferc-r29-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = self._finalize_related_fixture(fixture, con)
            con.close()
            finding = next(row for row in rows if row["issue_id"] ==
                           "R29-ELIBRARY-SIBLING-FILER-CONTAMINATION")
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertTrue(all(
                not any(population.values()) for population in
                gate["checks"]["prohibited_pair_populations"].values()))
            self.assertEqual(
                7, len(gate["checks"]["shared_commission_order"]["relations"]))
            self.assertEqual(
                9, len(gate["checks"]["shared_commission_order"]["dockets"]))

        mutations = (
            "known_bad_observation", "known_bad_historical_observation",
            "wrong_occurrence_relation", "correct_package_removed",
            "restored_docket_removed", "commission_relation_removed",
            "relevant_export_dropped",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r29-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "known_bad_observation":
                    fixture._observation(
                        con, "obs-1c494d241863969dfac2cf7a59124ec0",
                        "20240903-5081", "C005518")
                elif mutation == "known_bad_historical_observation":
                    con.execute(
                        "INSERT INTO observation_versions(observation_id,version_seq,"
                        "superseded_at,source_system,filing_id,row_json) VALUES(?,?,?,?,?,?)",
                        ("obs-40400bea4371a78fa5cd19b418156b16", 1,
                         "2026-09-09T00:00:00Z", "eLibrary", "20260522-5206",
                         json.dumps({"entity_key": "C000606",
                                     "accession_number": "20260522-5206"})))
                elif mutation == "wrong_occurrence_relation":
                    fixture._insert(con, "filing_entities", {
                        "source_system": "eLibrary", "filing_id": "20260522-5206",
                        "entity_key": "C000606", "association_role": "named_filer",
                        "facility_key": "", "evidence_ref": "inert wrong relation",
                    })
                elif mutation == "correct_package_removed":
                    con.execute("DELETE FROM filings WHERE source_system='eLibrary' "
                                "AND filing_id='20260522-5194'")
                elif mutation == "restored_docket_removed":
                    con.execute("DELETE FROM filing_dockets "
                                "WHERE filing_id='SYNTHETIC-C005518-IS25-632'")
                elif mutation == "commission_relation_removed":
                    con.execute("DELETE FROM filing_entities "
                                "WHERE source_system='eLibrary' "
                                "AND filing_id='20251029-3064' AND entity_key='C010798'")
                else:
                    export_path = fixture.root / "exports" / "filing_inventory.csv"
                    header, rows, _ = records._csv_rows(export_path, "fixture export")
                    target = next(row for row in rows
                                  if row["filing_id"] == "20260522-5194")
                    target["associated_entity_keys"] = ""
                    _write_csv(export_path, header, rows)
                con.commit()
                association_checks, association_failures = \
                    records._filing_association_integrity(fixture.root, con)
                gate = records._r29_sibling_contamination_gate(
                    fixture.root, con, association_checks, association_failures)
                self.assertFalse(gate["accepted"])
                self.assertTrue(gate["failures"])
                con.close()

    def test_r30_publication_boundary_rejects_incomplete_unit_state(self):
        with tempfile.TemporaryDirectory(prefix="ferc-r30-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = self._finalize_related_fixture(fixture, con)
            con.close()
            finding = next(row for row in rows if row["issue_id"] ==
                           "R30-RETRIEVAL-UNIT-PARTIAL-ROW-LEAKAGE")
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual([], gate["checks"]["unfinished_runs"])
            self.assertEqual([], gate["checks"]["in_progress_checkpoints"])
            self.assertEqual([], gate["checks"]["latest_in_progress_unit_status"])
            self.assertEqual(0, gate["checks"]["orphan_input_inventory_rows"])

        for mutation in ("running_run", "in_progress_checkpoint",
                         "latest_in_progress_status", "orphan_inventory"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r30-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "running_run":
                    con.execute(
                        "UPDATE runs SET status='running',finished_at=NULL "
                        "WHERE run_id='run-elibrary-fixture'")
                elif mutation == "in_progress_checkpoint":
                    fixture._insert(con, "checkpoints", {
                        "adapter": "synthetic", "entity_cid": "C-R30",
                        "scope_key": "2024:2026:fixture", "state": "in_progress",
                        "attempts": 1, "last_run_id": "run-elibrary-fixture",
                        "updated_at": "2026-09-09T00:00:00Z",
                    })
                elif mutation == "latest_in_progress_status":
                    fixture._insert(con, "run_unit_status", {
                        "run_id": "run-elibrary-fixture", "adapter": "synthetic",
                        "entity_cid": "C-R30", "scope_key": "2024:2026:fixture",
                        "seq": 1, "state": "in_progress", "detail": "fault injected",
                        "recorded_at": "2026-09-09T00:00:00Z",
                    })
                else:
                    fixture._insert(con, "run_input_inventory", {
                        "run_id": "missing-run", "adapter": "synthetic",
                        "entity_cid": "C-R30", "input_order": 1,
                        "source_system": "eLibrary", "filing_id": "R30-input",
                        "accession_number": "R30-input",
                        "observed_at": "2026-09-09T00:00:00Z",
                    })
                con.commit()
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused, "R30.*failed Build A"):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_r36_named_lng_filer_gate_binds_database_quality_and_export(self):
        with tempfile.TemporaryDirectory(prefix="ferc-r36-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = self._finalize_related_fixture(fixture, con)
            con.close()
            finding = next(row for row in rows if row["issue_id"] ==
                           "R36-NAMED-LNG-FILER-COVERAGE-GATE")
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual(8, len(gate["checks"]["exact_observations"]))
            self.assertEqual(8, len(gate["checks"]
                                    ["expanded_cross_anchor_observation_ids"]))
            self.assertEqual(5, len(gate["checks"]["route_anchors"]))
            self.assertEqual(
                {"current_in_window": 3, "historical_or_noncurrent": 5,
                 "filed_occurrences": 3, "indexed_occurrences": 5},
                gate["checks"]["quality_counts"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             finding["gates"]["data"])
            self.assertEqual("matched_to_published_generation_and_database",
                             finding["gates"]["exports"])

        mutations = (
            "missing_observation", "facility_subject", "quality_association",
            "missing_measurement", "missing_export_row",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r36-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                target_id = "obs-46bac3c37b38fc21dc86d98ff48ee868"
                if mutation == "missing_observation":
                    con.execute("DELETE FROM observations WHERE observation_id=?",
                                (target_id,))
                elif mutation == "facility_subject":
                    con.execute(
                        "UPDATE filing_entities SET association_role='facility_subject' "
                        "WHERE source_system='eLibrary' AND filing_id='20241121-3047' "
                        "AND entity_key='NO-FERC-CID:Elba Liquefaction Company, L.L.C.'")
                elif mutation == "quality_association":
                    path = fixture.root / "verification" / "coverage_generation.json"
                    body = json.loads(path.read_text())
                    row = next(item for item in body["document_occurrence_quality"]["rows"]
                               if item["observation_id"] == target_id)
                    row["entity_association"] = \
                        "reviewed_shared_lng_facility:named_filer:CP99-999"
                    _write_json(path, body)
                elif mutation == "missing_measurement":
                    con.execute(
                        "DELETE FROM coverage_measured WHERE slot_id IN "
                        "(SELECT slot_id FROM coverage_expected WHERE entity_key=? "
                        "AND metric_id='lng_liquefaction_capacity')",
                        ("NO-FERC-CID:Elba Liquefaction Company, L.L.C.",))
                else:
                    path = fixture.root / "exports" / "coverage_by_slot.csv"
                    with path.open(newline="", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        header = reader.fieldnames
                        rows = list(reader)
                    rows = [row for row in rows if row["slot_id"] != "slot-r36-01"]
                    _write_csv(path, header, rows)
                con.commit()
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused,
                        "R28.*failed Build A|R36.*failed Build A"):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_r37_taxonomy_cache_plan_and_replay_boundary_is_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="ferc-r37-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = self._finalize_related_fixture(fixture, con)
            finding = next(row for row in rows if row["issue_id"] ==
                           "R37-STALE-TAXONOMY-CACHE-FREEZE")
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual(15, len(gate["checks"]["pin_routes"]))
            self.assertEqual(1, len(gate["checks"]["exercised_taxonomy_routes"]))
            wrong_code = records._r37_taxonomy_cache_gate(
                fixture.root, con, {"metadata": {"code_snapshot": "0" * 64}})
            self.assertFalse(wrong_code["accepted"])
            self.assertIn(
                "coverage and published exports use different code snapshots",
                wrong_code["failures"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             finding["gates"]["data"])
            self.assertEqual("matched_to_published_generation_and_database",
                             finding["gates"]["exports"])
            con.close()

        mutations = (
            "stale_freeze", "missing_object", "stale_plan_declaration",
            "missing_coverage_input", "stale_active_plan",
            "wrong_recorded_input", "wrong_taxonomy_source", "changed_coverage_output",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r37-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                pins_path = fixture.root / "config" / "taxonomy_pins.json"
                plan_path = fixture.root / "config" / "run_plan.json"
                status_path = fixture.root / "verification" / "replay_step_status.json"
                if mutation == "stale_freeze":
                    body = json.loads(pins_path.read_text())
                    body["source_cache_index_sha256_at_freeze"] = "0" * 64
                    _write_json(pins_path, body)
                elif mutation == "missing_object":
                    pin = fixture.taxonomy_pins[0]
                    content_hash = pin["evidence_ref"].rsplit("#sha256=", 1)[1]
                    (fixture.root / "source_cache" / "objects" /
                     content_hash[:2] / content_hash).unlink()
                elif mutation == "stale_plan_declaration":
                    body = json.loads(plan_path.read_text())
                    declaration = next(row for row in body["required_inputs"]
                                       if row["path"] == "config/taxonomy_pins.json")
                    declaration["sha256"] = "0" * 64
                    _write_json(plan_path, body)
                elif mutation == "missing_coverage_input":
                    body = json.loads(plan_path.read_text())
                    step = next(row for row in body["steps"]
                                if row["id"] == "generate_and_measure_coverage")
                    step["inputs"].remove("source_cache/index.json")
                    _write_json(plan_path, body)
                elif mutation == "stale_active_plan":
                    body = json.loads(status_path.read_text())
                    body["active_plan_sha256"] = "0" * 64
                    _write_json(status_path, body)
                elif mutation == "wrong_recorded_input":
                    body = json.loads(status_path.read_text())
                    recorded = next(
                        row for row in body["steps"]["generate_and_measure_coverage"]
                        ["input_identity"]["inputs"]
                        if row["name"] == "source_cache/index.json")
                    recorded["sha256"] = "0" * 64
                    _write_json(status_path, body)
                elif mutation == "wrong_taxonomy_source":
                    con.execute(
                        "UPDATE taxonomy_sources SET retrieved=0 "
                        "WHERE form='Form 2' AND taxonomy_version='2024-04-01' "
                        "AND artefact='entry_point'")
                else:
                    path = fixture.root / "verification" / "coverage_generation.json"
                    body = json.loads(path.read_text())
                    body["post_status_mutation"] = True
                    _write_json(path, body)
                con.commit()
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused, "R37.*failed Build A"):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_r38_ioc_lineage_gate_binds_exact_data_validation_and_exports(self):
        issue_id = "R38-IOC-LINEAGE-VALIDATOR-SCOPE-AND-EXTENDED-POINT-CODE"
        observation_id = "obs-65199973861961ad89dda4a2f43be6f5"
        population_id = "pop-4637ebf709b91d5a98cb86dffd9945d9"
        with tempfile.TemporaryDirectory(prefix="ferc-r38-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            rows = self._finalize_related_fixture(fixture, con)
            finding = next(row for row in rows if row["issue_id"] == issue_id)
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             finding["gates"]["data"])
            self.assertEqual("matched_to_published_generation_and_database",
                             finding["gates"]["exports"])
            con.close()

        mutations = (
            "failed_validation", "stale_green_validation", "truncated_point_code",
            "wrong_member_digest",
            "missing_contributing_edge", "missing_observation_export",
            "missing_lineage_export",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r38-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation in {"failed_validation", "stale_green_validation"}:
                    body = json.loads(fixture.validation.read_text())
                    target = next(row for row in body["results"]
                                  if row["check"] ==
                                  "IOC aggregates redraw from their persisted source populations")
                    if mutation == "failed_validation":
                        target["status"] = "FAIL"
                        target["detail"] = "fault-injected real validator failure"
                        body["summary"] = {
                            "PASS": 2, "FAIL": 1, "ERROR": 0, "SKIPPED": 0}
                    else:
                        target["detail"] += "; stale green file substituted after step 10"
                    _write_json(fixture.validation, body)
                elif mutation == "truncated_point_code":
                    con.execute(
                        "UPDATE source_facts SET typed_dims_json=? "
                        "WHERE source_system='eLibrary' AND filing_id='20250808-5173' "
                        "AND source_fact_id='P00005'",
                        (json.dumps({"point_code": "13"}, sort_keys=True),))
                elif mutation == "wrong_member_digest":
                    con.execute(
                        "UPDATE lineage_populations SET member_digest=? "
                        "WHERE population_id=?", ("0" * 64, population_id))
                elif mutation == "missing_contributing_edge":
                    con.execute(
                        "DELETE FROM lineage_edges WHERE observation_id=? "
                        "AND input_role='contributing_row'", (observation_id,))
                elif mutation == "missing_observation_export":
                    path = fixture.root / "exports" / "canonical_observations.csv"
                    header, rows, _ = records._csv_rows(path, "fixture export")
                    _write_csv(path, header, [row for row in rows
                                             if row["observation_id"] != observation_id])
                else:
                    path = fixture.root / "exports" / "lineage_edges.csv"
                    header, rows, _ = records._csv_rows(path, "fixture export")
                    _write_csv(path, header, [
                        row for row in rows
                        if not (row["observation_id"] == observation_id
                                and row["input_source_fact_id"] == "P00005")])
                con.commit()
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused, "R38.*failed Build A"):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_r39_blank_point_code_gate_requires_source_retention_and_export_removal(self):
        issue_id = "R39-BLANK-IOC-POINT-CODE-PROMOTION"
        head_id = "obs-afaef0b47b3e803331ec404a0c7d6469"
        old_id = "obs-b88fa75ca303069090bff41279ed43d7"
        old_population = "pop-7351c8563a7d53f0317eab2a790376f5"
        with tempfile.TemporaryDirectory(prefix="ferc-r39-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            self.assertEqual(ioc.audit_lineage(_QueryView(con), "C000654"), [])
            rows = self._finalize_related_fixture(fixture, con)
            finding = next(row for row in rows if row["issue_id"] == issue_id)
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             finding["gates"]["data"])
            self.assertEqual("matched_to_published_generation_and_database",
                             finding["gates"]["exports"])
            con.close()

        mutations = (
            "missing_blank_fact", "blank_fact_promoted", "old_observation_restored",
            "old_population_restored", "warning_removed", "candidate_count_changed",
            "head_export_removed", "old_export_restored", "old_lineage_export_restored",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r39-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "missing_blank_fact":
                    con.execute(
                        "DELETE FROM source_facts WHERE source_system='eLibrary' "
                        "AND filing_id='20251001-5149' AND source_fact_id='P01406'")
                elif mutation == "blank_fact_promoted":
                    con.execute(
                        "UPDATE source_facts SET typed_dims_json=? "
                        "WHERE source_system='eLibrary' AND filing_id='20251001-5149' "
                        "AND source_fact_id='P01406'",
                        (json.dumps({"point_code": "S9"}, sort_keys=True),))
                elif mutation == "old_observation_restored":
                    fixture._insert(con, "observations", {
                        "observation_id": old_id, "entity_key": "C000654",
                        "metric_id": "ioc_points", "source_regime": "Form 549B IOC",
                        "period_basis": "snapshot", "instant_date": "2025-10-01",
                        "scope": "location detail only | point code blank",
                        "unit": "codes", "value_text": " (code not in manual)",
                        "availability": "present", "origin": "elibrary_document",
                        "method": "filed", "version_status": "original",
                        "validation": "scope_incompatible", "source_system": "eLibrary",
                        "filing_id": "20251001-5149",
                        "accession_number": "20251001-5149", "candidate_count": 2,
                        "selector": "ioc_p_records",
                    })
                elif mutation == "old_population_restored":
                    fixture._insert(con, "lineage_populations", {
                        "population_id": old_population,
                        "observation_id": head_id, "source_system": "eLibrary",
                        "source_table": "source_facts",
                        "filing_ids": json.dumps(["20251001-5149"]),
                        "inclusion_rule": ("P records of this filing occurrence whose "
                                           "item yh (point code) is  -- blank code"),
                        "exclusion_rule": "all P records with nonblank codes are excluded",
                        "row_count": 2, "candidate_count": 1821,
                        "excluded_count": 1819,
                        "member_key": "filing_id:source_fact_id",
                        "member_digest": ("b0fcb6b98c979cc687d9b7fb85bde7b5d7a9742a"
                                          "40d999fbaecae6a9b34241a7"),
                        "members_sample": json.dumps([
                            "20251001-5149:P01406", "20251001-5149:P01457"]),
                        "aggregate_unit": "codes",
                        "created_at": "2026-09-09T00:00:00Z",
                    })
                elif mutation == "warning_removed":
                    con.execute("UPDATE observations SET qa_flags='' "
                                "WHERE observation_id=?", (head_id,))
                elif mutation == "candidate_count_changed":
                    con.execute("UPDATE observations SET candidate_count=1819 "
                                "WHERE observation_id=?", (head_id,))
                elif mutation in ("head_export_removed", "old_export_restored"):
                    path = fixture.root / "exports" / "canonical_observations.csv"
                    header, rows, _ = records._csv_rows(path, "fixture export")
                    if mutation == "head_export_removed":
                        rows = [row for row in rows if row["observation_id"] != head_id]
                    else:
                        old = dict(next(row for row in rows
                                        if row["observation_id"] == head_id))
                        old["observation_id"] = old_id
                        old["scope"] = "location detail only | point code blank"
                        old["value_text"] = " (code not in manual)"
                        old["candidate_count"] = "2"
                        rows.append(old)
                    _write_csv(path, header, rows)
                else:
                    path = fixture.root / "exports" / "lineage_edges.csv"
                    header, rows, _ = records._csv_rows(path, "fixture export")
                    old = dict(next(row for row in rows
                                    if row["observation_id"] == head_id))
                    old["observation_id"] = old_id
                    old["input_population_id"] = old_population
                    rows.append(old)
                    _write_csv(path, header, rows)
                con.commit()
                expected_gate = ("R3[89].*failed Build A"
                                 if mutation == "old_observation_restored"
                                 else "R39.*failed Build A")
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused, expected_gate):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_r40_zero_point_census_gate_binds_exact_rows_and_exports(self):
        issue_id = "R40-IOC-ZERO-POINT-CENSUS-CANDIDATE-COUNT"
        accession = "20240102-5176"
        observation_id = "obs-4952218c315d35d13923a19928873a83"
        population_id = "pop-bb8b4359313d73868e270683227f4d68"
        expected_accessions = {
            "20240102-5176", "20240401-5209", "20240701-5063",
            "20241001-5081", "20250102-5082", "20250401-5104",
            "20250701-5084", "20251001-5072", "20260102-5094",
            "20260331-5115", "20260701-5148",
        }
        with tempfile.TemporaryDirectory(prefix="ferc-r40-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            direct = records._r40_zero_point_census_gate(fixture.root, con)
            self.assertTrue(direct["accepted"], direct["failures"])
            occurrences = direct["checks"]["occurrences"]
            self.assertEqual(11, len(occurrences))
            self.assertEqual(
                expected_accessions,
                {row["accession_number"] for row in occurrences})
            self.assertTrue(all(row["accepted"] for row in occurrences))
            self.assertTrue(all(
                row["observation"][0]["candidate_count"] == 0
                and row["observation"][0]["availability"] == "source_blank"
                and row["observation"][0]["value_text"] is None
                and row["observation"][0]["unit"] is None
                and row["population"][0]["row_count"] == 0
                and row["population"][0]["candidate_count"] == 0
                and row["population"][0]["excluded_count"] == 0
                for row in occurrences))
            rows = self._finalize_related_fixture(fixture, con)
            finding = next(row for row in rows if row["issue_id"] == issue_id)
            gate = finding["evidence"]["build_a_gate"]
            self.assertTrue(gate["accepted"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             finding["gates"]["data"])
            self.assertEqual("matched_to_published_generation_and_database",
                             finding["gates"]["exports"])
            con.close()

        mutations = (
            "candidate_count_null", "candidate_count_one", "inserted_p_fact",
            "canonical_export_removed", "canonical_export_mutated",
            "missing_population_edge",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-r40-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "candidate_count_null":
                    con.execute(
                        "UPDATE observations SET candidate_count=NULL "
                        "WHERE observation_id=?", (observation_id,))
                elif mutation == "candidate_count_one":
                    con.execute(
                        "UPDATE observations SET candidate_count=1 "
                        "WHERE observation_id=?", (observation_id,))
                elif mutation == "inserted_p_fact":
                    fixture._insert(con, "source_facts", {
                        "source_system": "eLibrary", "filing_id": accession,
                        "source_fact_id": "P00001", "document_order": 1,
                        "concept_qname": "ferc549b:P",
                        "concept_local": "ioc_point_record",
                        "context_id": "contract0000", "unit_id": "T",
                        "unit_text": "Dth", "value_as_filed": (
                            "P\tS8\tFAULT-INJECTED POINT\t95\t1\tZONE\t0"),
                        "is_nil": 0, "period_class": "snapshot",
                        "instant": "2024-01-01", "current_or_prior": "current",
                        "typed_dims_json": json.dumps({
                            "point_code": "S8", "point_id": "1",
                            "point_name": "FAULT-INJECTED POINT",
                        }, sort_keys=True),
                        "taxonomy_version": "no_version_indicator_in_format",
                    })
                elif mutation in (
                        "canonical_export_removed", "canonical_export_mutated"):
                    path = fixture.root / "exports" / "canonical_observations.csv"
                    header, exported, _ = records._csv_rows(path, "fixture export")
                    if mutation == "canonical_export_removed":
                        exported = [row for row in exported
                                    if row["observation_id"] != observation_id]
                    else:
                        target = next(row for row in exported
                                      if row["observation_id"] == observation_id)
                        target["candidate_count"] = "1"
                    _write_csv(path, header, exported)
                else:
                    con.execute(
                        "DELETE FROM lineage_edges WHERE observation_id=? "
                        "AND input_population_id=?",
                        (observation_id, population_id))
                con.commit()
                direct = records._r40_zero_point_census_gate(fixture.root, con)
                self.assertFalse(direct["accepted"])
                target_check = next(
                    row for row in direct["checks"]["occurrences"]
                    if row["accession_number"] == accession)
                self.assertFalse(target_check["accepted"])
                with self.assertRaisesRegex(
                        records.FinalRecordsRefused, "(?:R39|R40).*failed Build A"):
                    self._finalize_related_fixture(fixture, con)
                con.close()

    def test_w5_horizon_attached_unit_counterevidence_is_occurrence_bound(self):
        with tempfile.TemporaryDirectory(prefix="ferc-w5-valid-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            gate = records._w5_horizon_attached_unit_counterevidence_gate(
                fixture.root, con)
            self.assertTrue(gate["accepted"], gate["failures"])
            self.assertEqual(
                set(records.W5_HORIZON_ATTACHED_UNIT_FACTS),
                {row["document_fact_id"]
                 for row in gate["checks"]["facts"]})
            con.close()

        first_id = sorted(records.W5_HORIZON_ATTACHED_UNIT_FACTS)[0]
        for mutation in ("missing", "longer_number", "wrong_unit",
                         "wrong_occurrence", "export_removed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-w5-bad-") as td:
                fixture = Fixture(td)
                con = sqlite3.connect(fixture.db)
                con.row_factory = sqlite3.Row
                if mutation == "missing":
                    con.execute("DELETE FROM document_facts WHERE document_fact_id=?",
                                (first_id,))
                elif mutation == "longer_number":
                    con.execute(
                        "UPDATE document_facts SET verbatim_span=? "
                        "WHERE document_fact_id=?",
                        ("Firm Transportation Service FTS 1,380,000MMBTU/d", first_id))
                elif mutation == "wrong_unit":
                    con.execute(
                        "UPDATE document_facts SET unit='Mcf/day' "
                        "WHERE document_fact_id=?", (first_id,))
                elif mutation == "wrong_occurrence":
                    con.execute(
                        "UPDATE document_facts SET filing_id='20250227-9999' "
                        "WHERE document_fact_id=?", (first_id,))
                else:
                    path = fixture.root / "exports" / "document_facts.csv"
                    header, rows, _ = records._csv_rows(path, "fixture export")
                    _write_csv(path, header, [row for row in rows
                                             if row["document_fact_id"] != first_id])
                con.commit()
                gate = records._w5_horizon_attached_unit_counterevidence_gate(
                    fixture.root, con)
                self.assertFalse(gate["accepted"])
                con.close()

    def test_supplemental_population_overlap_and_unobserved_test_are_refused(self):
        for mutation in ("overlap", "unobserved"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-related-bad-") as td:
                fixture = Fixture(td)
                related = json.loads(fixture.related_path.read_text())
                if mutation == "overlap":
                    related["rows"][0]["issue_id"] = "A01"
                else:
                    related["rows"][0]["target_test_ids"] = [
                        "tests.synthetic.Missing.test_not_executed"]
                _write_json(fixture.related_path, related)
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_basis_placeholder_gate_rejects_markers_but_accepts_domain_term(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-basis-marker-") as td:
            fixture = Fixture(td)
            body = json.loads(fixture.related_path.read_text())
            body["rows"][0]["root_cause"] = (
                "A listing placeholder retained its prior capture boundary.")
            _write_json(fixture.related_path, body)
            valid = records._load_and_validate_supplemental_drafts(
                fixture.root, fixture.audit, fixture.related_path,
                fixture.additional_input_path)
            self.assertEqual(records.RELATED_FINDING_IDS,
                             valid["ids"]["related findings"])

            for marker in ("TODO", "TODO: investigate", "placeholder",
                           "placeholder text", "FIXME before release"):
                with self.subTest(marker=marker):
                    body["rows"][0]["root_cause"] = marker
                    _write_json(fixture.related_path, body)
                    with self.assertRaisesRegex(
                            records.FinalRecordsRefused, "placeholder root cause"):
                        records._load_and_validate_supplemental_drafts(
                            fixture.root, fixture.audit, fixture.related_path,
                            fixture.additional_input_path)

    def test_complete_fixture_publishes_and_preserves_explicit_limits(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
            capacity_population = {
                (accession, entity): count
                for accession, entity, count in con.execute(
                    "SELECT accession_number,entity_key,COUNT(*) FROM observations "
                    "WHERE accession_number IN "
                    "('20250225-5101','20250227-5034','20260202-5047') "
                    "GROUP BY accession_number,entity_key")
            }
            self.assertEqual({
                ("20250225-5101", "C001685"): 19,
                ("20250227-5034", "C000584"): 13,
                ("20260202-5047", "C001506"): 2,
            }, capacity_population)
            blocked_null = list(con.execute(
                "SELECT availability,validation,value_num FROM observations "
                "WHERE accession_number IN "
                "('20250225-5101','20250227-5034','20260202-5047') "
                "AND validation='blocked_ambiguity' AND value_num IS NULL"))
            self.assertEqual(22, len(blocked_null))
            self.assertEqual({("interpretation_blocked", "blocked_ambiguity", None)},
                             set(blocked_null))
            self.assertEqual(18, con.execute(
                "SELECT COUNT(*) FROM document_facts "
                "WHERE filing_id='20250225-5101' AND value_num IS NULL"
            ).fetchone()[0])
            self.assertEqual({
                ("20250225-5101", "interpretation_blocked", 1),
                ("20260202-5047", "interpretation_blocked", 1),
            }, set(con.execute(
                "SELECT o.accession_number,m.outcome,m.source_matched "
                "FROM coverage_measured m JOIN observations o "
                "ON o.observation_id=m.observation_id "
                "WHERE o.accession_number IN ('20250225-5101','20260202-5047')")))
            self.assertEqual(12, con.execute(
                "SELECT COUNT(*) FROM observations WHERE source_regime="
                "'Form 549B Capacity' AND availability='present' "
                "AND validation='pass' AND value_num IS NOT NULL"
            ).fetchone()[0])
            con.close()
            self.assertEqual(0, records.main(fixture.args()))
            for name in records.FINAL_FILES + (records.RECEIPT_NAME,):
                self.assertTrue((fixture.output / name).is_file(), name)
            inputs = json.loads((fixture.output /
                                 "FINAL_INPUT_INVENTORY_13.json").read_text())
            self.assertFalse(inputs["draft"])
            self.assertEqual(13, len(inputs["rows"]))
            self.assertEqual(1, inputs["counts"]["disposition"]
                             ["captured_and_cached_not_applied"])
            ledger = json.loads((fixture.output /
                                 "FINAL_REPAIR_LEDGER.json").read_text())
            self.assertEqual(64, ledger["population"]["audited_final_findings"])
            self.assertEqual(33, ledger["population"]
                             ["related_findings_discovered_during_repair"])
            self.assertEqual(97, len(ledger["rows"]))
            r31 = next(row for row in ledger["rows"] if row["issue_id"] ==
                       "R31-REDUNDANT-INPUT-SNAPSHOT-PACKAGING-MISMATCH")
            self.assertEqual("verified_at_read_only_build_a_input_boundary",
                             r31["gates"]["data"])
            self.assertTrue(r31["evidence"]["build_a_gate"]["accepted"])
            related_by_id = {row["issue_id"]: row for row in ledger["rows"]
                             if row["source_row_type"] == "related_repair_finding"}
            self.assertEqual(33, len(related_by_id))
            for issue_id, disposition in {
                    "R41-IOC-INTEGRATION-FIXTURE-FK-LIFECYCLE":
                        "test_fixture_repaired_and_observed_passing",
                    "R42-A13-LAST-GOOD-ATTEMPT-RECEIPT-ASSERTION":
                        "acceptance_assertion_repaired_and_observed_passing",
                    "R43-FINAL-LEDGER-SUBSET-HIDDEN-W5-DEPENDENCY":
                        "finalizer_test_wiring_and_dependency_scope_repaired",
                    }.items():
                finding = related_by_id[issue_id]
                self.assertEqual(disposition, finding["disposition"])
                self.assertEqual("not_applicable", finding["gates"]["data"])
                self.assertEqual("not_applicable", finding["gates"]["exports"])
                self.assertTrue(finding["evidence"]["build_a_gate"]["accepted"])
            r44 = related_by_id["R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY"]
            self.assertEqual("stored_text_normalised_and_capacity_semantic_gates_verified",
                             r44["disposition"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             r44["gates"]["data"])
            self.assertEqual("matched_to_published_generation_and_database",
                             r44["gates"]["exports"])
            self.assertTrue(r44["evidence"]["build_a_gate"]["accepted"])
            r44_checks = r44["evidence"]["build_a_gate"]["checks"]
            self.assertEqual([], r44_checks["portable_text"]["stored_control_cells"])
            self.assertTrue(all(
                not export["binary_c0_controls"]
                for export in r44_checks["portable_text"]["exports"]))
            semantic = r44_checks["capacity_semantic_status"]
            self.assertEqual(
                ["20250225-5101", "20250227-5034", "20260202-5047"],
                semantic["affected_accessions"])
            self.assertEqual(34, semantic["affected_observations"])
            self.assertEqual(22, semantic["blocked_null_observations"])
            self.assertEqual(19, semantic["arlington_observations"])
            self.assertEqual(18, semantic["arlington_document_facts"])
            self.assertEqual({
                ("20250225-5101", "interpretation_blocked"),
                ("20260202-5047", "interpretation_blocked"),
            }, {(row["accession_number"], row["outcome"])
                for row in semantic["matched_coverage"]})
            self.assertEqual(2, len(semantic["matched_coverage"]))
            self.assertEqual(12, semantic["clean_numeric_present_pass_controls"])
            self.assertEqual([], semantic["export_failures"])
            for issue_id, disposition in {
                    "R45-BUILD-B-PENDING-INSTRUCTION-CORRUPTS-EXTRACTION":
                        "deferred_build_b_mandatory_and_immutable_extraction_documented",
                    "R46-CANDIDATE-MATERIALIZER-RETAINS-HIDDEN-SCRATCH":
                        "candidate_materializer_excludes_scratch_and_refuses_reuse",
                    "R47-FORM549D-HIDDEN-CANDIDATE-WRITE":
                        "form549d_resolution_routed_to_declared_output_root",
                    "R48-BUILD-B-REPLAY-LEDGER-OBJECT-ORDER":
                        "replay_order_bound_to_explicit_step_ordinals",
                    "R49-BUILD-B-ACCEPTANCE-MIRROR-MISSING-DEPENDENCY":
                        "acceptance_mirror_includes_declared_regression_dependency",
                    "R52-BUILD-B-REFUSAL-COMPARISON-EVIDENCE":
                        "comparison_evidence_recorded_before_fail_closed_gate",
                    "R54-FULL-RELEASE-INSTRUCTION-DEFER-GATE":
                        "full_release_instructions_match_the_deferred_build_b_gate",
                    "R55-PLACEHOLDER-MARKER-DOMAIN-TERM-COLLISION":
                        "draft_marker_gate_distinguishes_domain_specific_language",
                    }.items():
                finding = related_by_id[issue_id]
                self.assertEqual(disposition, finding["disposition"])
                self.assertEqual("not_applicable", finding["gates"]["data"])
                self.assertEqual("not_applicable", finding["gates"]["exports"])
            for issue_id, disposition in {
                    "R50-ELIBRARY-CAPTURE-TIME-DETERMINISM":
                        "cache_bound_source_times_applied_and_exact",
                    "R51-RUN-ENVELOPE-PUBLICATION-DETERMINISM":
                        "lifecycle_times_narrowly_normalised_and_exports_stably_ordered",
                    }.items():
                finding = related_by_id[issue_id]
                self.assertEqual(disposition, finding["disposition"])
                self.assertEqual("verified_in_read_only_build_a_database",
                                 finding["gates"]["data"])
                self.assertEqual("matched_to_published_generation_and_database",
                                 finding["gates"]["exports"])
            r53 = related_by_id[
                "R53-SHARED-OCCURRENCE-LISTING-TIMESTAMP-CONVERGENCE"]
            self.assertEqual(
                "shared_listing_capture_converges_without_enriching_attachments",
                r53["disposition"])
            self.assertEqual("verified_in_read_only_build_a_database",
                             r53["gates"]["data"])
            self.assertEqual("not_applicable", r53["gates"]["exports"])
            self.assertTrue(r53["evidence"]["build_a_gate"]["accepted"])
            self.assertEqual([], r53["evidence"]["build_a_gate"]["checks"]
                             ["inconsistent_listing_placeholders"])
            counterevidence_row = next(row for row in ledger["rows"]
                                       if row["issue_id"] == "C01")
            self.assertEqual(
                {"claim_boundary": "exact rows rebut one classification only",
                 "facts": ["dfact-one", "dfact-two"]},
                counterevidence_row["evidence"]["repair_counterevidence"])
            additional = json.loads((fixture.output /
                                     "FINAL_ADDITIONAL_INPUTS_DISCOVERED.json").read_text())
            self.assertEqual(5, len(additional["rows"]))
            self.assertEqual(
                {"accepted_for_candidate": 2,
                 "accepted_as_repair_evidence_not_execution_prerequisite": 3},
                dict(additional["counts"]["acceptance_result"]))
            by_id = {row["input_id"]: row for row in additional["rows"]}
            self.assertEqual("accession_package",
                             by_id[fixture.additional_input_id]["input_kind"])
            self.assertEqual(
                "elibrary_dependency_capture_bundle",
                by_id[fixture.dependency_input_id]["input_kind"])
            self.assertTrue(
                by_id[fixture.dependency_input_id]["build_a_application"]["accepted"])
            report = (fixture.output / "INTEGRATED_REPAIR_REPORT.md").read_text()
            self.assertIn("1 repair-discovered accession package applied", report)
            self.assertIn("1 mandatory eLibrary dependency bundle applied", report)
            self.assertIn(
                "3 captured search responses accepted as diagnostic evidence rather than "
                "final-plan execution prerequisites", report)
            exceptions = json.loads((fixture.output /
                                     "FINAL_EXCEPTION_DISPOSITIONS_34.json").read_text())
            capacity = [r for r in exceptions["rows"]
                        if r["occurrence_or_obligation"]["adapter"] == "capacity"]
            self.assertEqual(2, len(capacity))
            self.assertTrue(all(r["disposition"] ==
                                "safely_gated_hash_bound_offline_ocr" for r in capacity))
            self.assertTrue(all("OCR" in r["residual_limitation"] for r in capacity))
            index = json.loads((fixture.output /
                               "FINAL_TEST_EVIDENCE_INDEX.json").read_text())
            self.assertEqual("PASS_WITH_EXPLICIT_LIMITS", index["status"])
            self.assertEqual(400, index["test_runs"]["runs"][0]["counts"]["executed"])
            receipt = json.loads((fixture.output / records.RECEIPT_NAME).read_text())
            self.assertEqual(set(records.FINAL_FILES), set(receipt["files"]))

    def test_accepted_search_evidence_is_not_an_additional_input_limit(self):
        accepted = [
            {"acceptance_result": "accepted_for_candidate"},
            {"acceptance_result":
             "accepted_as_repair_evidence_not_execution_prerequisite"},
        ]
        self.assertFalse(records._additional_inputs_have_limits(accepted))
        self.assertTrue(records._additional_inputs_have_limits(
            accepted + [{"acceptance_result": "qualified_open_data_application"}]))

    def test_duplicate_wrong_population_and_stale_final_draft_are_refused(self):
        for mutation in ("duplicate", "short", "stale"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-records-bad-draft-") as td:
                fixture = Fixture(td)
                body = json.loads(fixture.ledger_path.read_text())
                if mutation == "duplicate":
                    body["rows"][1]["issue_id"] = body["rows"][0]["issue_id"]
                elif mutation == "short":
                    body["rows"].pop()
                    body["counts"]["rows"] = 63
                else:
                    body["draft"] = False
                    body["schema"] = "ferc-final-repair-ledger-v1"
                _write_json(fixture.ledger_path, body)
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_unclaimed_empty_evidence_and_placeholder_basis_are_refused(self):
        for mutation in ("unclaimed", "empty_evidence", "placeholder"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-records-unclaimed-") as td:
                fixture = Fixture(td)
                body = json.loads(fixture.exception_path.read_text())
                if mutation == "unclaimed":
                    body["rows"][0]["disposition"] = "unclaimed"
                elif mutation == "empty_evidence":
                    body["rows"][0]["evidence"] = []
                else:
                    body["rows"][0]["root_cause"] = "TODO"
                _write_json(fixture.exception_path, body)
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_declared_source_and_test_log_hash_mismatches_are_refused(self):
        for mutation in ("source", "log"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-records-bad-hash-") as td:
                fixture = Fixture(td)
                if mutation == "source":
                    (fixture.root / "config" / "universe.csv").write_bytes(b"changed\n")
                else:
                    (fixture.logs / "complete.log").write_bytes(
                        b"Ran 400 tests in 1.0s\n\nOK (skipped=1)\nchanged\n")
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_zero_case_and_required_skip_cannot_be_green(self):
        for mutation in ("zero", "required_skip"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-records-bad-test-") as td:
                fixture = Fixture(td)
                manifest = json.loads(fixture.test_manifest.read_text())
                run = manifest["runs"][0]
                if mutation == "zero":
                    log = b"Ran 0 tests in 0.0s\n\nOK\n"
                    (fixture.logs / "complete.log").write_bytes(log)
                    run.update(_ident(log))
                    run["counts"] = {"collected": 0, "executed": 0, "pass": 0,
                                     "fail": 0, "error": 0, "skip": 0,
                                     "xfail": 0, "xpass": 0}
                    run["skip_details"] = []
                else:
                    run["skip_details"][0]["required"] = True
                _write_json(fixture.test_manifest, manifest)
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_wrong_export_schema_is_rejected_even_with_matching_new_receipt(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-schema-") as td:
            fixture = Fixture(td)
            path = fixture.root / "exports" / "canonical_observations.csv"
            path.write_text("observation_id,wrong\nonly,bad\n", encoding="utf-8")
            fixture.rewrite_publication()
            self.assertEqual(2, records.main(fixture.args()))
            self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_same_schema_semantic_export_mutations_are_refused(self):
        for filename, column in (("canonical_observations.csv", "value"),
                                 ("source_manifest.csv", "content_hash")):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(
                    prefix="ferc-final-records-semantic-export-") as td:
                fixture = Fixture(td)
                path = fixture.root / "exports" / filename
                with path.open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    header, rows = list(reader.fieldnames or []), list(reader)
                self.assertTrue(rows)
                rows[0][column] = "corrupt-but-same-schema"
                _write_csv(path, header, rows)
                fixture.rewrite_publication()
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_current_code_and_executed_test_ids_are_independently_bound(self):
        for mutation in ("code", "test_id"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-final-records-boundary-") as td:
                fixture = Fixture(td)
                if mutation == "code":
                    (fixture.root / "exporters.py").write_text(
                        "changed after publication\n", encoding="utf-8")
                else:
                    manifest = json.loads(fixture.test_manifest.read_text())
                    manifest["issue_test_evidence"][fixture.issue_ids[0]] = [
                        "tests.synthetic.CompleteSuite.test_not_executed"]
                    _write_json(fixture.test_manifest, manifest)
                self.assertEqual(2, records.main(fixture.args()))
                self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_real_shaped_sha_only_run_plan_is_accepted_when_republished(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-run-plan-") as td:
            fixture = Fixture(td)
            plan_path = fixture.root / "config" / "run_plan.json"
            plan = json.loads(plan_path.read_text())
            for item in plan["required_inputs"]:
                item.pop("bytes", None)
                item["why"] = "synthetic equivalent of the production sha-only schema"
            _write_json(plan_path, plan)
            status_path = fixture.root / "verification" / "replay_step_status.json"
            status = json.loads(status_path.read_text())
            status["active_plan_sha256"] = hashlib.sha256(
                plan_path.read_bytes()).hexdigest()
            _write_json(status_path, status)
            receipt = json.loads(fixture.receipt.read_text())
            receipt["metadata"]["input_snapshot"] = records._candidate_snapshots(
                fixture.root)["input_snapshot"]
            _write_json(fixture.receipt, receipt)
            fixture.rewrite_publication()
            fixture.rewrite_validation_boundary()
            self.assertEqual(0, records.main(fixture.args()))

    def test_a17_render_identity_tamper_and_filing_only_input_are_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-a17-") as td:
            fixture = Fixture(td)
            gate_path = fixture.root / "verification" / "a17_reviewed_image_gate.json"
            gate = json.loads(gate_path.read_text())
            gate["sources"][0]["page_1_render"]["sha256"] = "0" * 64
            _write_json(gate_path, gate)
            self.assertEqual(2, records.main(fixture.args()))

        with tempfile.TemporaryDirectory(prefix="ferc-final-records-filing-only-") as td:
            fixture = Fixture(td)
            accession = fixture.input_accessions[-1]
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            for suffix in ("a", "b"):
                fixture._insert(con, "filings", {
                    "source_system": "eLibrary", "filing_id": accession + suffix,
                    "entity_key": "C009999", "form": "IOC",
                    "accession_number": accession, "reporting_year": 2026,
                    "reporting_period": "Q1", "content_hash": "f" * 64,
                    "is_canonical": 1, "version_status": "original"})
            con.commit()
            draft = json.loads(fixture.input_path.read_text())
            audit = {a: {"accession_number": a} for a in fixture.input_accessions}
            rows, _ = records._input_records(fixture.root, con, draft, audit)
            con.close()
            target = next(row for row in rows if row["accession_number"] == accession)
            self.assertEqual("captured_and_cached_not_applied", target["disposition"])
            self.assertIn("observations",
                          target["build_a_application"]["missing_required_populations"])

    def test_stale_database_semantic_identity_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-stale-db-") as td:
            fixture = Fixture(td)
            con = sqlite3.connect(fixture.db)
            fixture._observation(
                con, "post-receipt-observation", "20260909-5999", "C009999")
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            self.assertEqual(2, records.main(fixture.args()))
            self.assertFalse((fixture.output / records.RECEIPT_NAME).exists())

    def test_repaired_rows_are_not_called_resolved_while_blocker_is_open(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-open-blocker-") as td:
            fixture = Fixture(td)
            target_id = fixture.exception_ids[2]
            con = sqlite3.connect(fixture.db)
            con.row_factory = sqlite3.Row
            fixture._insert(con, "blockers", {
                "blocker_id": target_id, "adapter": "ioc", "scope": "source",
                "kind": "source", "summary": "retained unresolved blocker",
                "attempts": "one exact replay", "exact_error": "source unresolved",
                "human_decision_needed": 0, "opened_at": "2026-09-09T00:00:00Z",
                "resolved_at": None,
            })
            con.commit()
            a17 = {"accessions": ["20240223-5073", "20240223-5075"],
                   "identity": {"path": "verification/a17.json", "bytes": 1,
                                "sha256": "a" * 64}}
            draft = json.loads(fixture.exception_path.read_text())
            audit = {row["original_exception_id"]: {"blocker_id": row["original_exception_id"]}
                     for row in draft["rows"]}
            rows, _ = records._exception_records(con, draft, a17, audit)
            con.close()
            target = next(r for r in rows if r["original_exception_id"] == target_id)
            self.assertEqual("data_repaired_but_persisted_blocker_open",
                             target["disposition"])
            self.assertEqual("qualified_open", target["acceptance_result"])

    def test_failed_regeneration_preserves_last_good_collection(self):
        with tempfile.TemporaryDirectory(prefix="ferc-final-records-preserve-") as td:
            fixture = Fixture(td)
            self.assertEqual(0, records.main(fixture.args()))
            before = {name: (fixture.output / name).read_bytes()
                      for name in records.FINAL_FILES + (records.RECEIPT_NAME,)}
            manifest = json.loads(fixture.test_manifest.read_text())
            manifest["exception_test_evidence"].pop(fixture.exception_ids[-1])
            _write_json(fixture.test_manifest, manifest)
            self.assertEqual(2, records.main(fixture.args()))
            self.assertEqual(before, {name: (fixture.output / name).read_bytes()
                                      for name in records.FINAL_FILES +
                                      (records.RECEIPT_NAME,)})


if __name__ == "__main__":
    unittest.main()
