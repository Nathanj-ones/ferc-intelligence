#!/usr/bin/env python3
"""Attach one-pass official-FERC recovery evidence to the candidate inventory.

This is an evidence-packaging helper, not project code.  It reads the retained
recovery ledger and immutable raw responses, updates only the candidate-owned
13-input inventory JSON/CSV, and emits validation/hash records under the
candidate implementation log.  It never imports or executes project modules.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import zipfile
from collections import Counter
from io import StringIO
from pathlib import Path, PurePosixPath


CANDIDATE = Path("/Users/nathanjones/Desktop/ferc/outputs/codex_integrated_repair_20260909_lokqmf/candidate")
RECORD_ROOT = CANDIDATE / "evidence/repair_records"
LOG_ROOT = CANDIDATE / "implementation_logs/input_recovery"
RECOVERY_ROOT = CANDIDATE / "inputs/official_ferc_recovery"
INVENTORY_JSON = RECORD_ROOT / "MISSING_INPUT_INVENTORY_13.json"
INVENTORY_CSV = RECORD_ROOT / "MISSING_INPUT_INVENTORY_13.csv"
RESULTS = LOG_ROOT / "OFFICIAL_FERC_RECOVERY_RESULTS.json"
VALIDATION = LOG_ROOT / "OFFICIAL_FERC_RECOVERY_VALIDATION.json"
SUMMARY = LOG_ROOT / "OFFICIAL_FERC_RECOVERY_SUMMARY.md"
RECOVERY_HASHES = LOG_ROOT / "OFFICIAL_FERC_RECOVERY_HASHES.json"
RECORD_HASHES = RECORD_ROOT / "REPAIR_RECORDS_HASHES.json"

EXPECTED_PREUPDATE_INVENTORY_SHA256 = "d78bc51bde259ff0058775a3e7ffe4a8e18fc6ad849e4dfb46ba901ace337d4f"
OFFICIAL_LIST_PREFIX = "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/GetFileListFromP8/"
OFFICIAL_DOWNLOAD = "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
CSV_FIELDS = [
    "input_id",
    "accession_number",
    "request_url",
    "source_system",
    "adapter",
    "classification",
    "classification_basis",
    "audit_population_link",
    "entities",
    "consumers",
    "captured_or_local_status",
    "retrieval_status",
    "identity",
    "current_audited_database_application",
    "action_needed",
    "evidence",
    "official_recovery",
    "implementation_status",
]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> dict:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "mode": stat.filemode(path.stat().st_mode),
    }


def assert_within(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise AssertionError(f"path escapes authorized root: {path}")
    if path.is_symlink():
        raise AssertionError(f"symlink not permitted in recovery evidence: {path}")


def write_atomic(path: Path, value: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def csv_bytes(rows: list[dict]) -> bytes:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        flattened = {}
        for key in CSV_FIELDS:
            value = row.get(key)
            if isinstance(value, (dict, list)):
                flattened[key] = json.dumps(value, separators=(",", ":"), sort_keys=True)
            elif value is None:
                flattened[key] = ""
            else:
                flattened[key] = value
        writer.writerow(flattened)
    return output.getvalue().encode("utf-8")


def marker_identity(accession: str, request_kind: str) -> dict:
    path = LOG_ROOT / "requests" / f"{accession}.{request_kind}.attempt.json"
    assert_within(path, LOG_ROOT)
    marker = json.loads(path.read_text())
    if marker["accession"] != accession or marker["request_kind"] != request_kind:
        raise AssertionError(f"marker identity mismatch: {path}")
    if marker.get("credentials_or_cookies_sent") is not False:
        raise AssertionError(f"credential marker is not false: {path}")
    return file_identity(path)


def response_identity(response: dict, *, allowed_root: Path) -> dict:
    body_path = Path(response["raw_body_path"])
    header_path = Path(response["raw_headers_path"])
    assert_within(body_path, allowed_root)
    assert_within(header_path, allowed_root)
    body = file_identity(body_path)
    headers = file_identity(header_path)
    if body["bytes"] != response["response_bytes"]:
        raise AssertionError(f"response size mismatch: {body_path}")
    if body["sha256"] != response["response_sha256"]:
        raise AssertionError(f"response SHA mismatch: {body_path}")
    if body["mode"] != "-r--r--r--" or headers["mode"] != "-r--r--r--":
        raise AssertionError(f"raw evidence is not mode 0444: {body_path}")
    return {
        "http_status": response["http_status"],
        "http_reason": response["http_reason"],
        "method": response["method"],
        "official_url": response["official_url"],
        "final_url": response["final_url"],
        "redirects": response["redirects"],
        "attempt_started_utc": response["attempt_started_utc"],
        "attempt_ended_utc": response["attempt_ended_utc"],
        "network_error": response["network_error"],
        "body_capture_limitation": response["body_capture_limitation"],
        "media_type": response["response_media_type"],
        "content_disposition": response["content_disposition"],
        "response_sha256_is_complete": response["response_sha256_is_complete"],
        "body": body,
        "headers": headers,
    }


def inspect_zip_again(path: Path, recorded_members: list[dict]) -> dict:
    actual = []
    with zipfile.ZipFile(path) as archive:
        bad_crc_member = archive.testzip()
        infos = archive.infolist()
        duplicate_names = [name for name, count in Counter(info.filename for info in infos).items() if count > 1]
        unsafe_names = []
        for info in infos:
            pure = PurePosixPath(info.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                unsafe_names.append(info.filename)
            data = archive.read(info)
            actual.append({
                "name": info.filename,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "zip_crc32": f"{info.CRC:08x}",
            })
    if actual != recorded_members:
        raise AssertionError(f"ZIP member ledger mismatch: {path}")
    return {
        "member_count": len(actual),
        "members": actual,
        "bad_crc_member": bad_crc_member,
        "duplicate_member_names": duplicate_names,
        "unsafe_member_names": unsafe_names,
    }


def compact_result(result: dict) -> dict:
    accession = result["accession_number"]
    listed_url = OFFICIAL_LIST_PREFIX + accession
    list_response = result["list_response"]
    attachment_response = result["attachment_response"]
    parsed = result["parsed_file_list"]
    inspection = result["attachment_inspection"] or {}

    if result["inventory_url"] != listed_url:
        raise AssertionError(f"inventory URL mismatch for {accession}")
    if list_response["attempted_this_run"] is not True or list_response["method"] != "GET":
        raise AssertionError(f"list request was not one attempted GET for {accession}")
    if list_response["official_url"] != listed_url or list_response["credentials_or_cookies_sent"] is not False:
        raise AssertionError(f"non-official list request or credential ambiguity for {accession}")
    if list_response["http_status"] != 200 or not parsed["json_parsed"] or not parsed["attachment_ids"]:
        raise AssertionError(f"list response did not provide public attachments for {accession}")
    if attachment_response is None or attachment_response["attempted_this_run"] is not True:
        raise AssertionError(f"attachment request was not attempted once for {accession}")
    if attachment_response["method"] != "POST" or attachment_response["official_url"] != OFFICIAL_DOWNLOAD:
        raise AssertionError(f"unexpected attachment request for {accession}")
    if attachment_response["credentials_or_cookies_sent"] is not False:
        raise AssertionError(f"credential ambiguity for attachment {accession}")

    list_identity = response_identity(list_response, allowed_root=RECOVERY_ROOT)
    attachment_identity = response_identity(attachment_response, allowed_root=RECOVERY_ROOT)
    list_marker = marker_identity(accession, "list")
    attachment_marker = marker_identity(accession, "attachment")

    captured = attachment_response["http_status"] == 200 and attachment_response["body_capture_limitation"] is None
    if captured:
        if inspection.get("kind") != "zip" or inspection.get("inspection_error") is not None:
            raise AssertionError(f"successful attachment response is not a clean ZIP for {accession}")
        zip_validation = inspect_zip_again(Path(attachment_response["raw_body_path"]), inspection["members"])
        if zip_validation["bad_crc_member"] or zip_validation["duplicate_member_names"] or zip_validation["unsafe_member_names"]:
            raise AssertionError(f"unsafe/corrupt ZIP response for {accession}")
        capture_status = "captured_new_official_attachment_bytes"
        residual = "Raw official bytes are captured, but no consumer replay or database/export application has been performed."
        action = (
            "In an isolated candidate cache, map these new official bytes to the listed source request, replay every listed consumer, "
            "and record database/export/regression outcomes; do not treat capture alone as implementation completion."
        )
    else:
        zip_validation = None
        capture_status = "official_list_metadata_only_attachment_http_failure"
        residual = (
            f"The public list and {len(parsed['attachment_ids'])} attachment identities were captured, but the one permitted "
            f"attachment request returned HTTP {attachment_response['http_status']}; no attachment bytes were obtained."
        )
        action = (
            "Keep the input unresolved. Evaluate a distinct permitted official-FERC retrieval route or obtain a policy decision; "
            "do not replay the identical failed request or infer unavailability from this single HTTP 500."
        )

    return {
        "evidence_lane": "new official-FERC evidence collected after the audit; not retroactive proof of frozen/local capture",
        "capture_status": capture_status,
        "official_attachment_bytes_captured": captured,
        "consumer_replay_performed": False,
        "consumer_impact": residual,
        "list_response": list_identity,
        "file_list": {
            "data_list_count": parsed["data_list_count"],
            "attachment_ids": parsed["attachment_ids"],
            "attachment_metadata": parsed["attachment_metadata"],
            "error_list": parsed["error_list"],
        },
        "attachment_response": attachment_identity,
        "attachment_zip": zip_validation,
        "attempt_markers": {"list": list_marker, "attachment": attachment_marker},
        "historical_absence_preserved": True,
        "implementation_status_after_capture": "pending",
        "next_action": action,
    }


def main() -> int:
    before_bytes = INVENTORY_JSON.read_bytes()
    before_sha = sha256_bytes(before_bytes)
    if before_sha != EXPECTED_PREUPDATE_INVENTORY_SHA256:
        raise SystemExit(f"unexpected pre-update inventory SHA-256: {before_sha}")
    inventory = json.loads(before_bytes)
    results_bytes = RESULTS.read_bytes()
    results_doc = json.loads(results_bytes)
    rows = inventory["rows"]
    result_rows = results_doc["results"]
    if len(rows) != 13 or len(result_rows) != 13:
        raise AssertionError("inventory and recovery must each contain 13 rows")
    by_accession = {item["accession_number"]: item for item in result_rows}
    if len(by_accession) != 13 or set(by_accession) != {row["accession_number"] for row in rows}:
        raise AssertionError("recovery/inventory accession population mismatch")

    captures = Counter()
    for row in rows:
        compact = compact_result(by_accession[row["accession_number"]])
        row["official_recovery"] = compact
        row["action_needed"] = compact["next_action"]
        if row["implementation_status"] != "pending":
            raise AssertionError("capture must not promote implementation status")
        captures[compact["capture_status"]] += 1

    inventory["official_recovery"] = {
        "schema": "candidate_official_ferc_recovery_inventory_update_v1",
        "evidence_lane": "new official-FERC evidence collected after the audit; historical cache absence fields remain unchanged",
        "source_results": {
            "path": str(RESULTS),
            "bytes": len(results_bytes),
            "sha256": sha256_bytes(results_bytes),
        },
        "completed_utc": results_doc["completed_utc"],
        "attempt_policy": results_doc["network_policy"],
        "permitted_local_evidence_check": {
            "scope": [
                str(CANDIDATE / "inputs/audit_baseline"),
                str(CANDIDATE / "inputs/day3"),
                str(CANDIDATE / "evidence/audit_baseline"),
                str(CANDIDATE / "evidence/test_fixtures"),
                "/Users/nathanjones/Desktop/ferc_reaudit_handoff/codex_reaudit_20260909_aonXiE/audit_output/final_payload.nosync",
            ],
            "method": "exact accession-string inventory and exact retained audit identity review only; no unrelated-folder search",
            "result": "only audit/report metadata was identified; no pre-existing raw bytes for these 13 were found in the bounded evidence/input scope",
            "limitation": "binary content was not guessed from filenames or approximate values; new response hashes were not used to relabel historical evidence",
        },
        "outcomes": dict(sorted(captures.items())),
        "consumer_replay_performed": False,
    }
    inventory["counts"]["official_recovery_capture_status"] = dict(sorted(captures.items()))
    inventory["counts"]["official_attachment_bytes_captured"] = captures["captured_new_official_attachment_bytes"]
    inventory["counts"]["official_list_metadata_only"] = captures["official_list_metadata_only_attachment_http_failure"]

    updated_json = json_bytes(inventory)
    updated_csv = csv_bytes(rows)
    write_atomic(INVENTORY_JSON, updated_json)
    write_atomic(INVENTORY_CSV, updated_csv)

    # Reload and validate JSON/CSV parity at field level.
    loaded = json.loads(INVENTORY_JSON.read_text())
    with INVENTORY_CSV.open(newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    parity_errors = []
    for json_row, csv_row in zip(loaded["rows"], csv_rows):
        for key in CSV_FIELDS:
            expected = json_row.get(key)
            if isinstance(expected, (dict, list)):
                actual = json.loads(csv_row[key])
            else:
                actual = csv_row[key]
            if actual != expected:
                parity_errors.append({"input_id": json_row["input_id"], "field": key})

    request_markers = sorted((LOG_ROOT / "requests").glob("*.attempt.json"))
    raw_files = sorted(path for path in RECOVERY_ROOT.rglob("*") if path.is_file())
    validation = {
        "schema": "official_ferc_recovery_validation_v1",
        "valid": not parity_errors,
        "preupdate_inventory": {"bytes": len(before_bytes), "sha256": before_sha},
        "postupdate_inventory": file_identity(INVENTORY_JSON),
        "postupdate_csv": file_identity(INVENTORY_CSV),
        "checks": {
            "inventory_rows": len(loaded["rows"]),
            "csv_rows": len(csv_rows),
            "unique_accessions": len({row["accession_number"] for row in loaded["rows"]}),
            "unique_exact_urls": len({row["request_url"] for row in loaded["rows"]}),
            "request_markers": len(request_markers),
            "list_attempt_markers": sum(path.name.endswith(".list.attempt.json") for path in request_markers),
            "attachment_attempt_markers": sum(path.name.endswith(".attachment.attempt.json") for path in request_markers),
            "raw_response_files": len(raw_files),
            "raw_files_mode_0444": sum(stat.filemode(path.stat().st_mode) == "-r--r--r--" for path in raw_files),
            "json_csv_parity_errors": parity_errors,
            "implementation_status_pending": sum(row["implementation_status"] == "pending" for row in loaded["rows"]),
            "historical_absence_preserved": sum(row["official_recovery"]["historical_absence_preserved"] is True for row in loaded["rows"]),
            "consumer_replay_false": sum(row["official_recovery"]["consumer_replay_performed"] is False for row in loaded["rows"]),
            "capture_status": dict(sorted(captures.items())),
        },
        "assertions": {
            "thirteen_exact_rows_and_urls": len(loaded["rows"]) == len(csv_rows) == 13,
            "twenty_six_unique_one_pass_markers": len(request_markers) == 26 and len({path.name for path in request_markers}) == 26,
            "all_raw_evidence_immutable": len(raw_files) > 0 and all(stat.filemode(path.stat().st_mode) == "-r--r--r--" for path in raw_files),
            "json_csv_exact_field_parity": not parity_errors,
            "all_implementation_status_pending": all(row["implementation_status"] == "pending" for row in loaded["rows"]),
            "all_consumer_replay_pending": all(row["official_recovery"]["consumer_replay_performed"] is False for row in loaded["rows"]),
            "historical_absence_distinguished": all(row["official_recovery"]["historical_absence_preserved"] is True for row in loaded["rows"]),
        },
    }
    validation["valid"] = all(validation["assertions"].values())
    write_atomic(VALIDATION, json_bytes(validation))

    summary_lines = [
        "# Official FERC recovery pass",
        "",
        f"Completed: `{results_doc['completed_utc']}`",
        "",
        "This is new official-FERC evidence collected after the audit. It does not change the finding that these inputs were absent from the frozen and checked local cache indexes.",
        "",
        f"- Exact accessions/list GETs: {len(rows)} / {results_doc['summary']['list_http_200']} HTTP 200",
        f"- Attachment POSTs: {results_doc['summary']['attachment_requests_attempted']} attempted once; {results_doc['summary']['attachment_http_200']} HTTP 200",
        f"- Raw official attachment ZIPs captured and independently CRC/hash checked: {captures['captured_new_official_attachment_bytes']}",
        f"- Metadata-only unresolved attachment failures: {captures['official_list_metadata_only_attachment_http_failure']}",
        "- Consumer replay/database/export application: not performed",
        "- Implementation status: all 13 remain pending",
        "",
        "## Unresolved retrieval",
        "",
        "`20231229-5212` returned a public 10-item list, then the one permitted batch attachment request returned HTTP 500 with a 61-byte official JSON error body. This is not evidence that the source is unavailable, and no identical retry was made.",
        "",
    ]
    write_atomic(SUMMARY, ("\n".join(summary_lines)).encode("utf-8"))

    # Refresh the repair-record index without adding unrelated candidate files.
    record_index = json.loads(RECORD_HASHES.read_text())
    for filename in ["MISSING_INPUT_INVENTORY_13.json", "MISSING_INPUT_INVENTORY_13.csv"]:
        path = RECORD_ROOT / filename
        record_index["files"][filename] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    write_atomic(RECORD_HASHES, json_bytes(record_index))

    hash_paths = [
        RESULTS,
        LOG_ROOT / "recovery_progress.jsonl",
        LOG_ROOT / "recover_official_ferc.py",
        Path(__file__),
        VALIDATION,
        SUMMARY,
        INVENTORY_JSON,
        INVENTORY_CSV,
        RECORD_HASHES,
        *request_markers,
        *raw_files,
    ]
    hash_doc = {
        "schema": "official_ferc_recovery_hash_index_v1",
        "self_excluded": True,
        "files": {str(path): {"bytes": path.stat().st_size, "sha256": sha256_file(path)} for path in hash_paths},
    }
    write_atomic(RECOVERY_HASHES, json_bytes(hash_doc))
    print(json.dumps({
        "valid": validation["valid"],
        "captures": dict(sorted(captures.items())),
        "inventory_json": file_identity(INVENTORY_JSON),
        "inventory_csv": file_identity(INVENTORY_CSV),
        "validation": file_identity(VALIDATION),
        "summary": file_identity(SUMMARY),
        "hash_index": file_identity(RECOVERY_HASHES),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
