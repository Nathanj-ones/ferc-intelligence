#!/usr/bin/env python3
"""Recover only R6 dependency-cache objects declared by CAPTURE.json.

This is deliberately narrower than the production HTTP client.  It never edits
the source-cache index, never accepts a response whose bytes differ from the R6
capture, and only publishes an object after both its length and SHA-256 match.
No credential is read or sent; all requests target the public FERC eLibrary API.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


UA = "ferc-intel/0.37 (+local recovery; exact R6 bytes only)"
ALLOWED_HOST = "elibrary.ferc.gov"


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def request_bytes(row: dict, attempts: int = 3) -> bytes:
    method = row.get("method")
    source_url = str(row.get("source_url") or "")
    parts = urllib.parse.urlsplit(source_url)
    if parts.scheme != "https" or parts.hostname != ALLOWED_HOST:
        raise ValueError(f"refusing non-FERC URL: {source_url!r}")
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))

    payload = row.get("request") or {}
    body = payload.get("body")
    if method == "GET":
        if body is not None:
            raise ValueError(f"GET row unexpectedly has a body: {source_url}")
        data = None
        headers = {"User-Agent": UA, "Accept": "application/json"}
    elif method == "POST":
        if not isinstance(body, dict):
            raise ValueError(f"POST row lacks a JSON body: {source_url}")
        data = json.dumps(body, sort_keys=True).encode("utf-8")
        expected_request_bytes = int(payload.get("bytes") or 0)
        expected_request_hash = str(payload.get("sha256") or "")
        if len(data) != expected_request_bytes or digest(data) != expected_request_hash:
            raise ValueError(f"request identity mismatch for {source_url}")
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    else:
        raise ValueError(f"unsupported method {method!r}")

    accession = row.get("accession_number")
    if accession:
        headers["Origin"] = "https://elibrary.ferc.gov"
        headers["Referer"] = (
            "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/"
            f"GetFileListFromP8/{accession}"
        )

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return response.read()
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--capture-manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    root = args.candidate_root.resolve()
    cache_root = (root / "source_cache").resolve()
    object_root = (cache_root / "objects").resolve()
    capture_path = args.capture_manifest.resolve()
    if not capture_path.is_relative_to(root):
        raise ValueError("capture manifest must be inside the candidate root")

    capture_blob = capture_path.read_bytes()
    capture = json.loads(capture_blob)
    rows = capture.get("rows") or []
    if capture.get("schema") != "official_ferc_elibrary_dependency_capture_v1":
        raise ValueError("unexpected capture-manifest schema")
    if len(rows) != int(capture.get("new_entries") or -1):
        raise ValueError("capture row count does not match new_entries")

    report = {
        "schema": "r6_dependency_cache_exact_recovery_v1",
        "candidate_root": str(root),
        "capture_manifest": {
            "path": str(capture_path.relative_to(root)),
            "bytes": len(capture_blob),
            "sha256": digest(capture_blob),
        },
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "index_modified": False,
        "rows": [],
        "status": "running",
    }

    try:
        for row in rows:
            expected = row.get("response") or {}
            expected_hash = str(expected.get("sha256") or "")
            expected_bytes = int(expected.get("bytes") or -1)
            rel = Path(str(expected.get("cache_path") or ""))
            target = (root / rel).resolve()
            if not target.is_relative_to(object_root):
                raise ValueError(f"unsafe cache target {rel}")
            if target.name != expected_hash or len(expected_hash) != 64:
                raise ValueError(f"cache target/hash mismatch {rel}")

            result = {
                "cache_path": str(rel),
                "expected_bytes": expected_bytes,
                "expected_sha256": expected_hash,
                "method": row.get("method"),
                "source_url": row.get("source_url"),
            }
            if target.is_file():
                existing = target.read_bytes()
                if len(existing) != expected_bytes or digest(existing) != expected_hash:
                    raise ValueError(f"existing object has wrong identity: {rel}")
                result["action"] = "already_present_exact"
            else:
                blob = request_bytes(row)
                result["retrieved_bytes"] = len(blob)
                result["retrieved_sha256"] = digest(blob)
                if len(blob) != expected_bytes or digest(blob) != expected_hash:
                    result["action"] = "rejected_changed_response"
                    report["rows"].append(result)
                    raise ValueError(f"official response no longer matches R6 bytes: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "wb", dir=target.parent, prefix=target.name + ".", delete=False
                ) as handle:
                    handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.replace(temp_path, target)
                result["action"] = "recovered_exact"
            report["rows"].append(result)
            atomic_json(args.report, report)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_json(args.report, report)
        raise

    report["status"] = "complete"
    report["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["recovered_exact"] = sum(
        row["action"] == "recovered_exact" for row in report["rows"]
    )
    report["already_present_exact"] = sum(
        row["action"] == "already_present_exact" for row in report["rows"]
    )
    atomic_json(args.report, report)
    print(json.dumps({
        "status": report["status"],
        "recovered_exact": report["recovered_exact"],
        "already_present_exact": report["already_present_exact"],
        "report": str(args.report),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
