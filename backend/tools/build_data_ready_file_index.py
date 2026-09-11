#!/usr/bin/env python3
"""Build a non-circular byte identity index for the standalone handoff."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "deliverables" / "DATA_READY_FILE_INDEX.json"

FIXED = (
    "DATA_READY.md",
    "FRONTEND_BACKEND_CONTRACT.md",
    "CONNECTION_LATER.md",
    "publication_receipt.json",
    "staging/operating_assets.sqlite",
    "rollback/operating_assets_pre_data_ready_scope_migration.sqlite",
    "verification/validation_results.json",
    "exports/frontend_v1/contract.json",
    "exports/frontend_v1/assets.json",
    "exports/frontend_v1/headline_availability.json",
    "exports/frontend_v1/instruments.json",
    "exports/frontend_v1/source_index.json",
    "RUNTIME_REQUIREMENTS.json",
    "run.py",
    "exporters.py",
    "build_field_status.py",
    "validate_data_ready_candidate.py",
    "ferclib/coverage.py",
    "ferclib/frontend_exports.py",
    "ferclib/registry.py",
    "ferclib/staging.py",
    "adapters/elibrary_docs.py",
    "adapters/lng.py",
    "migrations/004_document_scope_contract_2026_09_10.py",
    "tools/recover_r6_dependency_cache.py",
    "tools/reconstruct_r6_time_variant_zip.py",
    "tools/validate_source_cache.py",
    "tools/build_data_ready_file_index.py",
    "tests/test_coverage.py",
    "tests/test_frontend_exports_data_ready.py",
    "tests/test_oil_index_source_evidence_codex.py",
    "tests/test_replay_publication_codex.py",
)

LOGS = (
    "implementation_logs/data_ready/final_coverage_regeneration_v4.log",
    "implementation_logs/data_ready/final_field_status_regeneration_v2.log",
    "implementation_logs/data_ready/final_changed_controls_tests.log",
    "implementation_logs/data_ready/final_export_publication_v4.log",
    "implementation_logs/data_ready/final_candidate_validate_v2.log",
    "implementation_logs/data_ready/final_data_ready_contract_validation.log",
    "implementation_logs/data_ready/final_data_ready_contract_validation_post_tests.log",
    "implementation_logs/data_ready/final_source_cache_validation.log",
    "implementation_logs/data_ready/SOURCE_CACHE_FULL_VALIDATION_FINAL.json",
    "implementation_logs/data_ready/final_full_backend_suite_cpython314.log",
    "implementation_logs/data_ready/reviewed_image_gate_unsandboxed.log",
    "implementation_logs/data_ready/open_blocker_self_contained_regression.log",
    "implementation_logs/data_ready/targeted_elibrary_refresh.log",
    "implementation_logs/data_ready/incremental_no_change_refresh.log",
)


def identity(path: pathlib.Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def main() -> int:
    receipt = json.loads((ROOT / "publication_receipt.json").read_text())
    rels = list(FIXED) + list(LOGS)
    rels.extend(
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "deliverables").rglob("*"))
        if path.is_file() and path != OUT
    )
    ordered = []
    seen = set()
    for rel in rels:
        if rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    missing = [rel for rel in ordered if not (ROOT / rel).is_file()]
    if missing:
        raise SystemExit(f"missing indexed files: {missing}")
    files = {rel: identity(ROOT / rel) for rel in ordered}
    payload = {
        "schema": "ferc_data_ready_file_index_v1",
        "root": str(ROOT),
        "publication_generation_id": receipt["generation_id"],
        "publication_metadata": {
            key: receipt["metadata"].get(key)
            for key in ("as_of", "code_version", "registry_version", "code_snapshot",
                        "input_snapshot", "database_identity", "unit_commit_snapshot")
        },
        "indexed_files": len(files),
        "files": files,
        "note": "This index is external to the receipt-bound 257-file export generation and excludes itself to avoid a circular identity.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_name(f".{OUT.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, OUT)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(json.dumps({"status": "pass", "files": len(files),
                      "output": str(OUT)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
