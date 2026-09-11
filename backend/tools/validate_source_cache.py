#!/usr/bin/env python3
"""Validate every index-declared source-cache object without changing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    root = args.cache_root.resolve()
    object_root = (root / "objects").resolve()
    index_path = root / "index.json"
    index_blob = index_path.read_bytes()
    index = json.loads(index_blob)
    declared: dict[str, list[dict]] = {}
    index_errors: list[dict] = []
    for cache_key, entry in index.items():
        rel_text = str(entry.get("cache_path") or "")
        rel = Path(rel_text)
        target = (root / rel).resolve()
        content_hash = str(entry.get("content_hash") or "")
        if (
            not target.is_relative_to(object_root)
            or target.name != content_hash
            or len(content_hash) != 64
        ):
            index_errors.append({"cache_key": cache_key, "cache_path": rel_text})
            continue
        declared.setdefault(rel_text, []).append(entry)

    missing: list[str] = []
    mismatches: list[dict] = []
    total_bytes = 0
    for rel_text in sorted(declared):
        path = root / rel_text
        if not path.is_file():
            missing.append(rel_text)
            continue
        size = path.stat().st_size
        total_bytes += size
        expected_hash = path.name
        expected_sizes = {int(entry.get("byte_size") or -1) for entry in declared[rel_text]}
        actual_hash = file_hash(path)
        if expected_sizes != {size} or actual_hash != expected_hash:
            mismatches.append(
                {
                    "cache_path": rel_text,
                    "actual_bytes": size,
                    "declared_bytes": sorted(expected_sizes),
                    "actual_sha256": actual_hash,
                    "expected_sha256": expected_hash,
                }
            )

    actual = {
        str(path.relative_to(root))
        for path in object_root.rglob("*")
        if path.is_file()
    }
    unreferenced = sorted(actual - set(declared))
    report = {
        "schema": "source_cache_full_validation_v1",
        "status": "pass" if not (index_errors or missing or mismatches or unreferenced) else "fail",
        "cache_root": str(root),
        "index": {
            "bytes": len(index_blob),
            "sha256": hashlib.sha256(index_blob).hexdigest(),
            "url_entries": len(index),
            "distinct_objects": len(declared),
        },
        "validated_objects": len(declared) - len(missing),
        "validated_logical_bytes": total_bytes,
        "index_errors": index_errors,
        "missing": missing,
        "content_or_size_mismatches": mismatches,
        "unreferenced_objects": unreferenced,
    }
    atomic_json(args.report, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
