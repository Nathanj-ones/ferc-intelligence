#!/usr/bin/env python3
"""Replace only File Provider placeholders with verified R6 archive bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile


UF_DATALESS = 0x40000000


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def is_dataless(path: Path) -> bool:
    return bool(path.stat(follow_symlinks=False).st_flags & UF_DATALESS)


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
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    root = args.candidate_root.resolve()
    archive_path = args.archive.resolve()
    archive_hash = file_digest(archive_path)
    if archive_hash != args.archive_sha256:
        raise ValueError("archive SHA-256 mismatch")

    manifest_blob = (root / "artifact_manifest.json").read_bytes()
    manifest = json.loads(manifest_blob)
    declared = manifest.get("files") or {}
    placeholders = sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and is_dataless(path)
    )
    declared_placeholders = [
        path for path in placeholders
        if path.relative_to(root).as_posix() in declared
    ]
    undeclared_placeholders = [
        path.relative_to(root).as_posix() for path in placeholders
        if path.relative_to(root).as_posix() not in declared
    ]
    report = {
        "schema": "r6_placeholder_materialization_v1",
        "status": "running",
        "candidate_root": str(root),
        "archive": {
            "path": str(archive_path),
            "bytes": archive_path.stat().st_size,
            "sha256": archive_hash,
        },
        "artifact_manifest": {
            "bytes": len(manifest_blob),
            "sha256": digest(manifest_blob),
        },
        "initial_placeholders": len(placeholders),
        "initial_placeholder_bytes": sum(path.stat().st_size for path in placeholders),
        "declared_placeholders": len(declared_placeholders),
        "undeclared_placeholders": undeclared_placeholders,
        "restored": [],
    }

    try:
        with zipfile.ZipFile(archive_path) as archive:
            for path in declared_placeholders:
                rel = path.relative_to(root).as_posix()
                expected = declared.get(rel)
                if not isinstance(expected, dict):
                    raise AssertionError(f"declared placeholder disappeared: {rel}")
                member = f"operating_assets_all_regimes/{rel}"
                info = archive.getinfo(member)
                if info.is_dir() or info.flag_bits & 0x1:
                    raise ValueError(f"unsafe or encrypted member: {member}")
                blob = archive.read(info)
                if len(blob) != int(expected["bytes"]) or digest(blob) != expected["sha256"]:
                    raise ValueError(f"archive member identity mismatch: {member}")

                mode = path.stat(follow_symlinks=False).st_mode & 0o777
                with tempfile.NamedTemporaryFile(
                    "wb", dir=path.parent, prefix=path.name + ".", delete=False
                ) as handle:
                    handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.chmod(temp_path, mode)
                os.replace(temp_path, path)
                report["restored"].append(
                    {"path": rel, "bytes": len(blob), "sha256": digest(blob)}
                )
                atomic_json(args.report, report)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        atomic_json(args.report, report)
        raise

    remaining = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and is_dataless(path)
    )
    remaining_declared = [rel for rel in remaining if rel in declared]
    report["status"] = "complete" if not remaining_declared else "failed"
    report["restored_files"] = len(report["restored"])
    report["restored_bytes"] = sum(row["bytes"] for row in report["restored"])
    report["remaining_placeholders"] = remaining
    report["remaining_declared_placeholders"] = remaining_declared
    atomic_json(args.report, report)
    print(json.dumps({
        "status": report["status"],
        "restored_files": report["restored_files"],
        "restored_bytes": report["restored_bytes"],
        "remaining_placeholders": len(remaining),
    }, sort_keys=True))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
