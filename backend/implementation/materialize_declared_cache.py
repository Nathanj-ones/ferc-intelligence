#!/usr/bin/env python3
"""Materialize only index-declared candidate cache objects from local evidence.

This is a one-time implementation-workspace utility, not a pipeline runtime
dependency.  It never writes the source cache.  A target placeholder is
replaced only after a resident source file hashes to the content-addressed
filename and matches every indexed byte-size declaration.  APFS clone-copy is
used so the source inode and directory remain untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import uuid


DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)


def sha_and_size(path: pathlib.Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def resident_regular(path: pathlib.Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink() \
        and not (info.st_flags & DATALESS)


def atomic_clone(source: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        completed = subprocess.run(
            ["/bin/cp", "-c", str(source), str(temporary)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=120)
        if completed.returncode != 0:
            raise RuntimeError(
                f"clone-copy failed for {source}: {completed.stderr.strip()[:500]}")
        os.chmod(temporary, 0o444)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path: pathlib.Path, value: dict) -> None:
    body = (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-cache", type=pathlib.Path, required=True)
    parser.add_argument("--source-cache", type=pathlib.Path, action="append", default=[])
    parser.add_argument("--recovery-root", type=pathlib.Path, action="append", default=[])
    parser.add_argument("--ledger", type=pathlib.Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    candidate = args.candidate_cache.resolve()
    sources = [path.resolve() for path in args.source_cache]
    recovery_roots = [path.resolve() for path in args.recovery_root]
    if any(source == candidate for source in sources):
        raise SystemExit("candidate and source cache must be distinct")
    index_path = candidate / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    expected: dict[str, int] = {}
    for key, entry in index.items():
        digest = str(entry.get("content_hash", ""))
        size = int(entry.get("byte_size", -1))
        canonical = f"objects/{digest[:2]}/{digest}"
        if (len(key) != 64 or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in key + digest)
                or entry.get("cache_path") != canonical or size < 0):
            raise SystemExit(f"invalid cache-index entry: {key}")
        if digest in expected and expected[digest] != size:
            raise SystemExit(f"conflicting indexed sizes for {digest}")
        expected[digest] = size

    recovery_candidates = []
    for root in recovery_roots:
        if root.is_dir():
            recovery_candidates.extend(
                path for path in root.rglob("*.body") if resident_regular(path))
    recovery_by_size: dict[int, list[pathlib.Path]] = {}
    for path in recovery_candidates:
        recovery_by_size.setdefault(path.stat().st_size, []).append(path)

    records = []
    unresolved = []
    already_resident = 0
    for digest, size in sorted(expected.items()):
        target = candidate / "objects" / digest[:2] / digest
        if resident_regular(target):
            actual = sha_and_size(target)
            if actual != (digest, size):
                raise SystemExit(f"resident candidate object has wrong identity: {target}")
            already_resident += 1
            continue
        choices = [source / "objects" / digest[:2] / digest for source in sources]
        choices.extend(recovery_by_size.get(size, []))
        matched = None
        for source in choices:
            if not resident_regular(source) or source.stat().st_size != size:
                continue
            if sha_and_size(source) == (digest, size):
                matched = source
                break
        if matched is None:
            unresolved.append({"sha256": digest, "bytes": size,
                               "target": str(target)})
            continue
        record = {"sha256": digest, "bytes": size, "source": str(matched),
                  "target": str(target), "method": "APFS clone-copy",
                  "applied": bool(args.apply)}
        if args.apply:
            atomic_clone(matched, target)
            if not resident_regular(target) or sha_and_size(target) != (digest, size):
                raise SystemExit(f"post-clone identity failed: {target}")
        records.append(record)

    result = {
        "schema": "ferc-local-cache-materialization-v1",
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "complete" if not unresolved and args.apply else
                  "dry_run" if not args.apply else "partial",
        "candidate_cache": str(candidate),
        "candidate_index": {"path": str(index_path),
                            "sha256": sha_and_size(index_path)[0],
                            "url_entries": len(index),
                            "unique_objects": len(expected),
                            "unique_bytes": sum(expected.values())},
        "source_caches_read_only": [str(path) for path in sources],
        "recovery_roots_read_only": [str(path) for path in recovery_roots],
        "already_resident": already_resident,
        "materialized": records,
        "unresolved": unresolved,
        "network_used": False,
        "source_files_modified": False,
    }
    atomic_json(args.ledger, result)
    print(json.dumps({"status": result["status"], "already_resident": already_resident,
                      "materialized": len(records), "unresolved": len(unresolved),
                      "ledger": str(args.ledger)}, sort_keys=True))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
