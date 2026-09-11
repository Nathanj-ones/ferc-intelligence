#!/usr/bin/env python3
"""
Assemble IMPLEMENTATION_ACCEPTANCE.md from what the run actually produced.

Every number here is read out of the staging database or a verification result
file at generation time. Nothing is transcribed from an earlier report, and a
missing verification file is reported as NOT EXECUTED rather than assumed to
have passed.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ferclib.registry import REGISTRY, REGISTRY_VERSION              # noqa: E402
from ferclib.staging import Staging                                  # noqa: E402

DB = HERE / "staging" / "operating_assets.sqlite"
OUT = HERE / "IMPLEMENTATION_ACCEPTANCE.md"

ADAPTERS = ["gas_xbrl", "liquids_xbrl", "ioc", "capacity", "form549d",
            "elibrary_docs", "lng"]


def load(path: pathlib.Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> int:
    db = Staging(DB, create=False)
    q = db.query
    L: list[str] = []
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    L += [f"# Implementation acceptance — all-regime FERC operating-asset staging pipeline",
          "", f"Generated {stamp} from the staging database. Registry `{REGISTRY_VERSION}`.",
          "", "Every figure below is read from the run's own records at generation time. "
              "A verification result that does not exist is reported as NOT EXECUTED, "
              "never inferred from an earlier report.", ""]

    # ---------------------------------------------------------------- scope
    universe = q("""SELECT a.template, COUNT(DISTINCT a.asset_id) assets,
                           COUNT(DISTINCT m.entity_key) entities
                    FROM assets a LEFT JOIN asset_entity_map m ON m.asset_id=a.asset_id
                    GROUP BY a.template ORDER BY a.template""")
    L += ["## 1. Scope actually run", "",
          "| Template | Asset rows | Filing entities |", "|---|---:|---:|"]
    for r in universe:
        L.append(f"| {r['template']} | {r['assets']} | {r['entities']} |")
    L += [""]

    # ---------------------------------------------------------------- adapters
    L += ["## 2. Per-adapter results", "",
          "| Adapter | Entities | Filings | Expected slots | Observations | Populated | "
          "Validated | In review | Source-absent | Our gap |", "|---|" + "---:|" * 9]
    per_adapter = {}
    for a in ADAPTERS:
        metrics = [m.id for m in REGISTRY if m.adapter == a]
        if not metrics:
            continue
        ph = ",".join("?" * len(metrics))
        row = q(f"""SELECT COUNT(DISTINCT entity_key) ents, COUNT(*) obs,
                       SUM(availability='present') pop,
                       SUM(availability='present' AND validation='pass') val,
                       SUM(validation IN ('source_anomaly_review','blocked_ambiguity',
                            'scope_incompatible','unit_warning','rounding_warning',
                            'source_date_warning')) rev,
                       SUM(availability IN ('source_blank','filed_nil','not_required',
                            'not_applicable','not_yet_due','nonpublic')) absent,
                       SUM(availability IN ('parse_failed','retrieval_failed',
                            'not_implemented','known_not_retrieved')) gap
                    FROM observations WHERE metric_id IN ({ph})""", tuple(metrics))[0]
        slots = q(f"SELECT COUNT(*) n FROM coverage_expected WHERE metric_id IN ({ph})",
                  tuple(metrics))[0]["n"]
        filings = q("SELECT COUNT(*) n FROM filings")[0]["n"] if a in ("gas_xbrl",) else None
        per_adapter[a] = dict(row)
        L.append(f"| {a} | {row['ents'] or 0} | {filings if filings is not None else '-'} | "
                 f"{slots} | {row['obs'] or 0} | {row['pop'] or 0} | {row['val'] or 0} | "
                 f"{row['rev'] or 0} | {row['absent'] or 0} | {row['gap'] or 0} |")
    L += [""]

    # ---------------------------------------------------------------- coverage
    cov = load(HERE / "exports" / "coverage_statistics.json")
    L += ["## 3. Coverage at the requested grain", ""]
    if cov:
        L += [f"- expected slots frozen: **{cov.get('expected_total')}** "
              f"(core {cov.get('expected_core')}, not-required {cov.get('expected_not_required')}, "
              f"unknown applicability {cov.get('expected_unknown')})",
              f"- core populated: **{cov.get('core_populated')} / {cov.get('expected_core')} "
              f"= {cov.get('core_populated_pct')}%**",
              f"- core validated: **{cov.get('core_validated')} = {cov.get('core_validated_pct')}%**",
              f"- core in review (value present, warning attached): {cov.get('core_in_review')}",
              "", "Outcomes over the core denominator:", "",
              "| Outcome | Slots |", "|---|---:|"]
        for k, v in sorted((cov.get("core_outcomes") or {}).items(), key=lambda x: -x[1]):
            L.append(f"| {k} | {v} |")
        L += ["", "Populated, source-matched, validated and review-free are counted "
                  "separately. The denominator is the frozen expected grid, never the "
                  "populated output.", ""]
    else:
        L += ["NOT EXECUTED: `exports/coverage_statistics.json` does not exist. "
              "Run `python3 run.py coverage`.", ""]

    # ---------------------------------------------------------------- fields
    fs = load(HERE / "exports" / "field_status_summary.json")
    L += ["## 4. Per-field implementation status", ""]
    if fs:
        L += ["| Outcome | Fields |", "|---|---:|"]
        for k, v in sorted((fs.get("by_outcome") or {}).items(), key=lambda x: -x[1]):
            L.append(f"| {k} | {v} |")
        L += [""]
        unfinished = q("""SELECT DISTINCT field_id, adapter, blocker FROM field_status
                          WHERE outcome='not_implemented_unfinished_work'
                          ORDER BY adapter, field_id""")
        L += [f"**Explicitly unfinished engineering ({len(unfinished)} fields)** — counted as "
              "our work, never as a FERC data gap:", ""]
        if unfinished:
            L += ["| Field | Adapter | Blocker |", "|---|---|---|"]
            for r in unfinished:
                L.append(f"| {r['field_id']} | {r['adapter']} | {(r['blocker'] or '')[:90]} |")
        else:
            L.append("None.")
        L += [""]
    else:
        L += ["NOT EXECUTED: run `python3 build_field_status.py`.", ""]

    # ---------------------------------------------------------------- regressions
    reg = load(HERE / "verification" / "reference_regressions.json")
    L += ["## 5. Reference regressions", ""]
    if reg:
        L += [f"Overall: **{reg['status']}**", "",
              "| Reference | Status | Reference values | Value diffs | Identity diffs | "
              "Not produced | Additional |", "|---|---|---:|---:|---:|---:|---:|"]
        for r in reg["results"]:
            if r.get("status") == "SKIPPED":
                L.append(f"| {r['reference']} | SKIPPED | - | - | - | - | - |")
                continue
            L.append(f"| {r['reference']} | {r['status']} | {r['reference_rows_with_values']} | "
                     f"{len(r['value_differences'])} | {len(r['identity_differences'])} | "
                     f"{len(r['missing_from_produced'])} | {len(r['additional_in_produced'])} |")
        L += ["", "Reference-directory integrity after the run:", ""]
        for n, i in (reg.get("reference_integrity") or {}).items():
            L.append(f"- {n}: {i['files']} files, **{len(i['changed'])} changed**")
        L += ["", "Authorised differences (each dispositioned, not waived):", ""]
        for c in reg.get("authorised_changes", []):
            L.append(f"- **{c['kind']}** — {c['reason']}")
        L += [""]
    else:
        L += ["NOT EXECUTED: `verification/reference_regressions.json` does not exist. "
              "Run `python3 run_regressions.py --rebuild`.", ""]

    # ---------------------------------------------------------------- validation
    val = load(HERE / "verification" / "validation_results.json")
    L += ["## 6. Integration and adversarial checks", ""]
    if val:
        s = val["summary"]
        L += [f"**{s['PASS']} PASS / {s['FAIL']} FAIL / {s['ERROR']} ERROR / "
              f"{s['SKIPPED']} SKIPPED**", "",
              "| Check | Group | Status | Detail |", "|---|---|---|---|"]
        for r in val["results"]:
            L.append(f"| {r['check']} | {r['group']} | {r['status']} | "
                     f"{(r['detail'] or '')[:110]} |")
        L += ["", "A missing comparison is SKIPPED, never PASS.", ""]
    else:
        L += ["NOT EXECUTED: run `python3 run.py validate`.", ""]

    # ---------------------------------------------------------------- release
    rel = load(HERE / "release_receipt.json")
    L += ["## 7. Release verification", ""]
    if rel:
        cr = rel["clean_room"]
        L += [f"- status: **{rel['status']}**",
              f"- ZIP: `{rel['zip']}`  {rel['zip_bytes']:,} bytes",
              f"- SHA-256: `{rel['zip_sha256']}`",
              f"- manifest: {rel['manifest_file_count']} files",
              f"- clean room: {cr['unpacked']} files verified, "
              f"{len(cr['hash_mismatches'])} hash mismatches, "
              f"{len(cr['missing_outputs'])} missing outputs",
              f"- negative test: {cr['negative_test']}",
              f"- offline suite in the clean room: rc={cr['suite']['returncode']} "
              f"{cr['suite']['stdout']}",
              "", f"{rel['evidence_packaging_note']}", ""]
    else:
        L += ["NOT EXECUTED: run `python3 build_release.py` after all artefacts exist.", ""]

    # ---------------------------------------------------------------- blockers
    blockers = q("""SELECT adapter, kind, summary, scope, exact_error,
                           human_decision_needed FROM blockers
                    WHERE resolved_at IS NULL ORDER BY human_decision_needed DESC, kind, adapter""")
    L += [f"## 8. Open blockers ({len(blockers)})", ""]
    if blockers:
        by_kind = Counter(b["kind"] for b in blockers)
        L += ["By kind: " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())), "",
              "| Kind | Adapter | Scope | Summary |", "|---|---|---|---|"]
        for b in blockers[:60]:
            L.append(f"| {b['kind']} | {b['adapter']} | {(b['scope'] or '')[:34]} | "
                     f"{b['summary'][:88]} |")
        if len(blockers) > 60:
            L.append(f"| … | | | {len(blockers)-60} more in `exports/blockers.csv` |")
    else:
        L.append("None.")
    L += [""]

    # ---------------------------------------------------------------- integrity
    L += ["## 9. Protected-input integrity", ""]
    before = load(HERE / "verification" / "protected_before.json")
    after = load(HERE / "verification" / "protected_after.json")
    if before and after:
        L += ["| Tree | Files | Changed |", "|---|---:|---:|"]
        for t, b in before["trees"].items():
            a = after["trees"].get(t, {"files": {}})
            changed = [k for k, v in b["files"].items() if a["files"].get(k) != v]
            L.append(f"| {t} | {b['file_count']} | **{len(changed)}** |")
        L += ["", f"Declared exclusions before hashing: "
                  f"{before['declared_exclusions']['rationale']}", ""]
    else:
        L += ["NOT EXECUTED: run `python3 verify_integrity.py`.", ""]

    # ---------------------------------------------------------------- staging
    L += ["## 10. Staging contents", "", "| Table | Rows |", "|---|---:|"]
    for t, n in db.counts().items():
        if n:
            L.append(f"| {t} | {n:,} |")
    L += [""]

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(L)} lines)")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
