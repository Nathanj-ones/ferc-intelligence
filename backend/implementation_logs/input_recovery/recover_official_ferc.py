#!/usr/bin/env python3
"""One-pass, no-credential recovery of 13 exact official FERC eLibrary inputs.

Reads only the candidate repair-record inventory.  Writes only to the candidate
official_ferc_recovery and implementation_logs/input_recovery directories.
Every logical HTTP request gets an immutable pre-attempt marker, so rerunning the
helper will not repeat a request whose attempt began.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path


CANDIDATE = Path("/Users/nathanjones/Desktop/ferc/outputs/codex_integrated_repair_20260909_lokqmf/candidate")
INVENTORY = CANDIDATE / "evidence/repair_records/MISSING_INPUT_INVENTORY_13.json"
RECOVERY = CANDIDATE / "inputs/official_ferc_recovery"
LOG_ROOT = CANDIDATE / "implementation_logs/input_recovery"
LIST_ROOT = RECOVERY / "list_responses"
ATTACHMENT_ROOT = RECOVERY / "attachment_responses"
REQUEST_ROOT = LOG_ROOT / "requests"
PROGRESS = LOG_ROOT / "recovery_progress.jsonl"
RESULTS = LOG_ROOT / "OFFICIAL_FERC_RECOVERY_RESULTS.json"

OFFICIAL_PREFIX = "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/GetFileListFromP8/"
DOWNLOAD_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
UI_REFERER_PREFIX = "https://elibrary.ferc.gov/eLibrary/filelist?accession_num="
USER_AGENT = "Codex-FERC-independent-audit-recovery/1.0"
TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 300 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
ACCESSION_RE = re.compile(r"^[0-9]{8}-[0-9]+$")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, data: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    if immutable:
        path.chmod(0o444)


def write_json_atomic(path: Path, value, *, immutable: bool = False) -> None:
    write_atomic(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"), immutable=immutable)


def append_progress(value: dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("ab") as handle:
        handle.write((json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


class RecordingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, redirects: list[dict]):
        super().__init__()
        self.redirects = redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects.append({"status": code, "from_url": req.full_url, "to_url": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_headers(accession: str, *, attachment: bool) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://elibrary.ferc.gov",
        "Referer": UI_REFERER_PREFIX + accession,
        "Accept": "application/octet-stream,*/*" if attachment else "application/json",
        "Connection": "close",
    }
    if attachment:
        headers["Content-Type"] = "application/json"
    return headers


def header_rows(headers) -> list[dict]:
    return [{"name": key, "value": value} for key, value in headers.items()]


def read_response_with_guard(response) -> tuple[bytes, dict | None]:
    """Read one response once while preserving a 1-GiB candidate-volume floor."""
    length_text = response.headers.get("Content-Length")
    try:
        declared_length = int(length_text) if length_text is not None else None
    except ValueError:
        declared_length = None
    free_before = shutil.disk_usage(CANDIDATE).free
    permitted = min(MAX_RESPONSE_BYTES, max(0, free_before - MIN_FREE_BYTES))
    if declared_length is not None and declared_length > permitted:
        return b"", {
            "type": "ResponseBodyNotReadDiskGuard",
            "declared_content_length": declared_length,
            "permitted_bytes": permitted,
            "free_before": free_before,
            "minimum_free_floor": MIN_FREE_BYTES,
        }
    body = response.read(permitted + 1)
    if len(body) > permitted:
        return body[:permitted], {
            "type": "ResponseBodyTruncatedDiskGuard",
            "captured_partial_bytes": permitted,
            "declared_content_length": declared_length,
            "free_before": free_before,
            "minimum_free_floor": MIN_FREE_BYTES,
        }
    return body, None


def immutable_attempt_marker(accession: str, request_kind: str, request: dict) -> Path | None:
    path = REQUEST_ROOT / f"{accession}.{request_kind}.attempt.json"
    if path.exists():
        return None
    marker = {
        "schema": "official_ferc_http_attempt_v1",
        "attempt_started_utc": utc_now(),
        "accession": accession,
        "request_kind": request_kind,
        "retry_policy": "one attempt only; no automatic or manual identical retry in this pass",
        "credentials_or_cookies_sent": False,
        "request": request,
        "outcome": "attempt_started; see response metadata/result ledger",
    }
    write_json_atomic(path, marker, immutable=True)
    return path


def perform_request(accession: str, request_kind: str, request: urllib.request.Request, request_public: dict) -> dict:
    marker = immutable_attempt_marker(accession, request_kind, request_public)
    if marker is None:
        result = {
            "accession": accession,
            "request_kind": request_kind,
            "attempted_this_run": False,
            "outcome": "skipped_existing_attempt_marker",
            "attempt_marker": str(REQUEST_ROOT / f"{accession}.{request_kind}.attempt.json"),
        }
        append_progress(result)
        return result

    started = utc_now()
    redirects: list[dict] = []
    opener = urllib.request.build_opener(RecordingRedirectHandler(redirects))
    status = None
    reason = None
    final_url = request.full_url
    response_headers = []
    body = b""
    network_error = None
    body_capture_limitation = None
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            reason = response.reason
            final_url = response.geturl()
            response_headers = header_rows(response.headers)
            body, body_capture_limitation = read_response_with_guard(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        reason = exc.reason
        final_url = exc.geturl()
        response_headers = header_rows(exc.headers)
        body, body_capture_limitation = read_response_with_guard(exc)
    except Exception as exc:  # retain exact type/message; never retry
        network_error = {"type": type(exc).__name__, "message": str(exc)}

    ended = utc_now()
    response_root = LIST_ROOT if request_kind == "list" else ATTACHMENT_ROOT
    body_path = response_root / f"{accession}.body"
    headers_path = response_root / f"{accession}.headers.json"
    if body or status is not None:
        write_atomic(body_path, body, immutable=True)
        write_json_atomic(headers_path, response_headers, immutable=True)

    content_type = None
    content_disposition = None
    for item in response_headers:
        lowered = item["name"].lower()
        if lowered == "content-type":
            content_type = item["value"]
        elif lowered == "content-disposition":
            content_disposition = item["value"]

    result = {
        "accession": accession,
        "request_kind": request_kind,
        "attempted_this_run": True,
        "attempt_started_utc": started,
        "attempt_ended_utc": ended,
        "attempt_marker": str(marker),
        "method": request.get_method(),
        "official_url": request.full_url,
        "final_url": final_url,
        "redirects": redirects,
        "http_status": status,
        "http_reason": str(reason) if reason is not None else None,
        "network_error": network_error,
        "body_capture_limitation": body_capture_limitation,
        "response_media_type": content_type,
        "content_disposition": content_disposition,
        "response_bytes": len(body) if (body or status is not None) else None,
        "response_sha256": sha256_bytes(body) if (body or status is not None) else None,
        "response_sha256_is_complete": body_capture_limitation is None if status is not None else None,
        "raw_body_path": str(body_path) if body_path.exists() else None,
        "raw_headers_path": str(headers_path) if headers_path.exists() else None,
        "credentials_or_cookies_sent": False,
    }
    append_progress(result)
    print(f"{accession} {request_kind}: status={status} bytes={result['response_bytes']} error={network_error}", flush=True)
    return result


def parse_file_list(result: dict) -> dict:
    path_text = result.get("raw_body_path")
    parsed = {
        "json_parsed": False,
        "data_list_count": None,
        "error_list": None,
        "attachment_ids": [],
        "attachment_metadata": [],
        "parse_error": None,
    }
    if not path_text:
        return parsed
    body = Path(path_text).read_bytes()
    try:
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"top-level JSON is {type(value).__name__}, not object")
        rows = value.get("DataList") or []
        errors = value.get("ErrorList") or []
        attachments = []
        ids = []
        for row in rows:
            attachment_id = str(row.get("ID") or "")
            if attachment_id:
                ids.append(attachment_id)
            attachments.append({
                "attachment_id": attachment_id or None,
                "accession_number": row.get("Accession_Number"),
                "mime_type": row.get("MimeType"),
                "availability_mode": row.get("Availability_Mode"),
                "availability_code": row.get("Availability_Code"),
                "description": row.get("Description"),
                "transmittal_type": row.get("TransmittalType"),
            })
        parsed.update({
            "json_parsed": True,
            "data_list_count": len(rows),
            "error_list": errors,
            "attachment_ids": ids,
            "attachment_metadata": attachments,
        })
    except Exception as exc:
        parsed["parse_error"] = {"type": type(exc).__name__, "message": str(exc)}
    return parsed


def inspect_attachment_response(result: dict) -> dict:
    path_text = result.get("raw_body_path")
    if not path_text:
        return {"kind": None, "members": [], "inspection_error": None}
    body = Path(path_text).read_bytes()
    kind = "binary"
    if body.startswith(b"PK\x03\x04"):
        kind = "zip"
    elif body.startswith(b"%PDF"):
        kind = "pdf"
    elif body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        kind = "ole2"
    elif body.startswith(b"{\\rt"):
        kind = "rtf"
    elif body.lstrip().startswith((b"{", b"[")):
        kind = "json_or_text"

    members = []
    inspection_error = None
    if kind == "zip":
        try:
            with zipfile.ZipFile(BytesIO(body)) as archive:
                bad_crc_member = archive.testzip()
                for member in archive.infolist():
                    data = archive.read(member)
                    members.append({
                        "name": member.filename,
                        "bytes": len(data),
                        "sha256": sha256_bytes(data),
                        "zip_crc32": f"{member.CRC:08x}",
                    })
            if bad_crc_member:
                inspection_error = {"type": "BadCRC", "message": bad_crc_member}
        except Exception as exc:
            inspection_error = {"type": type(exc).__name__, "message": str(exc)}
    return {"kind": kind, "members": members, "inspection_error": inspection_error}


def main() -> int:
    inventory_bytes = INVENTORY.read_bytes()
    inventory = json.loads(inventory_bytes)
    rows = inventory["rows"]
    if len(rows) != 13 or len({row["request_url"] for row in rows}) != 13:
        raise SystemExit("inventory must contain exactly 13 unique URLs")

    for directory in [RECOVERY, LOG_ROOT, LIST_ROOT, ATTACHMENT_ROOT, REQUEST_ROOT]:
        directory.mkdir(parents=True, exist_ok=True)

    results = []
    for row in rows:
        accession = row["accession_number"]
        url = row["request_url"]
        if not ACCESSION_RE.fullmatch(accession):
            raise SystemExit(f"unsafe accession: {accession!r}")
        if url != OFFICIAL_PREFIX + accession:
            raise SystemExit(f"unexpected/non-official exact URL for {accession}: {url}")

        get_headers = public_headers(accession, attachment=False)
        get_public = {
            "method": "GET",
            "url": url,
            "headers": get_headers,
            "body_bytes": 0,
            "body_sha256": sha256_bytes(b""),
        }
        get_request = urllib.request.Request(url, headers=get_headers, method="GET")
        list_result = perform_request(accession, "list", get_request, get_public)
        file_list = parse_file_list(list_result)

        download_result = None
        download_inspection = None
        attachment_ids = file_list["attachment_ids"]
        if list_result.get("http_status") == 200 and file_list["json_parsed"] and attachment_ids:
            payload = json.dumps({
                "FileType": "",
                "accession": accession,
                "fileid": 0,
                "FileIDAll": "",
                "fileidLst": attachment_ids,
                "Islegacy": False,
            }, separators=(",", ":")).encode("utf-8")
            post_headers = public_headers(accession, attachment=True)
            post_public = {
                "method": "POST",
                "url": DOWNLOAD_URL,
                "headers": post_headers,
                "body_bytes": len(payload),
                "body_sha256": sha256_bytes(payload),
                "payload_shape": {"accession": accession, "attachment_id_count": len(attachment_ids)},
            }
            post_request = urllib.request.Request(DOWNLOAD_URL, data=payload, headers=post_headers, method="POST")
            download_result = perform_request(accession, "attachment", post_request, post_public)
            download_inspection = inspect_attachment_response(download_result)

        results.append({
            "accession_number": accession,
            "inventory_url": url,
            "classification_before_recovery": row["classification"],
            "consumers": row["consumers"],
            "list_response": list_result,
            "parsed_file_list": file_list,
            "attachment_response": download_result,
            "attachment_inspection": download_inspection,
        })

    result_doc = {
        "schema": "official_ferc_recovery_results_v1",
        "evidence_lane": "new official-FERC evidence collected after the audit; not retroactive bundled evidence",
        "network_policy": "public official FERC eLibrary endpoints only; no credentials, cookies, bypass or retries",
        "inventory_identity": {
            "path": str(INVENTORY),
            "bytes": len(inventory_bytes),
            "sha256": sha256_bytes(inventory_bytes),
        },
        "completed_utc": utc_now(),
        "results": results,
        "summary": {
            "accessions": len(results),
            "list_requests_attempted": sum(item["list_response"].get("attempted_this_run") is True for item in results),
            "list_http_200": sum(item["list_response"].get("http_status") == 200 for item in results),
            "list_network_errors": sum(item["list_response"].get("network_error") is not None for item in results),
            "file_lists_with_attachment_ids": sum(bool(item["parsed_file_list"]["attachment_ids"]) for item in results),
            "attachment_requests_attempted": sum(item["attachment_response"] is not None and item["attachment_response"].get("attempted_this_run") is True for item in results),
            "attachment_http_200": sum(item["attachment_response"] is not None and item["attachment_response"].get("http_status") == 200 for item in results),
        },
    }
    write_json_atomic(RESULTS, result_doc)
    print(json.dumps(result_doc["summary"], sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
