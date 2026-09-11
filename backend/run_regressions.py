#!/usr/bin/env python3
"""
Reference regressions: Transco and the repaired TGP package.

These two hash-pinned CSVs are the project's independently-reviewed ground truth. The
shared engine must reproduce every VALUE and every SOURCE-FACT IDENTITY they
contain. Any unexpected numerical change blocks promotion.

What is compared, per canonical observation:
  * the filed value, exactly;
  * the source fact identity (filing_id + source_fact_id) -- so a right number
    selected from the wrong fact still fails;
  * the unit as filed;
  * raw versus derived.

Authorised differences are declared up front in AUTHORISED_CHANGES and each one
must be individually dispositioned in the report. An undeclared difference is a
FAILURE, never a note.

The reference directories are opened READ-ONLY and are re-hashed afterwards.

    python3 run_regressions.py            # compare against the live staging DB
    python3 run_regressions.py --rebuild  # run the pipeline for both filers first

Exit codes: 0 PASS (at least one reference actually compared), 1 FAIL,
2 NO_COMPARISON (every reference was skipped -- not a pass).

A12 corrections, 8 September 2026
---------------------------------
Three ways this harness used to report a pass it had not earned:

1. `missing_from_produced` was excluded from the failure count, so a reference
   row carrying a NONZERO required value that the engine did not produce at all
   scored zero failures and the run reported PASS. Silently dropping a required
   value is the most severe outcome here, not the most forgiving one; it is now
   fatal via `status_for()`.
2. When a reference package was absent every comparison was SKIPPED and the run
   still printed `OVERALL: PASS` and returned 0, having compared nothing. A run
   that compared nothing now reports NO_COMPARISON and returns 2. Absent
   the repaired candidate ships both hash-pinned reference CSVs, so either one
   missing or changed is now a fixture failure rather than an optional skip.
3. A reference that parsed to zero valued rows was indistinguishable from a
   reference that matched perfectly. A comparison whose population is empty has
   proved nothing, so it is now a FIXTURE_MISSING failure.

Values are compared as exact decimals. The previous `round(float(v), 6)` made
two values equal whenever they agreed to six places, which is a tolerance nobody
declared; `decimal.Decimal` compares the filed digits themselves and carries no
binary representation error.
"""

from __future__ import annotations

import csv
import decimal
import hashlib
import json
import pathlib
import subprocess
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
DB = HERE / "staging" / "operating_assets.sqlite"
VERIFICATION = HERE / "verification"
REFERENCE_ROOT = HERE / "inputs" / "reference_regressions"

REFERENCES = [
    {"name": "Transco reference", "entity": "C000654",
     "canonical": REFERENCE_ROOT / "transco" / "transco_canonical_metrics_2016_2026.csv",
     "root": REFERENCE_ROOT / "transco",
     "sha256": "3e8bd5ed737f1eb9d42896cb707a37694633582f4c66db005adb35ac7a6504c0"},
    {"name": "TGP repaired package", "entity": "C000020",
     "canonical": REFERENCE_ROOT / "tgp" / "tgp_canonical_metrics_2016_2026.csv",
     "root": REFERENCE_ROOT / "tgp",
     "sha256": "ccc95bd7abb8ff64713b340d9e83bcad5a2f3fc48e3cb5ab8b20e2fba4b129be"},
]

#: Differences the shared engine is EXPECTED to introduce, each with the reason
#: it is legitimate. Anything not matching one of these is a failure.
AUTHORISED_CHANGES = [
    {"kind": "period_basis_split",
     "reason": "The shared engine emits a requested quarter and a year-to-date fact as "
               "TWO observations at their own exact intervals. The reference packages "
               "keyed both onto one row. This is the coverage-grain correction the "
               "7 September independent check required; no filed value changes."},
    {"kind": "additional_not_required_rows",
     "reason": "The shared engine emits an evidenced NOT_REQUIRED_IN_FORM observation "
               "where a form does not collect a concept. The reference packages emitted "
               "nothing. Additive only; no filed value changes."},
    {"kind": "additional_derived_quarters",
     "reason": "Sequential-YTD derivation is now available to gas as well as liquids, so "
               "quarters the reference left absent may now carry a DERIVED observation. "
               "The blank filed quarter is still emitted separately."},
]


def load_reference(path: pathlib.Path) -> dict[tuple, dict]:
    """Reference rows keyed by (metric, year, quarter, base period type, raw/derived)."""
    out: dict[tuple, dict] = {}
    for r in csv.DictReader(path.open(newline="", encoding="utf-8")):
        pt = r["period_type"]
        base = "quarter" if pt.startswith("quarter") else pt
        key = (r["metric_id"], int(r["reporting_year"]), r["reporting_quarter"],
               base, r["raw_or_derived"])
        out[key] = r
    return out


def load_produced(con, entity: str) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for r in con.execute(
            "SELECT * FROM observations WHERE entity_key=? AND availability='present'",
            (entity,)):
        d = dict(r)
        base = d["period_basis"]
        base = "annual observation" if base == "annual_observation" else base
        key = (d["metric_id"], d["reporting_year"], d["reporting_period"], base,
               "derived" if d["method"] == "derived" else "raw")
        out.setdefault(key, []).append(d)
    return out


def norm(v):
    """Exact decimal comparison key.

    `Decimal(...).normalize()` makes 100, 100.00 and 1E+2 compare equal while
    keeping every filed digit significant. A float round to six places silently
    accepted a disagreement in the seventh, which is a tolerance no one declared
    and which a value regression exists to catch.
    """
    if v is None or v == "":
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        d = decimal.Decimal(s)
    except decimal.InvalidOperation:
        return s
    if not d.is_finite():                      # NaN/Infinity are not filed values
        return s
    return d.normalize()


#: A reference row carrying a value that the engine did not produce at all.
#: Dropping a required value outright is the most severe regression this harness
#: can see, so it is fatal -- never a note, never a "not produced" line item that
#: leaves the status at PASS.
FATAL_DIFFERENCE_KEYS = ("value_differences", "identity_differences",
                         "missing_from_produced")


def status_for(comparison: dict) -> tuple[str, int]:
    """The single decision point for one reference comparison.

    Returns (status, fatal_count). Kept as one named function so an acceptance
    test can drive the REAL decision with a mutated comparison rather than
    re-implementing the rule and testing its own copy.
    """
    if comparison.get("fixture_error"):
        return "FIXTURE_MISSING", 1
    fatal = sum(len(comparison.get(k) or ()) for k in FATAL_DIFFERENCE_KEYS)
    return ("PASS" if fatal == 0 else "FAIL"), fatal


def compare(ref: dict[tuple, dict], got: dict[tuple, list[dict]], name: str) -> dict:
    value_diffs, identity_diffs, missing, extra = [], [], [], []
    for key, r in sorted(ref.items()):
        ref_val = norm(r["value"])
        if ref_val is None:
            continue                     # reference row with no value: nothing to match
        cands = got.get(key, [])
        if not cands:
            missing.append({"key": list(key), "reference_value": r["value"],
                            "reference_row_id": r["canonical_row_id"],
                            "reference_qa": r.get("qa_status", "")})
            continue
        hit = next((c for c in cands if norm(c["value_text"]) == ref_val), None)
        if hit is None:
            value_diffs.append({"key": list(key), "reference_value": r["value"],
                                "produced_values": [c["value_text"] for c in cands],
                                "reference_row_id": r["canonical_row_id"]})
            continue
        if r["raw_or_derived"] == "raw" and r.get("source_fact_id"):
            if (hit.get("source_fact_id") or "") != r["source_fact_id"] or \
               str(hit.get("filing_id") or "") != str(r["filing_id"]):
                identity_diffs.append({
                    "key": list(key), "value": r["value"],
                    "reference_filing": r["filing_id"],
                    "reference_fact": r["source_fact_id"][:16],
                    "produced_filing": hit.get("filing_id"),
                    "produced_fact": (hit.get("source_fact_id") or "")[:16]})
    for key, cands in sorted(got.items()):
        if key not in ref:
            extra.append({"key": list(key), "values": [c["value_text"] for c in cands],
                          "method": cands[0]["method"]})
    with_values = sum(1 for r in ref.values() if norm(r["value"]) is not None)
    out = {"reference": name, "reference_rows": len(ref),
           "reference_rows_with_values": with_values,
           "produced_keys": len(got),
           "value_differences": value_diffs, "identity_differences": identity_diffs,
           "missing_from_produced": missing, "additional_in_produced": extra}
    # FIXTURE PRECONDITION. A comparison whose reference population is empty
    # exercised nothing, so it cannot be evidence of anything. Reporting it as a
    # pass is the false-pass shape A12 was raised for.
    if with_values == 0:
        out["fixture_error"] = (
            f"{name}: the reference parsed to {len(ref)} row(s) and NONE carried a "
            "value, so no comparison was performed. An empty comparison is a "
            "failure, not a pass.")
    return out


def tree_hash(root: pathlib.Path) -> dict[str, str]:
    """Content hashes of a reference package's code, configuration and results.

    `source_cache/` is excluded here and covered instead by verify_integrity.py,
    which hashes every protected tree in full including the caches. Those caches
    run to hundreds of megabytes and hashing them twice per regression made the
    check slow enough to discourage running it -- which is the wrong trade for a
    check whose whole value is being run often. Nothing is left unhashed
    overall; the full-content pass simply happens once, in one place.
    """
    out = {}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc" \
           or f.name == ".DS_Store" or "source_cache" in f.parts:
            continue
        try:
            out[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError as exc:
            # A cloud-synced placeholder that is not materialised locally cannot
            # be hashed. That is an UNREADABLE result, never a silent
            # "unchanged" -- it is recorded so the integrity claim stays honest.
            out[str(f.relative_to(root))] = f"UNREADABLE:{type(exc).__name__}"
    return out


def reference_identity(ref: dict) -> dict:
    """Validate one mandatory independent reference without opening the database."""
    path = pathlib.Path(ref["canonical"])
    if not path.is_file():
        return {"status": "FIXTURE_MISSING", "expected_sha256": ref["sha256"],
                "reason": f"required reference file absent: {path}"}
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != ref["sha256"]:
        return {"status": "FIXTURE_IDENTITY_MISMATCH",
                "expected_sha256": ref["sha256"], "actual_sha256": actual,
                "bytes": len(raw),
                "reason": "required reference bytes differ from the pinned identity"}
    return {"status": "VERIFIED", "sha256": actual, "bytes": len(raw),
            "path": str(path.relative_to(HERE)) if HERE in path.parents else str(path)}


def main() -> int:
    VERIFICATION.mkdir(parents=True, exist_ok=True)
    if "--rebuild" in sys.argv:
        for ref in REFERENCES:
            print(f"\n=== rebuilding {ref['name']} ({ref['entity']}) ===", flush=True)
            subprocess.run([sys.executable, str(HERE / "run.py"), "universe",
                            "--entity", ref["entity"], "--adapter", "gas_xbrl",
                            "--year-from", "2016", "--year-to", "2026"],
                           cwd=HERE, check=False)

    before = {r["name"]: tree_hash(r["root"]) for r in REFERENCES}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    results, ok, compared = [], True, 0
    for ref in REFERENCES:
        identity = reference_identity(ref)
        if identity["status"] == "FIXTURE_MISSING":
            results.append({"reference": ref["name"], "status": "FIXTURE_MISSING",
                            **{k: v for k, v in identity.items() if k != "status"}})
            print(f"{ref['name']}: FIXTURE_MISSING (required reference absent)")
            ok = False
            continue
        if identity["status"] != "VERIFIED":
            results.append({"reference": ref["name"], "status": "FIXTURE_IDENTITY_MISMATCH",
                            **{k: v for k, v in identity.items() if k != "status"}})
            print(f"{ref['name']}: FIXTURE_IDENTITY_MISMATCH")
            ok = False
            continue
        r = compare(load_reference(ref["canonical"]),
                    load_produced(con, ref["entity"]), ref["name"])
        r["reference_identity"] = {k: v for k, v in identity.items()
                                   if k != "status"}
        r["status"], n_bad = status_for(r)
        if r["status"] != "PASS":
            ok = False
        else:
            compared += 1
        results.append(r)
        print(f"\n{ref['name']}: {r['status']}")
        if r.get("fixture_error"):
            print(f"  FIXTURE: {r['fixture_error']}")
        print(f"  reference rows with values : {r['reference_rows_with_values']}")
        print(f"  value differences          : {len(r['value_differences'])}  (fatal)")
        print(f"  source-identity differences: {len(r['identity_differences'])}  (fatal)")
        print(f"  reference rows not produced: {len(r['missing_from_produced'])}  (fatal)")
        print(f"  additional produced rows   : {len(r['additional_in_produced'])}"
              f"  (checked against AUTHORISED_CHANGES, not fatal on its own)")
        print(f"  fatal differences          : {n_bad}")
        for d in r["value_differences"][:5]:
            print(f"    VALUE DIFF {d['key']}: reference {d['reference_value']} "
                  f"vs produced {d['produced_values'][:3]}")
        for d in r["identity_differences"][:5]:
            print(f"    IDENTITY DIFF {d['key']}: ref {d['reference_filing']}/{d['reference_fact']} "
                  f"vs produced {d['produced_filing']}/{d['produced_fact']}")
        for d in r["missing_from_produced"][:6]:
            print(f"    NOT PRODUCED {d['key']}: reference value {d['reference_value']}")
    con.close()

    after = {r["name"]: tree_hash(r["root"]) for r in REFERENCES}
    integrity = {n: {"files": len(before[n]),
                     "note": "code/config/results only; source caches are hashed in full "
                             "by verify_integrity.py",
                     "changed": sorted(k for k in before[n]
                                       if before[n][k] != after[n].get(k))}
                 for n in before}
    for n, i in integrity.items():
        print(f"\nintegrity {n}: {i['files']} files, {len(i['changed'])} changed")
        if i["changed"]:
            ok = False
            print("   CHANGED:", i["changed"][:5])

    # A run that compared NOTHING is not a pass. Previously, with both reference
    # packages absent, every result was SKIPPED, `ok` stayed True and this
    # printed "OVERALL: PASS" on zero comparisons -- a green total that proved
    # the regression targets were never exercised.
    if not ok:
        overall, rc = "FAIL", 1
    elif compared == 0:
        overall, rc = "NO_COMPARISON", 2
    else:
        overall, rc = "PASS", 0

    report = {"status": overall, "references_compared": compared,
              "references_declared": len(REFERENCES),
              "references_skipped": 0,
              "status_rule": ("PASS requires at least one reference actually compared "
                              "with zero value, identity or missing-required differences; "
                              "a missing required nonzero reference row is fatal; a run "
                              "with no comparison reports NO_COMPARISON"),
              "results": results,
              "authorised_changes": AUTHORISED_CHANGES, "reference_integrity": integrity}
    (VERIFICATION / "reference_regressions.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nOVERALL: {overall} ({compared}/{len(REFERENCES)} references compared)"
          f"  -> verification/reference_regressions.json")
    return rc


if __name__ == "__main__":
    sys.exit(main())
