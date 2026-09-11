#!/usr/bin/env python3
"""
Re-hash every protected tree and compare against the baseline taken before work
began.

Modification time is never used as proof of integrity: only content hashes are.
Exclusions (tool telemetry, build caches, OS metadata, SQLite volatile sidecars)
were declared in the baseline file BEFORE hashing and are re-read from it here
rather than redeclared, so the two passes cannot silently disagree.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BEFORE = HERE / "verification" / "protected_before.json"
AFTER = HERE / "verification" / "protected_after.json"

#: every cache root to sweep, relative to ROOT
CACHE_ROOTS = ("outputs/operating_assets_all_regimes/source_cache",
               "outputs/repair_all_regimes/source_cache")


def check_content_addressed(root: pathlib.Path) -> dict:
    """Every cache object must hash to its own name. TOTAL, never sampled.

    w2-ioc found two objects in the shipped capture whose bytes were not their
    names: one held an events CSV, the other held `run.py`'s own source. Both
    were written by a script using a wrong path variable, and both survived
    every check the delivered release ran.

    They survived because those checks trusted the filename. `build_full_bundle`
    recorded `digest = f.name` for any 64-character name without reading the
    file, and `build_release.verify_cache` re-hashed a 40-object sample. A
    content-addressed store's whole guarantee is that name equals content, so a
    check that ASSUMES that guarantee cannot test it -- it can only restate it.

    The sweep is total for that reason, and it is cheap: 3,477 objects hash in
    about a second once the bytes are local. It is slow only on a first pass over
    cloud-evicted files, which is a materialisation cost, not a hashing one.
    """
    objects = root / "objects"
    if not objects.is_dir():
        return {"available": False, "why": f"{objects} is absent"}
    mismatches, unreadable, odd = [], [], []
    n = 0
    for p in objects.rglob("*"):
        if not p.is_file():
            continue
        if len(p.name) != 64:
            # not content-addressed: it does not belong in this store
            odd.append(str(p.relative_to(root)))
            continue
        n += 1
        try:
            data = p.read_bytes()
        except OSError as exc:
            unreadable.append({"object": p.name, "error": type(exc).__name__})
            continue
        if hashlib.sha256(data).hexdigest() != p.name:
            mismatches.append({"object": p.name, "bytes_on_disk": len(data),
                               "first_line": data.splitlines()[0][:120].decode(
                                   "utf-8", "replace") if data else ""})
    # an index entry pointing at an object that is not there is the other half
    missing = []
    idx = root / "index.json"
    if idx.is_file():
        try:
            for entry in json.loads(idx.read_text(encoding="utf-8")).values():
                h = entry.get("content_hash", "")
                if h and not (objects / h[:2] / h).is_file():
                    missing.append({"content_hash": h,
                                    "source_url": entry.get("source_url", "")[:160]})
        except json.JSONDecodeError as exc:
            return {"available": True, "objects": n,
                    "index_unreadable": f"{type(exc).__name__}: {exc}",
                    "mismatches": mismatches, "unreadable": unreadable,
                    "non_content_addressed": odd}
    return {"available": True, "objects": n, "mismatches": mismatches,
            "unreadable": unreadable, "non_content_addressed": odd,
            "index_entries_without_object": missing,
            "method": "every object re-hashed from its bytes; not sampled"}


def main() -> int:
    if not BEFORE.is_file():
        print(f"no baseline at {BEFORE}; cannot verify integrity", file=sys.stderr)
        return 1
    base = json.loads(BEFORE.read_text(encoding="utf-8"))
    ex = base["declared_exclusions"]
    ex_dirs, ex_names, ex_suffix = set(ex["dirs"]), set(ex["names"]), set(ex["suffixes"])

    def files(target: str):
        p = ROOT / target
        if p.is_file():
            yield p
            return
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            if any(part in ex_dirs for part in f.parts):
                continue
            if f.name in ex_names or f.suffix in ex_suffix:
                continue
            yield f

    out = {"baseline_captured_at_utc": base["captured_at_utc"],
           "verified_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "declared_exclusions": ex, "trees": {}}
    total_changed = 0
    for target, b in base["trees"].items():
        now = {}
        for f in files(target):
            try:
                now[str(f.relative_to(ROOT))] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError as exc:
                # A cloud-synced placeholder that is not materialised locally
                # cannot be read. That is UNREADABLE, never a silent "unchanged":
                # recording it keeps the integrity claim honest rather than
                # letting an unverifiable file pass as verified.
                now[str(f.relative_to(ROOT))] = f"UNREADABLE:{type(exc).__name__}"
        changed = sorted(k for k, v in b["files"].items() if now.get(k) != v)
        added = sorted(k for k in now if k not in b["files"])
        removed = sorted(k for k in b["files"] if k not in now)
        out["trees"][target] = {"file_count": len(now), "baseline_count": b["file_count"],
                                "changed": changed, "added": added, "removed": removed,
                                "files": now}
        total_changed += len(changed) + len(removed)
        flag = "OK " if not changed and not removed else "!! "
        print(f"{flag}{target:52s} {len(now):5d} files  changed={len(changed)} "
              f"added={len(added)} removed={len(removed)}")
        for c in (changed + removed)[:5]:
            print(f"     CHANGED/REMOVED: {c}")

    # -------------------------------------------------- content-addressed stores
    cache_bad = 0
    out["content_addressed"] = {}
    for rel in CACHE_ROOTS:
        res = check_content_addressed(ROOT / rel)
        out["content_addressed"][rel] = res
        if not res.get("available"):
            print(f"-- {rel:52s} {res.get('why', 'unavailable')}")
            continue
        n_bad = (len(res["mismatches"]) + len(res["unreadable"])
                 + len(res["non_content_addressed"])
                 + len(res.get("index_entries_without_object", [])))
        cache_bad += n_bad
        print(f"{'OK ' if not n_bad else '!! '}{rel:52s} {res['objects']:5d} objects  "
              f"mismatched={len(res['mismatches'])} unreadable={len(res['unreadable'])} "
              f"orphan-index={len(res.get('index_entries_without_object', []))}")
        for m in res["mismatches"][:5]:
            print(f"     BYTES ARE NOT THEIR NAME: {m['object'][:16]}... "
                  f"{m['bytes_on_disk']:,}b starting {m['first_line'][:60]!r}")

    ok = total_changed == 0 and cache_bad == 0
    out["status"] = "PASS" if ok else "FAIL"
    AFTER.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nprotected-input integrity: {out['status']} "
          f"({total_changed} changed or removed, {cache_bad} cache defect(s)) "
          f"-> {AFTER.name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
