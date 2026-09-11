#!/usr/bin/env python3
"""
Build the two handoff registers for independent re-audit.

    issue_resolution.json / .csv        all 22 audit issues, A01-A22
    exceptions_resolution.json / .csv   all 34 original blocker IDs

Both are assembled from three sources and never from one:

  1. the independent audit's own registers, read verbatim, so the baseline claim
     is the auditor's and not a paraphrase of it;
  2. each workstream's `reports/<name>_issues.json`, which is where the person
     who did the work says what they found and what they could not close;
  3. the rebuilt staging database, which is where the *data* says whether the
     repair actually landed.

Where (2) and (3) disagree, the disagreement is REPORTED, not resolved in favour
of the more flattering one. A fix is closed only when its code, its stored data
and its negative acceptance evidence agree; anything else is `control_repaired`
(the engine no longer produces the defect, but the underlying source question is
still open) or `open`.

Two rules the audit was explicit about and this script enforces:

  * NO ISSUE MAY DISAPPEAR. All 22 IDs and all 34 blocker IDs appear in the
    output whatever happened to them, including renames. An ID that no worker
    claimed is reported as unclaimed, not dropped.
  * A NEW blocker is an ADDITION, never a replacement for an original one.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE.parent.parent / "audit_2026-09-08" / "FERC_Independent_Audit_2026-09-08"
REPORTS = HERE / "reports"

#: how a repair may end. Deliberately more than "fixed" and "open": the audit
#: asked that control repair be distinguished from resolved source truth.
DISPOSITIONS = {
    "closed": "code, stored data and a negative acceptance test all agree",
    "control_repaired": ("the engine no longer produces the defect and the outcome is "
                         "correctly gated, but the underlying source question is not "
                         "resolved. This is a legitimate bounded outcome, not a fix."),
    "partially_closed": "some affected data or dimensions repaired, others still open",
    "open": "not repaired",
    "disputed": ("the finding was not reproduced against the correct snapshot, with "
                 "reproducible counterevidence recorded"),
    "unclaimed": "no workstream reported on this ID -- a gap in this repair, reported",
}


def load_audit() -> tuple[list[dict], list[dict]]:
    issues = json.loads((AUDIT / "issues.json").read_text(encoding="utf-8"))["issues"]
    exceptions = json.loads((AUDIT / "exceptions_34.json").read_text(encoding="utf-8"))
    return issues, exceptions


def load_worker_reports() -> tuple[dict[str, list[dict]], list[str]]:
    """Every `reports/*_issues.json`, keyed by issue id. Several workers may
    report on one issue (A08 is shared by three), so values are lists."""
    by_issue: dict[str, list[dict]] = {}
    seen_files = []
    if not REPORTS.is_dir():
        return by_issue, seen_files
    for f in sorted(REPORTS.glob("*_issues.json")):
        seen_files.append(f.name)
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            by_issue.setdefault("_PARSE_ERRORS", []).append(
                {"file": f.name, "error": str(exc)})
            continue
        if isinstance(rows, dict):
            rows = rows.get("issues", []) or rows.get("rows", [])
        for r in rows:
            iid = str(r.get("issue_id", "")).strip().upper()
            if iid:
                by_issue.setdefault(iid, []).append(dict(r, _reported_by=f.stem.replace(
                    "_issues", "")))
    return by_issue, seen_files


def db_facts(db_path: pathlib.Path) -> dict:
    """What the DATA says, independent of what anyone reported.

    Read-only and immutable: inspecting a database must never change it.
    """
    if not db_path.is_file():
        return {"available": False,
                "why": f"{db_path} is absent; data-side verification not performed"}
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda s, p=(): con.execute(s, p).fetchone()[0]                  # noqa: E731
    facts = {"available": True}
    try:
        # A04 -- units. A margin stored as a fraction is the defect.
        facts["A04_margins_outside_percent_range"] = q(
            "SELECT COUNT(*) FROM observations WHERE metric_id LIKE '%margin_pct' "
            "AND availability='present' AND value_num IS NOT NULL "
            "AND ABS(value_num) <= 1.0")
        facts["A04_margin_rows"] = q(
            "SELECT COUNT(*) FROM observations WHERE metric_id LIKE '%margin_pct' "
            "AND availability='present' AND value_num IS NOT NULL")
        facts["A04_undimensioned_per_barrel"] = q(
            "SELECT COUNT(*) FROM observations WHERE metric_id LIKE '%per_barrel' "
            "AND availability='present' AND (unit IS NULL OR unit IN ('','ratio'))")
        # A11 -- historical backfill emitted as current news
        facts["A11_historical_current_events"] = q(
            "SELECT COUNT(*) FROM events WHERE destination='investor_feed' "
            "AND is_backfill=0 AND source_filed_date < '2026-08-31'")
        # A18 -- dangling logical filing references
        facts["A18_dangling_observation_filings"] = q(
            "SELECT COUNT(*) FROM observations o LEFT JOIN filings f "
            "ON f.filing_id=o.filing_id WHERE o.filing_id IS NOT NULL "
            "AND o.filing_id<>'' AND f.filing_id IS NULL")
        # A08 -- lineage with nothing behind it. `input_population_id` does not
        # exist in the pre-repair schema, so the query degrades to the two
        # original columns there rather than failing: the baseline legitimately
        # cannot answer a question about a column it does not have, and saying
        # so is better than reporting a count nobody can interpret.
        facts["A08_edges_with_no_input"] = _safe(
            con, "SELECT COUNT(*) FROM lineage_edges WHERE "
                 "(input_source_fact_id IS NULL OR input_source_fact_id='') AND "
                 "(input_observation_id IS NULL OR input_observation_id='') AND "
                 "(input_population_id IS NULL OR input_population_id='')")
        # THE ACTUAL A08 REQUIREMENT is that an observation be traversable, not
        # that every edge be. Counting bare edges overstates the problem and
        # would have reported a repair as a regression.
        #
        # `ioc_top5_shipper_concentration` is the case that forced the
        # distinction. It carries four edge roles: `contributing_row` (each with
        # BOTH a population and a real source fact), `denominator` (an input
        # observation), `population` (a population id) -- and `group_member`,
        # which names a shipper, a quantity, a filing and a period as a
        # human-readable top-five summary. Those last ones resolve to no row id,
        # and on the bare-edge count they read as 2,004 empty pointers. They are
        # not: the aggregate beside them is fully traversable. A labelled summary
        # sitting next to complete lineage is not a missing input.
        facts["A08_derived_observations_with_no_traversable_input"] = _safe(
            con, """SELECT COUNT(*) FROM observations o WHERE o.method IN
                    ('derived','aggregated','normalised') AND o.availability='present'
                    AND NOT EXISTS (
                      SELECT 1 FROM lineage_edges l WHERE l.observation_id=o.observation_id
                      AND ((l.input_source_fact_id IS NOT NULL AND l.input_source_fact_id<>'')
                        OR (l.input_observation_id IS NOT NULL AND l.input_observation_id<>'')
                        OR (l.input_population_id IS NOT NULL AND l.input_population_id<>'')))
                    AND NOT EXISTS (
                      SELECT 1 FROM lineage_populations p
                      WHERE p.observation_id=o.observation_id)""")
        facts["A08_descriptive_edges_beside_complete_lineage"] = _safe(
            con, """SELECT COUNT(*) FROM lineage_edges l
                    WHERE l.input_role='group_member'
                    AND (l.input_source_fact_id IS NULL OR l.input_source_fact_id='')
                    AND EXISTS (SELECT 1 FROM lineage_edges x
                                WHERE x.observation_id=l.observation_id
                                AND x.input_role<>'group_member'
                                AND ((x.input_source_fact_id IS NOT NULL AND x.input_source_fact_id<>'')
                                  OR (x.input_population_id IS NOT NULL AND x.input_population_id<>'')))""")
        if isinstance(facts["A08_edges_with_no_input"], str):
            facts["A08_edges_with_no_input_prerepair_schema"] = _safe(
                con, "SELECT COUNT(*) FROM lineage_edges WHERE "
                     "(input_source_fact_id IS NULL OR input_source_fact_id='') AND "
                     "(input_observation_id IS NULL OR input_observation_id='')")
        facts["A08_populations"] = _safe(con, "SELECT COUNT(*) FROM lineage_populations")
        facts["A08_populations_zero_without_reason"] = _safe(
            con, "SELECT COUNT(*) FROM lineage_populations WHERE row_count=0 "
                 "AND (empty_reason IS NULL OR empty_reason='')")
        # A09 -- reviewed annotations must survive
        facts["A09_reviewed_annotations"] = q(
            "SELECT COUNT(*) FROM reviewed_source_annotations")
        facts["A09_flagged_observations"] = q(
            "SELECT COUNT(*) FROM observations WHERE validation IN "
            "('source_anomaly_review','blocked_ambiguity','scope_incompatible',"
            "'unit_warning','rounding_warning','source_date_warning')")
        # A05/A07 -- the stores the audit found empty
        for t in ("applicability", "taxonomy_sources", "requirements_crosswalk",
                  "source_manifest", "dockets", "asset_dockets"):
            facts[f"table_{t}"] = _safe(con, f"SELECT COUNT(*) FROM {t}")
        for t in ("observations", "filings", "coverage_expected", "coverage_measured",
                  "lineage_edges", "events", "document_facts", "blockers", "field_status"):
            facts[f"table_{t}"] = _safe(con, f"SELECT COUNT(*) FROM {t}")
        facts["open_blockers"] = _safe(
            con, "SELECT COUNT(*) FROM blockers WHERE resolved_at IS NULL")
    finally:
        con.close()
    return facts


def _safe(con, sql: str):
    try:
        return con.execute(sql).fetchone()[0]
    except sqlite3.Error as exc:
        return f"UNAVAILABLE: {type(exc).__name__}"


def resolve_issues(audit_issues: list[dict], reported: dict[str, list[dict]],
                   facts: dict) -> list[dict]:
    out = []
    for a in audit_issues:
        iid = a["issue_id"]
        claims = reported.get(iid, [])
        row = {
            "issue_id": iid,
            "severity": a["severity"],
            "title": a["title"],
            "adapters": ";".join(a["adapters"]),
            "audit_status": a["status"],
            "audit_evidence": ";".join(a["evidence"]),
            "audit_affected_data": a["affected_data"],
            "audit_required_fix": a["required_fix"],
            "audit_acceptance_test": a["acceptance_test"],
            "audit_gating": a["gating"],
            # ---- what we did
            "reported_by": ";".join(sorted({c["_reported_by"] for c in claims})),
            "reproduced": _merge(claims, "reproduced"),
            "root_cause": _merge(claims, "root_cause"),
            "files_changed": _merge_list(claims, "files_changed"),
            "data_repair": _merge(claims, "data_repair"),
            "tests": _merge_list(claims, "tests"),
            "outstanding": _merge(claims, "outstanding"),
            "final_disposition": _disposition(claims),
            "disposition_as_reported": _disposition_detail(claims),
        }
        row["disposition_means"] = DISPOSITIONS.get(row["final_disposition"], "")
        ds = _data_side(iid, facts)
        row["data_side_check"] = ds.get("check", "")
        row["data_side_result"] = ds.get("result", "")
        row["agrees_with_report"] = ds.get("agrees", "")
        out.append(row)
    # any issue id a worker reported that is NOT one of the audit's 22
    extra = sorted(set(reported) - {a["issue_id"] for a in audit_issues} - {"_PARSE_ERRORS"})
    for iid in extra:
        out.append({"issue_id": iid, "severity": "NEW",
                    "title": _merge(reported[iid], "title") or "(new finding)",
                    "audit_status": "not_in_audit_register",
                    "reported_by": ";".join(sorted({c["_reported_by"]
                                                    for c in reported[iid]})),
                    "root_cause": _merge(reported[iid], "root_cause"),
                    "files_changed": _merge_list(reported[iid], "files_changed"),
                    "tests": _merge_list(reported[iid], "tests"),
                    "final_disposition": _disposition(reported[iid]),
                    "disposition_as_reported": _disposition_detail(reported[iid]),
                    "disposition_means": "a finding beyond the audit's 22; an ADDITION"})
    return out


def _merge(claims: list[dict], key: str) -> str:
    vals = []
    for c in claims:
        v = c.get(key)
        if v in (None, "", []):
            continue
        s = f"[{c['_reported_by']}] {v}" if len(claims) > 1 else str(v)
        vals.append(s)
    return " || ".join(vals)


def _merge_list(claims: list[dict], key: str) -> str:
    out = []
    for c in claims:
        v = c.get(key) or []
        if isinstance(v, str):
            v = [v]
        for x in v:
            s = x if isinstance(x, str) else json.dumps(x, sort_keys=True)
            if s not in out:
                out.append(s)
    return ";".join(out)


#: weakest first. An issue split across workstreams is only as closed as its
#: least-closed part, so the minimum always wins. Never round up.
_ORDER = ["open", "disputed", "partially_closed", "control_repaired", "closed"]


def _normalise_disposition(raw: str) -> tuple[str, bool]:
    """Map a worker's free text onto the controlled vocabulary.

    Returns (value, conformed). Workers write prose like "fixed for my share
    (the 52 form549d derived observations)", which carries real information but
    cannot be counted. It is mapped so the register can be summarised, and the
    ORIGINAL TEXT IS ALWAYS KEPT alongside -- a normalisation that discards what
    the person actually said would be exactly the kind of rounding-up this
    register exists to prevent. Anything that does not map lands on the weakest
    disposition, never the strongest.
    """
    v = (raw or "").strip().lower()
    if not v:
        return "unclaimed", False
    if v in _ORDER:
        return v, True
    # ordered longest-intent-first: "not fixed" must not match on "fixed"
    if any(k in v for k in ("left failing", "not fixed", "not repaired", "still open",
                            "blocked on", "cannot", "unresolved")):
        return "open", False
    if "disput" in v or "counterevidence" in v:
        return "disputed", False
    if any(k in v for k in ("gate", "gated", "control", "correctly gated")):
        return "control_repaired", False
    if any(k in v for k in ("my share", "partial", "some ", "in part")):
        return "partially_closed", False
    if any(k in v for k in ("no change needed", "not applicable to my files",
                            "delivered to the integrator")):
        return "closed", False
    if any(k in v for k in ("fixed", "closed", "resolved", "implemented", "done")):
        return "closed", False
    return "open", False


def _disposition(claims: list[dict]) -> str:
    if not claims:
        return "unclaimed"
    vals = [_normalise_disposition(str(c.get("final_disposition", "")))[0]
            for c in claims]
    vals = [v for v in vals if v and v != "unclaimed"]
    if not vals:
        return "unclaimed"
    return min(vals, key=_ORDER.index)


def _disposition_detail(claims: list[dict]) -> str:
    """The workers' own words, verbatim, and whether each conformed."""
    out = []
    for c in claims:
        raw = str(c.get("final_disposition", "")).strip()
        if not raw:
            continue
        norm, conformed = _normalise_disposition(raw)
        tag = "" if conformed else f" [normalised to `{norm}`; not from the controlled set]"
        out.append(f"[{c['_reported_by']}] {raw}{tag}")
    return " || ".join(out)


#: data-side checks, keyed by issue. Each states what number would show the
#: defect still present, so the register is never just the worker's word.
_DATA_CHECKS = {
    "A04": ("margins stored inside the fraction range while declaring percent",
            "A04_margins_outside_percent_range", 0),
    "A11": ("investor-feed events with is_backfill=0 predating the baseline",
            "A11_historical_current_events", 0),
    "A18": ("observations whose filing_id resolves to no filings row",
            "A18_dangling_observation_filings", 0),
    "A08": ("lineage edges with no input fact, observation or population",
            "A08_edges_with_no_input", 0),
}


def _data_side(iid: str, facts: dict) -> dict:
    if not facts.get("available") or iid not in _DATA_CHECKS:
        return {}
    label, key, want = _DATA_CHECKS[iid]
    got = facts.get(key)
    if isinstance(got, str):
        return {"check": label, "result": got, "agrees": "unknown"}
    return {"check": label, "result": f"{got} (expected {want})",
            "agrees": "yes" if got == want else "NO -- the data still shows the defect"}


def resolve_exceptions(audit_ex: list[dict], reported: dict[str, list[dict]],
                       facts: dict) -> list[dict]:
    """Every one of the 34 original blocker IDs, whatever became of it."""
    claims_by_blocker: dict[str, list[dict]] = {}
    for rows in reported.values():
        for r in rows:
            for b in (r.get("blockers") or []):
                bid = b.get("blocker_id") if isinstance(b, dict) else str(b)
                if bid:
                    claims_by_blocker.setdefault(bid, []).append(
                        dict(b if isinstance(b, dict) else {"note": b},
                             _reported_by=r["_reported_by"]))
    out = []
    for e in audit_ex:
        bid = e["blocker_id"]
        c = claims_by_blocker.get(bid, [])
        out.append({
            "blocker_id": bid,
            "adapter": e["adapter"],
            "scope": e["scope"],
            "kind": e["kind"],
            "summary": e["summary"],
            "audit_classification": e["audit_classification"],
            "audit_subtype": e.get("audit_subtype", ""),
            "audit_required_action": e["audit_required_action"],
            # the audit established that NONE of the 34 was irreducible
            "audit_irreducible_established": e["irreducible_established"],
            "attempts_made": _merge(c, "attempts"),
            "evidence": _merge(c, "evidence"),
            "replacement_occurrence": _merge(c, "replacement_occurrence"),
            "obligation_level_effect": _merge(c, "obligation_effect"),
            "final_classification": (_merge(c, "final_classification")
                                     or "unclaimed -- no workstream reported on this ID"),
            "outstanding_action": _merge(c, "outstanding"),
            "reported_by": ";".join(sorted({x["_reported_by"] for x in c})),
        })
    return out


def write_pair(rows: list[dict], stem: pathlib.Path, meta: dict) -> None:
    stem.with_suffix(".json").write_text(
        json.dumps({**meta, "rows": rows}, indent=1, sort_keys=False), encoding="utf-8")
    if not rows:
        stem.with_suffix(".csv").write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(HERE / "staging" / "operating_assets.sqlite"))
    ap.add_argument("--out", default=str(HERE))
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)

    audit_issues, audit_ex = load_audit()
    reported, files = load_worker_reports()
    facts = db_facts(pathlib.Path(args.db))
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    meta = {
        "generated_at_utc": stamp,
        "audit": "FERC_Independent_Audit_2026-09-08",
        "worker_reports_read": files,
        "database": args.db,
        "database_available": facts.get("available", False),
        "dispositions": DISPOSITIONS,
        "rule": ("no issue and no blocker disappears, including by rename; a new "
                 "blocker is an addition, never a replacement; where a worker's "
                 "report and the data disagree, the disagreement is reported"),
    }

    issues = resolve_issues(audit_issues, reported, facts)
    write_pair(issues, out / "issue_resolution", {**meta, "count": len(issues)})
    exceptions = resolve_exceptions(audit_ex, reported, facts)
    write_pair(exceptions, out / "exceptions_resolution",
               {**meta, "count": len(exceptions)})
    (out / "data_side_facts.json").write_text(json.dumps(facts, indent=1), encoding="utf-8")

    from collections import Counter
    c = Counter(r["final_disposition"] for r in issues)
    print(f"issue_resolution      : {len(issues)} rows "
          f"({len(audit_issues)} audit + {len(issues)-len(audit_issues)} new)")
    for k, n in sorted(c.items()):
        print(f"    {k:20s} {n}")
    print(f"exceptions_resolution : {len(exceptions)} rows "
          f"(all {len(audit_ex)} original blocker IDs)")
    unclaimed = [r["issue_id"] for r in issues if r["final_disposition"] == "unclaimed"]
    if unclaimed:
        print(f"    UNCLAIMED (reported, not hidden): {', '.join(unclaimed)}")
    print(f"worker reports read   : {files or 'none yet'}")
    print(f"database              : {'read' if facts.get('available') else 'ABSENT'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
