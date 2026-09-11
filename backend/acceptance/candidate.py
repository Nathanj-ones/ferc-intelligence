"""
Build a disposable candidate release tree, then mutate it.

A13 and A21 can only be falsified against a real candidate that a real builder
runs on. Cloning the delivered tree is not an option -- it is a read-only
protected tree and it carries a 288 MB content-addressed cache and a 700 MB
database -- so this assembles a small, complete-enough candidate in the run's
scratch directory:

  * every code file the extracted offline suite needs;
  * the real exports, which are what the required-output contract is about;
  * a SMALL staging database carrying real rows, so the contract's
    non-empty-if-table rule has genuine counts to work against rather than an
    empty database that satisfies every rule vacuously.

Nothing here writes to a protected tree and nothing is ever deleted to make a
check pass: each run gets its own directory.
"""

from __future__ import annotations

import csv
import pathlib
import shutil
import sqlite3

from .harness import BASELINE_DB, BASELINE_TREE, TREE, TierUnavailable

#: code the extracted clean-room suite imports
CODE_DIRS = ("ferclib", "adapters", "tests")
CODE_GLOBS = ("*.py",)
#: content the required-output contract is about
DATA_DIRS = ("exports", "config")
# Required, hash-pinned historical evidence and synthetic positive controls.
# These must travel with a standalone candidate; acceptance is not permitted to
# rediscover them from a sibling working tree.
EVIDENCE_DIRS = (pathlib.Path("evidence") / "test_fixtures",)

#: tables sampled into the small candidate database. `observations`, `filings`
#: and `coverage_expected` are the contract's MINIMUM_VIABLE_POPULATION, so they
#: must carry rows or the candidate is correctly judged unpublishable.
SAMPLE_TABLES = {
    "entities": 40, "filings": 400, "observations": 4000,
    "lineage_edges": 4000, "coverage_expected": 4000, "coverage_measured": 4000,
    "documents": 200, "document_facts": 200, "events": 200, "blockers": 60,
    "reviewed_source_annotations": 20, "field_status": 200, "source_manifest": 200,
}


#: Rows kept per CSV in a slim candidate. The delivered exports run to ~105 MB
#: and every one of them is a cloud-evicted placeholder, so a faithful copy costs
#: a full download per candidate. A slim candidate keeps the real header and a
#: real prefix of the real rows: every column contract stays exactly as strict,
#: and every output stays non-empty over a populated table, which is all the
#: required-output contract examines. Nothing about the mutations changes.
SLIM_ROWS = 200


def _slim_copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy a file, truncating a CSV to its header plus SLIM_ROWS data rows."""
    if src.suffix != ".csv":
        shutil.copyfile(src, dst)
        return
    # Count CSV RECORDS, not physical lines.  Document passages legitimately
    # contain quoted newlines; truncating after a physical line can stop inside
    # one quoted field and create a malformed candidate that never reaches the
    # mutation being tested.
    with src.open("r", encoding="utf-8", errors="replace", newline="") as source, \
            dst.open("w", encoding="utf-8", newline="") as target:
        reader = csv.reader(source)
        writer = csv.writer(target, lineterminator="\n")
        try:
            writer.writerow(next(reader))
        except StopIteration:
            return
        for number, row in enumerate(reader):
            if number >= SLIM_ROWS:
                break
            writer.writerow(row)


def build_candidate(env, name: str, *, source_tree: pathlib.Path | None = None,
                    with_database: bool = True, slim: bool = True) -> pathlib.Path:
    """Assemble a candidate release tree under this run's scratch directory."""
    # CODE comes from the WORKING tree and DATA from the explicitly configured
    # current reference. Historical assertions never use this path: they are
    # release-relative hash-pinned fixtures. Keeping the reference explicit
    # avoids the old standalone-release alias where a sibling "baseline" silently
    # resolved back to the candidate under test.
    code_src = source_tree or TREE
    data_src = BASELINE_TREE
    if not code_src.is_dir():
        raise TierUnavailable(f"code tree for a candidate release absent: {code_src}")
    root = env.scratch / name
    if root.exists():
        root = env.scratch / f"{name}_{len(list(env.scratch.iterdir()))}"
    root.mkdir(parents=True)

    for pattern in CODE_GLOBS:
        for f in code_src.glob(pattern):
            shutil.copyfile(f, root / f.name)
    for d in CODE_DIRS + ("acceptance",):
        if (code_src / d).is_dir():
            shutil.copytree(code_src / d, root / d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for rel in EVIDENCE_DIRS:
        source = code_src / rel
        if not source.is_dir():
            raise TierUnavailable(f"required acceptance fixtures absent: {source}")
        shutil.copytree(source, root / rel)
    copy = _slim_copy if slim else shutil.copyfile
    for d in DATA_DIRS:
        source = data_src / d
        if not source.is_dir() or not any(source.iterdir()):
            source = code_src / d
        if not source.is_dir():
            continue
        for f in sorted(source.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            target = root / d / f.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            copy(f, target)
    (root / "verification").mkdir(exist_ok=True)

    if with_database:
        _small_database(root / "staging" / "operating_assets.sqlite")
    return root


def _small_database(dst: pathlib.Path) -> None:
    """A small database of REAL rows, so the emptiness rule has real counts."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(dst)
    con.executescript((TREE / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys=OFF")
    src = sqlite3.connect(f"file:{BASELINE_DB}?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row
    for table, limit in SAMPLE_TABLES.items():
        try:
            rows = src.execute(f"SELECT * FROM {table} LIMIT {limit}").fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            d = dict(r)
            try:
                con.execute(f"INSERT OR IGNORE INTO {table} ({','.join(d)}) "
                            f"VALUES ({','.join('?' * len(d))})", list(d.values()))
            except sqlite3.Error:
                continue
    con.commit()
    src.close()
    con.close()


_SHARED: dict[int, pathlib.Path] = {}


def shared(env) -> pathlib.Path:
    """One reusable intact candidate per run, for mutate-and-restore checks.

    Building a fresh candidate per mutation costs a full download of every
    evicted export each time. Mutations that only read the tree (the contract
    checks) share this one and put it back afterwards, so each check still runs
    against a real, intact starting state.
    """
    key = id(env)
    if key not in _SHARED:
        _SHARED[key] = build_candidate(env, "shared_candidate")
    return _SHARED[key]


class mutated:
    """Apply a mutation to a shared candidate, then restore it exactly.

    Restoration is from the bytes held in memory, so a check can never leave a
    damaged candidate behind for the next one and a later pass can never be
    explained by an earlier mutation.
    """

    def __init__(self, root: pathlib.Path, rel: str, how: str = "remove",
                 column: str | None = None):
        self.path = root / rel
        self.root, self.rel, self.how, self.column = root, rel, how, column
        self.original: bytes | None = None

    def __enter__(self) -> pathlib.Path:
        if not self.path.is_file():
            raise AssertionError(
                f"cannot mutate {self.rel}: it is not present in the candidate, so its "
                "mutation would prove nothing")
        self.original = self.path.read_bytes()
        if self.how == "remove":
            self.path.unlink()
        elif self.how == "blank":
            self.path.write_bytes(b"")
        elif self.how == "drop_column":
            drop_column(self.root, self.rel, self.column)
        else:
            raise ValueError(f"unknown mutation {self.how!r}")
        return self.path

    def __exit__(self, *exc):
        if self.original is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(self.original)
        return False


# ------------------------------------------------------------------ mutations

def remove_output(root: pathlib.Path, rel: str) -> pathlib.Path:
    """Delete a declared required output from the candidate. Returns the path."""
    p = root / rel
    if not p.is_file():
        raise AssertionError(f"cannot mutate {rel}: it is not present in the candidate, "
                             "so its removal would prove nothing")
    p.unlink()
    return p


def blank_output(root: pathlib.Path, rel: str) -> pathlib.Path:
    """Truncate a declared required output to zero bytes.

    The subtler A13 shape: the file still EXISTS, so an inventory-based manifest
    is perfectly happy with it, but it carries no data.
    """
    p = root / rel
    if not p.is_file():
        raise AssertionError(f"cannot blank {rel}: it is not present in the candidate")
    p.write_bytes(b"")
    return p


def drop_column(root: pathlib.Path, rel: str, column: str) -> pathlib.Path:
    """Remove one column from a declared CSV output, keeping every row.

    This is the "drop the warning" shape: the file is present, the row count is
    unchanged, and only the column that qualifies the values is gone.
    """
    import csv
    import io

    p = root / rel
    text = p.read_bytes().decode("utf-8", errors="replace")
    rdr = csv.reader(io.StringIO(text))
    header = next(rdr)
    if column not in header:
        raise AssertionError(f"cannot drop {column} from {rel}: it is not a column there")
    keep = [i for i, c in enumerate(header) if c != column]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([header[i] for i in keep])
    for row in rdr:
        w.writerow([row[i] if i < len(row) else "" for i in keep])
    p.write_bytes(buf.getvalue().encode("utf-8"))
    return p
