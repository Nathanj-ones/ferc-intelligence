"""
Acceptance harness for the all-regime repair.

This package exists to FALSIFY the repair, not to re-run the implementers'
examples. Everything here obeys four rules that come straight from the repair
contract:

1. A suite passes by DETECTING AND REJECTING a bad mutation. A test that passes
   because the mutated record became acceptable is a failed test.
2. Every required fixture asserts that its intended population actually exists.
   A check that silently matches zero rows is a FAILURE, never a pass -- that is
   the exact false-skip shape A12 was raised for.
3. A missing precondition that is genuinely outside this workstream's control
   (the canonical repaired DB not built yet; an optional external reference
   package absent) is SKIPPED and reported as SKIPPED. It is never counted as a
   pass and never silently folded into a total.
4. An acceptance target whose fix has not landed yet is recorded as XFAIL with
   the reason, and the moment it starts passing it is reported as XPASS so the
   marker gets removed. Targets are never weakened or deleted to go green.

Tiers are reported separately because they prove different things:

    WORKER      runs against hash-pinned historical evidence, a current
                read-only reference where needed, and disposable fixtures under
                work/w6-acceptance/. Proves a mutation is caught.
    INTEGRATED  needs the canonical repaired database the integrator builds.
    EXTERNAL    needs an optional external reference package (Transco / TGP).

Synthetic records are labelled with SYNTH_PREFIX, live only in a per-run
disposable directory, and are never written to a production source or counted in
coverage.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
TREE = HERE.parent                      # outputs/repair_all_regimes
REPAIR_DB = pathlib.Path(os.environ.get(
    "FERC_STAGING_DB", TREE / "staging" / "operating_assets.sqlite"))
# Historical evidence is never inferred from a sibling output directory.  The
# old layout happened to put ``operating_assets_all_regimes`` beside the repair,
# but in a standalone extraction that expression resolves back to the candidate
# itself and silently turns historical-negative tests into current-state tests.
# ``BASELINE_*`` remains as a compatibility name for worker cases that merely
# need a stable populated REFERENCE row; it is explicitly the current candidate
# unless the caller supplies another current reference.
BASELINE_TREE = pathlib.Path(os.environ.get("FERC_ACCEPTANCE_REFERENCE_TREE", TREE))
BASELINE_DB = pathlib.Path(os.environ.get("FERC_ACCEPTANCE_REFERENCE_DB", REPAIR_DB))
FIXTURE_DIR = TREE / "evidence" / "test_fixtures"
FIXTURE_MANIFEST = FIXTURE_DIR / "MANIFEST.json"
WORK = TREE / "work" / "w6-acceptance"

#: every synthetic identifier this workstream mints starts with this, so a
#: synthetic row can never be mistaken for FERC evidence, and so a grep proves
#: none of it reached an export or a coverage denominator.
SYNTH_PREFIX = "W6ACC_SYNTHETIC"


# ------------------------------------------------------------------ statuses

class Status:
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    XFAIL = "XFAIL"        # expected-failing acceptance target; fix not landed
    XPASS = "XPASS"        # acceptance target now met -- remove the marker


class Tier:
    WORKER = "worker"
    INTEGRATED = "integrated"
    EXTERNAL = "external_optional"


class FixtureMissing(AssertionError):
    """A required fixture population is empty.

    This is deliberately an AssertionError and therefore a FAILURE. A regression
    check whose population is empty has proved nothing; reporting it as a pass or
    a skip is the false-skip defect itself.
    """


class FixtureIntegrityError(FixtureMissing):
    """A required packaged fixture is absent, aliased or hash-mismatched."""


def fixture_bytes(name: str) -> bytes:
    """Read one release-relative fixture only after validating its manifest.

    Required audit evidence is deliberately fail-closed: a missing manifest,
    unlisted file, symlink, byte-count mismatch or digest mismatch is a test
    failure.  No sibling tree, current database or user directory is searched as
    a fallback, so a standalone extraction exercises exactly what it ships.
    """
    if pathlib.PurePath(name).name != name or name == "MANIFEST.json":
        raise FixtureIntegrityError(f"invalid fixture name: {name!r}")
    if not FIXTURE_DIR.is_dir() or FIXTURE_DIR.is_symlink():
        raise FixtureIntegrityError(
            f"required fixture directory is absent or aliased: {FIXTURE_DIR}")
    if not FIXTURE_MANIFEST.is_file() or FIXTURE_MANIFEST.is_symlink():
        raise FixtureIntegrityError(
            f"required fixture manifest is absent or aliased: {FIXTURE_MANIFEST}")
    try:
        manifest = json.loads(FIXTURE_MANIFEST.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureIntegrityError(
            f"required fixture manifest is unreadable: {FIXTURE_MANIFEST}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise FixtureIntegrityError(
            f"required fixture manifest has no object-valued files map: "
            f"{FIXTURE_MANIFEST}")
    entry = manifest["files"].get(name)
    if not isinstance(entry, dict):
        raise FixtureIntegrityError(f"required fixture {name!r} is not manifested")
    path = FIXTURE_DIR / name
    if not path.is_file() or path.is_symlink():
        raise FixtureIntegrityError(f"required fixture is absent or aliased: {path}")
    data = path.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if len(data) != entry.get("bytes") or actual_hash != entry.get("sha256"):
        raise FixtureIntegrityError(
            f"required fixture integrity failure for {name}: expected "
            f"{entry.get('bytes')} bytes/{entry.get('sha256')}, got "
            f"{len(data)} bytes/{actual_hash}")
    return data


def fixture_json(name: str) -> dict:
    """Return a verified JSON-object fixture."""
    try:
        value = json.loads(fixture_bytes(name))
    except json.JSONDecodeError as exc:
        raise FixtureIntegrityError(f"fixture {name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FixtureIntegrityError(f"fixture {name} must contain a JSON object")
    return value


class TierUnavailable(Exception):
    """A precondition outside this workstream's control is absent.

    Reported as SKIPPED with the reason. Never a pass.
    """


class NegativeControlNotFired(AssertionError):
    """A mutation that must be rejected was accepted.

    Raised only by `must_reject`, so a negative control can never quietly turn
    into a positive one.
    """


# ------------------------------------------------------------------ registry

@dataclasses.dataclass
class Case:
    fn: object
    name: str
    issue: str
    tier: str
    group: str
    owner: str
    xfail_reason: str | None
    mutation: str | None


REGISTRY: list[Case] = []


def acceptance(*, issue: str, group: str, tier: str = Tier.WORKER,
               owner: str = "w6-acceptance", xfail: str | None = None,
               mutation: str | None = None):
    """Register one acceptance case.

    issue     the audit issue ID (or matrix point) this case defends.
    group     reporting group.
    tier      WORKER / INTEGRATED / EXTERNAL -- reported separately.
    owner     the workstream that must fix a failure here.
    xfail     if set, this is an acceptance TARGET whose fix has not landed.
              The case still runs in full; a failure is recorded as XFAIL with
              this reason and a success as XPASS. The assertion is never
              softened -- only its accounting changes.
    mutation  one-line description of the bad state the case must reject.
    """
    def deco(fn):
        REGISTRY.append(Case(fn=fn, name=fn.__doc__.strip().splitlines()[0].strip(),
                             issue=issue, tier=tier, group=group, owner=owner,
                             xfail_reason=xfail, mutation=mutation))
        return fn
    return deco


# ------------------------------------------------------------------ assertions

def require(cond, msg: str) -> None:
    """A plain acceptance assertion."""
    if not cond:
        raise AssertionError(msg)


def require_population(rows, what: str, minimum: int = 1):
    """Assert the fixture population this check needs actually exists.

    An empty population means the check exercised nothing. That is a FAILURE
    (FixtureMissing), never a pass and never a silent skip.
    """
    n = len(rows) if rows is not None else 0
    if n < minimum:
        raise FixtureMissing(
            f"fixture precondition not met: expected at least {minimum} row(s) of "
            f"{what}, found {n}. The check exercised nothing, so it proves nothing.")
    return rows


def must_reject(fn, *, expect, what: str):
    """Run `fn` and require that it REJECTS the mutation.

    `expect` is an exception type (or tuple). If `fn` returns normally the
    mutation was accepted and this raises NegativeControlNotFired -- the suite
    can only go green by detecting the bad state, never by tolerating it.

    Returns the exception instance, so a caller can additionally assert that the
    rejection names the right reason rather than failing for an unrelated cause.
    """
    try:
        result = fn()
    except expect as exc:
        return exc
    raise NegativeControlNotFired(
        f"NEGATIVE CONTROL DID NOT FIRE: {what}. The mutation was accepted "
        f"(returned {result!r}) instead of raising {getattr(expect, '__name__', expect)}.")


# ------------------------------------------------------------------ environment

class Env:
    """Per-run environment: read-only sources plus a disposable scratch dir.

    The scratch directory is unique per run, so nothing is ever deleted to make a
    test pass -- a fresh run gets a fresh path.
    """

    def __init__(self, run_id: str | None = None):
        self.run_id = run_id or dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        self.scratch = WORK / f"run_{self.run_id}"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._cons: list[sqlite3.Connection] = []
        self.notes: list[str] = []

    # ---- read-only sources

    @property
    def baseline_path(self) -> pathlib.Path:
        if not BASELINE_DB.is_file():
            raise TierUnavailable(f"populated acceptance reference database absent: {BASELINE_DB}")
        return BASELINE_DB

    def baseline(self) -> sqlite3.Connection:
        """A current populated reference database, opened read-only and immutable.

        Historical assertions use :func:`fixture_json`; this compatibility
        method exists only for mutation cases needing genuine row shapes.
        """
        if not BASELINE_DB.is_file():
            raise TierUnavailable(f"populated acceptance reference database absent: {BASELINE_DB}")
        return self._ro(BASELINE_DB, immutable=True)

    #: A database is "built" when it carries the populations a check needs. The
    #: same floor the release contract uses: an EMPTY database is not a small
    #: database, it is an unbuilt one, and every query against it returns zero
    #: rows without failing -- which is the false-pass shape this suite exists to
    #: catch. File size is not the test: a schema-only database is 331 KB.
    MINIMUM_POPULATION = ("observations", "filings", "coverage_expected")

    def repaired(self) -> sqlite3.Connection:
        """The canonical repaired DB. Absent or empty until the integrator builds it."""
        if not REPAIR_DB.is_file():
            raise TierUnavailable(
                f"canonical repaired database not built yet: {REPAIR_DB}. "
                "This is an INTEGRATED-tier check; it is SKIPPED, not passed.")
        con = self._ro(REPAIR_DB, immutable=False)
        empty = []
        for table in self.MINIMUM_POPULATION:
            try:
                if con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0:
                    empty.append(table)
            except sqlite3.Error:
                empty.append(f"{table} (absent)")
        if empty:
            raise TierUnavailable(
                f"the canonical repaired database exists but is not populated "
                f"({', '.join(empty)} empty): {REPAIR_DB}. A schema-only database "
                "answers every query with zero rows, so treating it as built would "
                "turn each check into a silent no-op.")
        return con

    def any_db(self) -> tuple[sqlite3.Connection, str]:
        """The stable fixture database for a mutation check.

        A worker-tier case asks "is this bad state detected?" and may need a real
        row shape.  This source is current/reference data, never the historical
        negative baseline. Historical evidence is packaged and hash-checked.

        Integrated-tier cases call `repaired()` directly and are SKIPPED until it
        is built. Returns the connection and which database it is, so a report
        never claims a baseline result was an integrated one.
        """
        return self.baseline(), "current_reference"

    def _ro(self, path: pathlib.Path, immutable: bool) -> sqlite3.Connection:
        uri = f"file:{path}?mode=ro" + ("&immutable=1" if immutable else "")
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        self._cons.append(con)
        return con

    # ---- disposable copies

    def clone(self, name: str, source: pathlib.Path | None = None) -> pathlib.Path:
        """Copy a database into this run's scratch dir under a NEW name.

        Never writes a canonical DB and never deletes one: each run gets its own
        directory, so a rebuild is a new file rather than a removal.
        """
        src = source or BASELINE_DB
        if not src.is_file():
            raise TierUnavailable(f"source database absent: {src}")
        dst = self.scratch / f"{name}.sqlite"
        if dst.exists():                       # same run, same name -> distinct file
            dst = self.scratch / f"{name}_{len(list(self.scratch.glob('*.sqlite')))}.sqlite"
        shutil.copyfile(src, dst)
        return dst

    def rw(self, path: pathlib.Path) -> sqlite3.Connection:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        self._cons.append(con)
        return con

    def subset_db(self, name: str, tables: dict[str, list[sqlite3.Row]]) -> sqlite3.Connection:
        """Build a small disposable DB carrying only the rows a check needs.

        Cloning a 700 MB reference database per mutation is slow enough that people stop
        running the suite, which is the wrong trade for a check whose value is
        being run often. The schema comes from the real schema.sql, so a subset
        still has the real constraints.
        """
        path = self.scratch / f"{name}.sqlite"
        if path.exists():
            path = self.scratch / f"{name}_{len(list(self.scratch.glob('*.sqlite')))}.sqlite"
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.executescript((TREE / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
        for table, rows in tables.items():
            for r in rows:
                d = dict(r)
                cols = ",".join(d)
                con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) "
                            f"VALUES ({','.join('?' * len(d))})", list(d.values()))
        con.commit()
        self._cons.append(con)
        return con

    # ---- tier gates

    def need_file(self, path: pathlib.Path, why: str) -> pathlib.Path:
        if not path.is_file():
            raise TierUnavailable(f"{why}: {path}")
        return path

    def need_dir(self, path: pathlib.Path, why: str) -> pathlib.Path:
        if not path.is_dir():
            raise TierUnavailable(f"{why}: {path}")
        return path

    def close(self):
        for c in self._cons:
            with contextlib.suppress(Exception):
                c.close()


class ReadOnlyStaging:
    """A `Staging`-shaped read-only view, for driving validate.py checks safely.

    `ferclib.staging.Staging.__init__` sets `PRAGMA journal_mode=WAL`, which is a
    WRITE. Constructing it against a protected tree rewrites that database's
    header even when nothing is inserted -- it silently moved the delivered
    baseline off its audited bytes once already. Anything in this package that
    needs a staging-like object over a protected database uses this instead.
    """

    def __init__(self, path: pathlib.Path, *, immutable: bool = True):
        self.path = pathlib.Path(path)
        uri = f"file:{self.path}?mode=ro" + ("&immutable=1" if immutable else "")
        self.con = sqlite3.connect(uri, uri=True)
        self.con.row_factory = sqlite3.Row

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, params).fetchall()

    def counts(self) -> dict[str, int]:
        out = {}
        for (t,) in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            out[t] = self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out

    def close(self):
        with contextlib.suppress(Exception):
            self.con.close()


class Ctx:
    """Minimal validate.py-shaped context."""

    def __init__(self, staging):
        self.staging = staging


# ------------------------------------------------------------------ byte hygiene

def read_bytes_and_size(path: pathlib.Path) -> tuple[bytes, int, int]:
    """Return (bytes, true size from the bytes, size stat() claimed BEFORE reading).

    On this volume `stat().st_size` returns a stale placeholder size for a
    cloud-evicted file until the bytes are actually read. Any size or hash taken
    without reading is therefore unreliable, which is why every size this
    workstream records comes from `len(read_bytes())`.
    """
    stat_before = path.stat().st_size
    data = path.read_bytes()
    return data, len(data), stat_before


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def code_only(source: str) -> str:
    """Strip comments and string literals, leaving executable code.

    A source check that greps raw text fails on the documentation of the very fix
    it is checking for -- `build_release.py` explains at length why it does NOT
    call `stat().st_size`, and a naive grep reads that explanation as the defect.
    Tokenising first means the check sees what the module DOES, not what it says.
    """
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return source                       # unparseable: fall back to the raw text
    return " ".join(out)


def sha256_read(path: pathlib.Path) -> tuple[str, int]:
    """Hash and size taken from ONE read of the bytes. Never from stat()."""
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


# ------------------------------------------------------------------ runner

def run(cases: list[Case], env: Env, only_tier: str | None = None,
        only_issue: str | None = None) -> dict:
    results: list[dict] = []
    for case in cases:
        if only_tier and case.tier != only_tier:
            continue
        if only_issue and case.issue != only_issue:
            continue
        rec = {"issue": case.issue, "tier": case.tier, "group": case.group,
               "owner": case.owner, "check": case.name,
               "mutation_rejected": case.mutation}
        try:
            detail = case.fn(env)
            if case.xfail_reason:
                rec.update(status=Status.XPASS, detail=(
                    f"acceptance target now MET -- remove the xfail marker. "
                    f"was: {case.xfail_reason}. result: {detail}"))
            else:
                rec.update(status=Status.PASS, detail=str(detail or ""))
        except TierUnavailable as exc:
            rec.update(status=Status.SKIPPED, detail=str(exc))
        except AssertionError as exc:
            if case.xfail_reason:
                rec.update(status=Status.XFAIL,
                           detail=f"EXPECTED FAILURE until fixed -- {case.xfail_reason}",
                           failure=str(exc))
            else:
                rec.update(status=Status.FAIL, detail=str(exc))
        except Exception as exc:                                    # noqa: BLE001
            rec.update(status=Status.ERROR,
                       detail=f"{type(exc).__name__}: {exc}",
                       traceback=traceback.format_exc()[-2000:])
        results.append(rec)
        icon = {Status.PASS: "ok   ", Status.FAIL: "FAIL ", Status.ERROR: "ERROR",
                Status.SKIPPED: "skip ", Status.XFAIL: "xfail",
                Status.XPASS: "XPASS"}[rec["status"]]
        print(f"  [{icon}] {case.issue:<6} {case.name}")
        if rec["status"] in (Status.FAIL, Status.ERROR):
            for line in str(rec["detail"]).splitlines()[:6]:
                print(f"            {line}")
        elif rec["status"] in (Status.SKIPPED, Status.XFAIL, Status.XPASS):
            print(f"            {str(rec['detail']).splitlines()[0][:150]}")
    return summarise(results)


def summarise(results: list[dict]) -> dict:
    def count(rs, s):
        return sum(1 for r in rs if r["status"] == s)

    by_tier = {}
    for tier in (Tier.WORKER, Tier.INTEGRATED, Tier.EXTERNAL):
        rs = [r for r in results if r["tier"] == tier]
        if not rs:
            continue
        by_tier[tier] = {s: count(rs, s) for s in
                         (Status.PASS, Status.FAIL, Status.ERROR,
                          Status.SKIPPED, Status.XFAIL, Status.XPASS)}
        by_tier[tier]["total"] = len(rs)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "totals": {s: count(results, s) for s in
                   (Status.PASS, Status.FAIL, Status.ERROR,
                    Status.SKIPPED, Status.XFAIL, Status.XPASS)},
        "by_tier": by_tier,
        "failures": [r for r in results if r["status"] in (Status.FAIL, Status.ERROR)],
        "expected_failing_targets": [r for r in results if r["status"] == Status.XFAIL],
        "targets_now_met": [r for r in results if r["status"] == Status.XPASS],
        "skipped": [r for r in results if r["status"] == Status.SKIPPED],
        "results": results,
    }
