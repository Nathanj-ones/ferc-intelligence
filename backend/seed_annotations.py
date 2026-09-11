#!/usr/bin/env python3
"""
Reviewed source annotations: bundle them, check them, load them.

An annotation is a reviewed conclusion about ONE exact source observation. It is
data, never code, and it may annotate but never change a filed value. It applies
only when source system, entity, filing, source fact, metric AND the filed text
all still match -- so an unrelated filer reporting the same number matches
nothing.

WHY THIS FILE CHANGED (audit A09)
----------------------------------
It used to read two reference packages that live OUTSIDE the release, and it was
a manual step nothing in the build depended on. A clean rebuild from the shipped
bundle therefore loaded zero annotations, and the four reviewed Fayetteville
FY2025 `999999` observations came back with validation `pass` instead of
`source_anomaly_review` -- a suspected placeholder silently promoted to
apparently validated data.

The annotations are now a versioned input that ships INSIDE the tree, in
`config/annotations/`, with a manifest that records a checksum and a row count
for each file. `run.py` loads them on every build and refuses to start when the
input is missing or altered.

    python3 seed_annotations.py --export --source-root /path/to/outputs
                                             # rebuild from explicitly selected
                                             # external reference packages
    python3 seed_annotations.py              # load the bundle into the staging
                                             # database (honours FERC_STAGING_DB)
    python3 seed_annotations.py --check      # verify the bundle only

Source packages are opened READ-ONLY and are never modified.

ATTRIBUTION IS REPORTED HONESTLY
---------------------------------
None of the seven delivered annotations names a person. Each records a process
and a date -- a reference-package review, a bounded sample test -- so each is
carried as an `unattributed_prior_annotation`: real provenance, no human
sign-off, and never described as one. The source occurrence identity is hashed
canonically and the explicit review date in the provenance text is retained;
neither field is inferred from a file timestamp or from value equality.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ferclib.staging import Staging                                   # noqa: E402

BUNDLE_DIR = HERE / "config" / "annotations"
BUNDLE_FILE = "reviewed_source_annotations.json"
MANIFEST_FILE = "MANIFEST.json"
ANNOTATION_SET_VERSION = "1.1.0"

#: The reference packages the annotations were originally reviewed in. They are
#: NOT part of the release, which is precisely why the bundle exists. --export
#: reads them; a normal build never touches them.
def _sources(source_root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    return [
        ("transco_tgp_reference",
         source_root / "tgp_10y_generalisation_test_repaired" / "package"
         / "reviewed_source_annotations.csv"),
        ("form2a_fayetteville_validation",
         source_root / "form2a_fayetteville_validation" / "package"
         / "reviewed_source_annotations.csv"),
    ]
SOURCE_SYSTEM = "eCollection_XBRL"

COLUMNS = ("source_system", "entity_key", "filing_id", "source_fact_id", "metric_id",
           "filed_text", "review_status", "rationale", "evidence_ref", "evidence_hash",
           "reviewer", "reviewed_at", "applied_count")


def _attribution(reviewer: str) -> str:
    """What kind of provenance this annotation actually has.

    An email address or an explicit "reviewed by <name>" would be attribution.
    A process description is provenance. The distinction is recorded rather than
    smoothed over, because "a human confirmed this" and "a bounded test flagged
    this and no human has confirmed it" are different claims."""
    r = (reviewer or "").strip()
    if not r:
        return "no_recorded_provenance"
    if "@" in r:
        return "named_reviewer"
    return "unattributed_prior_annotation"


def _evidence_metadata(row: dict, reviewer: str) -> tuple[str, str, str]:
    """Return a reproducible occurrence reference, checksum and review date.

    The checksum deliberately covers only the source occurrence identity and
    exact as-filed text. It can therefore be recomputed from a rebuilt database
    without trusting this annotation's rationale, and it does not become
    circular when the annotation bundle itself is re-hashed.
    """
    identity = {
        "entity_key": row["filer_cid"],
        "filed_text": row["filed_text"],
        "filing_id": row["filing_id"],
        "metric_id": row["metric_id"],
        "source_fact_id": row["source_fact_id"],
        "source_system": SOURCE_SYSTEM,
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    reference = (f"{SOURCE_SYSTEM}|filing={row['filing_id']}|"
                 f"fact={row['source_fact_id']}|metric={row['metric_id']}")
    explicit_dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", reviewer or "")
    reviewed_at = explicit_dates[0] if explicit_dates else ""
    return reference, hashlib.sha256(raw).hexdigest(), reviewed_at


def read_sources(source_root: pathlib.Path) -> tuple[list[dict], list[dict]]:
    rows, seen, provenance = [], set(), []
    for label, src in _sources(source_root):
        if not src.is_file():
            provenance.append({"package": label, "path": str(src), "present": False,
                               "rows": 0,
                               "note": "absent at export time; recorded, not fabricated"})
            print(f"  !! absent (recorded, not fabricated): {src}")
            continue
        n = 0
        for a in csv.DictReader(src.open(newline="", encoding="utf-8")):
            key = (SOURCE_SYSTEM, a["filer_cid"], a["filing_id"],
                   a["source_fact_id"], a["metric_id"])
            if key in seen:
                continue
            seen.add(key)
            reviewer = a.get("reviewer") or a.get("reviewer_provenance", "")
            default_ref, default_hash, default_reviewed_at = _evidence_metadata(a, reviewer)
            rows.append({
                "source_system": SOURCE_SYSTEM, "entity_key": a["filer_cid"],
                "filing_id": a["filing_id"], "source_fact_id": a["source_fact_id"],
                "metric_id": a["metric_id"], "filed_text": a["filed_text"],
                "review_status": a.get("review_status", "reviewed"),
                "rationale": a.get("rationale", ""),
                "evidence_ref": (a.get("evidence_reference") or
                                 a.get("evidence_ref") or default_ref),
                "evidence_hash": (a.get("evidence_checksum") or
                                  a.get("evidence_hash") or default_hash),
                "reviewer": reviewer,
                "reviewed_at": a.get("reviewed_at") or default_reviewed_at,
                "applied_count": 0,
                # bundle-only metadata; not a database column
                "_attribution": _attribution(reviewer),
                "_source_package": label})
            n += 1
        provenance.append({"package": label, "path": str(src), "present": True,
                           "rows": n, "sha256": hashlib.sha256(
                               src.read_bytes()).hexdigest()})
        print(f"  read {src.relative_to(source_root)}: {n} annotation(s)")
    return rows, provenance


def cmd_export(source_root: pathlib.Path) -> int:
    rows, provenance = read_sources(source_root)
    if not rows:
        print("no annotations were read; refusing to write an empty bundle over a "
              "good one", file=sys.stderr)
        return 2
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    body = {
        "annotation_set_version": ANNOTATION_SET_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_system": SOURCE_SYSTEM,
        "note": ("Reviewed conclusions about exact source observations. Keyed to the "
                 "source occurrence and fact: (source_system, entity_key, filing_id, "
                 "source_fact_id, metric_id), and applied only when filed_text still "
                 "matches, so a re-parse that changes the value detaches the annotation "
                 "instead of mislabelling a different number."),
        "attribution_note": (
            "No row names a person. Each records a process and a date, so each is an "
            "unattributed prior annotation: genuine provenance, no human sign-off. "
            "reviewed_at is the first explicit ISO review date in that provenance; "
            "evidence_hash is SHA-256 of canonical JSON over the exact FERC source "
            "occurrence identity and as-filed text."),
        "exported_from": provenance,
        "annotations": sorted(rows, key=lambda r: (r["entity_key"], r["filing_id"],
                                                   r["metric_id"])),
    }
    path = BUNDLE_DIR / BUNDLE_FILE
    raw = json.dumps(body, indent=1, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    manifest = {
        "annotation_set_version": ANNOTATION_SET_VERSION,
        "generated_at": body["generated_at"],
        "required": True,
        "why_required": (
            "Without these rows a rebuild reverts reviewed observations from "
            "source_anomaly_review to pass, which is a quality upgrade with no "
            "evidence behind it. run.py treats a missing or altered file as fatal."),
        "evidence_hash_algorithm": (
            "sha256 canonical JSON (sorted keys, compact separators) over "
            "source_system, entity_key, filing_id, source_fact_id, metric_id, filed_text"),
        "files": [{"path": BUNDLE_FILE,
                   "sha256": hashlib.sha256(raw).hexdigest(),
                   "row_count": len(rows),
                   "entities": sorted({r["entity_key"] for r in rows})}],
    }
    (BUNDLE_DIR / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nbundled {len(rows)} annotation(s) -> {path.relative_to(HERE)}")
    print(f"manifest sha256 {manifest['files'][0]['sha256'][:16]}...")
    _print_rows(rows)
    return 0


def load_bundle() -> tuple[list[dict], dict]:
    """Read and verify the bundle. Raises SystemExit with a specific message."""
    manifest_path = BUNDLE_DIR / MANIFEST_FILE
    if not manifest_path.is_file():
        raise SystemExit(f"required annotation manifest is missing: {manifest_path}\n"
                         "  regenerate with: python3 seed_annotations.py --export")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for decl in manifest.get("files", []):
        p = BUNDLE_DIR / decl["path"]
        if not p.is_file():
            raise SystemExit(f"declared annotation file is missing: {p}")
        raw = p.read_bytes()
        got = hashlib.sha256(raw).hexdigest()
        if decl.get("sha256") and got != decl["sha256"]:
            raise SystemExit(f"{p.name} does not match its manifest checksum\n"
                             f"  declared {decl['sha256']}\n  found    {got}")
        body = json.loads(raw.decode("utf-8"))
        got_rows = body.get("annotations", [])
        if decl.get("row_count") is not None and len(got_rows) != decl["row_count"]:
            raise SystemExit(f"{p.name} declares {decl['row_count']} rows but carries "
                             f"{len(got_rows)}")
        rows.extend(got_rows)
    return rows, manifest


def _print_rows(rows) -> None:
    for r in sorted(rows, key=lambda x: (x["entity_key"], x["filing_id"], x["metric_id"])):
        print(f"  {r['entity_key']:10s} filing {r['filing_id']:>7s} "
              f"{r['metric_id'][:36]:36s} {r['review_status']:16s} "
              f"filed={str(r['filed_text'])[:14]:14s} "
              f"{r.get('_attribution', _attribution(r.get('reviewer', '')))}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--export", action="store_true",
                   help="rebuild config/annotations/ from the reference packages")
    p.add_argument("--source-root", type=pathlib.Path,
                   help="explicit parent of the two external reference packages; required "
                        "with --export so a build cannot discover sibling files implicitly")
    p.add_argument("--check", action="store_true",
                   help="verify the bundle without writing to any database")
    args = p.parse_args(argv)

    if args.export:
        if args.source_root is None:
            print("--export requires --source-root; external annotation evidence is never "
                  "discovered from a sibling tree implicitly", file=sys.stderr)
            return 2
        return cmd_export(args.source_root.resolve())

    rows, manifest = load_bundle()
    if args.check:
        print(f"annotation set {manifest.get('annotation_set_version')}: "
              f"{len(rows)} row(s), checksum verified")
        _print_rows(rows)
        return 0

    db_path = Staging.default_path()
    db = Staging(db_path)
    payload = [{k: r.get(k, 0 if k == "applied_count" else "") for k in COLUMNS}
               for r in rows]
    n = db.write_reviewed_annotations(payload)
    print(f"loaded {n} reviewed annotation(s) from annotation set "
          f"{manifest.get('annotation_set_version')} into {db_path}")
    _print_rows(rows)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
