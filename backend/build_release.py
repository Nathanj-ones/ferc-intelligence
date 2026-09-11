#!/usr/bin/env python3
"""
Build and verify the release.

Rewritten for audit A13 and A21.

A13: the old builder made its manifest by walking the tree, so the manifest was
an INVENTORY OF WHATEVER HAPPENED TO EXIST. Deleting `exports/quarterly_key_
metrics.csv` simply produced a manifest with one fewer entry, the clean room
verified the archive against that shrunken manifest, and the release reported
PASS with 84 files. A required output was silently redefined out of the release.
The fix is that the release is now measured against a FIXED CONTRACT declared in
advance -- `acceptance/contract.py` -- which does not move when the tree does.

A21: the old scheme was circular and stale. `artifact_manifest.json` carried
three entries for files written after it (including the database and the receipt
itself), so those entries were wrong the moment they landed. The chain is now
strictly acyclic, in exactly four stages, each consuming only what the previous
one froze:

    1. freeze payload   every generated artefact exists; nothing will change again
    2. hash payload     hash each file FROM ITS BYTES -> artifact_manifest.json
    3. create archive   the archive contains exactly the payload plus the manifest
    4. issue receipt    an EXTERNAL receipt naming the finished archive's hash

The manifest never contains itself, the receipt, or anything written after it.
The receipt lives outside the archive it certifies, so it can state that
archive's hash truthfully. A payload change after stage 2 invalidates the
payload digest and the build must be re-run -- which the verification detects
rather than trusting.

EVERY HASH AND SIZE IS TAKEN FROM BYTES ACTUALLY READ. On this volume
`stat().st_size` returns a stale placeholder size for a cloud-evicted file until
its bytes are read: `RUN_STATUS.md` reported 8,046 bytes by stat() and 35,171 by
reading. A manifest built on stat() records sizes that were never verified.

    python3 build_release.py                 # code + reports + evidence (lite)
    python3 build_release.py --payload full --defer-build-b  # including DB/cache
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
import sys
import tempfile
import uuid
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from acceptance.contract import (                                    # noqa: E402
    REQUIRED_OUTPUTS, RELEASE_STAGES, WRITTEN_AFTER_MANIFEST,
    blocking, check_required_outputs, enforcement_evidence,
    manifest_cycle_violations, unverified)

NAME = "operating_assets_all_regimes"
#: Archive filenames are deliberately separate from the stable internal package
#: root.  The repaired marker prevents a new candidate from overwriting, or being
#: mistaken for, either audited predecessor.
ARCHIVE_STEM = "operating_assets_all_regimes_REPAIRED"
MANIFEST_NAME = "artifact_manifest.json"
LITE_MANIFEST_NAME = "artifact_manifest_lite.json"
FULL_MANIFEST_NAME = "artifact_manifest_full.json"
LITE_RECEIPT_NAME = "release_receipt_lite.json"
FULL_RECEIPT_NAME = "release_receipt_full.json"
LEGACY_RECEIPT_NAME = "release_receipt.json"
PUBLICATION_RECEIPT_NAME = "publication_receipt.json"

# One large official-response body is also the content-addressed cache object
# consumed by replay.  Shipping both byte-identical names adds 109 MB but no
# evidence.  This is an exact, fail-closed declaration -- never a suffix/glob.
# The omitted path, canonical object, source-cache index and provenance records
# must all agree before either payload can be hashed.
REDUNDANT_PAYLOAD_ALIASES = ({
    "omitted_path": (
        "inputs/official_ferc_recovery/individual_responses/20231229-5212/"
        "07_F19520DA-2360-C302-8619-8CB6CB100000.body"),
    "canonical_path": (
        "source_cache/objects/da/"
        "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b"),
    "sha256": "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b",
    "bytes": 109287608,
    "provenance_paths": [
        "implementation_logs/input_recovery/OFFICIAL_FERC_INDIVIDUAL_20231229-5212.json",
        "implementation_logs/input_recovery/cache_import_runs.jsonl",
    ],
    "reason": ("the exact successful response bytes are shipped once at their "
               "content-addressed cache path; request and import provenance remain "
               "separate small records"),
},)

#: Never part of any payload: build caches, OS metadata, tool telemetry and the
#: SQLite sidecars, which are folded into the database by a checkpoint first.
EXCLUDE_DIRS = {"__pycache__", ".claude-flow", ".git"}
EXCLUDE_SUFFIX = {".pyc", ".tmp", ".sqlite-wal", ".sqlite-shm"}
EXCLUDE_RELATIVE = {
    # Exact stale/provisional snapshots.  Lookalike evidence at other paths is
    # retained; only these two known payload leaks are excluded.
    "config/universe_draft.csv",
    "data_side_facts.json",
}
EXCLUDE_PREFIXES = (
    # Acceptance and adapter scratch is never release evidence.  In particular,
    # the acceptance harness can leave disposable database copies below
    # work/w6-acceptance; allowing FULL payload discovery to sweep those files in
    # would both leak scratch state and make package size depend on test history.
    "work/",
    "staging/migration_test/",
    "staging/work/",
    # Pre-Build-A working records are inputs to the final-record generator, not
    # release claims.  The hash-bound FINAL_* records replace them in payloads.
    "implementation/REPAIR_LEDGER_DRAFT.",
    "implementation/EXCEPTION_DISPOSITIONS_DRAFT.",
    "implementation/FINAL_INPUT_INVENTORY_DRAFT.",
    "implementation/RELATED_REPAIR_FINDINGS_DRAFT.",
    "implementation/ADDITIONAL_INPUTS_DISCOVERED_DRAFT.",
    # The run specification is a pre-execution worksheet.  Only the manifest
    # reconstructed from complete verbose logs is release evidence.
    "implementation/FINAL_TEST_RUN_SPEC_DRAFT.",
)
# Worker/debug logs are intentionally not swept into a release merely because
# they happen to exist.  The narrowly captured official-source recovery records
# are declared replay inputs; final test logs live under verification/ and are
# bound by verification/final_test_run_manifest.json.
IMPLEMENTATION_LOG_INCLUDE_PREFIXES = ("implementation_logs/input_recovery/",)
#: Written AFTER the manifest, so they can never be manifest entries (A21).
EXCLUDE_NAMES = {".DS_Store", MANIFEST_NAME, LITE_MANIFEST_NAME,
                 FULL_MANIFEST_NAME, LITE_RECEIPT_NAME, FULL_RECEIPT_NAME,
                 LEGACY_RECEIPT_NAME, "release_attempt_failure_lite.json",
                 "release_attempt_failure_full.json", "REPAIR_REPORT.md",
                 "RELEASE_IDENTITY.md", "IMPLEMENTATION_ACCEPTANCE.md",
                 "issue_resolution.json", "issue_resolution.csv",
                 "exceptions_resolution.json", "exceptions_resolution.csv"}

#: Excluded from the LITE payload only, and declared as such rather than
#: quietly dropped. The lite package is never claimed to be standalone.
LITE_EXCLUDE_PREFIXES = ("source_cache/", "staging/")

#: Non-export artifacts a FULL, offline-reproducible payload must carry.  This is
#: fixed source data, not inferred by walking whatever happens to exist.
FULL_MANDATORY_ARTIFACTS = (
    "RUNTIME_REQUIREMENTS.json",
    "config/run_plan.json",
    "config/universe.csv",
    "config/annotations/MANIFEST.json",
    "config/annotations/reviewed_source_annotations.json",
    "config/requirements_crosswalk.csv",
    "config/requirements_crosswalk_summary.json",
    "config/field_crosswalk_166.csv",
    "config/crosswalk_publication.json",
    "evidence/test_fixtures/reviewed_image_sources.json",
    "ferclib/image_ocr.py",
    "tools/vision_ocr.swift",
    "tools/verify_reviewed_image_sources.py",
    "implementation/build_final_release_records.py",
    "implementation/build_test_run_manifest.py",
    "implementation/run_build_b_acceptance.py",
    "implementation/finalize_external_release_evidence.py",
    "implementation/import_bounded_elibrary_search_capture.py",
    "inputs/official_ferc/elibrary/rate_proceedings_v1.csv",
    "inputs/official_ferc/elibrary/lng_facility_sources_v1.csv",
    "migrations/003_filing_entity_associations_2026_09_10.py",
    "inputs/official_ferc_elibrary_search_recovery_20260909/CAPTURE.json",
    "inputs/official_ferc_elibrary_search_recovery_20260909/source_cache/index.json",
    "inputs/official_ferc_elibrary_search_recovery_20260909/source_cache/objects/2b/2bb46982d886dab57e12ecaebb1787aa4fedc02dcd18046fdce163da0ca84b6b",
    "inputs/official_ferc_elibrary_search_recovery_20260909/source_cache/objects/e0/e084af687cb7e44210ca12ba9fac107f20b0b990f607d8cca39c64bd5e732fad",
    "inputs/official_ferc_elibrary_search_recovery_20260909/source_cache/objects/79/79eb4eb2b984f550ba534704fd2bf74c39e71c629bd091d314631a9aaa1d85c3",
    "implementation_logs/input_recovery/bounded_elibrary_search_import_runs.jsonl",
    "inputs/official_ferc_elibrary_dependency_recovery_20260910/CAPTURE.json",
    "inputs/official_ferc_elibrary_dependency_recovery_20260910/source_cache_index_before.json",
    "implementation_logs/input_recovery/build_a_v4_dependency_capture.log",
    "inputs/reference_regressions/transco/transco_canonical_metrics_2016_2026.csv",
    "inputs/reference_regressions/tgp/tgp_canonical_metrics_2016_2026.csv",
    "source_cache/index.json",
    "staging/operating_assets.sqlite",
    "verification/a17_reviewed_image_gate.json",
    "verification/validation_results.json",
    "verification/final_test_run_manifest.json",
    "final_records/FINAL_REPAIR_LEDGER.json",
    "final_records/FINAL_REPAIR_LEDGER.csv",
    "final_records/FINAL_EXCEPTION_DISPOSITIONS_34.json",
    "final_records/FINAL_EXCEPTION_DISPOSITIONS_34.csv",
    "final_records/FINAL_INPUT_INVENTORY_13.json",
    "final_records/FINAL_INPUT_INVENTORY_13.csv",
    "final_records/FINAL_ADDITIONAL_INPUTS_DISCOVERED.json",
    "final_records/FINAL_ADDITIONAL_INPUTS_DISCOVERED.csv",
    "final_records/INTEGRATED_REPAIR_REPORT.md",
    "final_records/FINAL_TEST_EVIDENCE_INDEX.json",
    "final_records/FINAL_RECORDS_RECEIPT.json",
    PUBLICATION_RECEIPT_NAME,
)

FINAL_RECORD_FILES = (
    "FINAL_REPAIR_LEDGER.json",
    "FINAL_REPAIR_LEDGER.csv",
    "FINAL_EXCEPTION_DISPOSITIONS_34.json",
    "FINAL_EXCEPTION_DISPOSITIONS_34.csv",
    "FINAL_INPUT_INVENTORY_13.json",
    "FINAL_INPUT_INVENTORY_13.csv",
    "FINAL_ADDITIONAL_INPUTS_DISCOVERED.json",
    "FINAL_ADDITIONAL_INPUTS_DISCOVERED.csv",
    "INTEGRATED_REPAIR_REPORT.md",
    "FINAL_TEST_EVIDENCE_INDEX.json",
)
FINAL_RECORD_RECEIPT = "FINAL_RECORDS_RECEIPT.json"

CLASSES = {
    "engine_script": "code; hashed from bytes",
    "generated_data": "produced by a documented generator; hashed from bytes",
    "generated_report": "produced by a run; hashed from bytes",
    "frozen_evidence_input": "immutable input; hashed from bytes and must not change",
    "source_cache": "retrieved source bytes; hashed from bytes",
    "staging_database": "the staged output; hashed from bytes",
    "acceptance": "acceptance contract and harness; hashed from bytes",
}


def receipt_for(full: bool) -> str:
    """ONE RECEIPT PER PAYLOAD.

    A single `release_receipt.json` cannot describe two archives. Building lite
    after full silently overwrote the full payload's receipt, leaving the 646 MB
    deliverable with no identity record and the surviving file describing a
    different archive entirely -- the same one-artefact-serving-two-facts shape as
    A21's stale manifest, introduced by the builder written to fix A21.
    """
    return FULL_RECEIPT_NAME if full else LITE_RECEIPT_NAME


def manifest_for(full: bool) -> str:
    """A tree-side manifest can never describe two payloads."""
    return FULL_MANIFEST_NAME if full else LITE_MANIFEST_NAME


def archive_name(full: bool, version: str = "") -> str:
    suffix = f"_{version}" if version else ""
    return f"{ARCHIVE_STEM}{suffix}{'_FULL' if full else ''}.zip"


class ReleaseRefused(RuntimeError):
    """The candidate does not satisfy the fixed contract. Not a warning."""


# ------------------------------------------------------------------ helpers

def sha_and_size(p: pathlib.Path) -> tuple[str, int]:
    """Hash and size, both from the bytes actually read. Never stat()."""
    h = hashlib.sha256()
    n = 0
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    """Publish a small sidecar without exposing a partly written identity file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _safe_relative_name(rel: str) -> bool:
    """Portable, extraction-safe relative member name."""
    if not rel or "\\" in rel or "\x00" in rel:
        return False
    path = pathlib.PurePosixPath(rel)
    return not path.is_absolute() and all(p not in ("", ".", "..") for p in path.parts)


SEMANTIC_TABLES = (
    "entities", "assets", "asset_entity_map", "ownership", "dockets",
    "asset_dockets", "filings", "filing_entities", "filing_dockets", "documents", "source_facts",
    "source_contexts", "source_dimensions", "source_units", "source_manifest",
    "observations", "observation_versions", "lineage_populations", "lineage_edges",
    "document_facts", "events", "coverage_expected", "coverage_measured",
    "field_status", "requirements_crosswalk", "applicability", "taxonomy_sources",
    "blockers", "reviewed_source_annotations")


def database_semantic_identity(db: pathlib.Path) -> str:
    """Recompute the same consumer/provenance identity published by Build A."""
    import sqlite3

    uri = f"file:{db}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReleaseRefused("Build-A database failed PRAGMA quick_check")
        fk = con.execute("PRAGMA foreign_key_check").fetchmany(5)
        if fk:
            raise ReleaseRefused(f"Build-A database has foreign-key violations: {fk}")
        h = hashlib.sha256()
        for table in SEMANTIC_TABLES:
            info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
            cols = [row[1] for row in info]
            if not cols:
                raise ReleaseRefused(f"Build-A semantic table absent: {table}")
            pk = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            order = pk or cols
            h.update((table + "\0" + "\0".join(cols) + "\n").encode("utf-8"))
            select = ",".join(f'"{c}"' for c in cols)
            ordering = ",".join(f'"{c}"' for c in order)
            for row in con.execute(f'SELECT {select} FROM "{table}" ORDER BY {ordering}'):
                h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                                    default=str).encode("utf-8") + b"\n")
        return h.hexdigest()
    finally:
        con.close()


def verify_build_a_boundary(root: pathlib.Path, *, required: bool) -> dict:
    """Prove the database and legacy export paths belong to one published Build A.

    The content-addressed export generation is the immutable source of truth;
    compatibility files under ``exports/`` must match it byte-for-byte.  The
    database must carry the same generation/snapshot identities and its current
    semantic content must still equal the identity published by Build A.
    """
    import sqlite3

    receipt_path = root / PUBLICATION_RECEIPT_NAME
    db = root / "staging" / "operating_assets.sqlite"
    if not receipt_path.is_file():
        if required:
            raise ReleaseRefused(
                f"{PUBLICATION_RECEIPT_NAME} absent: full release requires the Build-A "
                "database/export publication boundary")
        return {"status": "not_available", "required": False,
                "reason": "lite compatibility fixture has no Build-A receipt"}
    if not db.is_file():
        raise ReleaseRefused("Build-A publication receipt exists but staging database is absent")
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    if wal.exists():
        with wal.open("rb") as fh:
            if fh.read(1):
                raise ReleaseRefused(
                    f"SQLite WAL is non-empty at package freeze: {wal.name}; checkpoint a "
                    "consistent Build-A database before packaging")

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    generation_id = str(receipt.get("generation_id", ""))
    generation_path = str(receipt.get("generation_path", ""))
    if not generation_id or not _safe_relative_name(generation_path):
        raise ReleaseRefused("Build-A publication receipt has invalid generation identity/path")
    generation_root = root / generation_path
    if not generation_root.is_dir() or generation_root.resolve().is_relative_to(root.resolve()) is False:
        raise ReleaseRefused("Build-A generation directory is absent or escapes the release root")

    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseRefused("Build-A publication receipt has no files")
    required_exports = {
        spec.path[len("exports/"):]
        for spec in REQUIRED_OUTPUTS if spec.path.startswith("exports/")}
    absent_required = sorted(required_exports - set(files))
    if absent_required:
        raise ReleaseRefused(
            f"Build-A generation omits required export(s): {absent_required[:10]}")
    identity_payload = {"kind": receipt.get("kind"), "files": files,
                        "metadata": receipt.get("metadata") or {}}
    recomputed_generation = hashlib.sha256(json.dumps(
        identity_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if recomputed_generation != generation_id:
        raise ReleaseRefused("Build-A generation_id does not match its files and metadata")
    mismatches = []
    for logical, expected in sorted(files.items()):
        if not _safe_relative_name(logical):
            raise ReleaseRefused(f"unsafe Build-A export path: {logical!r}")
        immutable = generation_root / logical
        compatible = root / "exports" / logical
        for label, path in (("generation", immutable), ("compatibility", compatible)):
            if path.is_symlink() or not path.is_file():
                mismatches.append(f"{label} export absent/symlink: {logical}")
                continue
            digest, size = sha_and_size(path)
            if digest != expected.get("sha256") or size != expected.get("bytes"):
                mismatches.append(f"{label} export identity mismatch: {logical}")
    if mismatches:
        raise ReleaseRefused("; ".join(mismatches[:10]))

    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM publication_generations "
            "WHERE generation_id=? AND status='published'", (generation_id,)).fetchall()
    except sqlite3.Error as exc:
        raise ReleaseRefused(f"Build-A publication database record unreadable: {exc}") from exc
    finally:
        con.close()
    if len(rows) != 1:
        raise ReleaseRefused(
            f"Build-A generation {generation_id} has {len(rows)} published database rows")
    row = dict(rows[0])
    metadata = receipt.get("metadata") or {}
    try:
        stored_manifest = json.loads(row.get("manifest_json") or "")
    except json.JSONDecodeError as exc:
        raise ReleaseRefused("Build-A database manifest_json is invalid") from exc
    if stored_manifest != receipt:
        raise ReleaseRefused("Build-A receipt differs from database manifest_json")
    for key in ("code_snapshot", "input_snapshot", "database_identity"):
        if not metadata.get(key) or row.get(key) != metadata.get(key):
            raise ReleaseRefused(f"Build-A {key} differs between receipt and database")
    actual_semantic = database_semantic_identity(db)
    if actual_semantic != metadata["database_identity"]:
        raise ReleaseRefused(
            "Build-A database semantic state changed after the exports were published")
    return {"status": "verified", "required": required,
            "generation_id": generation_id, "generation_path": generation_path,
            "files": len(files), "database_identity": actual_semantic,
            "code_snapshot": metadata["code_snapshot"],
            "input_snapshot": metadata["input_snapshot"],
            "wal_bytes": 0,
            "shared_memory_sidecar_present": shm.exists()}


def verify_final_records_boundary(root: pathlib.Path, *, required: bool) -> dict:
    """Verify the final-record receipt, immutable generation and aliases.

    The archive manifest inventories the bytes that happen to be selected.  It
    must not be allowed to launder a final record that was edited after its own
    publication receipt, so this boundary is checked before payload hashing.
    """
    records_root = root / "final_records"
    receipt_path = records_root / FINAL_RECORD_RECEIPT
    if not receipt_path.is_file():
        if required:
            raise ReleaseRefused("full release requires the final-record receipt")
        return {"status": "not_available", "required": False}
    if receipt_path.is_symlink():
        raise ReleaseRefused("final-record receipt may not be a symlink")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "ferc-final-records-receipt-v1" \
            or receipt.get("status") != "published" \
            or receipt.get("kind") != "final_implementation_records":
        raise ReleaseRefused("final-record receipt has the wrong schema/status/kind")
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != set(FINAL_RECORD_FILES):
        actual = set(files) if isinstance(files, dict) else set()
        raise ReleaseRefused(
            "final-record receipt population differs from the fixed contract: "
            f"missing={sorted(set(FINAL_RECORD_FILES)-actual)}, "
            f"extra={sorted(actual-set(FINAL_RECORD_FILES))}")
    identity_payload = {"kind": "final_implementation_records", "files": files}
    recomputed = hashlib.sha256(json.dumps(
        identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    generation_id = str(receipt.get("generation_id") or "")
    generation_path = str(receipt.get("generation_path") or "")
    if recomputed != generation_id:
        raise ReleaseRefused("final-record generation_id does not match its file identities")
    expected_path = pathlib.PurePosixPath(".final_records") / generation_id
    if pathlib.PurePosixPath(generation_path) != expected_path:
        raise ReleaseRefused("final-record receipt generation path is not content-addressed")
    generation_root = records_root / generation_path
    if generation_root.is_symlink() or not generation_root.is_dir() \
            or not generation_root.resolve().is_relative_to(records_root.resolve()):
        raise ReleaseRefused("final-record immutable generation is absent or unsafe")
    mismatches = []
    for name, expected in sorted(files.items()):
        if not _safe_relative_name(name):
            raise ReleaseRefused(f"unsafe final-record name: {name!r}")
        for label, path in (("generation", generation_root / name),
                            ("compatibility", records_root / name)):
            if path.is_symlink() or not path.is_file():
                mismatches.append(f"{label} final record absent/symlink: {name}")
                continue
            digest, size = sha_and_size(path)
            if digest != expected.get("sha256") or size != expected.get("bytes"):
                mismatches.append(f"{label} final record identity mismatch: {name}")
    if mismatches:
        raise ReleaseRefused("; ".join(mismatches[:10]))
    return {"status": "verified", "required": required,
            "generation_id": generation_id, "generation_path": generation_path,
            "files": len(files)}


def classify(rel: str) -> str:
    if rel.startswith("source_cache/"):
        return "source_cache"
    if rel.startswith("staging/"):
        return "staging_database"
    if rel.startswith("discovery/") or rel.startswith("evidence/"):
        return "frozen_evidence_input"
    if rel.startswith("acceptance/"):
        return "acceptance"
    if rel.startswith("exports/") or rel.startswith("config/") or rel.startswith("verification/"):
        return "generated_data"
    if rel.endswith((".py", ".sql")):
        return "engine_script"
    return "generated_report"


def validate_redundant_aliases(root: pathlib.Path) -> list[dict]:
    """Validate exact duplicate evidence paths that are shipped only once.

    A declaration is activated when either named path exists.  This keeps the
    generic builder usable for synthetic fixtures, while making a half-present,
    changed or unindexed alias a hard failure for the real candidate.
    """
    resolved = []
    for declaration in REDUNDANT_PAYLOAD_ALIASES:
        omitted = root / declaration["omitted_path"]
        canonical = root / declaration["canonical_path"]
        if not omitted.exists() and not canonical.exists():
            continue
        for label, path in (("omitted", omitted), ("canonical", canonical)):
            if path.is_symlink() or not path.is_file():
                raise ReleaseRefused(
                    f"declared redundant {label} path absent or symlink: {path}")
        omitted_sha, omitted_size = sha_and_size(omitted)
        canonical_sha, canonical_size = sha_and_size(canonical)
        expected = (declaration["sha256"], declaration["bytes"])
        if ((omitted_sha, omitted_size) != expected
                or (canonical_sha, canonical_size) != expected):
            raise ReleaseRefused(
                "declared redundant evidence paths are not both the exact pinned bytes: "
                f"{declaration['omitted_path']}")
        provenance = []
        for rel in declaration["provenance_paths"]:
            path = root / rel
            if path.is_symlink() or not path.is_file():
                raise ReleaseRefused(
                    f"redundant-alias provenance absent or symlink: {rel}")
            digest, size = sha_and_size(path)
            provenance.append({"path": rel, "sha256": digest, "bytes": size})
        index_path = root / "source_cache" / "index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseRefused(
                f"cannot validate redundant alias against source-cache index: {exc}") \
                from None
        matches = [entry for entry in index.values() if isinstance(entry, dict)
                   and entry.get("content_hash") == declaration["sha256"]
                   and entry.get("cache_path") == declaration["canonical_path"].removeprefix(
                       "source_cache/")
                   and entry.get("byte_size") == declaration["bytes"]]
        if not matches:
            raise ReleaseRefused(
                "canonical redundant-alias object is not declared by source_cache/index.json")
        resolved.append({
            **declaration,
            "status": "verified_exact_alias_omitted",
            "canonical_packaged": True,
            "source_cache_index_matches": len(matches),
            "provenance": provenance,
        })
    return resolved


def source_cache_contract(root: pathlib.Path, *, include_identities: bool = False) -> tuple:
    """Resolve and optionally retain read-derived cache-object identities.

    The manifest stage needs both the cache contract and each object's digest/
    byte count.  Returning identities from the contract read lets that stage
    reuse the same verified stream instead of immediately reading every large
    object a second time.  The deliberate post-archive stability pass remains
    separate and re-reads the objects to detect mutation during packaging.
    """
    index_path = root / "source_cache" / "index.json"
    objects = root / "source_cache" / "objects"
    if not index_path.exists() and not objects.exists():
        absent = ({"status": "not_present"}, None)
        return (*absent, {}) if include_identities else absent
    if index_path.is_symlink() or not index_path.is_file() or not objects.is_dir():
        raise ReleaseRefused("source cache index/objects boundary is incomplete or aliased")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRefused(f"source cache index is unreadable: {exc}") from None
    if not isinstance(index, dict) or not index:
        raise ReleaseRefused("source cache index is empty or has the wrong schema")
    identities: dict[str, int] = {}
    for key, entry in index.items():
        if (not isinstance(key, str) or len(key) != 64
                or not isinstance(entry, dict)):
            raise ReleaseRefused("source cache index has an invalid URL-key record")
        digest = str(entry.get("content_hash", ""))
        expected_rel = f"objects/{digest[:2]}/{digest}"
        try:
            size = int(entry.get("byte_size", -1))
        except (TypeError, ValueError):
            size = -1
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or entry.get("cache_path") != expected_rel or size < 0):
            raise ReleaseRefused(f"source cache index record is non-canonical: {key}")
        if digest in identities and identities[digest] != size:
            raise ReleaseRefused(f"source cache object has conflicting sizes: {digest}")
        identities[digest] = size
    referenced = {f"source_cache/objects/{digest[:2]}/{digest}"
                  for digest in identities}
    missing = []
    verified_entries: dict[str, dict] = {}
    for digest, size in identities.items():
        path = objects / digest[:2] / digest
        rel = f"source_cache/objects/{digest[:2]}/{digest}"
        if path.is_symlink() or not path.is_file():
            missing.append(digest)
            continue
        # File Provider metadata can be stale until bytes are hydrated.  The
        # cache contract is an identity contract, so verify both facts from the
        # same byte stream instead of trusting stat metadata for one of them.
        actual_digest, actual_size = sha_and_size(path)
        if actual_size != size or actual_digest != digest:
            missing.append(digest)
            continue
        verified_entries[rel] = {
            "sha256": actual_digest, "bytes": actual_size,
            "class": classify(rel),
        }
    if missing:
        raise ReleaseRefused(
            f"source cache has {len(missing)} missing/aliased/wrong-size indexed objects: "
            f"{missing[:5]}")
    existing = {p.relative_to(root).as_posix() for p in objects.glob("*/*")
                if p.is_file() or p.is_symlink()}
    unreferenced = sorted(existing - referenced)
    result = ({
        "status": "index_is_payload_contract",
        "url_entries": len(index),
        "unique_objects": len(identities),
        "unique_bytes": sum(identities.values()),
        "unreferenced_objects_excluded": len(unreferenced),
        "unreferenced_paths": unreferenced,
    }, referenced)
    return (*result, verified_entries) if include_identities else result


def payload_paths(root: pathlib.Path, *, full: bool,
                  referenced_cache_objects: set[str] | None = None
                  ) -> list[tuple[pathlib.Path, str]]:
    out = []
    portable_names: dict[str, str] = {}
    omitted_aliases = {item["omitted_path"] for item in REDUNDANT_PAYLOAD_ALIASES}
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        srel = rel.as_posix()
        if any(p in EXCLUDE_DIRS for p in rel.parts) or f.suffix in EXCLUDE_SUFFIX \
           or f.name in EXCLUDE_NAMES or srel in EXCLUDE_RELATIVE:
            continue
        if srel.startswith(EXCLUDE_PREFIXES):
            continue
        if (srel.startswith("implementation_logs/")
                and not srel.startswith(IMPLEMENTATION_LOG_INCLUDE_PREFIXES)):
            continue
        if srel in omitted_aliases:
            continue
        if (referenced_cache_objects is not None
                and srel.startswith("source_cache/objects/")
                and srel not in referenced_cache_objects):
            continue
        if not full and srel.startswith(LITE_EXCLUDE_PREFIXES):
            continue
        if f.is_symlink():
            raise ReleaseRefused(f"payload contains a symlink: {srel}")
        if f.is_dir():
            continue
        if not f.is_file():
            raise ReleaseRefused(f"payload contains a special filesystem node: {srel}")
        if not _safe_relative_name(srel):
            raise ReleaseRefused(f"payload contains an unsafe/nonportable path: {srel!r}")
        folded = srel.casefold()
        if folded in portable_names and portable_names[folded] != srel:
            raise ReleaseRefused(
                f"payload has case-colliding paths: {portable_names[folded]!r}, {srel!r}")
        portable_names[folded] = srel
        out.append((f, srel))
    return out


def payload_digest(entries: dict[str, dict]) -> str:
    """One digest over the whole frozen payload.

    Any later change to any payload file changes this, so a receipt can state
    which payload it certifies without re-listing it, and a post-manifest edit
    cannot pass unnoticed.
    """
    h = hashlib.sha256()
    for rel in sorted(entries):
        h.update(f"{rel}\0{entries[rel]['sha256']}\0{entries[rel]['bytes']}\n".encode())
    return h.hexdigest()


def table_counts(root: pathlib.Path) -> dict[str, int]:
    """Row counts backing the contract's NON_EMPTY_IF_TABLE rule.

    Returned empty when the database is absent, so that rule reports as
    unenforced rather than silently passing.
    """
    import sqlite3
    db = root / "staging" / "operating_assets.sqlite"
    if not db.is_file():
        return {}
    counts = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        con.close()
    except sqlite3.Error:
        return {}
    return counts


# ------------------------------------------------------------------ stages

def stage1_freeze(root: pathlib.Path) -> dict:
    """Every declared output must exist and be substantive. HARD GATE.

    This is A13. A missing required output fails the release here, before a
    manifest is written -- it can never be redefined out of existence by a
    manifest that simply omits it.
    """
    counts = table_counts(root)
    violations = check_required_outputs(root, counts or None)
    return {"table_counts_available": bool(counts),
            "required_outputs_declared": len(REQUIRED_OUTPUTS),
            "violations": violations}


def full_payload_contract(root: pathlib.Path) -> list[dict]:
    """Fixed non-export contract for an offline-reproducible full payload."""
    violations = []
    for rel in FULL_MANDATORY_ARTIFACTS:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            violations.append({"path": rel, "kind": "MISSING_FULL_PAYLOAD_ARTIFACT",
                               "severity": "blocking",
                               "detail": "fixed full-payload artifact absent or a symlink"})
            continue
        try:
            with path.open("rb") as fh:
                if not fh.read(1):
                    violations.append({"path": rel, "kind": "EMPTY_FULL_PAYLOAD_ARTIFACT",
                                       "severity": "blocking",
                                       "detail": "fixed full-payload artifact is empty"})
        except OSError as exc:
            violations.append({"path": rel, "kind": "UNREADABLE_FULL_PAYLOAD_ARTIFACT",
                               "severity": "blocking",
                               "detail": f"{type(exc).__name__}: {exc}"})
    return violations


def stage2_hash(root: pathlib.Path, *, full: bool) -> dict:
    aliases = validate_redundant_aliases(root)
    if full:
        cache_contract, cache_paths, cache_entries = source_cache_contract(
            root, include_identities=True)
    else:
        cache_contract, cache_paths, cache_entries = (
            {"status": "excluded_from_lite"}, None, {})
    entries: dict[str, dict] = {}
    unreadable = []
    for f, rel in payload_paths(
            root, full=full, referenced_cache_objects=cache_paths):
        if rel in cache_entries:
            entries[rel] = cache_entries[rel]
            continue
        try:
            digest, size = sha_and_size(f)
        except OSError as exc:
            # An unreadable file is never an unchanged one. It is recorded and
            # the release fails; it is never written as though verified.
            unreadable.append({"path": rel, "error": type(exc).__name__})
            continue
        entries[rel] = {"sha256": digest, "bytes": size, "class": classify(rel)}
    return {"entries": entries, "unreadable": unreadable,
            "redundant_aliases": aliases,
            "source_cache_contract": cache_contract,
            "referenced_cache_objects": cache_paths}


def build_manifest(entries: dict, *, full: bool, extra: dict) -> dict:
    manifest = {
        "package": NAME,
        "payload": "full" if full else "lite",
        "identity_is_content_derived": True,
        "release_stages": [{"stage": s, "means": m} for s, m in RELEASE_STAGES],
        "artifact_classes": CLASSES,
        "hash_provenance": ("every sha256 and byte count in this manifest was computed "
                            "from the bytes read from the file. stat() is not used: on "
                            "the build volume it returns a stale placeholder size for a "
                            "cloud-evicted file until the bytes are read."),
        "never_manifested": {
            "files": sorted({*WRITTEN_AFTER_MANIFEST, manifest_for(full),
                             receipt_for(full), LEGACY_RECEIPT_NAME}),
            "why": ("written after this manifest; an entry for one of them would be "
                    "stale the moment it landed. This is audit A21."),
        },
        "lite_exclusions": ([] if full else {
            "prefixes": list(LITE_EXCLUDE_PREFIXES),
            "why": ("the lite package deliberately omits the source cache and staging "
                    "database and is NOT claimed to be standalone; build with "
                    "`--payload full` for the complete deliverable")}),
        "declared_exclusions": {"dirs": sorted(EXCLUDE_DIRS),
                                "relative_paths": sorted(EXCLUDE_RELATIVE),
                                "prefixes": list(EXCLUDE_PREFIXES),
                                "implementation_log_includes":
                                    list(IMPLEMENTATION_LOG_INCLUDE_PREFIXES),
                                "suffixes": sorted(EXCLUDE_SUFFIX),
                                "names": sorted(EXCLUDE_NAMES)},
        "file_count": len(entries),
        "payload_digest": payload_digest(entries),
        "files": entries,
    }
    manifest.update(extra)
    return manifest


def _zip_info(name: str, *, compressed: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o444) << 16
    info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    return info


def _write_verified_member(z: zipfile.ZipFile, root: pathlib.Path, rel: str,
                           expected: dict, *, compressed: bool) -> None:
    """Stream one source file once, hashing the exact bytes sent to the ZIP."""
    source = root / rel
    if source.is_symlink() or not source.is_file():
        raise ReleaseRefused(f"payload changed before archive write: {rel}")
    h, size = hashlib.sha256(), 0
    with source.open("rb") as src, z.open(
            _zip_info(f"{NAME}/{rel}", compressed=compressed), "w") as dst:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
            dst.write(chunk)
    if h.hexdigest() != expected["sha256"] or size != expected["bytes"]:
        raise ReleaseRefused(
            f"payload changed between hash and archive stages: {rel}")


def stage3_archive(root: pathlib.Path, entries: dict, manifest: dict,
                   out_zip: pathlib.Path, *, full: bool) -> dict:
    """Construct beside the destination, then atomically replace last-good."""
    stored = {".zip", ".gz", ".png", ".jpg", ".jpeg", ".pdf"}
    written = 0
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.with_name(f".{out_zip.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
                             allowZip64=True) as z:
            for rel in sorted(entries):
                _write_verified_member(
                    z, root, rel, entries[rel],
                    compressed=pathlib.PurePosixPath(rel).suffix.lower() not in stored)
                written += 1
            body = _json_bytes(manifest)
            z.writestr(_zip_info(f"{NAME}/{MANIFEST_NAME}", compressed=True), body)
            written += 1
        os.replace(tmp, out_zip)
    finally:
        if tmp.exists():
            tmp.unlink()
    # The RECEIPT is deliberately NOT written into the archive: a receipt that
    # ships inside the archive it certifies can never state that archive's hash.
    return {"members_written": written, "receipt_inside": False}


def inspect_archive(out_zip: pathlib.Path, manifest: dict, *, verify_crc: bool = True) -> dict:
    """Check CRC, exact membership and extraction safety before unpacking."""
    expected = {f"{NAME}/{rel}" for rel in manifest["files"]}
    expected.add(f"{NAME}/{MANIFEST_NAME}")
    result = {"duplicates": [], "case_collisions": [], "unsafe_members": [], "symlinks": [],
              "special_members": [], "encrypted_members": [], "unexpected": [],
              "missing": [], "crc_failure": None, "members": 0,
              "embedded_manifest_matches": False, "embedded_manifest_error": None}
    with zipfile.ZipFile(out_zip) as z:
        infos = z.infolist()
        names = [info.filename for info in infos]
        result["members"] = len(names)
        seen: set[str] = set()
        folded_seen: dict[str, str] = {}
        for info in infos:
            name = info.filename
            if name in seen:
                result["duplicates"].append(name)
            seen.add(name)
            folded = name.casefold()
            if folded in folded_seen and folded_seen[folded] != name:
                result["case_collisions"].append([folded_seen[folded], name])
            folded_seen[folded] = name
            if not name.startswith(f"{NAME}/"):
                result["unsafe_members"].append(name)
                continue
            rel = name[len(NAME) + 1:]
            if not _safe_relative_name(rel) or info.is_dir():
                result["unsafe_members"].append(name)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                result["symlinks"].append(name)
            elif mode not in (0, stat.S_IFREG):
                result["special_members"].append(name)
            if info.flag_bits & 0x1:
                result["encrypted_members"].append(name)
        result["unexpected"] = sorted(set(names) - expected)
        result["missing"] = sorted(expected - set(names))
        # Stream verification below reads each member once and exercises the
        # ZipExtFile CRC check while also hashing it.  The legacy extracted path
        # may still request this independent CRC-only pass.
        result["crc_failure"] = z.testzip() if verify_crc else None
        try:
            embedded = json.loads(z.read(f"{NAME}/{MANIFEST_NAME}"))
            result["embedded_manifest_matches"] = embedded == manifest
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            result["embedded_manifest_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _archive_safety_failures(check: dict) -> list[str]:
    failures = []
    for key in ("duplicates", "case_collisions", "unsafe_members", "symlinks", "special_members",
                "encrypted_members", "unexpected", "missing"):
        if check.get(key):
            failures.append(f"{key}: {check[key][:5]}")
    if check.get("crc_failure"):
        failures.append(f"crc_failure: {check['crc_failure']}")
    if check.get("embedded_manifest_matches") is not True:
        detail = check.get("embedded_manifest_error")
        failures.append("embedded manifest differs from the manifest used to build"
                        + (f": {detail}" if detail else ""))
    return failures


def stream_verify_archive(out_zip: pathlib.Path, manifest: dict, *, full: bool) -> dict:
    """Verify every archive member from its stream without extracting a second tree.

    This is the pre-Build-B gate used on space-constrained hosts.  It is not a
    substitute for Build B: it proves package identity, CRC and safety and then
    deliberately reports the clean rebuild/acceptance state as pending.
    """
    result = {
        "mode": "stream_pending_build_b", "verified_files": 0,
        "hash_mismatches": [], "missing_from_archive": [],
        "payload_digest_matches": False, "manifest_cycle": [],
        "archive_safety": {}, "build_b_status": "pending",
    }
    try:
        safety = inspect_archive(out_zip, manifest, verify_crc=False)
    except (OSError, zipfile.BadZipFile) as exc:
        result["hash_mismatches"].append(
            f"archive cannot be inspected: {type(exc).__name__}: {exc}")
        return result
    result["archive_safety"] = safety
    failures = _archive_safety_failures(safety)
    if failures:
        result["hash_mismatches"].extend(failures)
        return result

    recomputed = {}
    with zipfile.ZipFile(out_zip) as z:
        members = {info.filename for info in z.infolist()}
        cycle = manifest_cycle_violations(
            manifest, archive_members={m[len(NAME) + 1:] for m in members})
        if full:
            result["mutable_rule_set_aside"] = [
                dict(v, disposition=(
                    "the frozen Build-A database is a content-hashed payload member; "
                    "Build B will compare its semantic identity after clean extraction"))
                for v in cycle if v["kind"] == "MUTABLE_AFTER_MANIFEST"]
            cycle = [v for v in cycle if v["kind"] != "MUTABLE_AFTER_MANIFEST"]
        result["manifest_cycle"] = cycle
        for rel, expected in sorted(manifest["files"].items()):
            member = f"{NAME}/{rel}"
            try:
                info = z.getinfo(member)
            except KeyError:
                result["missing_from_archive"].append(rel)
                continue
            h = hashlib.sha256()
            size = 0
            try:
                with z.open(info) as src:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        size += len(chunk)
            except (OSError, EOFError, zipfile.BadZipFile) as exc:
                result["hash_mismatches"].append(
                    f"{rel}: stream/CRC failure: {type(exc).__name__}: {exc}")
                continue
            actual = {"sha256": h.hexdigest(), "bytes": size,
                      "class": expected["class"]}
            recomputed[rel] = actual
            if (actual["sha256"] != expected["sha256"]
                    or actual["bytes"] != expected["bytes"]):
                result["hash_mismatches"].append(
                    f"{rel}: manifest {expected['sha256'][:12]}/{expected['bytes']}b, "
                    f"stream {actual['sha256'][:12]}/{actual['bytes']}b")
            else:
                result["verified_files"] += 1
    result["payload_digest_matches"] = (
        not result["missing_from_archive"]
        and payload_digest(recomputed) == manifest["payload_digest"])
    return result


def load_embedded_manifest(archive: pathlib.Path) -> dict:
    try:
        with zipfile.ZipFile(archive) as z:
            raw = z.read(f"{NAME}/{MANIFEST_NAME}")
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError,
            zipfile.BadZipFile) as exc:
        raise ReleaseRefused(f"cannot load embedded release manifest: {exc}") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ReleaseRefused("embedded release manifest has the wrong schema")
    return manifest


def _offline_commands(pkg: pathlib.Path, *, full: bool) -> tuple[list[list[str]], str]:
    if full:
        plan = json.loads((pkg / "config" / "run_plan.json").read_text(encoding="utf-8"))
        as_of = str(plan.get("as_of") or "")
        return ([[sys.executable, "-I", "-S", "run.py", "plan", "--check"],
                 [sys.executable, "-I", "-S", "run.py", "validate", "--offline",
                  "--as-of", as_of]],
                "full extracted plan preflight plus run.py validate")
    script = (
        "import sys; sys.path.insert(0,'.');\n"
        "import ferclib.periods as p, ferclib.registry as r;\n"
        "ok,_=p.satisfies('quarter','2025-04-01','2025-06-30','',"
        "'ytd','2025-01-01','2025-06-30','');\n"
        "assert not ok, 'a YTD fact satisfied a discrete quarter';\n"
        "ok2,_=p.satisfies('quarter','2025-01-01','2025-03-31','',"
        "'ytd','2025-01-01','2025-03-31','');\n"
        "assert ok2, 'the Q1 equivalence was rejected';\n"
        "assert not r.check_unit_contract(), r.check_unit_contract();\n"
        "assert not r.units_compatible('percent','xbrli:pure');\n"
        "assert not r.units_compatible('utr:dth','MMcf');\n"
        "print('EXTRACTED_LITE_TREE_OK', len(r.REGISTRY))")
    return ([[sys.executable, "-I", "-S", "-c", script]],
            "lite extracted import/semantic smoke")


def run_extracted_offline_suite(pkg: pathlib.Path, *, full: bool,
                                temp_root: pathlib.Path) -> dict:
    """Run only extracted code/data with network and ambient Python paths denied."""
    commands, scope = _offline_commands(pkg, full=full)
    home = temp_root / "home"
    tmp = temp_root / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home), "TMPDIR": str(tmp),
        "PYTHONNOUSERSITE": "1", "PYTHONPATH": "", "PYTHONSAFEPATH": "1",
        "FERC_OFFLINE": "1", "FERC_STAGING_DB": str(pkg / "staging" / "operating_assets.sqlite"),
        "FERC_OUTPUT_DIR": str(pkg), "FERC_SOURCE_CACHE": str(pkg / "source_cache"),
        "FERC_UNIVERSE": str(pkg / "config" / "universe.csv"),
        "FERC_ANNOTATIONS_DIR": str(pkg / "config" / "annotations"),
        "FERC_RUN_PLAN": str(pkg / "config" / "run_plan.json"),
        "FERC_TAXONOMY_PINS": str(pkg / "config" / "taxonomy_pins.json"),
    }
    steps = []
    final_rc = 0
    for command in commands:
        proc = subprocess.run(command, cwd=str(pkg), env=env, capture_output=True,
                              text=True, timeout=900, check=False)
        steps.append({"argv": command, "returncode": proc.returncode,
                      "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]})
        final_rc = proc.returncode
        if proc.returncode != 0:
            break
    return {"scope": scope, "steps": steps, "cwd": str(pkg),
            "offline_enforced": True,
            "isolated_python": all("-I -S" in " ".join(c) for c in commands),
            "returncode": final_rc,
            "stdout": steps[-1]["stdout"] if steps else "",
            "stderr": steps[-1]["stderr"] if steps else ""}


def verify_extracted(out_zip: pathlib.Path, manifest: dict, *,
                     full: bool) -> dict:
    """Unpack the candidate and verify the EXTRACTED tree, not just ZIP hashes.

    A13's second half: the old clean room hashed files that were already sitting
    in the source tree and printed OFFLINE_OK without rebuilding anything. Here
    the archive is extracted to an empty directory and everything is checked
    against the extracted bytes -- including the required-output contract, which
    must hold for the thing a recipient actually receives.
    """
    res = {"extracted": 0, "hash_mismatches": [], "missing_from_archive": [],
           "contract_violations": [], "payload_digest_matches": None,
           "manifest_cycle": [], "offline_suite": {"returncode": None},
           "archive_safety": {}}
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        safety = inspect_archive(out_zip, manifest)
        res["archive_safety"] = safety
        safety_failures = _archive_safety_failures(safety)
        if safety_failures:
            res["hash_mismatches"].extend(safety_failures)
            return res
        with zipfile.ZipFile(out_zip) as z:
            members = set(z.namelist())
            for info in z.infolist():
                rel = info.filename[len(NAME) + 1:]
                target = root / NAME / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk)
        pkg = root / NAME

        cyc = manifest_cycle_violations(
            manifest, archive_members={m[len(NAME) + 1:] for m in members}, tree=pkg)
        if full:
            # `MUTABLE_AFTER_MANIFEST` on staging/ is correct for a LITE payload,
            # where the database is excluded and an entry for it would be stale by
            # construction -- one of A21's three stale entries.
            #
            # For a FULL payload the database IS the payload: hashed from its
            # bytes at stage 2 like everything else, frozen before the manifest is
            # written, and shipped inside the archive the manifest describes.
            # Refusing to manifest it would mean shipping 774 MB with no recorded
            # identity, the opposite of what A21 asks. Kept for lite, set aside
            # here with the reason recorded in the receipt rather than dropped.
            res["mutable_rule_set_aside"] = [
                dict(v, disposition=(
                    "not applicable to the candidate: every staging file was content-"
                    "hashed, streamed with the same hash, and is checked again after "
                    "extraction; the database additionally matches Build A"))
                for v in cyc if v["kind"] == "MUTABLE_AFTER_MANIFEST"]
            cyc = [v for v in cyc if v["kind"] != "MUTABLE_AFTER_MANIFEST"]
        res["manifest_cycle"] = cyc

        recomputed: dict[str, dict] = {}
        for rel, meta in manifest["files"].items():
            f = pkg / rel
            if not f.is_file():
                res["missing_from_archive"].append(rel)
                continue
            digest, size = sha_and_size(f)
            recomputed[rel] = {"sha256": digest, "bytes": size, "class": meta["class"]}
            if digest != meta["sha256"] or size != meta["bytes"]:
                res["hash_mismatches"].append(
                    f"{rel}: manifest {meta['sha256'][:12]}/{meta['bytes']}b, "
                    f"extracted {digest[:12]}/{size}b")
            else:
                res["extracted"] += 1

        res["payload_digest_matches"] = (
            payload_digest(recomputed) == manifest["payload_digest"]
            if not res["missing_from_archive"] else False)

        # The contract must hold for the EXTRACTED candidate.
        #
        # One rule cannot be re-checked from a lite archive: NON_EMPTY_IF_TABLE
        # compares an export against the row count of the table behind it, and
        # the lite payload excludes the database BY DECLARATION. The contract
        # correctly reports that as unenforced rather than assuming it passed.
        #
        # Treating an unenforceable-by-design rule as a hard failure would make
        # the lite package impossible to build; silently dropping it would be
        # exactly the laundering the contract exists to prevent. So it is
        # SEPARATED, not ignored: the substantive check already ran at stage 1
        # against the real database, and every output whose rule cannot be
        # re-verified from the archive alone is named in the receipt.
        #
        # The loophole is closed at stage 1: if the database was absent THERE
        # too, the rule was never enforced at all and the release fails.
        # The blocking/unverified split lives in the contract, not here, so the
        # builder and the acceptance suite share one rule instead of each
        # interpreting it (w6-acceptance R2).
        all_v = check_required_outputs(pkg, table_counts(pkg) or None)
        res["unverified_from_this_payload"] = [v["path"] for v in unverified(all_v)]
        # For a full payload the database ships, so nothing is excused: an
        # unverified rule there means the check genuinely could not run and must
        # stop the release.
        res["contract_violations"] = all_v if full else blocking(all_v)

        try:
            res["final_records_boundary"] = verify_final_records_boundary(
                pkg, required=full)
        except (OSError, ValueError, KeyError, json.JSONDecodeError,
                ReleaseRefused) as exc:
            res["contract_violations"].append({
                "path": "final_records/", "kind": "FINAL_RECORD_BOUNDARY_INVALID",
                "severity": "blocking",
                "detail": f"{type(exc).__name__}: {exc}",
            })

        res["offline_suite"] = run_extracted_offline_suite(
            pkg, full=full, temp_root=root)
    return res


FINAL_BUILD_B_CHECKS = (
    "build_b_from_empty",
    "offline_network_denied",
    "live_tree_access_denied",
    "replay_pass",
    "acceptance_pass",
    "build_a_build_b_semantic_match",
    "build_a_packaged_semantic_match",
    "required_exports_match",
    "annotations_match",
)


def finalize_build_b_receipt(archive: pathlib.Path, pending_path: pathlib.Path,
                             evidence_path: pathlib.Path,
                             receipt_path: pathlib.Path) -> dict:
    """Issue the external PASS receipt only after clean Build B evidence agrees.

    The archive is rehashed and stream-verified here.  Build-B evidence must be
    independently materialized by the clean extracted run and bind the exact
    Build-A, Build-B and packaged semantic identities.  A missing boolean is a
    failure; truthy strings are not accepted.
    """
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        evidence_raw = evidence_path.read_bytes()
        evidence = json.loads(evidence_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRefused(f"cannot read pending/Build-B evidence: {exc}") from None
    if pending.get("status") != "CANDIDATE_PENDING_BUILD_B":
        raise ReleaseRefused("pending sidecar does not name a Build-B-pending candidate")
    if evidence.get("schema") != "ferc-build-b-acceptance-v1" \
            or evidence.get("status") != "PASS":
        raise ReleaseRefused("Build-B evidence is not a PASS ferc-build-b-acceptance-v1 record")
    checks = evidence.get("checks") or {}
    failed_checks = [name for name in FINAL_BUILD_B_CHECKS if checks.get(name) is not True]
    if failed_checks:
        raise ReleaseRefused(f"Build-B evidence has failed/missing checks: {failed_checks}")

    archive_sha, archive_size = sha_and_size(archive)
    pending_archive = pending.get("archive") or {}
    if (archive_sha, archive_size) != (
            pending_archive.get("sha256"), pending_archive.get("bytes")):
        raise ReleaseRefused("archive bytes differ from the pending candidate identity")
    manifest = load_embedded_manifest(archive)
    manifest_sha = hashlib.sha256(_json_bytes(manifest)).hexdigest()
    if (manifest.get("payload_digest") != pending.get("payload_digest")
            or manifest_sha != (pending.get("manifest") or {}).get("sha256")):
        raise ReleaseRefused("embedded manifest differs from the pending candidate identity")
    streamed = stream_verify_archive(archive, manifest, full=True)
    if (streamed["hash_mismatches"] or streamed["missing_from_archive"]
            or not streamed["payload_digest_matches"] or streamed["manifest_cycle"]):
        raise ReleaseRefused("archive failed final stream verification")

    identities = evidence.get("identities") or {}
    expected_identity = (pending.get("build_a_publication") or {}).get(
        "database_identity")
    if not expected_identity:
        raise ReleaseRefused("pending sidecar lacks the Build-A database identity")
    if any(identities.get(key) != expected_identity for key in (
            "build_a_database_identity", "build_b_database_identity",
            "packaged_database_identity")):
        raise ReleaseRefused("Build-A, Build-B and packaged database identities do not agree")
    if (identities.get("archive_sha256") != archive_sha
            or identities.get("archive_bytes") != archive_size
            or identities.get("payload_digest") != manifest["payload_digest"]
            or identities.get("manifest_sha256") != manifest_sha):
        raise ReleaseRefused("Build-B evidence is not bound to this exact archive/manifest")

    receipt = {
        "schema": "ferc-full-release-receipt-v2",
        "package": NAME,
        "payload": "full",
        "status": "PASS",
        "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "archive": {"name": archive.name, "sha256": archive_sha,
                    "bytes": archive_size, "members": pending_archive.get("members")},
        "payload_digest": manifest["payload_digest"],
        "manifest": {"embedded_name": MANIFEST_NAME, "sha256": manifest_sha,
                     "file_count": manifest.get("file_count")},
        "build_a_publication": pending["build_a_publication"],
        "build_b_evidence": {"name": evidence_path.name,
                             "sha256": hashlib.sha256(evidence_raw).hexdigest(),
                             "bytes": len(evidence_raw),
                             "checks": {name: True for name in FINAL_BUILD_B_CHECKS}},
        "final_stream_verification": streamed,
        "chain": [
            "frozen Build-A payload -> embedded content manifest -> archive",
            "clean archive extraction with empty outputs -> Build B comparison evidence",
            "exact archive + Build-B evidence -> this external receipt",
        ],
        "scope_note": ("This receipt proves release identity, offline reproduction and the "
                       "declared acceptance comparisons. Source-unavailable/review states "
                       "remain data limitations and are not converted into recovered facts."),
    }
    _atomic_write(receipt_path, _json_bytes(receipt))
    return receipt


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payload", choices=["lite", "full"], default="lite")
    ap.add_argument("--root", default=str(HERE))
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--version", default="", help="optional filesystem-safe release label")
    ap.add_argument("--defer-build-b", action="store_true",
                    help="full payload only: stream-verify and publish a pending candidate; "
                         "do not issue a PASS receipt until clean Build B is supplied")
    ap.add_argument("--pending-sidecar", type=pathlib.Path,
                    help="external pending-candidate identity path")
    ap.add_argument("--finalize-build-b", type=pathlib.Path, metavar="EVIDENCE_JSON",
                    help="finalize an exact pending archive from clean Build-B evidence")
    ap.add_argument("--archive", type=pathlib.Path,
                    help="exact archive to finalize with --finalize-build-b")
    ap.add_argument("--receipt-out", type=pathlib.Path,
                    help="external final receipt for --finalize-build-b")
    args = ap.parse_args(argv)
    if args.finalize_build_b is not None:
        required = {"--archive": args.archive, "--pending-sidecar": args.pending_sidecar,
                    "--receipt-out": args.receipt_out}
        missing = [name for name, value in required.items() if value is None]
        if missing:
            print(f"finalize requires {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            receipt = finalize_build_b_receipt(
                args.archive.resolve(), args.pending_sidecar.resolve(),
                args.finalize_build_b.resolve(), args.receipt_out.resolve())
        except (OSError, ValueError, KeyError, ReleaseRefused) as exc:
            print(f"FINALIZE: FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"FINALIZE: PASS {receipt['archive']['sha256']} -> {args.receipt_out}")
        return 0
    root = pathlib.Path(args.root).resolve()
    full = args.payload == "full"
    if full and not args.defer_build_b:
        print("full release requires --defer-build-b; a PASS receipt is issued only "
              "after clean Build B is supplied with --finalize-build-b",
              file=sys.stderr)
        return 2
    if args.pending_sidecar is not None and not args.defer_build_b:
        print("--pending-sidecar requires --payload full --defer-build-b",
              file=sys.stderr)
        return 2
    out_dir = pathlib.Path(args.out_dir).resolve() if args.out_dir else root.parent
    if args.version and (not all(c.isalnum() or c in ".-_" for c in args.version)
                         or args.version in (".", "..")):
        print("invalid --version: use only letters, digits, dot, dash and underscore",
              file=sys.stderr)
        return 2
    out_zip = out_dir / archive_name(full, args.version)
    if args.defer_build_b and not full:
        print("--defer-build-b is valid only with --payload full", file=sys.stderr)
        return 2
    try:
        out_zip.relative_to(root)
    except ValueError:
        pass
    else:
        print("refusing an output archive inside the payload root", file=sys.stderr)
        return 2

    print(f"payload={args.payload}  root={root}")
    print(f"\n[1/4] {RELEASE_STAGES[0][0]}: {RELEASE_STAGES[0][1]}")
    s1 = stage1_freeze(root)
    if full:
        s1["violations"] = list(s1["violations"]) + full_payload_contract(root)
    table_count_state = ("available" if s1["table_counts_available"] else
                         "UNAVAILABLE (table-backed emptiness rule reported unenforced)")
    print(f"      contract declares {s1['required_outputs_declared']} required outputs; "
          f"table counts {table_count_state}")
    if not s1["table_counts_available"]:
        # The one place the emptiness rule can be enforced is here, against the
        # real database. If it is absent from the BUILD tree, the rule is never
        # enforced anywhere and a zero-byte export would satisfy the contract.
        s1["violations"] = list(s1["violations"]) + [{
            "kind": "NO_DATABASE_TO_ENFORCE_EMPTINESS", "path": "staging/",
            "severity": "blocking",
            "detail": ("the staging database is absent from the build tree, so no "
                       "declared output could be checked against the rows behind it; "
                       "a zero-byte export would otherwise satisfy the contract"),
            "group": "provenance", "rationale": "audit A13"}]
    # On the BUILD tree the database is present, so every rule is checkable and
    # every violation blocks. `blocking()` keeps the split in the contract rather
    # than duplicating the judgement here.
    s1["violations"] = blocking(s1["violations"])
    if s1["violations"]:
        print(f"      {len(s1['violations'])} CONTRACT VIOLATION(S):")
        for v in s1["violations"][:20]:
            print(f"        [{v['kind']}] {v['path']}: {v['detail']}")
        failure = {"package": NAME, "payload": args.payload, "status": "FAIL",
                   "failed_stage": "freeze_payload",
                   "contract_violations": s1["violations"],
                   "why": ("the candidate does not satisfy the fixed required-output "
                           "contract; no manifest, archive or receipt was produced. "
                           "A missing required output cannot be redefined out of a "
                           "release by omitting it from an inventory (audit A13)."),
                   "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
                       timespec="seconds")}
        _atomic_write(root / f"release_attempt_failure_{'full' if full else 'lite'}.json",
                      _json_bytes(failure))
        print("\nRELEASE: FAIL (required-output contract) -> no archive was built")
        return 1
    print("      contract satisfied")

    try:
        build_a = verify_build_a_boundary(root, required=full)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ReleaseRefused) as exc:
        print(f"      Build-A publication boundary FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"      Build-A publication boundary: {build_a['status']}")
    try:
        final_records = verify_final_records_boundary(root, required=full)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ReleaseRefused) as exc:
        print(f"      final-record boundary FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"      final-record boundary: {final_records['status']}")

    print(f"\n[2/4] {RELEASE_STAGES[1][0]}: {RELEASE_STAGES[1][1]}")
    try:
        s2 = stage2_hash(root, full=full)
    except ReleaseRefused as exc:
        print(f"      unsafe payload refused: {exc}")
        return 1
    if s2["unreadable"]:
        print(f"      {len(s2['unreadable'])} UNREADABLE payload file(s); refusing")
        for u in s2["unreadable"][:10]:
            print(f"        {u['path']}: {u['error']}")
        return 1
    entries = s2["entries"]
    by_class: dict[str, int] = {}
    for m in entries.values():
        by_class[m["class"]] = by_class.get(m["class"], 0) + 1
    total = sum(m["bytes"] for m in entries.values())
    print(f"      {len(entries):,} files, {total/1e6:,.1f} MB (sizes from bytes read)")
    print("      " + ", ".join(f"{k}={v}" for k, v in sorted(by_class.items())))
    manifest = build_manifest(entries, full=full,
                              extra={"contract_check": {
                                  "required_outputs": len(REQUIRED_OUTPUTS),
                                  "violations": 0},
                                  "build_a_publication": build_a,
                                  "final_records_publication": final_records,
                                  "declared_redundant_aliases": s2["redundant_aliases"],
                                  "source_cache_contract": s2["source_cache_contract"],
                                  "canonical_sidecar": manifest_for(full)})
    print(f"      payload_digest {manifest['payload_digest']}")

    print(f"\n[3/4] {RELEASE_STAGES[2][0]}: {RELEASE_STAGES[2][1]}")
    candidate_zip = out_zip.with_name(
        f".{out_zip.name}.{os.getpid()}.{uuid.uuid4().hex}.candidate")
    try:
        s3 = stage3_archive(root, entries, manifest, candidate_zip, full=full)
    except (OSError, zipfile.BadZipFile, ReleaseRefused) as exc:
        if candidate_zip.exists():
            candidate_zip.unlink()
        print(f"      archive construction FAILED: {type(exc).__name__}: {exc}")
        return 1
    zhash, zsize = sha_and_size(candidate_zip)
    print(f"      {out_zip.name}  {s3['members_written']:,} members  {zsize:,} bytes")
    print(f"      sha256 {zhash}")

    if args.defer_build_b:
        print("\n[verify] streaming every candidate member; clean Build B remains pending")
        v = stream_verify_archive(candidate_zip, manifest, full=True)
        try:
            final_aliases = validate_redundant_aliases(root)
            final_cache_contract, final_cache_paths = source_cache_contract(root)
            final_inventory = {rel for _path, rel in payload_paths(
                root, full=True, referenced_cache_objects=final_cache_paths)}
        except ReleaseRefused as exc:
            final_aliases = []
            final_cache_contract = {}
            final_inventory = set()
            v["hash_mismatches"].append(f"payload became unsafe during build: {exc}")
        v["source_inventory_matches"] = final_inventory == set(entries)
        v["redundant_aliases_still_match"] = final_aliases == s2["redundant_aliases"]
        v["source_cache_contract_still_matches"] = (
            final_cache_contract == s2["source_cache_contract"])
        if not v["source_inventory_matches"]:
            v["hash_mismatches"].append(
                "payload file set changed between hash and stream-verification stages")
        if not v["redundant_aliases_still_match"]:
            v["hash_mismatches"].append(
                "declared redundant alias changed between freeze and verification")
        if not v["source_cache_contract_still_matches"]:
            v["hash_mismatches"].append(
                "source-cache index/inventory contract changed during packaging")
        ok = (not v["hash_mismatches"] and not v["missing_from_archive"]
              and v["payload_digest_matches"] and not v["manifest_cycle"])
        if not ok:
            if candidate_zip.exists():
                candidate_zip.unlink()
            print("RELEASE CANDIDATE: FAIL (stream verification)")
            return 1
        os.replace(candidate_zip, out_zip)
        pending_path = (args.pending_sidecar.resolve() if args.pending_sidecar else
                        out_zip.with_suffix(out_zip.suffix + ".pending.json"))
        pending = {
            "schema": "ferc-full-release-pending-build-b-v1",
            "package": NAME, "payload": "full",
            "status": "CANDIDATE_PENDING_BUILD_B",
            "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"),
            "archive": {"name": out_zip.name, "sha256": zhash, "bytes": zsize,
                        "members": s3["members_written"]},
            "payload_digest": manifest["payload_digest"],
            "manifest": {"embedded_name": MANIFEST_NAME,
                         "sha256": hashlib.sha256(_json_bytes(manifest)).hexdigest(),
                         "file_count": manifest["file_count"]},
            "build_a_publication": build_a,
            "final_records_publication": final_records,
            "stream_verification": v,
            "next_required_action": (
                "leave a clean extraction of this exact archive byte-for-byte unchanged; "
                "direct the declared offline rebuild to a separate empty --work-root, "
                "compare Build A, Build B and packaged state, then finalize externally"),
        }
        manifest_bytes = _json_bytes(manifest)
        _atomic_write(root / manifest_for(True), manifest_bytes)
        _atomic_write(root / MANIFEST_NAME, manifest_bytes)
        _atomic_write(pending_path, _json_bytes(pending))
        print(f"      {v['verified_files']:,} payload files hash/CRC verified")
        print(f"      pending identity -> {pending_path}")
        print("\nRELEASE CANDIDATE: CANDIDATE_PENDING_BUILD_B (no PASS receipt issued)")
        return 0

    print("\n[verify] extracting the candidate and checking the extracted bytes")
    v = verify_extracted(candidate_zip, manifest, full=full)
    try:
        final_inventory = {rel for _path, rel in payload_paths(
            root, full=full,
            referenced_cache_objects=s2["referenced_cache_objects"])}
    except ReleaseRefused as exc:
        final_inventory = set()
        v["hash_mismatches"].append(f"payload became unsafe during build: {exc}")
    v["source_inventory_matches"] = final_inventory == set(entries)
    if not v["source_inventory_matches"]:
        v["hash_mismatches"].append(
            "payload file set changed between hash and verification stages")
    print(f"      files verified from extracted bytes : {v['extracted']:,}")
    print(f"      hash/size mismatches                : {len(v['hash_mismatches'])}")
    print(f"      missing from archive                : {len(v['missing_from_archive'])}")
    print(f"      payload digest recomputes           : {v['payload_digest_matches']}")
    print(f"      manifest/receipt cycle violations   : {len(v['manifest_cycle'])}")
    print(f"      contract holds on extracted tree    : {len(v['contract_violations'])} violation(s)")
    print(f"      archive safety violations           : "
          f"{len(_archive_safety_failures(v['archive_safety']))}")
    print(f"      offline suite in extracted tree     : rc={v['offline_suite']['returncode']} "
          f"{v['offline_suite'].get('stdout', '')[-400:]}")
    if v["offline_suite"]["returncode"] != 0:
        print(f"        stderr: {v['offline_suite']['stderr'][:300]}")
    for m in v["hash_mismatches"][:5]:
        print("        ", m)
    for c in v["manifest_cycle"][:5]:
        print(f"        [{c['kind']}] {c['path']}: {c['detail']}")

    ok = (not v["hash_mismatches"] and not v["missing_from_archive"]
          and v["payload_digest_matches"] and not v["manifest_cycle"]
          and not v["contract_violations"]
          and v["offline_suite"]["returncode"] == 0)

    if ok:
        os.replace(candidate_zip, out_zip)
    elif candidate_zip.exists():
        candidate_zip.unlink()

    print(f"\n[4/4] {RELEASE_STAGES[3][0]}: {RELEASE_STAGES[3][1]}")
    receipt = {
        "package": NAME,
        "payload": args.payload,
        "status": "PASS" if ok else "FAIL",
        "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        # what this receipt certifies, named unambiguously
        "archive": {"name": out_zip.name, "sha256": zhash, "bytes": zsize,
                    "members": s3["members_written"]},
        "payload_digest": manifest["payload_digest"],
        "manifest": {"name": manifest_for(full),
                     "embedded_name": MANIFEST_NAME,
                     "sha256": hashlib.sha256(_json_bytes(manifest)).hexdigest(),
                     "file_count": manifest["file_count"]},
        "chain": [
            "payload files -> hashed from bytes -> artifact_manifest.json",
            "artifact_manifest.json + payload -> archive",
            "archive -> hashed from bytes -> this receipt (which lives OUTSIDE it)",
        ],
        "acyclic": ("this receipt is not inside the archive it certifies; the manifest "
                    "contains neither itself nor this receipt. In the full payload the "
                    "frozen Build-A database is an ordinary content-hashed payload file."),
        "verification": v,
        "required_output_contract": {
            "declared": len(REQUIRED_OUTPUTS),
            "source": "acceptance/contract.py",
            "checked_on": ["build tree", "extracted archive"],
        },
        "build_a_publication": build_a,
        "final_records_publication": final_records,
        "scope_note": (
            "This receipt certifies the identity and internal consistency of the "
            "named archive and that the declared required outputs are present and "
            "substantive in it. It is NOT a statement that the data are accepted: "
            "open gates and blockers are reported separately and remain in force."),
    }
    if not full:
        receipt["standalone"] = False
        receipt["reassembly"] = (
            "This is the LITE payload: source_cache/ and staging/ are excluded by "
            "declaration, not by accident. Build the complete deliverable with "
            "`python3 build_release.py --payload full --defer-build-b`.")
        # A sentence claiming "the rule was enforced at stage 1" would be an
        # assertion by the same process that would have to be wrong for it to
        # matter -- a receipt certifying its own correctness, which is the A21
        # cycle in miniature. So the receipt carries the NUMBERS the rule was
        # decided on instead: the backing table, its row count in the real
        # database at build time, and the data rows actually written. A reader
        # with no database re-derives "rows_written > 0 whenever table_rows > 0"
        # rather than trusting the claim.
        receipt["emptiness_rule_scope"] = {
            "not_re_verifiable_from_this_archive": v.get(
                "unverified_from_this_payload", []),
            "why": ("this payload excludes the database by declaration, so a verifier "
                    "working from the archive alone cannot re-derive the "
                    "non-empty-if-table rule"),
            "enforcement_evidence": enforcement_evidence(root, table_counts(root)),
            "how_to_check": ("recompute `rows_written > 0 whenever table_rows > 0` from "
                             "the numbers below; challenge them against the full payload, "
                             "which re-verifies the rule on its extracted tree"),
        }
    if ok:
        manifest_bytes = _json_bytes(manifest)
        _atomic_write(root / manifest_for(full), manifest_bytes)
        # Compatibility alias for older inspection tools. It is never authoritative
        # across two payloads; the payload-specific sidecars above are.
        _atomic_write(root / MANIFEST_NAME, manifest_bytes)
        receipt_bytes = _json_bytes(receipt)
        _atomic_write(root / receipt_for(full), receipt_bytes)
        # Historical acceptance fixtures do not contain a consumer publication
        # receipt. Preserve their legacy release-receipt lookup without ever
        # overwriting Build A's distinct publication_receipt.json.
        if not (root / PUBLICATION_RECEIPT_NAME).exists() and not full:
            _atomic_write(root / LEGACY_RECEIPT_NAME, receipt_bytes)
    print((f"      receipt -> {receipt_for(full)} (outside the archive)"
           if ok else "      receipt not issued; prior receipt/archive remain authoritative"))
    print(f"\nRELEASE: {receipt['status']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
