#!/usr/bin/env python3
"""Extract the exact capacity semantic-gate population from a read-only build.

This is an evidence utility.  It never mutates the candidate database and it
does not decide whether a build passes; the integrated test and final-record
gate own that assertion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
from collections import Counter
from typing import Any


ACCESSIONS = ("20250225-5101", "20250227-5034", "20260202-5047")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: pathlib.Path) -> dict[str, Any]:
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": _sha256(path)}


def _rows(con: sqlite3.Connection, sql: str,
          parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, parameters)]


def extract(database: pathlib.Path, receipt: pathlib.Path) -> dict[str, Any]:
    if not database.is_file() or not receipt.is_file():
        raise ValueError("database and publication receipt must both exist")
    uri = "file:" + database.resolve().as_posix() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        integrity = str(con.execute("PRAGMA quick_check").fetchone()[0])
        marks = ",".join("?" for _ in ACCESSIONS)
        observations = _rows(
            con,
            "SELECT observation_id,accession_number,entity_key,metric_id,scope,"
            "value_num,availability,validation,source_regime FROM observations "
            f"WHERE accession_number IN ({marks}) "
            "AND metric_id='cap_reported_capacity' "
            "AND source_regime='Form 549B Capacity' "
            "ORDER BY accession_number,observation_id",
            ACCESSIONS,
        )
        facts = _rows(
            con,
            "SELECT document_fact_id,filing_id,value_num,confidence,review_state "
            "FROM document_facts WHERE filing_id='20250225-5101' "
            "ORDER BY document_fact_id",
        )
        coverage = _rows(
            con,
            "SELECT e.slot_id,o.accession_number,o.observation_id,o.availability,"
            "m.outcome,m.populated,m.validated FROM coverage_measured m "
            "JOIN coverage_expected e ON e.slot_id=m.slot_id "
            "JOIN observations o ON o.observation_id=m.observation_id "
            "WHERE o.accession_number IN ('20250225-5101','20260202-5047') "
            "AND o.validation='blocked_ambiguity' AND o.value_num IS NULL "
            "ORDER BY o.accession_number,e.slot_id",
        )
        valid_control = int(con.execute(
            "SELECT COUNT(*) FROM observations WHERE source_regime="
            "'Form 549B Capacity' AND availability='present' "
            "AND validation='pass' AND value_num IS NOT NULL"
        ).fetchone()[0])
    finally:
        con.close()

    gated = [row for row in observations
             if row["validation"] == "blocked_ambiguity"
             and row["value_num"] is None]
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_metadata = receipt_payload.get("metadata") or {}
    return {
        "schema": "ferc_capacity_semantic_status_extract_v1",
        "database": _identity(database),
        "publication_receipt": {
            **_identity(receipt),
            "generation_id": receipt_payload.get("generation_id"),
            "code_snapshot": receipt_metadata.get("code_snapshot"),
            "input_snapshot": receipt_metadata.get("input_snapshot"),
            "database_identity": receipt_metadata.get("database_identity"),
        },
        "quick_check": integrity,
        "affected_accessions": list(ACCESSIONS),
        "counts": {
            "affected_observations": len(observations),
            "blocked_null_observations": len(gated),
            "blocked_null_by_availability": dict(sorted(Counter(
                row["availability"] for row in gated).items())),
            "arlington_observations": sum(
                row["accession_number"] == "20250225-5101"
                for row in observations),
            "arlington_document_facts": len(facts),
            "matched_coverage_rows": len(coverage),
            "matched_coverage_outcomes": dict(sorted(Counter(
                row["outcome"] for row in coverage).items())),
            "clean_numeric_present_pass_controls": valid_control,
        },
        "observations": observations,
        "arlington_document_facts": facts,
        "matched_coverage": coverage,
    }


def _write_atomic(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
           + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=pathlib.Path, required=True)
    parser.add_argument("--publication-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    _write_atomic(args.output, extract(args.database, args.publication_receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
