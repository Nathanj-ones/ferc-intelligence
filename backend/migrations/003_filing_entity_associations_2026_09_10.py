#!/usr/bin/env python3
"""Install occurrence/entity associations and asset-authority evidence metadata.

Dry-run is the default. ``--apply`` performs one ``BEGIN IMMEDIATE``
transaction, preserves every existing filing and asset/docket row, creates the
many-to-many association table, and seeds one explicitly labelled compatibility
association for each pre-existing filing.  A normal replay then replaces those
legacy anchors with source-supported named/shared associations where available.
The command is idempotent and refuses unknown table shapes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys


HERE = pathlib.Path(__file__).resolve().parent
SCHEMA = HERE.parent / "ferclib" / "schema.sql"

TABLE_SQL = """
CREATE TABLE filing_entities (
  source_system TEXT NOT NULL,
  filing_id TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  association_role TEXT NOT NULL,
  facility_key TEXT,
  evidence_ref TEXT NOT NULL,
  PRIMARY KEY (source_system, filing_id, entity_key),
  FOREIGN KEY (source_system, filing_id)
    REFERENCES filings(source_system, filing_id) ON DELETE CASCADE,
  FOREIGN KEY (entity_key) REFERENCES entities(entity_key) ON DELETE CASCADE
)
"""
INDEX_SQL = (
    "CREATE INDEX ix_filing_entities_entity ON "
    "filing_entities(entity_key, source_system, filing_id)"
)


def _columns(con: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row[1:7]) for row in con.execute(f'PRAGMA table_xinfo("{table}")')]


def _foreign_keys(con: sqlite3.Connection, table: str) -> list[tuple]:
    return sorted(tuple(row[2:8]) for row in con.execute(
        f'PRAGMA foreign_key_list("{table}")'))


def _unique_keys(con: sqlite3.Connection, table: str) -> list[tuple]:
    out = []
    for row in con.execute(f'PRAGMA index_list("{table}")'):
        if row[3] not in ("u", "pk"):
            continue
        keys = tuple(item[2] for item in con.execute(
            f'PRAGMA index_xinfo("{row[1]}")') if int(item[5]))
        out.append((row[3], int(row[2]), int(row[4]), keys))
    return sorted(out)


def _reference() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def plan(con: sqlite3.Connection) -> dict:
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing_base = sorted({"entities", "assets", "asset_dockets", "filings"} - tables)
    if missing_base:
        raise ValueError("not an operating-assets database; missing " + ",".join(missing_base))

    ref = _reference()
    try:
        problems = []
        asset_columns = {row[1]: tuple(row[1:7]) for row in con.execute(
            'PRAGMA table_xinfo("asset_dockets")')}
        expected_asset = {row[1]: tuple(row[1:7]) for row in ref.execute(
            'PRAGMA table_xinfo("asset_dockets")')}
        for name in sorted(set(asset_columns) - {"evidence_ref"}):
            if name not in expected_asset or asset_columns[name] != expected_asset[name]:
                problems.append({"object": f"asset_dockets.{name}",
                                 "actual": asset_columns[name],
                                 "expected": expected_asset.get(name)})
        extra = sorted(set(asset_columns) - set(expected_asset))
        if extra:
            problems.append({"object": "asset_dockets", "extra_columns": extra})
        evidence_missing = "evidence_ref" not in asset_columns
        if not evidence_missing and asset_columns["evidence_ref"] != expected_asset["evidence_ref"]:
            problems.append({"object": "asset_dockets.evidence_ref",
                             "actual": asset_columns["evidence_ref"],
                             "expected": expected_asset["evidence_ref"]})

        association_missing = "filing_entities" not in tables
        if not association_missing:
            for label, actual, expected in (
                    ("columns", _columns(con, "filing_entities"),
                     _columns(ref, "filing_entities")),
                    ("foreign_keys", _foreign_keys(con, "filing_entities"),
                     _foreign_keys(ref, "filing_entities")),
                    ("unique_keys", _unique_keys(con, "filing_entities"),
                     _unique_keys(ref, "filing_entities"))):
                if actual != expected:
                    problems.append({"object": "filing_entities", "part": label,
                                     "actual": actual, "expected": expected})

        indexes = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        index_missing = "ix_filing_entities_entity" not in indexes
        filing_count = int(con.execute("SELECT COUNT(*) FROM filings").fetchone()[0])
        if association_missing:
            missing_associations = filing_count
        else:
            missing_associations = int(con.execute(
                "SELECT COUNT(*) FROM filings f WHERE NOT EXISTS ("
                "SELECT 1 FROM filing_entities fe WHERE fe.source_system=f.source_system "
                "AND fe.filing_id=f.filing_id)").fetchone()[0])
        return {
            "add_asset_dockets_evidence_ref": evidence_missing,
            "create_filing_entities": association_missing,
            "create_filing_entities_index": index_missing,
            "existing_filings": filing_count,
            "backfill_compatibility_associations": missing_associations,
            "problems": problems,
            "already_current": not (evidence_missing or association_missing or
                                    index_missing or missing_associations or problems),
        }
    finally:
        ref.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=pathlib.Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"database absent: {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            print("migration refused: preflight quick_check failed", file=sys.stderr)
            return 2
        before = plan(con)
        payload = {"schema": "ferc-migration-003-result-v1",
                   "database": str(args.db), "before": before,
                   "mode": "dry_run", "database_commit": "unchanged"}
        if before["problems"]:
            payload["result"] = "refused_unknown_shape"
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 2
        if not args.apply:
            payload["result"] = "ready" if not before["already_current"] else "already_current"
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 0
        if before["already_current"]:
            payload.update(mode="already_current", result="verified_noop")
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 0

        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        try:
            if before["add_asset_dockets_evidence_ref"]:
                con.execute("ALTER TABLE asset_dockets ADD COLUMN evidence_ref TEXT")
            if before["create_filing_entities"]:
                con.execute(TABLE_SQL)
            if before["create_filing_entities_index"]:
                con.execute(INDEX_SQL)
            con.execute(
                "INSERT INTO filing_entities(source_system,filing_id,entity_key,"
                "association_role,facility_key,evidence_ref) "
                "SELECT f.source_system,f.filing_id,f.entity_key,'compatibility_anchor','',"
                "'migration_003: legacy filings.entity_key; reviewed replay required' "
                "FROM filings f WHERE NOT EXISTS (SELECT 1 FROM filing_entities fe "
                "WHERE fe.source_system=f.source_system AND fe.filing_id=f.filing_id)")
            missing = con.execute(
                "SELECT COUNT(*) FROM filings f WHERE NOT EXISTS (SELECT 1 FROM "
                "filing_entities fe WHERE fe.source_system=f.source_system "
                "AND fe.filing_id=f.filing_id)").fetchone()[0]
            if missing:
                raise RuntimeError(f"{missing} filings remain without an association")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

        after = plan(con)
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        fk = [tuple(row) for row in con.execute("PRAGMA foreign_key_check").fetchmany(10)]
        if not after["already_current"] or quick != "ok" or fk:
            raise RuntimeError(
                f"post-migration verification failed: plan={after}, quick={quick}, fk={fk}")
        payload.update(mode="apply", database_commit="committed", result="applied",
                       after=after, quick_check=quick, foreign_key_violations=fk)
        print(json.dumps(payload, indent=1, sort_keys=True))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.report.with_suffix(args.report.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")
            temporary.replace(args.report)
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
