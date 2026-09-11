"""
The single integrating writer for the shared staging database.

Adapters never open the database. They return batches of plain dicts against the
contract in schema.sql, and this module commits them:

  * ONE writer, so concurrent workers cannot interleave writes;
  * ATOMIC per-filing transactions, so a failed download or a failed registry
    rule can never publish a partly-updated filing as complete;
  * IDEMPOTENT upserts on explicit uniqueness constraints, so the same input
    twice produces no duplicate facts, metrics or events;
  * DEPENDENCY INVALIDATION, so a revised filing restates only the observations
    that actually depend on it, with prior versions retained.

THE UNIT OF WORK (audit A01 clause 3)
--------------------------------------
The documented unit of work is **entity x adapter x window**. `commit_unit()`
is the only supported way to replace an adapter's canonical rows, and it is
staged: the complete new row set is built and validated first, and only then
does a single transaction remove what this unit no longer produces and install
what it does. There is no window in which the previous rows are gone and the new
ones are not yet there, so a fetch failure, a parse failure, a validation
failure, a process death or a failed commit all leave the last good rows exactly
as they were.

Pruning is bounded by the same window. A refresh of 2025 may only remove 2025
rows; 2024 and earlier are outside the unit and are never touched. Where a
derivation OUTSIDE the window genuinely depends on data that changed, it is
reached through `dependency_closure()` -- an explicit edge walk -- and marked
superseded, never deleted and never swept up by a broad year-blind delete.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
import uuid
import warnings
from contextlib import contextmanager

HERE = pathlib.Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"

#: Columns a canonical observation may never be missing. Checked while the rows
#: are still staged, so a malformed batch is refused before anything is removed.
REQUIRED_OBSERVATION_COLUMNS = (
    "observation_id", "entity_key", "metric_id", "source_regime", "period_basis",
    "scope", "availability", "origin", "method", "version_status", "validation")

#: Fields whose change makes a stored row a genuinely PRIOR version worth
#: archiving rather than an idempotent rewrite of the same answer.
_VERSION_SIGNIFICANT = (
    "value_text", "value_num", "normalized_iso", "source_system", "filing_id",
    "source_fact_id", "source_context_id", "document_id", "accession_number",
    "period_basis", "period_start", "period_end", "instant_date", "reporting_year",
    "reporting_period", "scope", "scope_rule", "unit", "method", "derivation",
    "version_status", "validation", "qa_flags", "review_status", "availability",
    "origin", "taxonomy_version", "registry_version", "applicability_version")

class StagedCommitRejected(RuntimeError):
    """A staged batch failed validation and was NOT committed.

    Raised before any destructive step. The previous good rows are still in
    place when this reaches the caller."""

    def __init__(self, unit: str, problems: list[str]):
        self.unit = unit
        self.problems = problems
        super().__init__(f"{unit}: staged batch rejected ({len(problems)} problem(s)): "
                         + "; ".join(problems[:8]))


class FilingEntityConflict(RuntimeError):
    """One occurrence was assigned to incompatible exclusive legal owners.

    Shared Commission/LNG occurrences must opt in explicitly and carry a
    many-to-many ``filing_entities`` population.  A normal source filing never
    becomes shared merely because two adapters happened to retrieve it.
    """


_FILING_ENTITY_ROLES = {
    "source_entity", "named_filer", "commission_docket_subject",
    "facility_subject", "compatibility_anchor",
}
_FILING_ENTITY_ROLE_PRIORITY = {
    "named_filer": 0,
    "source_entity": 1,
    "commission_docket_subject": 2,
    "facility_subject": 3,
    "compatibility_anchor": 9,
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _year_of(row: dict) -> int | None:
    """The year a canonical row belongs to, for windowing.

    `reporting_year` first because it is the filer's own statement of the
    reporting period. Falling back to the instant or the interval end covers
    rows whose grain is a date rather than a form year. A row with none of these
    is genuinely UNWINDOWED -- an as-of snapshot with no date at all -- and is
    never guessed at: see `commit_unit(prune_unwindowed=...)`.
    """
    y = row.get("reporting_year")
    if y not in (None, ""):
        try:
            return int(y)
        except (TypeError, ValueError):
            pass
    for k in ("instant_date", "period_end", "period_start"):
        v = row.get(k)
        if v and re.match(r"^\d{4}", str(v)):
            return int(str(v)[:4])
    return None


def observation_id(entity_key: str, metric_id: str, regime: str, basis: str,
                   start: str, end: str, instant: str, scope: str, unit: str,
                   method: str) -> str:
    """Deterministic ID over the full grain (schema rule 2). Re-running the same
    input regenerates the same ID, which is what makes refresh idempotent."""
    raw = "|".join([entity_key, metric_id, regime, basis, start or "", end or "",
                    instant or "", scope, unit or "", method])
    return "obs-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def slot_id(entity_key: str, metric_id: str, regime: str, basis: str,
            start: str, end: str, instant: str, scope: str) -> str:
    raw = "|".join([entity_key, metric_id, regime, basis, start or "", end or "",
                    instant or "", scope])
    return "slot-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Staging:
    """Owns the connection. Construct once in the lead process."""

    def __init__(self, path: pathlib.Path, *, create: bool = True,
                 readonly: bool = False, require_immutable: bool = False):
        """Open the staging database.

        `readonly=True` is a GENUINE read-only open: `mode=ro&immutable=1`, and
        not one pragma is issued. That matters because `PRAGMA journal_mode=WAL`
        is itself a write -- it rewrites the header and can touch the freelist --
        so the ordinary constructor modifies a database merely by opening it.
        Opening the protected delivered baseline that way drifted it by 12,288
        bytes with no change to any content, which is exactly the hazard this
        flag removes: an inspection path must leave no trace at all.

        It is the same failure this module's unit of work exists to prevent, one
        level down. An operation that presents itself as read-only -- take a
        snapshot, check freshness -- must not quietly mutate durable state.
        """
        self.path = pathlib.Path(path)
        self.readonly = bool(readonly)
        self.readonly_immutable = False
        if self.readonly:
            # `immutable=1` is the strongest form -- SQLite is told the file
            # cannot change and skips the WAL entirely, so not even a shared
            # memory file is created. It is also fragile on a WAL database:
            # while another process holds a connection, opening immutable can
            # fail outright ("disk I/O error"), and the delivered baseline IS a
            # WAL database. So it is attempted first and `mode=ro` is the
            # fallback, which has been measured not to alter the main database
            # file either -- it only creates the -shm/-wal sidecars. Neither
            # path issues a single pragma.
            try:
                self.con = sqlite3.connect(
                    f"file:{self.path}?mode=ro&immutable=1", uri=True, timeout=120)
                self.con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                self.readonly_immutable = True
            except sqlite3.Error as exc:
                if require_immutable:
                    raise
                # Loud, never silent: the caller asked for the strongest
                # read-only open and did not get it, and the difference is
                # observable (mode=ro creates -shm/-wal sidecars beside a WAL
                # database, though it leaves the database file itself byte
                # identical).
                print(f"[staging] immutable read-only open of {self.path} failed "
                      f"({exc}); falling back to mode=ro. The database file is "
                      "still not modified, but -shm/-wal sidecars may be created.",
                      file=sys.stderr)
                self.con = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, timeout=120)
            self.con.row_factory = sqlite3.Row
            self._lock = threading.RLock()
            self.run_id = None
            self._log_seq = 0
            self._recovery_done = True
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), timeout=120, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.execute("PRAGMA journal_mode = WAL")
        # WAL lets readers run while one writer commits, but two writers still
        # serialise. busy_timeout makes the loser WAIT rather than fail, which is
        # what we want: a slow batch is fine, a lost batch is not.
        self.con.execute("PRAGMA busy_timeout = 120000")
        self.con.execute("PRAGMA synchronous = NORMAL")
        # RLock is intentional. A publication transaction may emit a run-log
        # row before it commits; log() must join that transaction rather than
        # deadlock trying to reacquire the single-writer lock.
        self._lock = threading.RLock()
        self._savepoint_seq = 0
        self.run_id: str | None = None
        self._log_seq = 0
        self._recovery_done = False
        if create:
            self._apply_schema()

    # ------------------------------------------------------------- schema

    def _apply_schema(self) -> None:
        """Apply the clean schema only when every existing object is compatible.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` is silent when an old table has
        the right name and the wrong shape.  The previous implementation only
        inspected that shape *after* ``executescript`` raised ``no such
        column``.  A drift that did not happen to feed an index therefore passed
        unnoticed.  Here we compare existing tables against an in-memory clean
        schema before running any DDL, including types, NOT NULL declarations,
        defaults, primary/unique keys, foreign keys, table flags and named index
        definitions.

        Missing tables and indexes are normal on a new/partially initialized
        database: schema.sql creates them.  A missing or differently declared
        column on an existing table requires the deliberate migration path.
        Physical column-order differences caused by SQLite's additive ALTER are
        recorded in ``schema_column_order_differences`` but accepted; supported
        reads and writes use named columns, and rebuilding a populated table just
        to move a column is unnecessary risk.
        """
        sql = SCHEMA.read_text(encoding="utf-8")
        before = self._schema_contract_report(sql)
        populated_missing_associations = False
        if "filing_entities" in before["missing_tables"]:
            existing = {row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "filings" in existing:
                populated_missing_associations = bool(
                    self.con.execute("SELECT 1 FROM filings LIMIT 1").fetchone())
        if (before["missing_columns"] or before["incompatible"]
                or populated_missing_associations):
            details = []
            for table, cols in sorted(before["missing_columns"].items()):
                details.append(f"{table} missing {','.join(cols)}")
            details.extend(before["incompatible"][:12])
            m002_columns = {("observations", "scope_rule"),
                            ("source_manifest", "content_hash")}
            m003_columns = {("asset_dockets", "evidence_ref")}
            older_drift = any((table, col) not in m002_columns | m003_columns
                              for table, cols in before["missing_columns"].items()
                              for col in cols)
            migration_order = (
                "  python3 migrations/001_repair_2026_09_08.py --db "
                f"{self.path} --apply\n" if older_drift else "")
            auto = os.environ.get("FERC_SCHEMA_AUTOMIGRATE", "").strip()
            auto_note = (f" FERC_SCHEMA_AUTOMIGRATE={auto!r} is intentionally ignored;"
                         " constrained/data-bearing changes require an auditable migration."
                         if auto else "")
            raise sqlite3.OperationalError(
                f"{self.path} does not match current ferclib/schema.sql: "
                + "; ".join(details) + ". Run the dry-run first, then apply:\n"
                "  python3 migrations/002_integrated_repair_2026_09_09.py --db "
                f"{self.path}\n"
                + migration_order
                + "  python3 migrations/002_integrated_repair_2026_09_09.py --db "
                f"{self.path} --apply\n"
                + "  python3 migrations/003_filing_entity_associations_2026_09_10.py --db "
                f"{self.path}\n"
                + "  python3 migrations/003_filing_entity_associations_2026_09_10.py --db "
                f"{self.path} --apply\n"
                + auto_note + " Never delete a database to make schema drift disappear."
            )

        self.con.executescript(sql)
        after = self._schema_contract_report(sql)
        if (after["missing_tables"] or after["missing_columns"] or
                after["missing_indexes"] or after["incompatible"]):
            raise sqlite3.OperationalError(
                f"schema.sql did not establish its declared contract for {self.path}: "
                f"{json.dumps(after, sort_keys=True)}")
        self.schema_column_order_differences = after["column_order_differences"]
        for table in sorted(self.schema_column_order_differences):
            print(f"[staging] accepted named-column order difference in {table}; "
                  "no table rebuild was performed", file=sys.stderr)

    @staticmethod
    def _schema_column_contract(con: sqlite3.Connection, table: str) -> list[dict]:
        return [{"name": r[1], "type": re.sub(r"\s+", " ", (r[2] or "").upper()),
                 "notnull": int(r[3]), "default": r[4], "pk": int(r[5]),
                 "hidden": int(r[6])}
                for r in con.execute(f'PRAGMA table_xinfo("{table}")')]

    @staticmethod
    def _schema_unique_contract(con: sqlite3.Connection, table: str) -> list[tuple]:
        result = []
        for row in con.execute(f'PRAGMA index_list("{table}")'):
            if row[3] not in ("u", "pk"):
                continue
            keys = tuple(r[2] for r in con.execute(
                f'PRAGMA index_xinfo("{row[1]}")') if int(r[5]))
            result.append((row[3], int(row[2]), int(row[4]), keys))
        return sorted(result)

    @staticmethod
    def _schema_fk_contract(con: sqlite3.Connection, table: str) -> list[tuple]:
        return sorted(tuple(r[2:8]) for r in con.execute(
            f'PRAGMA foreign_key_list("{table}")'))

    @staticmethod
    def _schema_table_flags(con: sqlite3.Connection, table: str) -> tuple[int, int]:
        try:
            row = con.execute(
                "SELECT wr,strict FROM pragma_table_list "
                "WHERE schema='main' AND name=?", (table,)).fetchone()
        except sqlite3.Error:
            return (0, 0)
        return (int(row[0]), int(row[1])) if row else (0, 0)

    @staticmethod
    def _schema_check_contract(con: sqlite3.Connection, table: str) -> tuple[str, ...]:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        sql_text = row[0] if row and row[0] else ""
        # The current schema has no CHECK clauses. Token counting deliberately
        # catches nested forms such as CHECK(length(value) > 0), which a flat
        # parenthesis regex would miss.
        return tuple("check" for _ in re.finditer(r"\bCHECK\s*\(", sql_text, re.I))

    @staticmethod
    def _schema_index_contract(con: sqlite3.Connection, name: str) -> dict | None:
        row = con.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,)).fetchone()
        if row is None:
            return None
        listing = next((r for r in con.execute(f'PRAGMA index_list("{row[0]}")')
                        if r[1] == name), None)
        if listing is None:
            return None
        keys = tuple((r[2], int(r[3]), r[4]) for r in con.execute(
            f'PRAGMA index_xinfo("{name}")') if int(r[5]))
        sql_text = (row[1] or "").lower().replace('"', "").replace("`", "")
        sql_text = re.sub(r"\bif\s+not\s+exists\b", "", sql_text)
        sql_text = re.sub(r"\s+", " ", sql_text).strip()
        sql_text = re.sub(r"\s*([(),])\s*", r"\1", sql_text)
        return {"table": row[0], "unique": int(listing[2]),
                "origin": listing[3], "partial": int(listing[4]),
                "keys": keys, "sql": sql_text}

    def _schema_contract_report(self, sql: str) -> dict:
        """Compare extant objects with a clean in-memory schema contract.

        Missing whole objects are returned separately because schema.sql can
        create them safely. Extra user indexes are accepted. Existing tables
        with missing/extra/retyped columns or changed key structure are not.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(sql)
            expected_tables = {r[0] for r in ref.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
            actual_tables = {r[0] for r in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
            report = {"missing_tables": sorted(expected_tables - actual_tables),
                      "missing_columns": {}, "missing_indexes": [],
                      "column_order_differences": {}, "incompatible": []}
            for table in sorted(expected_tables & actual_tables):
                expected = self._schema_column_contract(ref, table)
                actual = self._schema_column_contract(self.con, table)
                emap = {c["name"]: c for c in expected}
                amap = {c["name"]: c for c in actual}
                missing = sorted(set(emap) - set(amap))
                extra = sorted(set(amap) - set(emap))
                if missing:
                    report["missing_columns"][table] = missing
                if extra:
                    report["incompatible"].append(
                        f"{table} has undeclared columns {','.join(extra)}")
                for name in sorted(set(emap) & set(amap)):
                    if emap[name] != amap[name]:
                        report["incompatible"].append(
                            f"{table}.{name} declaration is {amap[name]}, expected {emap[name]}")
                expected_names = [c["name"] for c in expected if c["name"] in amap]
                actual_names = [c["name"] for c in actual if c["name"] in emap]
                if not missing and not extra and expected_names != actual_names:
                    report["column_order_differences"][table] = {
                        "expected": expected_names, "actual": actual_names}
                expected_fk = self._schema_fk_contract(ref, table)
                actual_fk = self._schema_fk_contract(self.con, table)
                if expected_fk != actual_fk:
                    report["incompatible"].append(
                        f"{table} foreign keys are {actual_fk}, expected {expected_fk}")
                expected_unique = self._schema_unique_contract(ref, table)
                actual_unique = self._schema_unique_contract(self.con, table)
                if expected_unique != actual_unique:
                    report["incompatible"].append(
                        f"{table} primary/unique keys are {actual_unique}, "
                        f"expected {expected_unique}")
                expected_flags = self._schema_table_flags(ref, table)
                actual_flags = self._schema_table_flags(self.con, table)
                if expected_flags != actual_flags:
                    report["incompatible"].append(
                        f"{table} table flags are {actual_flags}, expected {expected_flags}")
                expected_checks = self._schema_check_contract(ref, table)
                actual_checks = self._schema_check_contract(self.con, table)
                if expected_checks != actual_checks:
                    report["incompatible"].append(
                        f"{table} CHECK constraints are {actual_checks}, "
                        f"expected {expected_checks}")

            expected_indexes = {r[0] for r in ref.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
            actual_indexes = {r[0] for r in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
            report["missing_indexes"] = sorted(expected_indexes - actual_indexes)
            for name in sorted(expected_indexes & actual_indexes):
                expected = self._schema_index_contract(ref, name)
                actual = self._schema_index_contract(self.con, name)
                if expected != actual:
                    report["incompatible"].append(
                        f"index {name} is {actual}, expected {expected}")
            return report
        finally:
            ref.close()

    @classmethod
    def default_path(cls) -> pathlib.Path:
        """The staging database this process should use.

        `FERC_STAGING_DB` exists so that a worker can build a disposable
        database without any risk of writing the canonical one (repair contract
        rule 2). It is read here, not only in run.py, so every entry point --
        run.py, seed_annotations.py, a test harness -- resolves it identically.
        """
        env = os.environ.get("FERC_STAGING_DB", "").strip()
        if env:
            return pathlib.Path(env).expanduser()
        return HERE.parent / "staging" / "operating_assets.sqlite"

    # ------------------------------------------------------------- lifecycle

    def _refuse_write(self, what: str) -> None:
        """A read-only handle refuses in OUR words, before SQLite is asked.

        Relying on the driver's "attempt to write a readonly database" would
        still be safe, but a caller that meant to inspect a protected database
        deserves to be told which operation it was, not to discover it in a
        traceback three frames down."""
        if self.readonly:
            raise PermissionError(
                f"{self.path} is open read-only (mode=ro&immutable=1); "
                f"refusing to {what}. Open it without readonly=True if you really "
                "intend to write, and never against a protected baseline.")

    def start_run(self, mode: str, scope: dict, registry_version: str,
                  code_version: str) -> str:
        self._refuse_write("start a run")
        if not self._recovery_done:
            self.recover_incomplete_runs()
        rid = f"run-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.con.execute(
            "INSERT INTO runs(run_id,started_at,mode,scope_json,registry_version,code_version,status)"
            " VALUES(?,?,?,?,?,?,'running')",
            (rid, now(), mode, json.dumps(scope, sort_keys=True), registry_version, code_version))
        self.run_id = rid
        self._log_seq = 0
        return rid

    def finish_run(self, status: str = "complete", note: str = "") -> None:
        self._refuse_write("finish a run")
        if self.run_id:
            self.con.execute("UPDATE runs SET finished_at=?, status=?, note=? WHERE run_id=?",
                             (now(), status, note, self.run_id))

    def log(self, level: str, message: str, *, adapter: str = "", entity_cid: str = "") -> None:
        if self.readonly:
            return                 # a read-only inspection leaves no trace, not even a log line
        with self._lock:
            self._log_seq += 1
            seq = self._log_seq
        self.con.execute(
            "INSERT OR REPLACE INTO run_log(run_id,seq,ts,level,adapter,entity_cid,message)"
            " VALUES(?,?,?,?,?,?,?)",
            (self.run_id or "no-run", seq, now(), level, adapter, entity_cid, message[:2000]))

    @contextmanager
    def transaction(self, *, attempts: int = 5):
        """Atomic unit. A failure inside rolls the whole filing back rather than
        leaving a half-written filing that looks complete.

        Retries on lock contention with a backoff. Several adapters may run
        against one staging database, and a writer that gives up would leave that
        adapter's entity silently absent -- indistinguishable, later, from a
        FERC source gap. Waiting is always the right trade here.
        """
        self._refuse_write("open a write transaction")
        with self._lock:
            # Retrieval is now enclosed by one entity/adapter/window
            # transaction. Adapter helpers such as write_filing_bundle(),
            # checkpoint() and freeze_expected() already use transaction(), so
            # nesting must join the caller's durability boundary rather than
            # attempting another BEGIN. A distinct SAVEPOINT still gives each
            # helper its original all-or-nothing behaviour if its caller catches
            # an ordinary failure and continues inside the outer transaction.
            if self.con.in_transaction:
                self._savepoint_seq += 1
                savepoint = f"ferc_nested_{self._savepoint_seq}"
                self.con.execute(f'SAVEPOINT "{savepoint}"')
                try:
                    yield self.con
                except BaseException as original:
                    # ROLLBACK TO leaves the savepoint active; RELEASE is needed
                    # both to restore nesting depth and to make later nested
                    # calls unambiguous. The outer transaction will still roll
                    # back if either cleanup operation itself fails.
                    try:
                        self.con.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')
                        self.con.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                    except BaseException as cleanup:                 # pragma: no cover - rare I/O fault
                        try:
                            original.add_note(
                                f"SQLite savepoint rollback also failed: "
                                f"{type(cleanup).__name__}: {cleanup}")
                        except AttributeError:
                            pass
                    raise
                else:
                    try:
                        self.con.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                    except BaseException as original:
                        # A RELEASE failure must not leave work from this nested
                        # helper available for a later accidental commit.
                        try:
                            if self.con.in_transaction:
                                self.con.execute(
                                    f'ROLLBACK TO SAVEPOINT "{savepoint}"')
                                self.con.execute(
                                    f'RELEASE SAVEPOINT "{savepoint}"')
                        except BaseException as cleanup:             # pragma: no cover - rare I/O fault
                            try:
                                original.add_note(
                                    f"SQLite cleanup after savepoint RELEASE failure also failed: "
                                    f"{type(cleanup).__name__}: {cleanup}")
                            except AttributeError:
                                pass
                        raise
                return

            for attempt in range(1, attempts + 1):
                try:
                    self.con.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    if attempt == attempts:
                        raise
                    time.sleep(min(30.0, 2.0 ** attempt))
            try:
                yield self.con
            except BaseException as original:
                # Cancellation is not an Exception: KeyboardInterrupt,
                # SystemExit and injected process-control failures must roll
                # back just as ordinary adapter errors do. Never replace the
                # original exception with a cleanup error.
                if self.con.in_transaction:
                    try:
                        self.con.execute("ROLLBACK")
                    except BaseException as cleanup:                 # pragma: no cover - rare I/O fault
                        try:
                            original.add_note(
                                f"SQLite rollback also failed: {type(cleanup).__name__}: {cleanup}")
                        except AttributeError:
                            pass
                raise
            else:
                try:
                    self.con.execute("COMMIT")
                except BaseException as original:
                    # A failed COMMIT may leave SQLite inside the transaction.
                    # Explicit rollback releases the lock and prevents a later
                    # status write from accidentally committing the failed unit.
                    if self.con.in_transaction:
                        try:
                            self.con.execute("ROLLBACK")
                        except BaseException as cleanup:             # pragma: no cover - rare I/O fault
                            try:
                                original.add_note(
                                    f"SQLite rollback after COMMIT failure also failed: "
                                    f"{type(cleanup).__name__}: {cleanup}")
                            except AttributeError:
                                pass
                    raise

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------- generic

    @staticmethod
    def _upsert(con, table: str, rows: list[dict], keys: list[str]) -> int:
        """Insert-or-update a batch.

        THE COLUMN LIST IS THE UNION OVER EVERY ROW, not the keys of the first
        one. It used to be `list(rows[0])`, which silently dropped any key a
        later row had and the first did not -- from ALL rows, since one SQL
        statement is built once and reused by executemany.

        w3-financial found this the expensive way. Their set-based lineage edge
        is appended after the per-row edges, so `input_population_id` was absent
        from row 0 and was stripped from the whole batch: 41 populations wrote,
        41 edges wrote, and all 41 foreign keys came back NULL. That state is
        worse than not having the feature, because the data then claims set-based
        lineage while the link is missing -- and it silently disarms
        `validate_staged`, which cannot reject an edge naming an undefined
        population when the naming column is removed before it ever looks.

        Any adapter mixing row shapes was losing columns the same way, with
        nothing to show for it. The union also makes a MISTYPED key louder
        rather than quieter: it now reaches SQL and raises `no such column`,
        where before it vanished whenever row 0 happened not to carry it.

        Raggedness is legitimate (an aggregate edge really does carry a key the
        detail edges do not), so it warns rather than raises -- but it is never
        silent.
        """
        if not rows:
            return 0
        cols: list[str] = []
        for r in rows:
            for c in r:
                if c not in cols:
                    cols.append(c)
        if any(len(r) != len(cols) for r in rows):
            ragged = sorted({c for c in cols if any(c not in r for r in rows)})
            warnings.warn(
                f"{table}: ragged batch -- {len(rows)} rows do not all carry "
                f"{ragged}. Every column is written (missing values as NULL); "
                "before this fix they were dropped from the whole batch.",
                RuntimeWarning, stacklevel=2)
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f'"{c}"=excluded."{c}"' for c in cols if c not in keys)
        conflict = ",".join(f'"{k}"' for k in keys)
        sql = (f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in cols)}) '
               f"VALUES ({placeholders}) ON CONFLICT({conflict}) DO UPDATE SET {updates}"
               if updates else
               f'INSERT OR IGNORE INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in cols)}) '
               f"VALUES ({placeholders})")
        con.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
        return len(rows)

    def upsert(self, table: str, rows: list[dict], keys: list[str]) -> int:
        with self.transaction() as con:
            return self._upsert(con, table, rows, keys)

    # ------------------------------------------------------------- domain

    def write_entities(self, rows: list[dict]) -> int:
        return self.upsert("entities", rows, ["entity_key"])

    def write_assets(self, assets: list[dict], mappings: list[dict],
                     ownership: list[dict], dockets: list[dict]) -> None:
        with self.transaction() as con:
            self._upsert(con, "assets", assets, ["asset_id"])
            self._upsert(con, "asset_entity_map", mappings, ["asset_id", "entity_key"])
            self._upsert(con, "ownership", ownership, ["entity_key", "parent"])
            self._upsert(con, "asset_dockets", dockets, ["asset_id", "docket"])

    def write_reviewed_annotations(self, rows: list[dict]) -> int:
        """Upsert annotation metadata without resetting application counts.

        Counts describe the observations in the last-good database generation.
        Seeding a new run must not zero them before that run has successfully
        republished the relevant entity.
        """
        clean = [{k: v for k, v in row.items() if k != "applied_count"}
                 for row in rows]
        return self.upsert(
            "reviewed_source_annotations", clean,
            ["source_system", "entity_key", "filing_id", "source_fact_id",
             "metric_id"])

    def write_filing_bundle(self, filing: dict, *, facts: list[dict] | None = None,
                            contexts: list[dict] | None = None,
                            dimensions: list[dict] | None = None,
                            units: list[dict] | None = None,
                            documents: list[dict] | None = None,
                            filing_dockets: list[dict] | None = None,
                            filing_entities: list[dict] | None = None,
                            allow_shared_entities: bool = False) -> None:
        """One filing and everything parsed from it, committed atomically.

        ``filings.entity_key`` remains a compatibility anchor for existing
        consumers, but it is not an exclusive-ownership assertion.  Every new
        occurrence receives at least one row in ``filing_entities``.  A caller
        may publish several associations only by opting into the shared path;
        the final anchor is selected by source-role priority and entity key, so
        reversing adapter/entity replay order cannot change it.
        """
        filing = dict(filing)
        source_system = str(filing.get("source_system") or "")
        filing_id = str(filing.get("filing_id") or "")
        requested_owner = str(filing.get("entity_key") or "")
        if not source_system or not filing_id or not requested_owner:
            raise ValueError("filing bundle requires source_system, filing_id and entity_key")

        associations = [dict(row) for row in (filing_entities or [{
            "entity_key": requested_owner,
            "association_role": "source_entity",
            "facility_key": "",
            "evidence_ref": "adapter-declared source entity",
        }])]
        for row in associations:
            row.setdefault("source_system", source_system)
            row.setdefault("filing_id", filing_id)
            row.setdefault("facility_key", "")
            if row.get("source_system") != source_system or row.get("filing_id") != filing_id:
                raise ValueError("filing entity association names a different occurrence")
            if not row.get("entity_key") or row.get("association_role") not in \
                    _FILING_ENTITY_ROLES or not row.get("evidence_ref"):
                raise ValueError(f"invalid filing entity association: {row}")
        if requested_owner not in {str(row["entity_key"]) for row in associations}:
            raise ValueError("filing compatibility owner is absent from filing_entities")
        if not allow_shared_entities and len({row["entity_key"] for row in associations}) != 1:
            raise FilingEntityConflict(
                f"{source_system}/{filing_id}: multiple entities require explicit shared mode")

        with self.transaction() as con:
            existing = con.execute(
                "SELECT * FROM filings WHERE source_system=? AND filing_id=?",
                (source_system, filing_id)).fetchone()
            prior = [dict(row) for row in con.execute(
                "SELECT * FROM filing_entities WHERE source_system=? AND filing_id=?",
                (source_system, filing_id))]
            if existing is not None and existing["entity_key"] != requested_owner \
                    and not allow_shared_entities:
                raise FilingEntityConflict(
                    f"{source_system}/{filing_id}: existing owner "
                    f"{existing['entity_key']!r}, attempted owner {requested_owner!r}")
            if existing is not None and not prior:
                prior.append({
                    "source_system": source_system,
                    "filing_id": filing_id,
                    "entity_key": existing["entity_key"],
                    "association_role": "compatibility_anchor",
                    "facility_key": "",
                    "evidence_ref": "legacy filings.entity_key pending reviewed replay",
                })

            merged: dict[str, dict] = {}
            for row in prior + associations:
                entity_key = str(row["entity_key"])
                old = merged.get(entity_key)
                if old is None:
                    merged[entity_key] = dict(row)
                    continue
                old_facility = str(old.get("facility_key") or "")
                new_facility = str(row.get("facility_key") or "")
                if old_facility and new_facility and old_facility != new_facility:
                    raise FilingEntityConflict(
                        f"{source_system}/{filing_id}/{entity_key}: incompatible facility "
                        f"associations {old_facility!r} and {new_facility!r}")
                old_rank = _FILING_ENTITY_ROLE_PRIORITY[old["association_role"]]
                new_rank = _FILING_ENTITY_ROLE_PRIORITY[row["association_role"]]
                if new_rank < old_rank:
                    merged[entity_key] = dict(row)
                elif new_rank == old_rank:
                    # Same-strength assertions must not become replay-order
                    # dependent. Retain one stable evidence identity.
                    chosen = min(str(old["evidence_ref"]), str(row["evidence_ref"]))
                    old["evidence_ref"] = chosen
                    old["facility_key"] = old_facility or new_facility

            if not allow_shared_entities and len(merged) > 1:
                raise FilingEntityConflict(
                    f"{source_system}/{filing_id}: an exclusive occurrence already has "
                    f"associations for {sorted(merged)}")
            anchor = min(
                merged.values(),
                key=lambda row: (_FILING_ENTITY_ROLE_PRIORITY[row["association_role"]],
                                 str(row["entity_key"])),
            )["entity_key"]
            filing["entity_key"] = anchor

            if existing is not None:
                for field in ("form", "accession_number", "content_hash"):
                    before, after = existing[field], filing.get(field)
                    if before not in (None, "") and after not in (None, "") \
                            and before != after:
                        raise FilingEntityConflict(
                            f"{source_system}/{filing_id}: immutable {field} changed from "
                            f"{before!r} to {after!r}")
                    if after in (None, "") and before not in (None, ""):
                        filing[field] = before
                first_seen = sorted(value for value in
                                    (existing["first_seen_at"], filing.get("first_seen_at"))
                                    if value)
                if first_seen:
                    filing["first_seen_at"] = first_seen[0]
                retrieved = sorted(value for value in
                                   (existing["retrieved_at"], filing.get("retrieved_at"))
                                   if value)
                if retrieved:
                    filing["retrieved_at"] = retrieved[-1]

            self._upsert(con, "filings", [filing], ["source_system", "filing_id"])
            self._upsert(con, "filing_entities", list(merged.values()),
                         ["source_system", "filing_id", "entity_key"])
            self._upsert(con, "source_facts", facts or [],
                         ["source_system", "filing_id", "source_fact_id"])
            self._upsert(con, "source_contexts", contexts or [],
                         ["source_system", "filing_id", "context_id"])
            self._upsert(con, "source_dimensions", dimensions or [],
                         ["source_system", "filing_id", "context_id", "seq"])
            self._upsert(con, "source_units", units or [],
                         ["source_system", "filing_id", "unit_id"])
            document_rows = [dict(row) for row in (documents or [])]
            for document in document_rows:
                prior_document = con.execute(
                    "SELECT retrieved_at FROM documents WHERE document_id=?",
                    (document.get("document_id"),)).fetchone()
                retrieved = sorted(value for value in (
                    prior_document["retrieved_at"] if prior_document else None,
                    document.get("retrieved_at")) if value)
                if retrieved:
                    document["retrieved_at"] = retrieved[-1]
            self._upsert(con, "documents", document_rows, ["document_id"])

            # A ``|listing`` row is the durable record that an occurrence was
            # located in the source listing; it is not an attachment byte
            # object.  The same eLibrary occurrence can later be revisited by a
            # second entity/adapter path that retrieves an attachment instead
            # of rewriting that listing row.  In either replay order, the
            # listing must therefore converge on the filing occurrence's latest
            # captured retrieval time.  Restrict this propagation to the exact
            # placeholder identity with no attachment/content so a failed or
            # uncaptured real attachment can never be made to look retrieved.
            occurrence_retrieved = filing.get("retrieved_at")
            listing_id = f"{source_system}|{filing_id}|listing"
            if occurrence_retrieved:
                listing = con.execute(
                    "SELECT retrieved_at,attachment_id,content_hash FROM documents "
                    "WHERE document_id=?", (listing_id,)).fetchone()
                if listing is not None \
                        and not (listing["attachment_id"] or "").strip() \
                        and not (listing["content_hash"] or "").strip():
                    captures = [value for value in
                                (listing["retrieved_at"], occurrence_retrieved) if value]
                    latest = max(captures) if captures else None
                    if latest != listing["retrieved_at"]:
                        con.execute(
                            "UPDATE documents SET retrieved_at=? WHERE document_id=?",
                            (latest, listing_id))
            self._upsert(con, "filing_dockets", filing_dockets or [],
                         ["source_system", "filing_id", "docket"])

    def write_observations(self, observations: list[dict], edges: list[dict],
                           qa_events: list[dict] | None = None) -> None:
        """Observations and their lineage in one transaction: a derived value can
        never be visible without the edges that justify it."""
        stamp = now()
        for o in observations:
            o.setdefault("run_id", self.run_id)
            o.setdefault("first_seen_at", stamp)
            o["updated_at"] = stamp
        with self.transaction() as con:
            # Edges are rebuilt wholesale for the observations being written, so
            # a recomputed derivation cannot keep stale inputs.
            ids = [o["observation_id"] for o in observations]
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                con.execute(f"DELETE FROM lineage_edges WHERE observation_id IN "
                            f"({','.join('?' * len(chunk))})", chunk)
            self._upsert(con, "observations", observations, ["observation_id"])
            self._upsert(con, "lineage_edges", edges, ["observation_id", "input_order"])
            self._upsert(con, "events", qa_events or [], ["event_id"])

    # ------------------------------------------------- staged unit of work

    @staticmethod
    def validate_staged(unit: str, observations: list[dict], edges: list[dict],
                        *, entity_key: str, foreign_metric_ids=None,
                        populations: list[dict] | None = None) -> list[str]:
        """Structural checks on a staged batch, run BEFORE anything is removed.

        Deliberately about integrity, not about whether the numbers are good:
        a missing status dimension, a duplicated grain, an edge pointing at no
        row, or another entity's or another adapter's rows arriving inside this
        unit. Any of those would make the swap unsound, so the batch is refused
        and the previous rows stand.
        """
        problems: list[str] = []
        foreign = set(foreign_metric_ids or ())
        seen_ids: set[str] = set()
        seen_grain: dict[tuple, str] = {}
        for i, o in enumerate(observations):
            missing = [c for c in REQUIRED_OBSERVATION_COLUMNS
                       if o.get(c) in (None, "")]
            if missing:
                problems.append(f"row {i} ({o.get('observation_id', '?')}) missing "
                                f"{','.join(missing)}")
                continue
            oid = o["observation_id"]
            if oid in seen_ids:
                problems.append(f"duplicate observation_id {oid} in one batch")
            seen_ids.add(oid)
            if o["entity_key"] != entity_key:
                problems.append(f"row {oid} carries entity_key {o['entity_key']!r} "
                                f"but the unit of work is {entity_key!r}")
            if o["metric_id"] in foreign:
                problems.append(f"row {oid} writes metric {o['metric_id']!r}, which "
                                "belongs to a different adapter")
            grain = (o["entity_key"], o["metric_id"], o["source_regime"],
                     o["period_basis"], o.get("period_start") or "",
                     o.get("period_end") or "", o.get("instant_date") or "",
                     o["scope"], o.get("unit") or "", o["method"])
            if grain in seen_grain and seen_grain[grain] != oid:
                problems.append(f"two observation_ids ({seen_grain[grain]}, {oid}) "
                                "for one grain; the unique grain index would reject one")
            seen_grain[grain] = oid
        for e in edges or []:
            if e.get("observation_id") not in seen_ids:
                problems.append(f"lineage edge references {e.get('observation_id')!r}, "
                                "which is not in this batch")
                break
        # A set-based edge must point at a population this batch actually
        # defines. An edge naming a population_id that is nowhere in the batch
        # would leave a dangling reference the moment the foreign key is
        # enforced, and -- worse -- an aggregate whose set nobody can inspect.
        pop_ids = {p.get("population_id") for p in (populations or [])}
        for e in edges or []:
            pid = e.get("input_population_id")
            if pid and pid not in pop_ids:
                problems.append(f"lineage edge on {e.get('observation_id')} names "
                                f"population {pid!r}, which this batch does not define")
                break
        for p in populations or []:
            if p.get("observation_id") not in seen_ids:
                problems.append(f"population {p.get('population_id')!r} belongs to "
                                f"{p.get('observation_id')!r}, which is not in this batch")
                break
        return problems

    def commit_unit(self, *, adapter: str, entity_key: str, metric_ids: list[str],
                    year_from: int, year_to: int, observations: list[dict],
                    edges: list[dict], qa_events: list[dict] | None = None,
                    document_facts: list[dict] | None = None,
                    populations: list[dict] | None = None,
                    revisions: list[dict] | None = None,
                    prune_unwindowed: bool = True,
                    foreign_metric_ids=None, scope_key: str = "",
                    input_digest: str = "", identity: dict | None = None) -> dict:
        """Validate a staged batch, then swap it in atomically.

        The unit of work is entity x adapter x window. Order matters and is the
        whole point of the method:

          1. validate the staged rows -- nothing has been touched yet;
          2. work out what this unit previously produced that it no longer
             produces, restricted to the window;
          3. archive any row whose answer actually changes;
          4. in ONE transaction, delete (2), install the batch, invalidate the
             transitive dependants of any superseded filing, and write the
             durable unit marker.

        A failure at 1 or 2 raises with the database untouched. A failure inside
        3-4 rolls the transaction back. A process death anywhere leaves either
        the complete previous state or the complete new state -- never a pruned
        database waiting for a write that will not arrive, which is what the
        previous prune-then-write sequence left behind.
        """
        unit = f"{adapter}:{entity_key}:{year_from}-{year_to}"
        problems = self.validate_staged(unit, observations, edges,
                                        entity_key=entity_key,
                                        foreign_metric_ids=foreign_metric_ids,
                                        populations=populations)
        if problems:
            raise StagedCommitRejected(unit, problems)

        keep = {o["observation_id"] for o in observations}
        stale, retained_unwindowed, out_of_window = [], 0, 0
        if metric_ids:
            for i in range(0, len(metric_ids), 400):
                chunk = metric_ids[i:i + 400]
                for r in self.con.execute(
                        "SELECT observation_id, reporting_year, instant_date, "
                        "period_end, period_start FROM observations WHERE entity_key=? "
                        f"AND metric_id IN ({','.join('?' * len(chunk))})",
                        [entity_key, *chunk]):
                    if r["observation_id"] in keep:
                        continue
                    y = _year_of(dict(r))
                    if y is None:
                        if prune_unwindowed:
                            stale.append(r["observation_id"])
                        else:
                            retained_unwindowed += 1
                    elif year_from <= y <= year_to:
                        stale.append(r["observation_id"])
                    else:
                        out_of_window += 1

        stamp = now()
        for o in observations:
            o.setdefault("run_id", self.run_id)
            o.setdefault("first_seen_at", stamp)
            o["updated_at"] = stamp

        archived = 0
        revision_reports: list[dict] = []
        with self.transaction() as con:
            archived = self._archive_changed(con, observations)
            ids = list(keep)
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                # Edges and populations are rebuilt wholesale for the
                # observations being written, so a recomputed derivation cannot
                # keep stale inputs or a stale definition of its set. Edges go
                # first: they are what reference a population.
                con.execute("DELETE FROM lineage_edges WHERE observation_id IN "
                            f"({','.join('?' * len(part))})", part)
                con.execute("DELETE FROM lineage_populations WHERE observation_id IN "
                            f"({','.join('?' * len(part))})", part)
            for i in range(0, len(stale), 400):
                part = stale[i:i + 400]
                con.execute("DELETE FROM lineage_edges WHERE observation_id IN "
                            f"({','.join('?' * len(part))})", part)
                con.execute("DELETE FROM observations WHERE observation_id IN "
                            f"({','.join('?' * len(part))})", part)
            self._upsert(con, "observations", observations, ["observation_id"])
            # ORDER IS LOAD-BEARING. A set-based lineage edge carries
            # `input_population_id` referencing lineage_populations, so the
            # population row must exist before the edge that points at it or the
            # foreign key is momentarily unsatisfied. Both happen inside this one
            # transaction, so an aggregate's edge and the definition of the set
            # it summarises become visible together or not at all -- the same
            # guarantee `write_observations` already gives edges and values.
            self._upsert(con, "lineage_populations", populations or [],
                         ["population_id"])
            self._upsert(con, "lineage_edges", edges or [],
                         ["observation_id", "input_order"])
            self._upsert(con, "events", qa_events or [], ["event_id"])

            # PRUNE document_facts FOR THIS UNIT, exactly as observations are.
            #
            # Without this, an adapter whose extraction IMPROVES leaves its own
            # contradictory history behind for ever. w5-documents hit it: their
            # `_docfact` id was keyed on character offsets, the offsets moved when
            # the refund-window extraction went from the eLibrary description to
            # the order body, and the upsert therefore wrote a SIBLING rather than
            # replacing. Two rows then coexisted for one refund-closure bound --
            # the corrected one citing the order's dateline, and the fossil citing
            # the heading the carve-out used to accept. A population-wide
            # span-support check over that table fails permanently on the fossil,
            # and regenerating does not clear it.
            #
            # Pruning is bounded to the (adapter, entity) pair that produced the
            # batch and to the facts this batch no longer emits, so one adapter
            # can never remove another's rows, and an adapter that emits no
            # document facts at all removes nothing.
            df = document_facts or []
            if df:
                keep = {r.get("document_fact_id") for r in df}
                ents = {r.get("entity_key") for r in df if r.get("entity_key")}
                types = {r.get("assertion_type") for r in df if r.get("assertion_type")}
                if ents and types:
                    ph_e = ",".join("?" * len(ents))
                    ph_t = ",".join("?" * len(types))
                    doomed = [r[0] for r in con.execute(
                        f"SELECT document_fact_id FROM document_facts "
                        f"WHERE entity_key IN ({ph_e}) AND assertion_type IN ({ph_t})",
                        (*ents, *types)) if r[0] not in keep]
                    for fid in doomed:
                        con.execute("DELETE FROM document_facts WHERE document_fact_id=?",
                                    (fid,))
                    if doomed:
                        self.log("info", f"{unit}: pruned {len(doomed)} superseded "
                                         "document fact(s) this unit no longer produces",
                                 adapter=adapter)
            self._upsert(con, "document_facts", df, ["document_fact_id"])

            # Reconcile annotation applications inside the same transaction as
            # the observation swap. The count covers the complete resulting
            # database, including retained out-of-window history. A rollback or
            # abrupt pre-commit exit therefore leaves both the observations and
            # their bookkeeping at the previous last-good generation.
            con.execute(
                "UPDATE reviewed_source_annotations AS a SET applied_count=("
                " SELECT COUNT(*) FROM observations AS o"
                " WHERE o.source_system=a.source_system"
                "   AND o.entity_key=a.entity_key"
                "   AND o.filing_id=a.filing_id"
                "   AND o.source_fact_id=a.source_fact_id"
                "   AND o.metric_id=a.metric_id"
                "   AND o.value_text=a.filed_text)"
                " WHERE a.entity_key=?",
                (entity_key,))

            # Dependency invalidation is part of the same durability boundary
            # as the new unit rows. Previously run_adapter called it after
            # commit_unit returned. A process death in that gap left a durable
            # unit_commits marker, so startup recovery declared the unit done
            # even though out-of-window/transitive dependants still described
            # the superseded filing. Keep the edge walk and its UPDATE inside
            # this transaction and write the marker only after it succeeds.
            for filing in revisions or []:
                if not isinstance(filing, dict):
                    continue
                prior = (filing.get("supersedes_filing_id")
                         or filing.get("superseded_filing_id"))
                if not prior or filing.get("version_status") != "revised":
                    continue
                source_system = str(filing.get("source_system") or "")
                if not source_system:
                    raise ValueError(
                        f"revised filing {filing.get('filing_id')!r} has no source_system")
                detail = self._invalidate_dependents_in_transaction(
                    con, source_system, str(prior))
                detail.update({"revising_filing_id": str(filing.get("filing_id") or ""),
                               "superseded_filing_id": str(prior),
                               "source_system": source_system})
                revision_reports.append(detail)
            if self.run_id and scope_key:
                # This marker is the crash-recovery boundary. It becomes
                # durable with the observations, never before or after them.
                con.execute(
                    "INSERT OR REPLACE INTO unit_commits("
                    "run_id,adapter,entity_cid,scope_key,identity_json,input_digest,"
                    "observation_count,edge_count,document_fact_count,committed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, adapter, entity_key, scope_key,
                     json.dumps(identity or {}, sort_keys=True), input_digest,
                     len(observations), len(edges or []), len(df), now()))
        annotation_matches = self.con.execute(
            "SELECT COALESCE(SUM(applied_count),0) FROM reviewed_source_annotations "
            "WHERE entity_key=?", (entity_key,)).fetchone()[0]
        return {"unit": unit, "committed": len(observations), "pruned": len(stale),
                "archived_prior_versions": archived,
                "retained_out_of_window": out_of_window,
                "retained_unwindowed": retained_unwindowed,
                "edges": len(edges or []), "events": len(qa_events or []),
                "populations": len(populations or []),
                "document_facts": len(document_facts or []),
                "annotation_matches": annotation_matches,
                "invalidated_dependents": sum(r["transitions"] for r in revision_reports),
                "revision_invalidation": revision_reports}

    def _archive_changed(self, con, observations: list[dict]) -> int:
        """Copy a stored row into observation_versions when the incoming row
        genuinely answers differently.

        This is what preserves superseded provenance under the schema's rule 2
        identity, where the observation id is the GRAIN and a revision therefore
        overwrites the current row in place. Re-running unchanged input archives
        nothing, so a no-change refresh stays idempotent instead of growing a
        version per run.
        """
        incoming = {o["observation_id"]: o for o in observations}
        ids = list(incoming)
        archived = 0
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            for row in con.execute(
                    "SELECT * FROM observations WHERE observation_id IN "
                    f"({','.join('?' * len(part))})", part):
                old = dict(row)
                new = incoming[old["observation_id"]]
                if all(old.get(k) == new.get(k) for k in _VERSION_SIGNIFICANT):
                    continue
                seq = con.execute(
                    "SELECT IFNULL(MAX(version_seq),0)+1 FROM observation_versions"
                    " WHERE observation_id=?", (old["observation_id"],)).fetchone()[0]
                con.execute(
                    "INSERT INTO observation_versions(observation_id,version_seq,"
                    "superseded_at,superseded_by_run_id,source_system,filing_id,"
                    "value_text,value_num,version_status,validation,qa_flags,"
                    "review_status,row_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (old["observation_id"], seq, now(), self.run_id,
                     old.get("source_system"), old.get("filing_id"),
                     old.get("value_text"), old.get("value_num"),
                     old.get("version_status"), old.get("validation"),
                     old.get("qa_flags"), old.get("review_status"),
                     json.dumps(old, sort_keys=True, default=str)))
                archived += 1
        return archived

    def version_history(self, observation_id: str) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM observation_versions WHERE observation_id=?"
            " ORDER BY version_seq", (observation_id,))]

    def prune_observations(self, entity_key: str, metric_ids: list[str],
                           *, keep: set[str]) -> int:
        """Remove observations this adapter produced for this entity in an
        earlier run and no longer produces.

        Scoped deliberately narrowly: one entity, one adapter's metrics, and only
        rows absent from the current result. It never touches another adapter's
        rows, and it is not the mechanism for source revisions -- those go
        through invalidate_dependents(), which SUPERSEDES rather than deletes so
        prior filed values survive.

        DEPRECATED, and no longer used by the runner. It is year-blind: called
        during a 2025-only refresh it deletes 2024 rows the run never looked at,
        and being a separate step from the write it leaves a hole if the process
        dies between the two (audit A01). Use `commit_unit()`, which does the
        same job bounded by the window and inside the same transaction as the
        write. Retained only so an out-of-tree caller does not break silently.
        """
        if not metric_ids:
            return 0
        removed = 0
        with self.transaction() as con:
            for i in range(0, len(metric_ids), 400):
                chunk = metric_ids[i:i + 400]
                rows = list(con.execute(
                    f"SELECT observation_id FROM observations WHERE entity_key=? "
                    f"AND metric_id IN ({','.join('?' * len(chunk))})",
                    [entity_key, *chunk]))
                stale = [r[0] for r in rows if r[0] not in keep]
                for j in range(0, len(stale), 400):
                    part = stale[j:j + 400]
                    con.execute(f"DELETE FROM lineage_edges WHERE observation_id IN "
                                f"({','.join('?' * len(part))})", part)
                    con.execute(f"DELETE FROM observations WHERE observation_id IN "
                                f"({','.join('?' * len(part))})", part)
                    removed += len(part)
        return removed

    def write_document_facts(self, rows: list[dict]) -> int:
        """Write document facts, removing the ones this batch supersedes.

        THE PRUNE LIVES HERE, not only in `commit_unit`, because this is the path
        adapters actually use: `adapters/elibrary_docs.py` calls it directly. A
        prune placed only in the unit of work never saw these rows, which is the
        same shape as the populations writer nobody called -- a guarantee written
        in one place and bypassed in another.

        Why it is needed at all: `_docfact` ids used to be keyed on character
        offsets, so when an extraction IMPROVED and the offsets moved, the upsert
        wrote a SIBLING rather than replacing. Two document facts then coexisted
        for one refund-closure bound -- the corrected one citing the order's
        dateline, and a fossil citing the eLibrary heading a since-removed
        carve-out had accepted. Regeneration does not clear a fossil, so a
        population-wide span-support check over this table fails for ever, and
        every span-backed assertion in the product is grounded in it.

        Scoped narrowly, to the (entity, assertion_type) pairs this batch
        actually writes: one adapter can never remove another's facts, and a
        batch that writes nothing removes nothing.
        """
        if not rows:
            return 0
        keep = {r.get("document_fact_id") for r in rows}
        pairs = {(r.get("entity_key"), r.get("assertion_type")) for r in rows
                 if r.get("entity_key") and r.get("assertion_type")}
        with self.transaction() as con:
            doomed = []
            for ent, typ in sorted(pairs):
                doomed += [r[0] for r in con.execute(
                    "SELECT document_fact_id FROM document_facts "
                    "WHERE entity_key=? AND assertion_type=?", (ent, typ))
                    if r[0] not in keep]
            for fid in doomed:
                con.execute("DELETE FROM document_facts WHERE document_fact_id=?", (fid,))
            n = self._upsert(con, "document_facts", rows, ["document_fact_id"])
        if doomed:
            self.log("info", f"pruned {len(doomed)} superseded document fact(s) "
                             "no longer produced by the current extraction")
        return n

    def write_events(self, rows: list[dict]) -> int:
        return self.upsert("events", rows, ["event_id"])

    def freeze_expected(self, rows: list[dict]) -> int:
        """Frozen BEFORE canonical results are examined. Re-freezing an existing
        slot is refused: the denominator must not move once the run has begun."""
        existing = {r[0] for r in self.con.execute("SELECT slot_id FROM coverage_expected")}
        fresh = [r for r in rows if r["slot_id"] not in existing]
        return self.upsert("coverage_expected", fresh, ["slot_id"]) if fresh else 0

    def write_measured(self, rows: list[dict]) -> int:
        return self.upsert("coverage_measured", rows, ["slot_id"])

    def write_field_status(self, rows: list[dict]) -> int:
        return self.upsert("field_status", rows, ["template", "field_id"])

    def replace_coverage(self, expected: list[dict], measured: list[dict]) -> dict:
        """Publish one complete denominator/numerator generation atomically.

        Coverage is a pair of tables. Replacing only one lets a consumer combine
        a new eligibility calendar with old measurements (or the reverse), so the
        supported final-generation path validates the complete pair and swaps it
        under one SQLite transaction.
        """
        expected_ids = {r.get("slot_id") for r in expected}
        measured_ids = {r.get("slot_id") for r in measured}
        if not expected or None in expected_ids or expected_ids != measured_ids:
            raise ValueError(
                "coverage replacement requires non-empty one-to-one expected/measured slots")
        with self.transaction() as con:
            con.execute("DELETE FROM coverage_measured")
            con.execute("DELETE FROM coverage_expected")
            self._upsert(con, "coverage_expected", expected, ["slot_id"])
            self._upsert(con, "coverage_measured", measured, ["slot_id"])
        return {"expected": len(expected), "measured": len(measured)}

    def replace_field_status(self, rows: list[dict]) -> int:
        if not rows:
            raise ValueError("refusing to replace field_status with zero rows")
        with self.transaction() as con:
            con.execute("DELETE FROM field_status")
            return self._upsert(con, "field_status", rows, ["template", "field_id"])

    def replace_snapshot(self, table: str, rows: list[dict], keys: list[str],
                         *, allow_empty: bool = False) -> int:
        """Replace a bounded reference snapshot after it has been built fully."""
        allowed = {"applicability", "taxonomy_sources", "source_manifest",
                   "requirements_crosswalk", "dockets", "asset_dockets"}
        if table not in allowed:
            raise ValueError(f"unsupported snapshot table {table!r}")
        if not rows and not allow_empty:
            raise ValueError(f"refusing to replace {table} with zero rows")
        with self.transaction() as con:
            con.execute(f'DELETE FROM "{table}"')
            return self._upsert(con, table, rows, keys)

    def replace_reference_state(self, *, applicability: list[dict],
                                taxonomy_sources: list[dict],
                                source_manifest: list[dict], dockets: list[dict],
                                asset_dockets: list[dict]) -> dict:
        """Publish the derived reference/provenance stores as one DB state."""
        required = {"applicability": applicability,
                    "taxonomy_sources": taxonomy_sources,
                    "source_manifest": source_manifest}
        empty = sorted(name for name, rows in required.items() if not rows)
        if empty:
            raise ValueError(f"refusing empty required reference snapshots: {empty}")
        specs = (
            ("applicability", applicability,
             ["form", "taxonomy_version", "concept_local"]),
            ("taxonomy_sources", taxonomy_sources,
             ["form", "taxonomy_version", "artefact", "url"]),
            ("source_manifest", source_manifest, ["cache_key"]),
            ("asset_dockets", asset_dockets, ["asset_id", "docket"]),
            ("dockets", dockets, ["docket"]),
        )
        with self.transaction() as con:
            # Child association rows must disappear before their parent rows.
            for table in ("asset_dockets", "dockets", "applicability",
                          "taxonomy_sources", "source_manifest"):
                con.execute(f'DELETE FROM "{table}"')
            counts = {}
            # Parent docket rows must exist before asset_dockets are inserted.
            ordered = [specs[0], specs[1], specs[2], specs[4], specs[3]]
            for table, rows, keys in ordered:
                counts[table] = self._upsert(con, table, rows, keys)
        return counts

    def record_publication(self, row: dict) -> int:
        required = {"generation_id", "code_snapshot", "input_snapshot",
                    "database_identity", "manifest_json", "status", "published_at"}
        missing = sorted(k for k in required if row.get(k) in (None, ""))
        if missing:
            raise ValueError(f"publication record missing {missing}")
        return self.upsert("publication_generations", [row], ["generation_id"])

    def open_blocker(self, adapter: str, kind: str, summary: str, *, scope: str = "",
                     attempts: str = "", exact_error: str = "",
                     human_decision: bool = False, key: str = "") -> str:
        """Open (or update) a blocker.

        IDENTITY. `key` is the blocker's stable identity: the QUESTION being
        blocked, not the sentence describing it today. Pass one. Without it the
        identity falls back to the summary, and any volatile text in that
        summary silently forks the blocker -- editing a dollar figure inside the
        sentence minted `blk-df2155105afc957e` and `blk-c29e1a69a09275d1` for
        one 549D policy question, which then reads as two unresolved issues.

        The summary is free to be rewritten, improved, or to carry a figure, as
        long as `key` is supplied and stays constant. A caller that cannot state
        a stable key should ask itself whether it knows what the blocker is
        about.
        """
        identity = f"{adapter}|{scope}|{key or summary}"
        bid = "blk-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        with self.transaction() as con:
            existing = con.execute(
                "SELECT opened_at FROM blockers WHERE blocker_id=?", (bid,)).fetchone()
            opened_at = (existing["opened_at"] if existing is not None
                         and existing["opened_at"] else now())
            self._upsert(con, "blockers", [{
                "blocker_id": bid, "adapter": adapter, "scope": scope, "kind": kind,
                "summary": summary, "attempts": attempts, "exact_error": exact_error,
                "human_decision_needed": int(human_decision), "opened_at": opened_at,
                "resolved_at": None}], ["blocker_id"])
        return bid

    # ------------------------------------------------------------- checkpoints

    @staticmethod
    def scope_key_for(year_from: int, year_to: int, identity_digest: str = "") -> str:
        """The checkpoint's scope, carrying the window and the conditions.

        The checkpoints table keys on (adapter, entity_cid, scope_key) and has
        no column for a configuration digest, so the digest travels inside the
        scope key. That is not a workaround, it is the correct semantics: a
        checkpoint written under a different registry version, a different
        window or different adapter code is a checkpoint for a DIFFERENT scope,
        and must not answer for this one.
        """
        return f"{year_from}-{year_to}#{identity_digest}" if identity_digest \
            else f"{year_from}-{year_to}"

    def checkpoint(self, adapter: str, entity_cid: str, scope_key: str, state: str,
                   *, error: str = "") -> None:
        row = self.con.execute(
            "SELECT attempts FROM checkpoints WHERE adapter=? AND entity_cid=? AND scope_key=?",
            (adapter, entity_cid, scope_key)).fetchone()
        attempts = (row["attempts"] if row else 0) + (1 if state in ("in_progress", "failed") else 0)
        self.upsert("checkpoints", [{
            "adapter": adapter, "entity_cid": entity_cid, "scope_key": scope_key,
            "state": state, "attempts": attempts, "last_error": error or None,
            "last_run_id": self.run_id, "updated_at": now()}],
            ["adapter", "entity_cid", "scope_key"])

    # ----------------------------------------------------- append-only run evidence

    @staticmethod
    def _append_unit_status(con, run_id: str, adapter: str, entity_cid: str,
                            scope_key: str, state: str, detail: str = "") -> None:
        seq = con.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM run_unit_status "
            "WHERE run_id=? AND adapter=? AND entity_cid=? AND scope_key=?",
            (run_id, adapter, entity_cid, scope_key)).fetchone()[0]
        con.execute(
            "INSERT INTO run_unit_status(run_id,adapter,entity_cid,scope_key,seq,state,"
            "detail,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, adapter, entity_cid, scope_key, seq, state, detail[:1000], now()))

    def record_unit_status(self, adapter: str, entity_cid: str, scope_key: str,
                           state: str, detail: str = "") -> None:
        self._refuse_write("record per-run unit status")
        if not self.run_id:
            return
        with self.transaction() as con:
            self._append_unit_status(con, self.run_id, adapter, entity_cid,
                                     scope_key, state, detail)

    def record_run_inputs(self, adapter: str, entity_cid: str,
                          filings: list[dict]) -> int:
        """Persist the source-occurrence inventory for THIS run.

        Rows are append-only by run id for complete units. A later IOC-only run
        cannot erase the evidence a successful universe run saw, and
        byte-identical resubmissions remain separate because filing occurrence,
        not content hash, is the identity.

        run_adapter invokes this inside its outer unit transaction. If a later
        required-subitem/completeness gate rejects the unit, this provisional
        inventory rolls back with its partial filing rows: a returned prefix is
        not misrepresented as the failed run's complete source universe. The
        durable failed unit status records the specific missing subitem/cache
        object, while captured immutable cache bytes remain available to retry.
        """
        self._refuse_write("record run input inventory")
        if not self.run_id:
            return 0
        ordered = sorted(
            (f for f in (filings or []) if isinstance(f, dict)),
            key=lambda f: (str(f.get("source_system") or ""),
                           str(f.get("filing_id") or f.get("accession_number") or "")))
        stamp = now()
        with self.transaction() as con:
            for i, f in enumerate(ordered, 1):
                con.execute(
                    "INSERT OR IGNORE INTO run_input_inventory("
                    "run_id,adapter,entity_cid,input_order,source_system,filing_id,"
                    "accession_number,content_hash,submitted_on,form,reporting_year,"
                    "reporting_period,snapshot_date,version_status,is_canonical,observed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, adapter, entity_cid, i,
                     f.get("source_system"), f.get("filing_id"),
                     f.get("accession_number"),
                     f.get("content_hash") or f.get("_content_hash"),
                     f.get("submitted_on"), f.get("form"), f.get("reporting_year"),
                     f.get("reporting_period"), f.get("snapshot_date"),
                     f.get("version_status"), f.get("is_canonical"), stamp))
        return len(ordered)

    def failed_subunits(self, adapter: str, entity_cid: str,
                        outer_scope_key: str) -> list[dict]:
        """Failures recorded by this run below an entity-level unit."""
        if not self.run_id:
            return []
        return [dict(r) for r in self.con.execute(
            "SELECT adapter,entity_cid,scope_key,state,last_error FROM checkpoints "
            "WHERE adapter=? AND entity_cid=? AND last_run_id=? AND scope_key!=? "
            "AND state IN ('failed','in_progress') ORDER BY scope_key",
            (adapter, entity_cid, self.run_id, outer_scope_key))]

    def recover_incomplete_runs(self) -> list[dict]:
        """Recover state left by an abrupt prior process exit.

        Cleanup code cannot run after SIGKILL or power loss. The unit_commits
        marker says whether the data transaction crossed COMMIT. A matching
        marker makes the unit durably complete; without one an in-progress
        checkpoint becomes failed/stale and the last-good generation remains.
        """
        self._refuse_write("recover incomplete runs")
        recovered: list[dict] = []
        with self.transaction() as con:
            running = [r[0] for r in con.execute(
                "SELECT run_id FROM runs WHERE status='running' ORDER BY started_at")]
            for rid in running:
                rows = list(con.execute(
                    "SELECT adapter,entity_cid,scope_key FROM checkpoints "
                    "WHERE last_run_id=? AND state='in_progress'",
                    (rid,)))
                for cp in rows:
                    marker = con.execute(
                        "SELECT identity_json,input_digest FROM unit_commits "
                        "WHERE run_id=? AND adapter=? AND entity_cid=? AND scope_key=?",
                        (rid, cp["adapter"], cp["entity_cid"], cp["scope_key"])).fetchone()
                    if marker:
                        state = "done"
                        detail = "startup recovery: data COMMIT was durable before status recording"
                    else:
                        state = "failed"
                        detail = "startup recovery: process exited before a durable unit COMMIT"
                    con.execute(
                        "UPDATE checkpoints SET state=?,last_error=?,updated_at=? "
                        "WHERE adapter=? AND entity_cid=? AND scope_key=?",
                        (state, None if marker else detail, now(), cp["adapter"],
                         cp["entity_cid"], cp["scope_key"]))
                    self._append_unit_status(con, rid, cp["adapter"], cp["entity_cid"],
                                             cp["scope_key"],
                                             "recovered_committed" if marker else "recovered_failed",
                                             detail)
                    recovered.append({
                        "run_id": rid, "adapter": cp["adapter"],
                        "entity_cid": cp["entity_cid"], "scope_key": cp["scope_key"],
                        "committed": bool(marker),
                        "identity": json.loads(marker["identity_json"]) if marker else {},
                        "input_digest": marker["input_digest"] if marker else "",
                        "detail": detail})
                con.execute(
                    "UPDATE runs SET finished_at=COALESCE(finished_at,?),status='interrupted',"
                    "note=CASE WHEN note IS NULL OR note='' THEN ? ELSE note END WHERE run_id=?",
                    (now(), "startup recovery after abrupt process exit", rid))
        self._recovery_done = True
        return recovered

    def is_done(self, adapter: str, entity_cid: str, scope_key: str) -> bool:
        r = self.con.execute(
            "SELECT state FROM checkpoints WHERE adapter=? AND entity_cid=? AND scope_key=?",
            (adapter, entity_cid, scope_key)).fetchone()
        return bool(r) and r["state"] == "done"

    def pending(self, adapter: str) -> list[sqlite3.Row]:
        return list(self.con.execute(
            "SELECT * FROM checkpoints WHERE adapter=? AND state!='done' ORDER BY entity_cid,scope_key",
            (adapter,)))

    # ------------------------------------------------------------- revisions

    def classify_version(self, source_system: str, entity_key: str, form: str,
                         year: int, period: str, filing_id: str,
                         content_hash: str, *, snapshot_date: str = "",
                         submitted_on: str = "") -> tuple[str, str | None]:
        """(version_status, superseded_filing_id).

        Byte-identical content under a new filing ID is an IDENTICAL RESUBMISSION:
        both occurrences are retained and no economic-change event is raised.
        Different content is a REVISION, which invalidates dependants.

        `snapshot_date` exists because (form, year, period) is an annual/quarterly
        FORM assumption that a snapshot source does not fit. Two Index of
        Customers filings in one quarter with different header as-of dates are two
        different indexes, not a revision of one -- and grouping them by quarter
        would report a routine new snapshot as a restatement.
        """
        if not snapshot_date:
            # An eLibrary accession IS the filing's identity. Without this, every
            # document a filer lodged in a year shares the grouping key
            # (form, year, "as_of") and unrelated documents are classified as
            # revisions of one another.
            acc = self.con.execute(
                "SELECT accession_number FROM filings WHERE source_system=? AND filing_id=?",
                (source_system, filing_id)).fetchone()
            if acc and acc[0]:
                rows = list(self.con.execute(
                    "SELECT f.filing_id,f.content_hash,f.submitted_on,f.filed_date,"
                    "f.posted_date FROM filings f JOIN filing_entities fe "
                    "ON fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
                    "WHERE f.source_system=? AND fe.entity_key=? AND "
                    "f.accession_number=? AND f.filing_id!=?",
                    (source_system, entity_key, acc[0], filing_id)))
                return self._classify_ordered_occurrence(
                    rows, filing_id, content_hash, submitted_on)
        if snapshot_date:
            rows = list(self.con.execute(
                "SELECT f.filing_id,f.content_hash,f.submitted_on,f.filed_date,"
                "f.posted_date FROM filings f JOIN filing_entities fe "
                "ON fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
                "WHERE f.source_system=? AND fe.entity_key=? AND f.form=? "
                "AND f.snapshot_date=? AND f.filing_id!=?",
                (source_system, entity_key, form, snapshot_date, filing_id)))
        else:
            rows = list(self.con.execute(
                "SELECT f.filing_id,f.content_hash,f.submitted_on,f.filed_date,"
                "f.posted_date FROM filings f JOIN filing_entities fe "
                "ON fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
                "WHERE f.source_system=? AND fe.entity_key=? AND f.form=? "
                "AND f.reporting_year=? AND f.reporting_period=? AND f.filing_id!=?",
                (source_system, entity_key, form, year, period, filing_id)))
        return self._classify_ordered_occurrence(rows, filing_id, content_hash,
                                                 submitted_on)

    @staticmethod
    def _occurrence_order(row) -> tuple[str, str]:
        """Source-supported chronological order with native identity as tie-breaker."""
        return (str(row.get("submitted_on") or row.get("filed_date") or
                    row.get("posted_date") or ""), str(row.get("filing_id") or ""))

    @classmethod
    def _classify_ordered_occurrence(cls, rows, filing_id: str,
                                     content_hash: str,
                                     submitted_on: str) -> tuple[str, str | None]:
        """Classify against the immediate earlier occurrence, never a future row.

        The former implementation selected ``rows[-1]`` from every row already
        in the database. Replaying an older occurrence after a newer one then
        pointed the older row forward; replaying the newer row in turn pointed
        it back, creating a two-node cycle. Ordering the complete occurrence set
        and looking only left makes the result replay-order invariant.
        """
        material = [dict(r) for r in rows]
        material.append({"filing_id": filing_id, "content_hash": content_hash,
                         "submitted_on": submitted_on, "filed_date": submitted_on,
                         "posted_date": ""})
        material.sort(key=cls._occurrence_order)
        pos = next(i for i, row in enumerate(material)
                   if row["filing_id"] == filing_id)
        if pos == 0:
            return "original", None
        previous = material[pos - 1]
        if content_hash and previous.get("content_hash") == content_hash:
            return "identical_resubmission", None
        return "revised", previous["filing_id"]

    def normalize_revision_group(self, source_system: str, entity_key: str,
                                 form: str, *, snapshot_date: str = "",
                                 year: int | None = None,
                                 period: str = "",
                                 set_canonical: bool = False) -> list[dict]:
        """Deterministically rebuild one occurrence graph from persisted rows.

        This is the supported repair path for databases created by the old
        order-dependent classifier. Every edge points to the immediate earlier
        occurrence in source date/native-id order. Byte-identical consecutive
        submissions remain distinct occurrences but are labelled identical and
        do not masquerade as an economic revision.
        """
        self._refuse_write("normalise filing revision graph")
        if snapshot_date:
            rows = [dict(r) for r in self.con.execute(
                "SELECT f.* FROM filings f JOIN filing_entities fe "
                "ON fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
                "WHERE f.source_system=? AND fe.entity_key=? "
                "AND f.form=? AND f.snapshot_date=?",
                (source_system, entity_key, form, snapshot_date))]
            group_label = f"snapshot {snapshot_date}"
        else:
            rows = [dict(r) for r in self.con.execute(
                "SELECT f.* FROM filings f JOIN filing_entities fe "
                "ON fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
                "WHERE f.source_system=? AND fe.entity_key=? "
                "AND f.form=? AND f.reporting_year=? AND f.reporting_period=?",
                (source_system, entity_key, form, year, period))]
            group_label = f"{period} {year}"
        rows.sort(key=self._occurrence_order)
        if not rows:
            return []

        updates = []
        for index, row in enumerate(rows):
            if index == 0:
                status, supersedes = "original", None
            else:
                previous = rows[index - 1]
                if row.get("content_hash") and (
                        row.get("content_hash") == previous.get("content_hash")):
                    status, supersedes = "identical_resubmission", None
                else:
                    status, supersedes = "revised", previous["filing_id"]
            canonical = int(index == len(rows) - 1) if set_canonical else row["is_canonical"]
            reason = row.get("canonical_reason")
            if set_canonical:
                reason = (f"{group_label}: latest source-dated occurrence of {len(rows)}"
                          if canonical else
                          f"{group_label}: superseded by a later source-dated occurrence")
            updates.append((status, supersedes, canonical, reason,
                            row["source_system"], row["filing_id"]))
            row.update(version_status=status,
                       supersedes_filing_id=supersedes,
                       is_canonical=canonical,
                       canonical_reason=reason)

        with self.transaction() as con:
            con.executemany(
                "UPDATE filings SET version_status=?,supersedes_filing_id=?,"
                "is_canonical=?,canonical_reason=? WHERE source_system=? AND filing_id=?",
                updates)
        return rows

    @staticmethod
    def _dependency_closure_on(con, observation_ids) -> list[str]:
        """Connection-scoped edge walk usable inside an existing transaction."""
        seen = set(observation_ids or ())
        frontier = list(seen)
        while frontier:
            nxt: set[str] = set()
            for i in range(0, len(frontier), 400):
                part = frontier[i:i + 400]
                for r in con.execute(
                        "SELECT DISTINCT observation_id FROM lineage_edges WHERE "
                        f"input_observation_id IN ({','.join('?' * len(part))})", part):
                    if r["observation_id"] not in seen:
                        nxt.add(r["observation_id"])
            seen |= nxt
            frontier = list(nxt)
        return sorted(seen)

    def dependency_closure(self, observation_ids) -> list[str]:
        """Every observation reachable from these by lineage, transitively.

        A ratio built on a difference built on a filed total is three hops from
        the filing that changed. Walking the edges is the explicit way to reach
        it; the alternative -- deleting broadly and hoping the rebuild covers
        the same ground -- destroys rows nothing asked about and still misses
        dependants in windows the run did not touch (audit A01 clause 3).

        Cycles are impossible in a derivation graph but are guarded anyway: the
        walk is over a visited set, so it terminates regardless.
        """
        return self._dependency_closure_on(self.con, observation_ids)

    def invalidate_dependents(self, source_system: str, filing_id: str) -> list[str]:
        """Observations that must be recomputed because a filing changed.

        Covers the observations selected directly from that filing, any
        observation with a lineage edge into it, and -- transitively -- anything
        derived from those, so a revision moves canonical selection, dependent
        derivations, coverage and events together. Prior values are NOT deleted:
        they are marked superseded, and `commit_unit()` archives the prior row
        into observation_versions when the replacement actually differs, so the
        earlier filed value survives.

        Idempotent by construction. The UPDATE only touches rows that are not
        already superseded, so committing the same revision twice performs the
        transition exactly once and `transitions` reports 0 the second time.
        """
        return self.invalidate_dependents_detailed(source_system, filing_id)["affected"]

    def invalidate_dependents_detailed(self, source_system: str, filing_id: str) -> dict:
        self._refuse_write("invalidate dependants")
        with self.transaction() as con:
            return self._invalidate_dependents_in_transaction(con, source_system, filing_id)

    def _invalidate_dependents_in_transaction(self, con, source_system: str,
                                              filing_id: str) -> dict:
        """Invalidate one occurrence using the caller's atomic transaction."""
        direct = [r["observation_id"] for r in con.execute(
            "SELECT observation_id FROM observations WHERE source_system=? AND filing_id=?",
            (source_system, filing_id))]
        derived = [r["observation_id"] for r in con.execute(
            "SELECT DISTINCT observation_id FROM lineage_edges"
            " WHERE input_source_system=? AND input_filing_id=?",
            (source_system, filing_id))]
        affected = self._dependency_closure_on(con, set(direct) | set(derived))
        transitions = 0
        for i in range(0, len(affected), 400):
            chunk = affected[i:i + 400]
            cur = con.execute(
                "UPDATE observations SET version_status='superseded', updated_at=?"
                f" WHERE observation_id IN ({','.join('?' * len(chunk))})"
                "  AND version_status!='superseded'",
                [now(), *chunk])
            transitions += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return {"affected": affected, "direct": sorted(set(direct)),
                "derived": sorted(set(derived) - set(direct)),
                "transitions": transitions}

    # ------------------------------------------------------------- reads

    def counts(self) -> dict[str, int]:
        out = {}
        for (t,) in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                " ORDER BY name"):
            out[t] = self.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        return out

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.con.execute(sql, params))

    def iter_query(self, sql: str, params: tuple = ()):
        """Stream a large read without materialising the result in memory."""
        return self.con.execute(sql, params)
