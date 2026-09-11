#!/usr/bin/env python3
"""Freeze the selected candidate tree outside macOS File Provider.

The implementation candidate lives below Desktop, where otherwise valid files
can be evicted immediately after they are read.  This one-time integration
utility copies the declared build inputs to a separate staging root.  Every
file is read completely, hashed, written to a temporary file, fsynced, and
atomically installed.  The source tree is never written.

The source cache, databases, stale generated outputs, Git metadata, bytecode,
and general worker logs are deliberately excluded.  Selected log subtrees can
be added explicitly because the run plan treats recovery records as inputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid


EXCLUDED_TOP = {
    ".git",
    ".generations",
    "__pycache__",
    "implementation_logs",
    "discovery",
    "exports",
    "final_records",
    "reports",
    "requests",
    "source_cache",
    "staging",
    "verification",
    "work",
}

EXCLUDED_RELATIVE = {
    "RELEASE_IDENTITY.md",
    "REPAIR_REPORT.md",
    "IMPLEMENTATION_ACCEPTANCE.md",
    "RUN_STATUS.md",
    "artifact_manifest.json",
    "artifact_manifest_full.json",
    "artifact_manifest_lite.json",
    "publication_receipt.json",
    "release_attempt_failure_full.json",
    "release_attempt_failure_lite.json",
    "release_receipt.json",
    "release_receipt_full.json",
    "release_receipt_lite.json",
    "config/crosswalk_publication.json",
    "config/field_crosswalk_166.csv",
    "config/requirements_crosswalk.csv",
    "config/requirements_crosswalk_summary.json",
    "run_status.json",
    "task_ledger.json",
    "issue_resolution.csv",
    "issue_resolution.json",
    "exceptions_resolution.csv",
    "exceptions_resolution.json",
    # Superseded/provisional snapshots are neither declared inputs nor outputs.
    "config/universe_draft.csv",
    "data_side_facts.json",
}

EXCLUDED_PREFIXES = {
    "evidence/audit_baseline/",
    "evidence/repair_records/",
}

# An explicit include can recover only the immutable official-source records
# deliberately omitted with the broader implementation_logs/ tree.  It must not
# provide a back door that reintroduces generated exports, test scratch, staging
# databases, or prior release claims into an allegedly empty Build-A root.
INCLUDEABLE_SUBTREES = {"implementation_logs/input_recovery"}


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def safe_relative(value: str) -> str:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise SystemExit(f"unsafe relative subtree: {value!r}")
    return pure.as_posix().rstrip("/")


def selected_files(source: Path, includes: list[str]) -> list[tuple[str, Path]]:
    selected: dict[str, Path] = {}
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        parts = PurePosixPath(relative).parts
        if (parts[0] in EXCLUDED_TOP or "__pycache__" in parts
                or path.suffix == ".pyc" or relative in EXCLUDED_RELATIVE
                or any(relative.startswith(prefix) for prefix in EXCLUDED_PREFIXES)
                or relative.startswith("config/.generations/")):
            continue
        selected[relative] = path
    for include in includes:
        if not any(include == allowed or include.startswith(allowed + "/")
                   for allowed in INCLUDEABLE_SUBTREES):
            raise SystemExit(
                f"included subtree is not an approved immutable-input subtree: {include}")
        root = source / include
        if not root.is_dir():
            raise SystemExit(f"included subtree is missing: {root}")
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            parts = PurePosixPath(relative).parts
            if "__pycache__" in parts or path.suffix == ".pyc":
                continue
            selected[relative] = path
    return sorted(selected.items())


def atomic_write(path: Path, body: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path: Path, value: dict) -> None:
    body = (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, body, 0o644)


def stable_source_bytes(path: Path, attempts: int = 5) -> tuple[bytes, os.stat_result, bool]:
    """Read through File Provider hydration without accepting a moving file.

    Hydration may replace an inode and even expose a placeholder size on the
    first stat.  A valid read therefore needs two consecutive identical byte
    populations whose final lstat metadata agrees with their length.  Ordinary
    concurrent edits continue to fail closed because their bytes or terminal
    metadata do not stabilise within the bounded retry count.
    """
    metadata_changed = False
    previous: bytes | None = None
    previous_after: os.stat_result | None = None
    for _attempt in range(attempts):
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or path.is_symlink():
            raise SystemExit(f"selected source is not a regular non-symlink: {path}")
        body = path.read_bytes()
        after = path.lstat()
        if not stat.S_ISREG(after.st_mode) or path.is_symlink():
            raise SystemExit(f"selected source changed type while being read: {path}")
        before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        metadata_changed = metadata_changed or before_id != after_id
        # A regular file whose identity is unchanged across a complete read is
        # already a stable snapshot; the second read is only needed when File
        # Provider hydration actually moved its metadata/inode beneath us.
        if len(body) == after.st_size and before_id == after_id:
            return body, after, metadata_changed
        if len(body) == after.st_size and previous == body and previous_after is not None:
            prior_id = (previous_after.st_dev, previous_after.st_ino,
                        previous_after.st_size, previous_after.st_mtime_ns)
            if prior_id == after_id:
                return body, after, metadata_changed
        previous, previous_after = body, after
    raise SystemExit(f"source did not reach a stable byte/metadata identity: {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--include-subtree", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    source = args.source.resolve(strict=True)
    destination = args.destination.resolve()
    ledger = args.ledger.resolve()
    includes = [safe_relative(value) for value in args.include_subtree]
    if destination == source or source in destination.parents:
        raise SystemExit("destination must be outside the source tree")
    if ledger == source or source in ledger.parents:
        raise SystemExit("ledger must be outside the source tree")
    if args.apply and destination.exists() and any(destination.iterdir()):
        raise SystemExit(
            "apply destination must be absent or empty; refusing to retain stale files")

    rows = []
    total_bytes = 0
    files = selected_files(source, includes)
    for relative, path in files:
        body, stable_stat, metadata_changed = stable_source_bytes(path)
        digest = sha256_bytes(body)
        target = destination / relative
        action = "would_write"
        if target.is_file() and not target.is_symlink():
            existing = sha256_file(target)
            if existing == (digest, len(body)):
                action = "verified_existing"
            elif args.apply:
                atomic_write(target, body, stat.S_IMODE(stable_stat.st_mode))
                action = "replaced_mismatch"
        elif args.apply:
            atomic_write(target, body, stat.S_IMODE(stable_stat.st_mode))
            action = "written"
        if args.apply and sha256_file(target) != (digest, len(body)):
            raise SystemExit(f"destination verification failed: {target}")
        total_bytes += len(body)
        rows.append({
            "path": relative,
            "bytes": len(body),
            "sha256": digest,
            "source_mode": f"{stat.S_IMODE(stable_stat.st_mode):04o}",
            "source_metadata_changed_during_read": metadata_changed,
            "action": action,
        })

    result = {
        "schema": "ferc-candidate-tree-materialization-v1",
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "complete" if args.apply else "dry_run",
        "source_read_only": str(source),
        "destination": str(destination),
        "included_subtrees": includes,
        "excluded_top_level": sorted(EXCLUDED_TOP),
        "excluded_relative": sorted(EXCLUDED_RELATIVE),
        "excluded_prefixes": sorted(EXCLUDED_PREFIXES),
        "includeable_subtrees": sorted(INCLUDEABLE_SUBTREES),
        "files": rows,
        "file_count": len(rows),
        "bytes": total_bytes,
        "source_files_modified": False,
        "ferc_source_network_used": False,
        "macos_file_provider_hydration_may_have_occurred_while_reading": True,
    }
    atomic_json(ledger, result)
    print(json.dumps({
        "status": result["status"],
        "files": len(rows),
        "bytes": total_bytes,
        "ledger": str(ledger),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
