#!/usr/bin/env python3
"""Backfill the independent registry scope contract on document observations.

The eLibrary/LNG adapters historically stored a detailed legal/facility scope
in ``observations.scope`` but left ``scope_rule`` empty.  Coverage obligations
carry the registry rule, so comparing the two different concepts made valid
document observations impossible to match.  This migration copies only the
current registry rule into ``scope_rule``.  It never changes the source-backed
actual scope, value, period, unit, quality state, or provenance.

Dry-run is the default.  ``--apply`` uses one ``BEGIN IMMEDIATE`` transaction,
refuses unknown metric identifiers or blank registry rules, verifies that the
actual-scope population is byte-for-byte unchanged, and is idempotent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import uuid


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from ferclib.registry import BY_ID, REGISTRY_VERSION  # noqa: E402


SOURCE_REGIME = "eLibrary document"


def _atomic_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _scope_digest(con: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for observation_id, scope in con.execute(
            "SELECT observation_id,scope FROM observations "
            "WHERE source_regime=? ORDER BY observation_id", (SOURCE_REGIME,)):
        digest.update(str(observation_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(scope).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def plan(con: sqlite3.Connection) -> dict:
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "observations" not in tables:
        raise ValueError("not an operating-assets database: observations table absent")
    columns = {row[1] for row in con.execute('PRAGMA table_xinfo("observations")')}
    required = {"observation_id", "metric_id", "source_regime", "scope", "scope_rule"}
    missing_columns = sorted(required - columns)
    if missing_columns:
        raise ValueError("observations missing columns: " + ",".join(missing_columns))

    rows = list(con.execute(
        "SELECT metric_id,COUNT(*) FROM observations WHERE source_regime=? "
        "AND TRIM(COALESCE(scope_rule,''))='' GROUP BY metric_id ORDER BY metric_id",
        (SOURCE_REGIME,),
    ))
    unknown_metrics = sorted(metric_id for metric_id, _count in rows
                             if metric_id not in BY_ID)
    blank_rules = sorted(metric_id for metric_id, _count in rows
                         if metric_id in BY_ID and not BY_ID[metric_id].scope.strip())
    targeted = {metric_id: count for metric_id, count in rows
                if metric_id in BY_ID and BY_ID[metric_id].scope.strip()}
    document_rows = int(con.execute(
        "SELECT COUNT(*) FROM observations WHERE source_regime=?", (SOURCE_REGIME,)
    ).fetchone()[0])
    populated_rows = int(con.execute(
        "SELECT COUNT(*) FROM observations WHERE source_regime=? "
        "AND TRIM(COALESCE(scope_rule,''))<>''", (SOURCE_REGIME,)
    ).fetchone()[0])
    return {
        "source_regime": SOURCE_REGIME,
        "registry_version": REGISTRY_VERSION,
        "document_observations": document_rows,
        "scope_rule_already_populated": populated_rows,
        "scope_rule_to_backfill": sum(targeted.values()),
        "target_metrics": targeted,
        "unknown_metrics": unknown_metrics,
        "blank_registry_rules": blank_rules,
        "actual_scope_digest": _scope_digest(con),
        "problems": ([f"unknown metric: {metric_id}" for metric_id in unknown_metrics] +
                     [f"blank registry scope rule: {metric_id}" for metric_id in blank_rules]),
        "already_current": not rows,
    }


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
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            print("migration refused: preflight quick_check failed", file=sys.stderr)
            return 2
        before = plan(con)
        payload = {
            "schema": "ferc-migration-004-result-v1",
            "database": str(args.db.resolve()),
            "before": before,
            "mode": "dry_run",
            "database_commit": "unchanged",
        }
        if before["problems"]:
            payload["result"] = "refused_unmapped_scope_contract"
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 2
        if not args.apply:
            payload["result"] = "already_current" if before["already_current"] else "ready"
            print(json.dumps(payload, indent=1, sort_keys=True))
            return 0
        if before["already_current"]:
            payload.update(mode="already_current", result="verified_noop")
            print(json.dumps(payload, indent=1, sort_keys=True))
            if args.report:
                _atomic_json(args.report, payload)
            return 0

        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        try:
            changed = 0
            for metric_id in sorted(before["target_metrics"]):
                cursor = con.execute(
                    "UPDATE observations SET scope_rule=? WHERE source_regime=? "
                    "AND metric_id=? AND TRIM(COALESCE(scope_rule,''))=''",
                    (BY_ID[metric_id].scope, SOURCE_REGIME, metric_id),
                )
                changed += int(cursor.rowcount)
            after = plan(con)
            if after["problems"] or not after["already_current"]:
                raise RuntimeError(f"post-migration scope plan incomplete: {after}")
            if before["actual_scope_digest"] != after["actual_scope_digest"]:
                raise RuntimeError("actual source-backed scope changed during migration")
            if changed != before["scope_rule_to_backfill"]:
                raise RuntimeError(
                    f"updated {changed} rows; planned {before['scope_rule_to_backfill']}")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = [tuple(row) for row in
                        con.execute("PRAGMA foreign_key_check").fetchmany(10)]
        if quick != "ok" or foreign_keys:
            raise RuntimeError(
                f"post-migration integrity failed: quick={quick}, foreign_keys={foreign_keys}")
        payload.update(
            mode="apply", result="applied", database_commit="committed",
            rows_updated=changed, after=after, quick_check=quick,
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
