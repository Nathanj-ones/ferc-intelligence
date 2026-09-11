#!/usr/bin/env python3
"""Upgrade an audited operating-assets database to the integrated schema.

The command is dry-run by default. ``--apply`` performs one bounded SQLite
transaction which:

* adds the nullable observation ``scope_rule`` metadata column;
* installs the four run/publication tables and their two indexes;
* makes ``source_manifest.content_hash`` genuinely ``TEXT NOT NULL`` while
  preserving every legacy manifest row; and
* removes retrieval timestamps incorrectly stored as regulatory snapshot dates
  on Data.FERC Form 549D filing occurrences.

The migration validates column declarations, defaults, primary keys, table
constraints and index definitions. It refuses an unknown shape instead of
silently declaring a similarly named object current. It is idempotent: a
second application is a verified no-op.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import re
import sqlite3
import sys


HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent
SCHEMA = PKG / "ferclib" / "schema.sql"

ADD_COLUMNS = [("observations", "scope_rule", "TEXT")]

TABLE_SQL = {
    "run_unit_status": """
        CREATE TABLE run_unit_status (
          run_id TEXT NOT NULL, adapter TEXT NOT NULL, entity_cid TEXT NOT NULL,
          scope_key TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL,
          detail TEXT, recorded_at TEXT NOT NULL,
          PRIMARY KEY (run_id,adapter,entity_cid,scope_key,seq))""",
    "run_input_inventory": """
        CREATE TABLE run_input_inventory (
          run_id TEXT NOT NULL, adapter TEXT NOT NULL, entity_cid TEXT NOT NULL,
          input_order INTEGER NOT NULL, source_system TEXT, filing_id TEXT,
          accession_number TEXT, content_hash TEXT, submitted_on TEXT, form TEXT,
          reporting_year INTEGER, reporting_period TEXT, snapshot_date TEXT,
          version_status TEXT, is_canonical INTEGER, observed_at TEXT NOT NULL,
          PRIMARY KEY (run_id,adapter,entity_cid,input_order))""",
    "unit_commits": """
        CREATE TABLE unit_commits (
          run_id TEXT NOT NULL, adapter TEXT NOT NULL, entity_cid TEXT NOT NULL,
          scope_key TEXT NOT NULL, identity_json TEXT NOT NULL,
          input_digest TEXT NOT NULL, observation_count INTEGER NOT NULL,
          edge_count INTEGER NOT NULL, document_fact_count INTEGER NOT NULL,
          committed_at TEXT NOT NULL,
          PRIMARY KEY (run_id,adapter,entity_cid,scope_key))""",
    "publication_generations": """
        CREATE TABLE publication_generations (
          generation_id TEXT PRIMARY KEY, run_id TEXT, code_snapshot TEXT NOT NULL,
          input_snapshot TEXT NOT NULL, database_identity TEXT NOT NULL,
          manifest_json TEXT NOT NULL, status TEXT NOT NULL,
          published_at TEXT NOT NULL)""",
}

INDEX_SQL = {
    "ix_run_unit_status_current":
        "CREATE INDEX ix_run_unit_status_current ON "
        "run_unit_status(run_id,state,adapter,entity_cid)",
    "ix_run_inputs_filing":
        "CREATE INDEX ix_run_inputs_filing ON "
        "run_input_inventory(source_system,filing_id,run_id)",
}

BASE_TABLES = ("observations", "source_manifest", "filings")
SOURCE_MANIFEST_TEMP = "source_manifest__m002_new"
EXIT_REPORT_UNAVAILABLE = 3


def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"unsafe SQLite identifier: {name!r}")
    return '"' + name + '"'


def _objects(con: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({_ident(table)})")}


def objects(con: sqlite3.Connection, kind: str) -> set[str]:
    """Backward-compatible public wrapper used by audit harnesses."""
    return _objects(con, kind)


def _normal_type(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().upper())


def _normal_default(value):
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value).strip())


def _column_contract(con: sqlite3.Connection, table: str) -> list[dict]:
    out = []
    for row in con.execute(f"PRAGMA table_xinfo({_ident(table)})"):
        out.append({
            "name": row[1], "type": _normal_type(row[2]),
            "notnull": int(row[3]), "default": _normal_default(row[4]),
            "pk": int(row[5]), "hidden": int(row[6]),
        })
    return out


def _foreign_key_contract(con: sqlite3.Connection, table: str) -> list[tuple]:
    return sorted(tuple(r[2:8]) for r in con.execute(
        f"PRAGMA foreign_key_list({_ident(table)})"))


def _unique_contract(con: sqlite3.Connection, table: str) -> list[tuple]:
    result = []
    for row in con.execute(f"PRAGMA index_list({_ident(table)})"):
        # Explicit non-unique indexes are checked by name below. Here we also
        # enforce table-level UNIQUE and PRIMARY KEY structures that are not
        # fully represented by PRAGMA table_xinfo.
        if row[3] not in ("u", "pk"):
            continue
        keys = tuple(r[2] for r in con.execute(
            f"PRAGMA index_xinfo({_ident(row[1])})") if int(r[5]))
        result.append((row[3], int(row[2]), int(row[4]), keys))
    return sorted(result)


def _table_flags(con: sqlite3.Connection, table: str) -> tuple[int, int]:
    try:
        row = con.execute(
            "SELECT wr,strict FROM pragma_table_list WHERE schema='main' AND name=?",
            (table,)).fetchone()
    except sqlite3.Error:  # pragma_table_list predates the minimum tested runtime
        return (0, 0)
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _check_contract(con: sqlite3.Connection, table: str) -> tuple[str, ...]:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    sql = row[0] if row and row[0] else ""
    # No table governed by migration 002 declares a CHECK. Counting the tokens
    # (rather than a flat-parenthesis regex) still detects nested expressions
    # such as CHECK(length(status) > 0), which the latter would miss.
    return tuple("check" for _ in re.finditer(r"\bCHECK\s*\(", sql, re.I))


def _table_comparison(actual: sqlite3.Connection, expected: sqlite3.Connection,
                      table: str, *, ignored_columns: set[str] | None = None) -> dict:
    ignored = ignored_columns or set()
    acols = [c for c in _column_contract(actual, table) if c["name"] not in ignored]
    ecols = [c for c in _column_contract(expected, table) if c["name"] not in ignored]
    amap = {c["name"]: c for c in acols}
    emap = {c["name"]: c for c in ecols}
    missing = sorted(set(emap) - set(amap))
    extra = sorted(set(amap) - set(emap))
    declarations = []
    for name in sorted(set(amap) & set(emap)):
        if amap[name] != emap[name]:
            declarations.append({"column": name, "expected": emap[name],
                                 "actual": amap[name]})
    order_expected = [c["name"] for c in ecols]
    order_actual = [c["name"] for c in acols]
    structural = []
    for label, avalue, evalue in (
        ("foreign_keys", _foreign_key_contract(actual, table),
         _foreign_key_contract(expected, table)),
        ("unique_and_primary_indexes", _unique_contract(actual, table),
         _unique_contract(expected, table)),
        ("table_flags", _table_flags(actual, table), _table_flags(expected, table)),
        ("check_constraints", _check_contract(actual, table),
         _check_contract(expected, table)),
    ):
        if avalue != evalue:
            structural.append({"part": label, "expected": evalue, "actual": avalue})
    return {
        "missing": missing, "extra": extra, "declarations": declarations,
        "structural": structural,
        "order_difference": None if order_actual == order_expected else {
            "expected": order_expected, "actual": order_actual},
    }


def _normal_sql(sql: str | None) -> str:
    value = (sql or "").lower().replace('"', "").replace("`", "")
    value = re.sub(r"\bif\s+not\s+exists\b", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*([(),])\s*", r"\1", value)
    return value


def _index_contract(con: sqlite3.Connection, name: str) -> dict | None:
    master = con.execute(
        "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?",
        (name,)).fetchone()
    if master is None:
        return None
    listed = None
    for row in con.execute(f"PRAGMA index_list({_ident(master[0])})"):
        if row[1] == name:
            listed = row
            break
    if listed is None:
        return None
    keys = []
    for row in con.execute(f"PRAGMA index_xinfo({_ident(name)})"):
        if int(row[5]):
            keys.append({"name": row[2], "descending": int(row[3]),
                         "collation": row[4]})
    return {"table": master[0], "unique": int(listed[2]),
            "origin": listed[3], "partial": int(listed[4]),
            "keys": keys, "sql": _normal_sql(master[1])}


def _reference() -> sqlite3.Connection:
    ref = sqlite3.connect(":memory:")
    # schema.sql's journal pragma is harmless on an in-memory reference. Its
    # stdout is suppressed so planning remains one machine-readable document.
    with contextlib.redirect_stdout(io.StringIO()):
        ref.executescript(SCHEMA.read_text(encoding="utf-8"))
    return ref


def _valid_sha256(value) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))


def plan(con: sqlite3.Connection) -> dict:
    tables = _objects(con, "table")
    absent_base = [name for name in BASE_TABLES if name not in tables]
    if absent_base:
        raise ValueError("not an operating-assets database; required table(s) absent: "
                         + ", ".join(absent_base))

    ref = _reference()
    try:
        problems: list[dict] = []
        order_differences: dict[str, dict] = {}
        missing_columns = []

        # Validate full pre-existing tables, ignoring only the column this
        # migration is specifically authorized to add/rebuild.
        for table, ignored in (("observations", {"scope_rule"}),
                               ("source_manifest", {"content_hash"})):
            cmp = _table_comparison(con, ref, table, ignored_columns=ignored)
            if cmp["missing"] or cmp["extra"] or cmp["declarations"] or cmp["structural"]:
                problems.append({"object": table, "kind": "table_structure",
                                 "detail": cmp})

        obs_columns = {c["name"]: c for c in _column_contract(con, "observations")}
        expected_obs = {c["name"]: c for c in _column_contract(ref, "observations")}
        if "scope_rule" not in obs_columns:
            missing_columns.append(("observations", "scope_rule", "TEXT"))
        elif obs_columns["scope_rule"] != expected_obs["scope_rule"]:
            problems.append({"object": "observations.scope_rule",
                             "kind": "column_declaration",
                             "expected": expected_obs["scope_rule"],
                             "actual": obs_columns["scope_rule"]})

        sm_columns = {c["name"]: c for c in _column_contract(con, "source_manifest")}
        expected_sm = {c["name"]: c for c in _column_contract(ref, "source_manifest")}
        sm_rebuild = False
        if "content_hash" not in sm_columns:
            missing_columns.append(("source_manifest", "content_hash", "TEXT NOT NULL"))
            sm_rebuild = True
        elif sm_columns["content_hash"] != expected_sm["content_hash"]:
            # The first version of migration 002 added nullable TEXT. That is
            # a known, safely repairable shape; unknown types/PK status are not.
            actual = sm_columns["content_hash"]
            if actual["type"] == "TEXT" and actual["pk"] == 0 and actual["hidden"] == 0:
                sm_rebuild = True
            else:
                problems.append({"object": "source_manifest.content_hash",
                                 "kind": "column_declaration",
                                 "expected": expected_sm["content_hash"],
                                 "actual": actual})

        if sm_rebuild:
            hash_rows = con.execute(
                "SELECT cache_key,content_hash FROM source_manifest"
                if "content_hash" in sm_columns else
                "SELECT cache_key,NULL AS content_hash FROM source_manifest"
            ).fetchall()
            bad = []
            for cache_key, content_hash in hash_rows:
                # NULL/empty is the known first-migration defect and can be
                # recovered from the audited legacy cache key. A non-empty but
                # malformed purported content hash is different: do not hide it
                # by silently substituting some other identity.
                value = cache_key if content_hash in (None, "") else content_hash
                if not _valid_sha256(value):
                    bad.append(cache_key)
            if bad:
                problems.append({"object": "source_manifest.content_hash",
                                 "kind": "unrecoverable_values", "count": len(bad),
                                 "sample_cache_keys": bad[:5]})
            attached = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name='source_manifest' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL")]
            if attached:
                problems.append({"object": "source_manifest",
                                 "kind": "attached_schema_objects",
                                 "names": attached})
            inbound = []
            for table in sorted(tables):
                if table.startswith("sqlite_"):
                    continue
                for row in con.execute(f"PRAGMA foreign_key_list({_ident(table)})"):
                    if row[2] == "source_manifest":
                        inbound.append({"table": table, "from": row[3], "to": row[4]})
            if inbound:
                problems.append({"object": "source_manifest",
                                 "kind": "inbound_foreign_keys", "detail": inbound})
            if SOURCE_MANIFEST_TEMP in tables:
                problems.append({"object": SOURCE_MANIFEST_TEMP,
                                 "kind": "reserved_temp_table_exists"})

        missing_tables = [name for name in TABLE_SQL if name not in tables]
        for table in TABLE_SQL:
            if table not in tables:
                continue
            cmp = _table_comparison(con, ref, table)
            if cmp["order_difference"]:
                order_differences[table] = cmp["order_difference"]
            if cmp["missing"] or cmp["extra"] or cmp["declarations"] or cmp["structural"]:
                problems.append({"object": table, "kind": "table_structure",
                                 "detail": cmp})

        indexes = _objects(con, "index")
        missing_indexes = [name for name in INDEX_SQL if name not in indexes]
        for name in INDEX_SQL:
            if name in indexes:
                actual = _index_contract(con, name)
                expected = _index_contract(ref, name)
                if actual != expected:
                    problems.append({"object": name, "kind": "index_definition",
                                     "expected": expected, "actual": actual})

        # A column appended by ALTER TABLE is logically equivalent but cannot
        # occupy schema.sql's middle position without rebuilding the full table.
        # Record that safe physical-order difference; never rebuild observations
        # merely to make PRAGMA output look identical.
        if "scope_rule" in obs_columns and not any(
                p["object"].startswith("observations") for p in problems):
            actual_order = [c["name"] for c in _column_contract(con, "observations")]
            expected_order = [c["name"] for c in _column_contract(ref, "observations")]
            if actual_order != expected_order:
                order_differences["observations"] = {
                    "expected": expected_order, "actual": actual_order,
                    "accepted_reason": ("SQLite ALTER TABLE appends a nullable column; "
                                        "all reads/writes use named columns")}

        timestamp_rows = con.execute(
            "SELECT COUNT(*) FROM filings WHERE source_system='DataFERC' "
            "AND form='Form 549D' AND snapshot_date IS NOT NULL"
        ).fetchone()[0]
        actions = bool(missing_columns or missing_tables or missing_indexes or
                       sm_rebuild or timestamp_rows)
        return {
            "missing_columns": missing_columns,
            "missing_tables": missing_tables,
            "missing_indexes": missing_indexes,
            "rebuild_source_manifest": sm_rebuild,
            "clear_dataferc_549d_snapshot_dates": int(timestamp_rows),
            "column_order_differences": order_differences,
            "problems": problems,
            "already_current": not actions and not problems,
        }
    finally:
        ref.close()


def _source_manifest_create_sql(con: sqlite3.Connection) -> str:
    ref = _reference()
    try:
        row = ref.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_manifest'"
        ).fetchone()
        if not row or not row[0]:
            raise RuntimeError("clean schema has no source_manifest definition")
        return re.sub(r"^(CREATE\s+TABLE\s+)source_manifest\b",
                      rf"\1{SOURCE_MANIFEST_TEMP}", row[0], count=1, flags=re.I)
    finally:
        ref.close()


def _rebuild_source_manifest(con: sqlite3.Connection) -> None:
    before = con.execute("SELECT COUNT(*) FROM source_manifest").fetchone()[0]
    existing = columns(con, "source_manifest")
    con.execute(_source_manifest_create_sql(con))
    expected = [c["name"] for c in _column_contract(con, SOURCE_MANIFEST_TEMP)]
    select = []
    for name in expected:
        if name == "content_hash":
            if "content_hash" in existing:
                select.append("CASE WHEN content_hash IS NULL OR content_hash='' "
                              "THEN cache_key ELSE content_hash END")
            else:
                select.append("cache_key")
        else:
            select.append(_ident(name))
    con.execute(
        f"INSERT INTO {_ident(SOURCE_MANIFEST_TEMP)} "
        f"({','.join(_ident(c) for c in expected)}) "
        f"SELECT {','.join(select)} FROM source_manifest")
    copied = con.execute(
        f"SELECT COUNT(*) FROM {_ident(SOURCE_MANIFEST_TEMP)}").fetchone()[0]
    if copied != before:
        raise RuntimeError(f"source_manifest copy count changed: {before} -> {copied}")
    con.execute("DROP TABLE source_manifest")
    con.execute(f"ALTER TABLE {_ident(SOURCE_MANIFEST_TEMP)} RENAME TO source_manifest")


def write_report(path: pathlib.Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _finish_report(path: pathlib.Path | None, payload: dict, *, committed: bool) -> int:
    print(json.dumps(payload, indent=1, sort_keys=True))
    try:
        write_report(path, payload)
        return 0
    except OSError as exc:
        state = "COMMITTED" if committed else "UNCHANGED"
        print(
            f"report unavailable after database state {state}: "
            f"{type(exc).__name__}: {exc}. The complete result is printed above; "
            "the database transaction remains committed when state is COMMITTED.",
            file=sys.stderr)
        return EXIT_REPORT_UNAVAILABLE


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=pathlib.Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", type=pathlib.Path)
    args = ap.parse_args(argv)
    if not args.db.is_file():
        print(f"database absent: {args.db}", file=sys.stderr)
        return 2
    if args.report is not None and args.report.resolve() == args.db.resolve():
        print("migration failed: report path is the database path", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db, isolation_level=None)
    committed = False
    try:
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            print(f"preflight quick_check failed: {integrity}", file=sys.stderr)
            return 2
        before = plan(con)
        print(json.dumps(before, indent=1, sort_keys=True))
        if before["problems"]:
            print("migration refused: schema/data preflight found incompatible state",
                  file=sys.stderr)
            return 2
        if not args.apply:
            payload = {
                "database": str(args.db), "mode": "dry_run", "database_commit": "unchanged",
                "applied": None, "verified": before, "quick_check": integrity,
                "note": "read-only planning; no schema or data was changed",
            }
            return _finish_report(args.report, payload, committed=False)
        if before["already_current"]:
            payload = {
                "database": str(args.db), "mode": "already_current",
                "database_commit": "unchanged", "applied": {
                    "missing_columns": [], "missing_tables": [], "missing_indexes": [],
                    "rebuilt_source_manifest": False,
                    "cleared_dataferc_549d_snapshot_dates": 0},
                "verified": before, "quick_check": integrity,
                "note": "idempotent no-op; no schema or data was changed",
            }
            return _finish_report(args.report, payload, committed=False)

        con.execute("BEGIN IMMEDIATE")
        try:
            for table, column, declaration in before["missing_columns"]:
                if table == "source_manifest" and column == "content_hash":
                    continue  # installed with its clean NOT NULL declaration below
                con.execute(
                    f"ALTER TABLE {_ident(table)} ADD COLUMN {_ident(column)} {declaration}")
            if before["rebuild_source_manifest"]:
                _rebuild_source_manifest(con)
            for name in before["missing_tables"]:
                con.execute(TABLE_SQL[name])
            for name in before["missing_indexes"]:
                con.execute(INDEX_SQL[name])
            cleared = con.execute(
                "UPDATE filings SET snapshot_date=NULL "
                "WHERE source_system='DataFERC' AND form='Form 549D' "
                "AND snapshot_date IS NOT NULL"
            ).rowcount
            if cleared != before["clear_dataferc_549d_snapshot_dates"]:
                raise RuntimeError(
                    "DataFERC Form 549D repair population changed during migration: "
                    f"planned {before['clear_dataferc_549d_snapshot_dates']}, wrote {cleared}")
            after = plan(con)
            if not after["already_current"]:
                raise RuntimeError(f"migration verification incomplete: {after}")
            fk_problem = con.execute("PRAGMA foreign_key_check").fetchone()
            if fk_problem is not None:
                raise RuntimeError(f"foreign_key_check failed after migration: {fk_problem}")
            post_integrity = con.execute("PRAGMA quick_check").fetchone()[0]
            if post_integrity != "ok":
                raise RuntimeError(f"post-migration quick_check failed: {post_integrity}")
            con.execute("COMMIT")
            committed = True
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

        payload = {
            "database": str(args.db), "mode": "applied", "database_commit": "committed",
            "applied": {
                "missing_columns": before["missing_columns"],
                "missing_tables": before["missing_tables"],
                "missing_indexes": before["missing_indexes"],
                "rebuilt_source_manifest": before["rebuild_source_manifest"],
                "cleared_dataferc_549d_snapshot_dates": cleared,
            },
            "verified": after, "quick_check": post_integrity,
            "note": ("one committed generation: schema contract repaired and only "
                     "DataFERC/Form 549D retrieval timestamps cleared from snapshot_date; "
                     "all other filing/observation/provenance fields preserved"),
        }
        return _finish_report(args.report, payload, committed=True)
    except (sqlite3.Error, ValueError, RuntimeError) as exc:
        if committed:
            print(f"migration database COMMITTED; post-commit operation failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_REPORT_UNAVAILABLE
        print(f"migration failed before commit: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
