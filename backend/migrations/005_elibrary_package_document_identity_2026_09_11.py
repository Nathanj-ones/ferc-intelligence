#!/usr/bin/env python3
"""Correct mixed eLibrary accession-package document identities.

The eLibrary document and LNG adapters retrieve one accession-level response,
which can be a ZIP containing several public attachments.  Historical rows
combined the selected member's title, the first listed attachment ID, and the
whole response's hash and byte size.  This data-only migration represents that
retrieved object honestly as ``eLibrary|<accession>|package``.  It rewires all
document references atomically and deliberately excludes exact IOC/capacity
attachments and metadata-only ``|listing`` rows.

Dry-run is the default.  ``--apply`` uses one ``BEGIN IMMEDIATE`` transaction,
is idempotent, refuses ambiguous or internally inconsistent source state, and
preserves row counts in every affected table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import uuid


SOURCE_SYSTEM = "eLibrary"
TARGET_FORM = "eLibrary document"
REFERENCE_TABLES = ("observations", "document_facts", "events")
REQUIRED_COLUMNS = {
    "filings": {
        "source_system", "filing_id", "accession_number", "form", "content_hash",
    },
    "documents": {
        "document_id", "source_system", "filing_id", "accession_number",
        "attachment_id", "title", "class_type", "media_type", "byte_size",
        "content_hash", "cache_path", "text_layer", "availability",
        "retrieved_at", "source_url",
    },
    "observations": {"document_id"},
    "document_facts": {
        "document_fact_id", "document_id", "source_system", "filing_id",
        "content_hash",
    },
    "events": {"document_id"},
}
DOCUMENT_COLUMNS = (
    "document_id", "source_system", "filing_id", "accession_number",
    "attachment_id", "title", "class_type", "media_type", "byte_size",
    "content_hash", "cache_path", "text_layer", "availability",
    "retrieved_at", "source_url",
)


def _atomic_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=1, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'PRAGMA table_xinfo("{table}")')}


def _counts(con: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in ("filings", "documents", *REFERENCE_TABLES)
    }


def _package_id(source_system: str, filing_id: str) -> str:
    return f"{source_system}|{filing_id}|package"


def _package_title(filing_id: str) -> str:
    return f"eLibrary accession {filing_id} package"


def _identity_digest(
        rows: list[sqlite3.Row], filings: list[sqlite3.Row],
        facts: list[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for filing in filings:
        digest.update(json.dumps(
            (filing["filing_id"], filing["accession_number"], filing["content_hash"]),
            separators=(",", ":"), ensure_ascii=False, default=str,
        ).encode("utf-8"))
        digest.update(b"\n")
    for row in rows:
        values = (
            row["source_system"], row["filing_id"], row["document_id"],
            row["attachment_id"], row["title"], row["content_hash"],
            row["byte_size"], row["text_layer"],
        )
        digest.update(json.dumps(values, separators=(",", ":"), ensure_ascii=False,
                                 default=str).encode("utf-8"))
        digest.update(b"\n")
    for fact in facts:
        digest.update(json.dumps(
            (fact["document_fact_id"], fact["document_id"], fact["source_system"],
             fact["filing_id"], fact["content_hash"]),
            separators=(",", ":"), ensure_ascii=False, default=str,
        ).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _target_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT d.*,f.content_hash AS filing_content_hash,"
        "f.accession_number AS filing_accession_number "
        "FROM documents d JOIN filings f "
        "ON f.source_system=d.source_system AND f.filing_id=d.filing_id "
        "WHERE f.source_system=? AND f.form=? "
        "AND TRIM(COALESCE(d.content_hash,''))<>'' "
        "ORDER BY d.filing_id,d.document_id",
        (SOURCE_SYSTEM, TARGET_FORM),
    ))


def _target_filings(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT filing_id,accession_number,content_hash FROM filings "
        "WHERE source_system=? AND form=? "
        "AND TRIM(COALESCE(content_hash,''))<>'' ORDER BY filing_id",
        (SOURCE_SYSTEM, TARGET_FORM),
    ))


def _target_facts(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT df.document_fact_id,df.document_id,df.source_system,df.filing_id,"
        "df.content_hash FROM document_facts df JOIN documents d "
        "ON d.document_id=df.document_id JOIN filings f "
        "ON f.source_system=d.source_system AND f.filing_id=d.filing_id "
        "WHERE f.source_system=? AND f.form=? "
        "AND TRIM(COALESCE(d.content_hash,''))<>'' "
        "ORDER BY df.document_fact_id",
        (SOURCE_SYSTEM, TARGET_FORM),
    ))


def plan(con: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise ValueError(
            "not an operating-assets database; missing " + ",".join(missing_tables))
    missing_columns = {
        table: sorted(required - _columns(con, table))
        for table, required in REQUIRED_COLUMNS.items()
        if required - _columns(con, table)
    }
    if missing_columns:
        raise ValueError("required migration columns are absent: "
                         + json.dumps(missing_columns, sort_keys=True))

    rows = _target_rows(con)
    target_filings = _target_filings(con)
    target_facts = _target_facts(con)
    by_filing: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_filing.setdefault(str(row["filing_id"]), []).append(row)

    problems: list[dict] = []
    legacy: list[sqlite3.Row] = []
    current: list[sqlite3.Row] = []
    facts_by_document: dict[str, list[sqlite3.Row]] = {}
    for fact in target_facts:
        facts_by_document.setdefault(str(fact["document_id"]), []).append(fact)
    for filing in target_filings:
        filing_id = str(filing["filing_id"] or "")
        if filing_id not in by_filing:
            problems.append({
                "filing_id": filing_id,
                "problem": "hashed eLibrary filing has no hashed package document",
            })
    for filing_id, group in sorted(by_filing.items()):
        if len(group) != 1:
            problems.append({
                "filing_id": filing_id,
                "problem": "multiple hashed documents for one accession package",
                "document_ids": [str(row["document_id"]) for row in group],
            })
            continue
        row = group[0]
        document_id = str(row["document_id"] or "")
        attachment_id = str(row["attachment_id"] or "")
        content_hash = str(row["content_hash"] or "")
        filing_hash = str(row["filing_content_hash"] or "")
        expected_package = _package_id(SOURCE_SYSTEM, filing_id)
        row_problems = []
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            row_problems.append("document content_hash is not lowercase SHA-256")
        if filing_hash != content_hash:
            row_problems.append("document hash differs from filing/package hash")
        if str(row["accession_number"] or "") != filing_id:
            row_problems.append("document accession_number differs from filing ID")
        if str(row["filing_accession_number"] or "") != filing_id:
            row_problems.append("filing accession_number differs from filing ID")
        if row["byte_size"] is None or int(row["byte_size"]) < 0:
            row_problems.append("package byte_size is absent or negative")
        for fact in facts_by_document.get(document_id, []):
            fact_hash = str(fact["content_hash"] or "")
            if fact_hash and fact_hash != content_hash:
                row_problems.append(
                    f"document fact {fact['document_fact_id']} hash differs from package")
            if str(fact["source_system"] or "") != SOURCE_SYSTEM \
                    or str(fact["filing_id"] or "") != filing_id:
                row_problems.append(
                    f"document fact {fact['document_fact_id']} names a different occurrence")
        if document_id == expected_package:
            if attachment_id:
                row_problems.append("package document carries an attachment ID")
            current.append(row)
        else:
            expected_legacy = f"{SOURCE_SYSTEM}|{filing_id}|{attachment_id}"
            if not attachment_id:
                row_problems.append("legacy document has no attachment ID")
            if document_id != expected_legacy:
                row_problems.append("legacy document ID is not derived from its attachment ID")
            legacy.append(row)
        if row_problems:
            problems.append({
                "filing_id": filing_id,
                "document_id": document_id,
                "problems": row_problems,
            })

    legacy_ids = [str(row["document_id"]) for row in legacy]
    blank_fact_hashes = sum(
        1 for fact in target_facts if not str(fact["content_hash"] or "").strip())
    references = {}
    for table in REFERENCE_TABLES:
        references[table] = sum(int(con.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE document_id=?', (document_id,)
        ).fetchone()[0]) for document_id in legacy_ids)

    listing_count = int(con.execute(
        "SELECT COUNT(*) FROM documents d JOIN filings f "
        "ON f.source_system=d.source_system AND f.filing_id=d.filing_id "
        "WHERE f.source_system=? AND f.form=? AND d.document_id=?||f.filing_id||'|listing'",
        (SOURCE_SYSTEM, TARGET_FORM, SOURCE_SYSTEM + "|"),
    ).fetchone()[0])
    protected_exact_count = int(con.execute(
        "SELECT COUNT(*) FROM documents d JOIN filings f "
        "ON f.source_system=d.source_system AND f.filing_id=d.filing_id "
        "WHERE d.source_system=? AND f.form IN ('Form 549B IOC','Form 549B Capacity')",
        (SOURCE_SYSTEM,),
    ).fetchone()[0])
    return {
        "source_system": SOURCE_SYSTEM,
        "target_form": TARGET_FORM,
        "hashed_package_candidates": len(rows),
        "hashed_target_filings": len(target_filings),
        "legacy_documents_to_migrate": len(legacy),
        "current_package_documents": len(current),
        "listing_documents_protected": listing_count,
        "ioc_capacity_documents_protected": protected_exact_count,
        "references_to_rewire": references,
        "document_fact_hashes_to_fill": blank_fact_hashes,
        "table_counts": _counts(con),
        "target_identity_digest": _identity_digest(rows, target_filings, target_facts),
        "problems": problems,
        "already_current": not legacy and not problems and blank_fact_hashes == 0,
    }


def _rewrite_document(con: sqlite3.Connection, row: sqlite3.Row) -> None:
    old_id = str(row["document_id"])
    filing_id = str(row["filing_id"])
    new_id = _package_id(str(row["source_system"]), filing_id)
    existing = con.execute(
        "SELECT * FROM documents WHERE document_id=?", (new_id,)).fetchone()
    if existing is not None:
        raise RuntimeError(f"{filing_id}: package document unexpectedly already exists")

    values = {column: row[column] for column in DOCUMENT_COLUMNS}
    values.update({
        "document_id": new_id,
        "attachment_id": "",
        "title": _package_title(filing_id),
        # The legacy value described only the selected member.  The database
        # does not retain enough member-level detail to make a package-wide
        # assertion, so migration must choose the conservative state.
        "text_layer": "unknown",
    })
    placeholders = ",".join("?" for _ in DOCUMENT_COLUMNS)
    con.execute(
        f"INSERT INTO documents({','.join(DOCUMENT_COLUMNS)}) VALUES({placeholders})",
        tuple(values[column] for column in DOCUMENT_COLUMNS),
    )
    for table in REFERENCE_TABLES:
        expected = int(con.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE document_id=?', (old_id,)
        ).fetchone()[0])
        if table == "document_facts":
            changed = con.execute(
                'UPDATE document_facts SET document_id=?,content_hash=? '
                'WHERE document_id=?',
                (new_id, row["content_hash"], old_id),
            ).rowcount
        else:
            changed = con.execute(
                f'UPDATE "{table}" SET document_id=? WHERE document_id=?',
                (new_id, old_id),
            ).rowcount
        if changed != expected:
            raise RuntimeError(
                f"{filing_id}: rewired {changed}/{expected} {table} references")
    deleted = con.execute(
        "DELETE FROM documents WHERE document_id=?", (old_id,)).rowcount
    if deleted != 1:
        raise RuntimeError(f"{filing_id}: deleted {deleted} legacy documents")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=pathlib.Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"database absent: {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db, isolation_level=None, timeout=120)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            print("migration refused: preflight quick_check failed", file=sys.stderr)
            return 2
        before = plan(con)
        payload = {
            "schema": "ferc-migration-005-result-v1",
            "database": str(args.db.resolve()),
            "before": before,
            "mode": "dry_run",
            "database_commit": "unchanged",
        }
        if before["problems"]:
            payload["result"] = "refused_ambiguous_package_identity"
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 2
        if not args.apply:
            payload["result"] = "already_current" if before["already_current"] else "ready"
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 0
        if before["already_current"]:
            payload.update(mode="already_current", result="verified_noop")
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 0

        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        try:
            locked = plan(con)
            if locked["problems"] or locked["target_identity_digest"] != \
                    before["target_identity_digest"]:
                raise RuntimeError("migration target changed after write lock acquisition")
            for row in _target_rows(con):
                if str(row["document_id"]) != _package_id(
                        str(row["source_system"]), str(row["filing_id"])):
                    _rewrite_document(con, row)
            fact_hashes_filled = locked["document_fact_hashes_to_fill"]
            for row in _target_rows(con):
                con.execute(
                    "UPDATE document_facts SET content_hash=? "
                    "WHERE document_id=? AND TRIM(COALESCE(content_hash,''))=''",
                    (row["content_hash"], row["document_id"]),
                )

            after = plan(con)
            if not after["already_current"] or after["problems"]:
                raise RuntimeError(f"post-migration identity plan is incomplete: {after}")
            if after["table_counts"] != before["table_counts"]:
                raise RuntimeError(
                    f"row counts changed: before={before['table_counts']} "
                    f"after={after['table_counts']}")
            if any(after["references_to_rewire"].values()):
                raise RuntimeError(
                    "references to legacy attachment-mixed identities remain: "
                    + json.dumps(after["references_to_rewire"], sort_keys=True))
            quick = con.execute("PRAGMA quick_check").fetchone()[0]
            foreign_keys = [tuple(row) for row in
                            con.execute("PRAGMA foreign_key_check").fetchmany(10)]
            if quick != "ok" or foreign_keys:
                raise RuntimeError(
                    f"integrity check failed: quick={quick}, foreign_keys={foreign_keys}")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

        payload.update(
            mode="apply",
            result="applied",
            database_commit="committed",
            rows_migrated=before["legacy_documents_to_migrate"],
            references_rewired=before["references_to_rewire"],
            document_fact_hashes_filled=fact_hashes_filled,
            after=after,
            quick_check=quick,
            foreign_key_violations=foreign_keys,
        )
        print(json.dumps(payload, indent=1, sort_keys=True))
        if args.report:
            _atomic_json(args.report, payload)
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        if con.in_transaction:
            con.execute("ROLLBACK")
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
