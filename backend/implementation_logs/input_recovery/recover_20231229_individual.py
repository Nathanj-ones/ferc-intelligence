#!/usr/bin/env python3
"""Capture the ten public files for eLibrary accession 20231229-5212.

The accession-wide DownloadP8File request returned HTTP 500.  FERC's public
file-list UI uses the same endpoint with one attachment ID at a time for an
individual-file download, so this script makes ten *distinct* requests.  It
does not retry, authenticate, send cookies, touch the source tree, or import
anything into the candidate cache.  Captured responses are immutable staging
evidence for a separately validated cache import.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import urllib.error
import urllib.request


HERE = pathlib.Path(__file__).resolve().parents[2]
ACCESSION = "20231229-5212"
LIST_BODY = HERE / "inputs/official_ferc_recovery/list_responses" / f"{ACCESSION}.body"
OUTPUT_ROOT = (HERE / "inputs/official_ferc_recovery/individual_responses" / ACCESSION)
RESULT_PATH = (HERE / "implementation_logs/input_recovery" /
               f"OFFICIAL_FERC_INDIVIDUAL_{ACCESSION}.json")
DOWNLOAD_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
LIST_SHA256 = "f5ef1ce4c1fda3adabb4f063fd6c0a4cd33f4d1e5dd391f176faa15e88538ddc"
ID_RE = re.compile(r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_new(path: pathlib.Path, body: bytes) -> None:
    """Create one file atomically; never replace earlier source evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to replace captured evidence: {path}")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(tmp), flags, 0o600)
        try:
            position = 0
            while position < len(body):
                written = os.write(fd, body[position:])
                if written <= 0:
                    raise OSError("zero-byte write while capturing response")
                position += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        path.chmod(0o444)
    finally:
        if tmp.exists():
            tmp.unlink()


def _safe_headers(headers) -> dict:
    keep = ("Content-Type", "Content-Disposition", "Content-Length", "Date",
            "ETag", "Last-Modified", "X-Request-ID")
    return {name.lower(): headers.get(name) for name in keep if headers.get(name)}


def _payload(attachment_id: str) -> bytes:
    value = {
        "FileType": "", "accession": ACCESSION, "fileid": 0,
        "FileIDAll": "", "fileidLst": [attachment_id], "Islegacy": False,
    }
    # Exact serialization used by ferclib.http.Client.post_json.
    return json.dumps(value, sort_keys=True).encode("utf-8")


def main() -> int:
    list_bytes = LIST_BODY.read_bytes()
    if _sha(list_bytes) != LIST_SHA256:
        raise SystemExit("pinned FERC list response changed")
    rows = json.loads(list_bytes.decode("utf-8")).get("DataList") or []
    if len(rows) != 10:
        raise SystemExit(f"expected 10 public attachments, found {len(rows)}")
    if RESULT_PATH.exists() or OUTPUT_ROOT.exists():
        raise SystemExit("individual recovery already exists; identical requests will not be repeated")

    results = []
    for order, row in enumerate(rows, 1):
        attachment_id = str(row.get("ID") or "")
        if not ID_RE.fullmatch(attachment_id) or row.get("Availability_Mode") != "P":
            raise SystemExit(f"invalid/non-public attachment declaration at row {order}")
        payload = _payload(attachment_id)
        request_headers = {
            "User-Agent": "Codex-FERC-integration-recovery/1.0",
            "Accept": "application/octet-stream,*/*",
            "Content-Type": "application/json",
            "Origin": "https://elibrary.ferc.gov",
            "Referer": ("https://elibrary.ferc.gov/eLibrary/filelist?"
                        f"accession_number={ACCESSION}"),
            "Connection": "close",
        }
        request = urllib.request.Request(
            DOWNLOAD_URL, data=payload, headers=request_headers, method="POST")
        started = _utc_now()
        status = None
        reason = ""
        response_headers = {}
        network_error = None
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                status = response.status
                reason = str(response.reason or "")
                response_headers = _safe_headers(response.headers)
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            reason = str(exc.reason or "")
            response_headers = _safe_headers(exc.headers)
            body = exc.read()
        except Exception as exc:  # retained as exact blocker; never retried
            body = b""
            network_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        ended = _utc_now()

        body_path = OUTPUT_ROOT / f"{order:02d}_{attachment_id}.body"
        meta_path = OUTPUT_ROOT / f"{order:02d}_{attachment_id}.json"
        _write_new(body_path, body)
        cache_body_sha = _sha(payload)
        record = {
            "schema": "official_ferc_individual_response_v1",
            "evidence_lane": "new official-FERC evidence after the independent audit",
            "accession_number": ACCESSION,
            "attachment_order": order,
            "attachment_id": attachment_id,
            "file_name": str(row.get("Orig_File_Name") or ""),
            "listed_media_type": str(row.get("MimeType") or ""),
            "listed_bytes": int(row.get("File_Size_Num") or 0),
            "availability_mode": row.get("Availability_Mode"),
            "request": {
                "method": "POST", "official_url": DOWNLOAD_URL,
                "payload_sha256": cache_body_sha, "payload_bytes": len(payload),
                "cache_url": f"{DOWNLOAD_URL}#body={cache_body_sha[:32]}",
                "credentials_or_cookies_sent": False,
                "retry_count": 0,
            },
            "response": {
                "started_utc": started, "ended_utc": ended,
                "http_status": status, "http_reason": reason,
                "headers": response_headers, "network_error": network_error,
                "body_path": str(body_path), "bytes": len(body),
                "sha256": _sha(body), "complete_body": network_error is None,
            },
        }
        _write_new(meta_path, (json.dumps(record, indent=1, sort_keys=True) + "\n").encode())
        results.append(record)

    result = {
        "schema": "official_ferc_individual_recovery_v1",
        "evidence_lane": "new official-FERC evidence after the independent audit",
        "accession_number": ACCESSION,
        "reason": ("the accession-wide ten-ID request returned HTTP 500; FERC's public "
                   "file-list UI independently confirms one-ID DownloadP8File requests"),
        "official_ui_version": "5.5.10.2 (release 2025-08-22, observed 2026-09-09)",
        "network_policy": "one request per public attachment; no retries/auth/cookies/bypass",
        "list_response": {"path": str(LIST_BODY), "bytes": len(list_bytes),
                          "sha256": _sha(list_bytes)},
        "completed_utc": _utc_now(),
        "results": results,
        "summary": {
            "requests": len(results),
            "http_200": sum(r["response"]["http_status"] == 200 for r in results),
            "failed": sum(r["response"]["http_status"] != 200 for r in results),
            "response_bytes": sum(r["response"]["bytes"] for r in results),
        },
    }
    _write_new(RESULT_PATH, (json.dumps(result, indent=1, sort_keys=True) + "\n").encode())
    print(json.dumps(result["summary"], sort_keys=True))
    return 0 if result["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
