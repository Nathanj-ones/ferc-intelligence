#!/usr/bin/env python3
"""Capture one newly discovered public eLibrary capacity filing, once.

This is deliberately narrower than the historical 13-accession recovery.  The
corrected MountainWest filer-name boundary exposed accession 20260226-5162 as
the revised 2024 MountainWest Pipeline capacity report.  The shipped search
response already pins the public attachment identity; this helper captures the
missing list response and, only when that list agrees exactly, makes one POST
for that single attachment.

The helper has no retry, authentication, cookie, proxy, or redirect path.  It
writes immutable response evidence outside ``source_cache`` and never imports
or replaces cache data.  A separate, hash-pinned invocation of
``tools/import_official_recovery.py`` is required for that step.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass


CANDIDATE_ROOT = pathlib.Path(__file__).resolve().parents[2]
ACCESSION = "20260226-5162"
ATTACHMENT_ID = "92064CA7-EE7A-C2DC-9CF5-9C9B20600000"
FILE_NAME = "Capacity 2024-MWP-Revised.pdf"
LISTED_FILE_BYTES = 173287
SEARCH_CACHE_KEY = "1a50db78a6f794a8e054797323778ba5d79fe9d56642fabd9f88664ac2c6f30d"
SEARCH_OBJECT_SHA256 = "94d8890253bdf7075cd21123bd136783dd113c4641690766dd0c51d87fa98ae3"
SEARCH_OBJECT_BYTES = 8072
SEARCH_SOURCE_URL = (
    "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"
    "#body=6576c27150c032c4c206e6cb0f5380bd"
)
LIST_URL = (
    "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/"
    f"GetFileListFromP8/{ACCESSION}"
)
DOWNLOAD_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
USER_AGENT = "Codex-FERC-capacity-evidence-capture/1.0"
TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
ID_RE = re.compile(r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$")


@dataclass(frozen=True)
class Layout:
    candidate: pathlib.Path
    recovery: pathlib.Path
    logs: pathlib.Path
    list_body: pathlib.Path
    list_headers: pathlib.Path
    attachment_body: pathlib.Path
    attachment_headers: pathlib.Path
    get_marker: pathlib.Path
    post_marker: pathlib.Path
    results: pathlib.Path
    hashes: pathlib.Path

    @classmethod
    def for_candidate(cls, candidate: pathlib.Path) -> "Layout":
        candidate = pathlib.Path(candidate).resolve()
        recovery = candidate / "inputs/official_ferc_capacity_20260226_5162"
        logs = candidate / "implementation_logs/input_recovery/capacity_20260226_5162"
        return cls(
            candidate=candidate,
            recovery=recovery,
            logs=logs,
            list_body=recovery / "list_response.body",
            list_headers=recovery / "list_response.headers.json",
            attachment_body=recovery / "attachment_response.body",
            attachment_headers=recovery / "attachment_response.headers.json",
            get_marker=logs / "get.attempt.json",
            post_marker=logs / "post.attempt.json",
            results=logs / "OFFICIAL_FERC_CAPACITY_20260226_5162_RESULTS.json",
            hashes=logs / "OFFICIAL_FERC_CAPACITY_20260226_5162_HASHES.json",
        )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(path: pathlib.Path) -> dict:
    body = path.read_bytes()
    return {"bytes": len(body), "sha256": _sha(body)}


def _write_new_immutable(path: pathlib.Path, body: bytes) -> None:
    """Atomically create one immutable file and never replace any evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to replace captured evidence: {path}")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(temp), flags, 0o600)
        try:
            position = 0
            while position < len(body):
                written = os.write(descriptor, body[position:])
                if written <= 0:
                    raise OSError("zero-byte evidence write")
                position += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, path)
        path.chmod(0o444)
    finally:
        if temp.exists():
            temp.unlink()


def _write_json(path: pathlib.Path, value: object) -> None:
    _write_new_immutable(
        path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not turn one authorised request into an unrecorded redirect request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    # Ignore ambient proxy/cookie/auth configuration: this capture is a direct,
    # unauthenticated request to the one pinned official host.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def _headers(*, attachment: bool) -> dict[str, str]:
    result = {
        "User-Agent": USER_AGENT,
        "Accept": "application/octet-stream,*/*" if attachment else "application/json",
        "Origin": "https://elibrary.ferc.gov",
        "Referer": (
            "https://elibrary.ferc.gov/eLibrary/filelist?"
            f"accession_num={ACCESSION}"
        ),
        "Connection": "close",
    }
    if attachment:
        result["Content-Type"] = "application/json"
    return result


def _safe_headers(headers) -> list[dict]:
    keep = {
        "content-type", "content-disposition", "content-length", "date",
        "etag", "last-modified", "x-request-id",
    }
    return [
        {"name": str(name), "value": str(value)}
        for name, value in headers.items()
        if str(name).lower() in keep
    ]


def _media_type(headers: list[dict]) -> str | None:
    for row in headers:
        if row["name"].lower() == "content-type":
            return row["value"]
    return None


def _read_guarded(response, candidate: pathlib.Path) -> tuple[bytes, dict | None]:
    length = response.headers.get("Content-Length")
    try:
        declared = int(length) if length is not None else None
    except ValueError:
        declared = None
    free = shutil.disk_usage(candidate).free
    permitted = min(MAX_RESPONSE_BYTES, max(0, free - MIN_FREE_BYTES))
    if declared is not None and declared > permitted:
        return b"", {
            "type": "ResponseBodyNotReadDiskGuard",
            "declared_content_length": declared,
            "permitted_bytes": permitted,
            "free_before": free,
        }
    body = response.read(permitted + 1)
    if len(body) > permitted:
        return body[:permitted], {
            "type": "ResponseBodyTruncatedDiskGuard",
            "captured_partial_bytes": permitted,
            "declared_content_length": declared,
            "free_before": free,
        }
    return body, None


def _request_record(
    *, opener, request: urllib.request.Request, kind: str, body_path: pathlib.Path,
    headers_path: pathlib.Path, candidate: pathlib.Path,
) -> tuple[dict, bytes]:
    started = _utc_now()
    status = None
    reason = None
    response_headers: list[dict] = []
    response_body = b""
    network_error = None
    capture_limitation = None
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(response.status)
            reason = str(response.reason or "")
            response_headers = _safe_headers(response.headers)
            response_body, capture_limitation = _read_guarded(response, candidate)
    except urllib.error.HTTPError as exc:
        # Redirects are retained as the single request's response; no follow-up.
        status = int(exc.code)
        reason = str(exc.reason or "")
        response_headers = _safe_headers(exc.headers)
        response_body, capture_limitation = _read_guarded(exc, candidate)
    except Exception as exc:  # exact one-attempt blocker; never retried
        network_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
    ended = _utc_now()

    if status is not None:
        _write_new_immutable(body_path, response_body)
        _write_json(headers_path, response_headers)
    return {
        "accession": ACCESSION,
        "request_kind": kind,
        "attempted_this_run": True,
        "attempt_started_utc": started,
        "attempt_ended_utc": ended,
        "method": request.get_method(),
        "official_url": request.full_url,
        "final_url": request.full_url,
        "redirects": [],
        "http_status": status,
        "http_reason": reason,
        "network_error": network_error,
        "body_capture_limitation": capture_limitation,
        "response_media_type": _media_type(response_headers),
        "content_disposition": next(
            (r["value"] for r in response_headers
             if r["name"].lower() == "content-disposition"), None),
        "response_bytes": len(response_body) if status is not None else None,
        "response_sha256": _sha(response_body) if status is not None else None,
        "response_sha256_is_complete": (
            capture_limitation is None if status is not None else None),
        "raw_body_path": str(body_path) if status is not None else None,
        "raw_headers_path": str(headers_path) if status is not None else None,
        "credentials_or_cookies_sent": False,
    }, response_body


def _parse_and_validate_list(body: bytes, response: dict) -> tuple[dict, str | None]:
    parsed = {
        "json_parsed": False,
        "data_list_count": None,
        "error_list": None,
        "attachment_ids": [],
        "attachment_metadata": [],
        "parse_error": None,
    }
    if response.get("http_status") != 200 or response.get("body_capture_limitation"):
        return parsed, "list response was not a complete HTTP 200"
    try:
        document = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("top-level JSON is not an object")
        rows = document.get("DataList")
        errors = document.get("ErrorList") or []
        if not isinstance(rows, list) or not isinstance(errors, list):
            raise ValueError("DataList/ErrorList have the wrong shape")
    except Exception as exc:
        parsed["parse_error"] = {"type": type(exc).__name__, "message": str(exc)}
        return parsed, "list body is not the expected JSON shape"

    metadata = []
    for row in rows:
        metadata.append({
            "attachment_id": row.get("ID"),
            "accession_number": row.get("Accession_Number"),
            "file_name": row.get("Orig_File_Name"),
            "file_size_num": row.get("File_Size_Num"),
            "mime_type": row.get("MimeType"),
            "availability_mode": row.get("Availability_Mode"),
            "availability_code": row.get("Availability_Code"),
            "description": row.get("Description"),
            "transmittal_type": row.get("TransmittalType"),
        })
    ids = [str(row.get("ID") or "") for row in rows]
    parsed.update({
        "json_parsed": True,
        "data_list_count": len(rows),
        "error_list": errors,
        "attachment_ids": ids,
        "attachment_metadata": metadata,
    })
    if errors:
        return parsed, f"official list returned ErrorList={errors!r}"
    if len(rows) != 1:
        return parsed, f"expected exactly one attachment, found {len(rows)}"
    row = rows[0]
    checks = {
        "attachment ID": str(row.get("ID") or "") == ATTACHMENT_ID,
        "accession": str(row.get("Accession_Number") or ACCESSION) == ACCESSION,
        "public availability": row.get("Availability_Mode") == "P",
        "file name": str(row.get("Orig_File_Name") or "") == FILE_NAME,
        "listed byte size": int(row.get("File_Size_Num") or 0) == LISTED_FILE_BYTES,
    }
    failures = [label for label, valid in checks.items() if not valid]
    if failures:
        return parsed, "pinned search/list identity mismatch: " + ", ".join(failures)
    if not ID_RE.fullmatch(ATTACHMENT_ID):
        return parsed, "internal pinned attachment ID is malformed"
    return parsed, None


def _canonical_payload() -> tuple[dict, bytes]:
    value = {
        "FileType": "", "accession": ACCESSION, "fileid": 0,
        "FileIDAll": "", "fileidLst": [ATTACHMENT_ID], "Islegacy": False,
    }
    return value, json.dumps(value, sort_keys=True).encode("utf-8")


def _inspect_attachment(body: bytes, response: dict) -> tuple[dict, str | None]:
    result = {"kind": None, "members": [], "inspection_error": None}
    if response.get("http_status") != 200 or response.get("body_capture_limitation"):
        return result, "attachment response was not a complete HTTP 200"
    if body.startswith(b"%PDF"):
        result["kind"] = "pdf"
        if len(body) != LISTED_FILE_BYTES:
            return result, f"raw PDF size {len(body)} != listed {LISTED_FILE_BYTES}"
        result["members"] = [{"name": FILE_NAME, "bytes": len(body), "sha256": _sha(body)}]
        return result, None
    if not body.startswith(b"PK\x03\x04"):
        result["kind"] = "unexpected"
        return result, "attachment body is neither PDF nor ZIP"
    result["kind"] = "zip"
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP CRC failure")
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1:
                raise ValueError(f"expected one ZIP member, found {len(members)}")
            item = members[0]
            name = item.filename.replace("\\", "/")
            if name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts:
                raise ValueError(f"unsafe ZIP member path: {item.filename!r}")
            expected_names = {FILE_NAME, f"{ACCESSION}_{FILE_NAME}"}
            if pathlib.PurePosixPath(name).name not in expected_names:
                raise ValueError(f"unexpected ZIP member name: {item.filename!r}")
            member = archive.read(item)
            if len(member) != LISTED_FILE_BYTES:
                raise ValueError(
                    f"ZIP member size {len(member)} != listed {LISTED_FILE_BYTES}")
            if not member.startswith(b"%PDF"):
                raise ValueError("ZIP member is not a PDF")
            result["members"] = [{
                "name": item.filename,
                "bytes": len(member),
                "sha256": _sha(member),
                "zip_crc32": f"{item.CRC:08x}",
            }]
    except Exception as exc:
        result["inspection_error"] = {"type": type(exc).__name__, "message": str(exc)}
        return result, f"attachment inspection failed: {type(exc).__name__}: {exc}"
    return result, None


def _attempt(marker: pathlib.Path, method: str, url: str, *, payload: bytes = b"") -> None:
    _write_json(marker, {
        "schema": "official_ferc_http_attempt_v1",
        "attempt_started_utc": _utc_now(),
        "accession": ACCESSION,
        "request_kind": "list" if method == "GET" else "attachment",
        "retry_policy": "one attempt only; no redirect and no retry",
        "credentials_or_cookies_sent": False,
        "request": {
            "method": method,
            "url": url,
            "body_bytes": len(payload),
            "body_sha256": _sha(payload),
        },
        "outcome": "attempt_started; see immutable result ledger",
    })


def _verify_pinned_search_evidence(candidate: pathlib.Path) -> dict:
    """Prove the network request is anchored to the already captured hit."""
    index_path = candidate / "source_cache/index.json"
    object_path = (
        candidate / "source_cache/objects" / SEARCH_OBJECT_SHA256[:2]
        / SEARCH_OBJECT_SHA256
    )
    if index_path.is_symlink() or not index_path.is_file():
        raise ValueError(f"missing regular source-cache index: {index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entry = index.get(SEARCH_CACHE_KEY)
    expected_entry = {
        "content_hash": SEARCH_OBJECT_SHA256,
        "byte_size": SEARCH_OBJECT_BYTES,
        "source_url": SEARCH_SOURCE_URL,
        "source_system": "eLibrary",
    }
    if not isinstance(entry, dict) or any(entry.get(key) != value for key, value in expected_entry.items()):
        raise ValueError("pinned capacity search cache entry is absent or has changed")
    if object_path.is_symlink() or not object_path.is_file():
        raise ValueError(f"missing regular pinned capacity search object: {object_path}")
    body = object_path.read_bytes()
    if len(body) != SEARCH_OBJECT_BYTES or _sha(body) != SEARCH_OBJECT_SHA256:
        raise ValueError("pinned capacity search object identity mismatch")
    document = json.loads(body.decode("utf-8"))
    matches = [
        row for row in document.get("searchHits", [])
        if row.get("acesssionNumber") == ACCESSION
    ]
    if len(matches) != 1:
        raise ValueError(f"pinned search should contain one {ACCESSION} hit")
    hit = matches[0]
    transmissions = hit.get("transmittals") or []
    authors = [
        row.get("affiliation") for row in (hit.get("affiliations") or [])
        if row.get("afType") == "AUTHOR"
    ]
    if (
        hit.get("description") != (
            "Revised Annual Peak Day Capacity Report of MountainWest Pipeline, "
            "LLC for 2024 under RM85-1.")
        or hit.get("availCode") != "P"
        or authors != ["MountainWest Pipeline, LLC"]
        or len(transmissions) != 1
        or transmissions[0].get("fileId") != ATTACHMENT_ID
        or transmissions[0].get("fileName") != FILE_NAME
        or transmissions[0].get("fileSize") != LISTED_FILE_BYTES
    ):
        raise ValueError("pinned search hit no longer has the audited entity/attachment identity")
    return {
        "index_path": str(index_path),
        "cache_key": SEARCH_CACHE_KEY,
        "source_url": SEARCH_SOURCE_URL,
        "object_path": str(object_path),
        "bytes": len(body),
        "sha256": _sha(body),
    }


def capture(candidate_root: pathlib.Path = CANDIDATE_ROOT, *, opener=None) -> dict:
    layout = Layout.for_candidate(candidate_root)
    if layout.get_marker.exists() or layout.results.exists() or layout.recovery.exists():
        raise FileExistsError(
            "capacity accession capture already began; refusing every repeated request")
    if opener is None:
        opener = _opener()
    search_identity = _verify_pinned_search_evidence(layout.candidate)

    get_headers = _headers(attachment=False)
    _attempt(layout.get_marker, "GET", LIST_URL)
    get_request = urllib.request.Request(LIST_URL, headers=get_headers, method="GET")
    list_response, list_body = _request_record(
        opener=opener, request=get_request, kind="list",
        body_path=layout.list_body, headers_path=layout.list_headers,
        candidate=layout.candidate)
    parsed_list, list_error = _parse_and_validate_list(list_body, list_response)

    attachment_response = None
    attachment_inspection = None
    attachment_error = "POST not attempted because the pinned list identity was not validated"
    payload = b""
    if list_error is None:
        _value, payload = _canonical_payload()
        _attempt(layout.post_marker, "POST", DOWNLOAD_URL, payload=payload)
        post_request = urllib.request.Request(
            DOWNLOAD_URL, data=payload, headers=_headers(attachment=True), method="POST")
        attachment_response, attachment_body = _request_record(
            opener=opener, request=post_request, kind="attachment",
            body_path=layout.attachment_body, headers_path=layout.attachment_headers,
            candidate=layout.candidate)
        attachment_inspection, attachment_error = _inspect_attachment(
            attachment_body, attachment_response)

    complete = list_error is None and attachment_error is None
    result = {
        "schema": "official_ferc_recovery_results_v1",
        "evidence_lane": (
            "new official-FERC evidence collected after the independent audit; "
            "not retroactive bundled evidence"),
        "network_policy": (
            "one direct public GET and at most one direct public POST; no proxy, "
            "redirect, retry, credential, cookie or bypass"),
        "discovery": {
            "cause": "corrected exact-filer capacity selection exposed a missing revised filing",
            "audited_missing_input_population": "not one of the final audit's 13 inputs",
            "pinned_search_evidence": {
                **search_identity,
                "accession_number": ACCESSION,
                "attachment_id": ATTACHMENT_ID,
                "file_name": FILE_NAME,
                "listed_bytes": LISTED_FILE_BYTES,
                "entity": "MountainWest Pipeline, LLC",
                "description": (
                    "Revised Annual Peak Day Capacity Report of MountainWest Pipeline, "
                    "LLC for 2024 under RM85-1."),
            },
        },
        "completed_utc": _utc_now(),
        "results": [{
            "accession_number": ACCESSION,
            "inventory_url": LIST_URL,
            "classification_before_recovery": (
                "mandatory executable prerequisite discovered during semantic repair"),
            "consumers": [
                "capacity adapter C001088 (MountainWest Pipeline, LLC)",
                "capacity observations/lineage/coverage/exports",
            ],
            "list_response": list_response,
            "parsed_file_list": parsed_list,
            "attachment_response": attachment_response,
            "attachment_inspection": attachment_inspection,
        }],
        "summary": {
            "accessions": 1,
            "list_requests_attempted": 1,
            "list_http_200": int(list_response.get("http_status") == 200),
            "attachment_requests_attempted": int(attachment_response is not None),
            "attachment_http_200": int(
                attachment_response is not None
                and attachment_response.get("http_status") == 200),
            "complete_validated_capture": complete,
            "list_validation_error": list_error,
            "attachment_validation_error": attachment_error,
            "canonical_post_payload_sha256": _sha(payload) if payload else None,
        },
    }
    _write_json(layout.results, result)

    indexed = [layout.results, layout.get_marker]
    if layout.post_marker.exists():
        indexed.append(layout.post_marker)
    if layout.recovery.exists():
        indexed.extend(sorted(path for path in layout.recovery.rglob("*") if path.is_file()))
    hash_document = {
        "schema": "official_ferc_recovery_hash_index_v1",
        "self_excluded": True,
        "files": {str(path): _identity(path) for path in indexed},
    }
    _write_json(layout.hashes, hash_document)
    return result


def main() -> int:
    try:
        result = capture()
    except FileExistsError as exc:
        print(json.dumps({"status": "not_run", "reason": str(exc)}, sort_keys=True))
        return 2
    summary = result["summary"]
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["complete_validated_capture"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
