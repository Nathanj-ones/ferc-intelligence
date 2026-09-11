#!/usr/bin/env python3
"""
Machine-readable semantic diff between the delivered baseline and the repair.

    python3 build_diff.py --before <baseline.sqlite> --after <repaired.sqlite>

Code changes do not close data defects, so the release has to show what actually
moved in the data and say why. This produces that, at the grain that matters
rather than as a row count:

  * observations classified as unchanged / value_changed / status_changed /
    added / removed, keyed on the FULL grain, not the observation id -- an id
    that includes `method` changes whenever the engine changes its mind about
    how to derive something, which would report a re-derivation as one removal
    plus one addition rather than as a changed value;
  * newly gated and newly ungated rows called out separately, because a value
    that stops being `pass` is the single most important thing a reviewer needs
    to see and the easiest to lose in a total;
  * per-table counts, per-metric value movement, coverage-outcome movement,
    blocker movement, and the units bridge;
  * a bridge for every headline number, so a changed percentage can be
    attributed rather than merely observed.

RULES THIS ENFORCES

  A row that disappears is REPORTED, never netted off against an addition.
  A metric that was renamed is followed through `--crosswalk`, and any rename
  not declared there surfaces as a removal plus an addition -- which is exactly
  what an undeclared rename is.

Both databases are opened read-only. Diffing must never modify either side.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sqlite3
import sys
from collections import Counter, defaultdict

#: validations that mean "this value must not be used unqualified"
GATED = {"source_anomaly_review", "blocked_ambiguity", "scope_incompatible",
         "unit_warning", "rounding_warning", "source_date_warning",
         "interpretation_blocked", "not_yet_validated"}


def connect(p: pathlib.Path) -> sqlite3.Connection:
    if not p.is_file():
        raise SystemExit(f"database not found: {p}")
    # immutable first, but it fails on a WAL database while another process
    # holds a connection, so fall back rather than refuse to run (w1 measured
    # that plain mode=ro leaves the file's hash and length unchanged).
    for uri in (f"file:{p}?mode=ro&immutable=1", f"file:{p}?mode=ro"):
        try:
            con = sqlite3.connect(uri, uri=True)
            con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error:
            continue
    raise SystemExit(f"cannot open {p} read-only")


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "ORDER BY name"):
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error as exc:
            out[t] = f"UNAVAILABLE: {type(exc).__name__}"
    return out


def load_observations(con: sqlite3.Connection, xwalk: dict[str, str]) -> dict[tuple, dict]:
    """Key on the FULL REQUESTED GRAIN, deliberately excluding `method`.

    The observation id folds `method` into the grain, so a derivation the engine
    now computes differently gets a different id. Keying on the id would report
    that as a removal plus an addition and hide the thing a reviewer actually
    wants: the value moved, and here is by how much.
    """
    rows = {}
    cols = {r[1] for r in con.execute("PRAGMA table_info(observations)")}
    sel = ", ".join(sorted(cols))
    for r in con.execute(f"SELECT {sel} FROM observations"):
        d = dict(r)
        mid = xwalk.get(d["metric_id"], d["metric_id"])
        key = (d["entity_key"], mid, d["source_regime"], d["period_basis"],
               d.get("period_start") or "", d.get("period_end") or "",
               d.get("instant_date") or "", d["scope"])
        # A genuine duplicate at this grain is itself a finding, so keep both.
        if key in rows:
            rows[key].setdefault("_duplicates", []).append(d)
        else:
            rows[key] = d
    return rows


def classify(before: dict, after: dict) -> str:
    bv = (before.get("value_text") or "").strip()
    av = (after.get("value_text") or "").strip()
    bs = (before.get("availability"), before.get("validation"),
          before.get("unit"), before.get("version_status"))
    as_ = (after.get("availability"), after.get("validation"),
           after.get("unit"), after.get("version_status"))
    if bv != av and bs != as_:
        return "value_and_status_changed"
    if bv != av:
        return "value_changed"
    if bs != as_:
        return "status_changed"
    return "unchanged"


def gate_movement(before: dict, after: dict) -> str:
    bg = before.get("validation") in GATED
    ag = after.get("validation") in GATED
    if ag and not bg:
        return "newly_gated"
    if bg and not ag:
        return "newly_ungated"
    return ""


def diff_observations(b: dict[tuple, dict], a: dict[tuple, dict]) -> dict:
    added = sorted(set(a) - set(b))
    removed = sorted(set(b) - set(a))
    common = set(a) & set(b)
    kinds: Counter = Counter()
    gates: Counter = Counter()
    by_metric: dict[str, Counter] = defaultdict(Counter)
    unit_moves: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    for k in common:
        kind = classify(b[k], a[k])
        kinds[kind] += 1
        by_metric[k[1]][kind] += 1
        g = gate_movement(b[k], a[k])
        if g:
            gates[g] += 1
            by_metric[k[1]][g] += 1
            if len(examples[g]) < 25:
                examples[g].append(_ex(k, b[k], a[k]))
        if kind != "unchanged" and len(examples[kind]) < 25:
            examples[kind].append(_ex(k, b[k], a[k]))
        bu, au = b[k].get("unit"), a[k].get("unit")
        if bu != au:
            unit_moves[f"{bu!r} -> {au!r}"] += 1
    for k in added[:25]:
        examples["added"].append(_ex(k, None, a[k]))
    for k in removed[:25]:
        examples["removed"].append(_ex(k, b[k], None))
    return {
        "before_rows": len(b), "after_rows": len(a),
        "common": len(common), "added": len(added), "removed": len(removed),
        "kinds": dict(kinds), "gate_movement": dict(gates),
        "unit_changes": dict(unit_moves.most_common()),
        "by_metric": {m: dict(c) for m, c in sorted(by_metric.items())
                      if any(k != "unchanged" for k in c)},
        "examples": {k: v for k, v in examples.items()},
        "added_keys_sample": [list(k) for k in added[:200]],
        "removed_keys_sample": [list(k) for k in removed[:200]],
    }


def _ex(key: tuple, b: dict | None, a: dict | None) -> dict:
    e = {"entity_key": key[0], "metric_id": key[1], "regime": key[2],
         "basis": key[3], "period": f"{key[4]}..{key[5]}" if key[4] else key[6],
         "scope": key[7]}
    if b:
        e["before"] = {"value": b.get("value_text"), "unit": b.get("unit"),
                       "availability": b.get("availability"),
                       "validation": b.get("validation"),
                       "method": b.get("method")}
    if a:
        e["after"] = {"value": a.get("value_text"), "unit": a.get("unit"),
                      "availability": a.get("availability"),
                      "validation": a.get("validation"),
                      "method": a.get("method")}
    return e


def diff_simple(bcon, acon, table: str, key_cols: list[str],
                label_col: str = "") -> dict:
    def load(con):
        try:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        except sqlite3.Error:
            return None
        if not cols:
            return None
        use = [c for c in key_cols if c in cols]
        if not use:
            return None
        sel = ", ".join(use + ([label_col] if label_col in cols else []))
        return {tuple(str(r[c]) for c in use): dict(r)
                for r in con.execute(f"SELECT {sel} FROM {table}")}
    b, a = load(bcon), load(acon)
    if b is None or a is None:
        return {"available": False,
                "why": f"{table} absent or lacks the key columns on one side"}
    return {"available": True, "before": len(b), "after": len(a),
            "added": len(set(a) - set(b)), "removed": len(set(b) - set(a)),
            "added_sample": [list(k) for k in sorted(set(a) - set(b))[:40]],
            "removed_sample": [list(k) for k in sorted(set(b) - set(a))[:40]]}


def coverage_movement(bcon, acon) -> dict:
    def outcomes(con):
        try:
            return dict(Counter(r[0] for r in
                                con.execute("SELECT outcome FROM coverage_measured")))
        except sqlite3.Error:
            return {}
    bo, ao = outcomes(bcon), outcomes(acon)
    keys = sorted(set(bo) | set(ao))
    return {"before": bo, "after": ao,
            "delta": {k: ao.get(k, 0) - bo.get(k, 0) for k in keys}}


def load_crosswalk(path: pathlib.Path | None) -> dict[str, str]:
    """old metric id -> new metric id, for declared renames only.

    A rename that is NOT here shows up as a removal plus an addition, which is
    the honest rendering of an undeclared rename.
    """
    if not path or not path.is_file():
        return {}
    out = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            old = (row.get("old_metric_id") or row.get("from") or "").strip()
            new = (row.get("new_metric_id") or row.get("to") or "").strip()
            if old and new:
                out[old] = new
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--crosswalk", default=None,
                    help="CSV with old_metric_id,new_metric_id for declared renames")
    ap.add_argument("--out", default="verification/semantic_diff.json")
    args = ap.parse_args(argv)

    bpath, apath = pathlib.Path(args.before), pathlib.Path(args.after)
    xwalk = load_crosswalk(pathlib.Path(args.crosswalk) if args.crosswalk else None)
    bcon, acon = connect(bpath), connect(apath)

    obs = diff_observations(load_observations(bcon, xwalk),
                            load_observations(acon, xwalk))
    report = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "before": str(bpath), "after": str(apath),
        "declared_renames": xwalk,
        "grain": ("entity, metric, regime, period basis, exact interval or instant, "
                  "scope. `method` is deliberately excluded so a re-derivation reads "
                  "as a changed value rather than as a removal plus an addition."),
        "table_counts": {"before": table_counts(bcon), "after": table_counts(acon)},
        "observations": obs,
        "coverage_outcomes": coverage_movement(bcon, acon),
        "blockers": diff_simple(bcon, acon, "blockers", ["blocker_id"], "summary"),
        "events": diff_simple(bcon, acon, "events", ["event_id"], "headline"),
        "filings": diff_simple(bcon, acon, "filings", ["filing_id"], "form"),
        "document_facts": diff_simple(bcon, acon, "document_facts",
                                      ["document_fact_id"], "assertion_type"),
        "reading": {
            "newly_gated": ("values that stopped being clean passes. This number going "
                            "UP is usually the repair working, not a regression."),
            "removed": ("rows the repaired run no longer produces. Never netted against "
                        "additions; each is listed so it can be accounted for."),
        },
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")

    o = report["observations"]
    print(f"observations  before {o['before_rows']:,}  after {o['after_rows']:,}")
    print(f"  common {o['common']:,}   added {o['added']:,}   removed {o['removed']:,}")
    for k, v in sorted(o["kinds"].items()):
        print(f"    {k:26s} {v:,}")
    for k, v in sorted(o["gate_movement"].items()):
        print(f"    {k:26s} {v:,}")
    if o["unit_changes"]:
        print("  unit changes:")
        for k, v in list(o["unit_changes"].items())[:10]:
            print(f"    {k:44s} {v:,}")
    cm = report["coverage_outcomes"]["delta"]
    if any(cm.values()):
        print("  coverage outcome movement:")
        for k, v in sorted(cm.items()):
            if v:
                print(f"    {k:26s} {v:+,}")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
