#!/usr/bin/env python3
"""Reconstruct a captured eLibrary ZIP when only DOS timestamps are volatile.

The tool requires two fresh server responses.  It proves that they differ only
at the four ZIP timestamp fields, validates every member against the immutable
R6 capture manifest, and searches a narrow Eastern-time window around the
recorded retrieval instant.  Publication occurs only on the full captured
SHA-256; the source-cache index is never changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zipfile


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def timestamp_offsets(blob: bytes) -> list[tuple[int, int]]:
    """Return (time_offset, date_offset) for every local and central header."""
    offsets: list[tuple[int, int]] = []
    with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
        handle.write(blob)
        temp_name = handle.name
    try:
        with zipfile.ZipFile(temp_name) as archive:
            infos = archive.infolist()
            start_dir = archive.start_dir
            for info in infos:
                if blob[info.header_offset : info.header_offset + 4] != b"PK\x03\x04":
                    raise ValueError("invalid local ZIP header")
                offsets.append((info.header_offset + 10, info.header_offset + 12))

            pos = start_dir
            central_count = 0
            while pos + 46 <= len(blob) and blob[pos : pos + 4] == b"PK\x01\x02":
                offsets.append((pos + 12, pos + 14))
                name_len, extra_len, comment_len = struct.unpack_from("<HHH", blob, pos + 28)
                pos += 46 + name_len + extra_len + comment_len
                central_count += 1
            if central_count != len(infos):
                raise ValueError("central-directory count mismatch")
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return offsets


def validate_members(blob: bytes, expected_members: list[dict]) -> list[dict]:
    with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
        handle.write(blob)
        temp_name = handle.name
    try:
        with zipfile.ZipFile(temp_name) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"template ZIP has bad CRC for {bad_member}")
            actual = [
                {
                    "name": info.filename,
                    "bytes": info.file_size,
                    "crc32": f"{info.CRC:08x}",
                }
                for info in archive.infolist()
                if not info.is_dir()
            ]
    finally:
        Path(temp_name).unlink(missing_ok=True)
    expected = [
        {"name": row["name"], "bytes": int(row["bytes"]), "crc32": row["crc32"]}
        for row in expected_members
    ]
    if actual != expected:
        raise ValueError("template ZIP members do not match the R6 capture")
    return actual


def dos_words(local_time: dt.datetime) -> tuple[int, int]:
    second = local_time.second // 2
    time_word = (local_time.hour << 11) | (local_time.minute << 5) | second
    date_word = ((local_time.year - 1980) << 9) | (local_time.month << 5) | local_time.day
    return time_word, date_word


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--capture-manifest", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--template-a", type=Path, required=True)
    parser.add_argument("--template-b", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--window-seconds", type=int, default=300)
    args = parser.parse_args()

    root = args.candidate_root.resolve()
    object_root = (root / "source_cache" / "objects").resolve()
    capture_path = args.capture_manifest.resolve()
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    if capture.get("schema") not in {
        "official_ferc_elibrary_dependency_capture_v1",
        "r6_capture_row_subset_v1",
    }:
        raise ValueError("unsupported capture-manifest schema")
    matching = [
        row for row in capture.get("rows", [])
        if (row.get("response") or {}).get("sha256") == args.expected_sha256
    ]
    if len(matching) != 1:
        raise ValueError("expected hash must identify exactly one capture row")
    row = matching[0]
    expected = row["response"]
    target = (root / expected["cache_path"]).resolve()
    if not target.is_relative_to(object_root) or target.name != args.expected_sha256:
        raise ValueError("unsafe or inconsistent destination")

    blob_a = args.template_a.read_bytes()
    blob_b = args.template_b.read_bytes()
    if len(blob_a) != int(expected["bytes"]) or len(blob_b) != len(blob_a):
        raise ValueError("template length does not match captured response")
    members = validate_members(blob_a, row["response_container"]["members"])
    if validate_members(blob_b, row["response_container"]["members"]) != members:
        raise ValueError("fresh template member populations differ")

    offsets_a = timestamp_offsets(blob_a)
    offsets_b = timestamp_offsets(blob_b)
    if offsets_a != offsets_b:
        raise ValueError("fresh template ZIP structures differ")
    mutable = set()
    for time_offset, date_offset in offsets_a:
        mutable.update(range(time_offset, time_offset + 2))
        mutable.update(range(date_offset, date_offset + 2))
    differing = {i for i, (left, right) in enumerate(zip(blob_a, blob_b)) if left != right}
    if not differing or not differing.issubset(mutable):
        raise ValueError("fresh responses differ outside ZIP timestamps")

    retrieved = dt.datetime.fromisoformat(row["retrieved_utc"])
    if retrieved.tzinfo is None:
        raise ValueError("capture retrieval time is not timezone-aware")
    found: tuple[bytes, dt.datetime, int] | None = None
    checked = 0
    # September is daylight time in the FERC server's Eastern time zone.  The
    # -5 fallback protects the method if a capture falls on a clock transition.
    for offset_hours in (-4, -5):
        zone = dt.timezone(dt.timedelta(hours=offset_hours))
        centre = retrieved.astimezone(zone).replace(tzinfo=None)
        for delta in range(-args.window_seconds, args.window_seconds + 1):
            candidate_time = centre + dt.timedelta(seconds=delta)
            time_word, date_word = dos_words(candidate_time)
            candidate = bytearray(blob_a)
            for time_offset, date_offset in offsets_a:
                struct.pack_into("<H", candidate, time_offset, time_word)
                struct.pack_into("<H", candidate, date_offset, date_word)
            candidate_blob = bytes(candidate)
            checked += 1
            if sha256(candidate_blob) == args.expected_sha256:
                found = candidate_blob, candidate_time, offset_hours
                break
        if found:
            break
    if not found:
        raise ValueError("captured hash not found in the declared timestamp window")

    reconstructed, reconstructed_time, offset_hours = found
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb", dir=target.parent, prefix=target.name + ".", delete=False
    ) as handle:
        handle.write(reconstructed)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, target)

    report = {
        "schema": "r6_time_variant_zip_reconstruction_v1",
        "status": "complete",
        "index_modified": False,
        "capture_manifest": str(capture_path.relative_to(root)),
        "source_capture_identity": capture.get("source_capture_identity"),
        "target": str(target.relative_to(root)),
        "bytes": len(reconstructed),
        "sha256": sha256(reconstructed),
        "template_a_sha256": sha256(blob_a),
        "template_b_sha256": sha256(blob_b),
        "fresh_differences_confined_to_zip_timestamps": True,
        "member_population": members,
        "recorded_retrieved_utc": row["retrieved_utc"],
        "matched_dos_local_time": reconstructed_time.isoformat(),
        "server_utc_offset_hours": offset_hours,
        "candidates_checked": checked,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.report.parent,
        prefix=args.report.name + ".", delete=False
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        report_temp = Path(handle.name)
    os.replace(report_temp, args.report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
