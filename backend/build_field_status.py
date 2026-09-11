#!/usr/bin/env python3
"""
Per-field implementation status, computed WITHIN the template that requests it.

Task section 12: every requested template field must end in a MEASURED status,
and a placeholder column or a function returning GATED is NOT an implemented
adapter. So this report derives each field's status from what the run actually
produced, never from a declaration in the registry:

    implemented_retrieved_validated        a real value passed its quality gate
    implemented_source_blank_or_not_required
                                           implemented; the source says nothing,
                                           with evidence for why
    source_exists_interpretation_blocked   raw evidence retained, comparison gated
    retrieval_or_access_failure            attempts and the exact blocker recorded
    not_implemented_unfinished_work        OUR gap, counted as unfinished

Two distinctions carry this file.

The first is that unfinished engineering is never reported as a FERC data gap,
and a source blank is never reported as unfinished engineering.

The second is the one the 8 September audit found broken (A07). The previous
version counted observations by `metric_id` ALONE and then wrote the same
verdict onto every template that requests the metric:

    SELECT availability, validation, COUNT(*) FROM observations WHERE metric_id=?

Storage capacity passing for forty interstate gas filers therefore marked gas
STORAGE ready, although not one of the nine storage filers had a present, passing
value for it. Seven of the 144 "implemented, retrieved, validated" rows were
carried entirely by another template's data, and four had no present value in
their own template at all. Readiness is now scoped to the observations of the
entities that actually sit in that template, restricted to the regimes the
metric declares, and three facts are kept apart rather than collapsed into one
label: whether the adapter is IMPLEMENTED, whether it HAS DATA here, and whether
that data VALIDATED here.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import sqlite3
import sys
from collections import Counter, defaultdict

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ferclib import coverage as cov                                  # noqa: E402
from ferclib.registry import BY_ID, REGISTRY                         # noqa: E402
from ferclib.staging import Staging                                  # noqa: E402
from ferclib.status import Availability, FieldOutcome, Validation    # noqa: E402

DB = HERE / "staging" / "operating_assets.sqlite"
DEFAULT_OUT = pathlib.Path(os.environ.get("FERC_OUTPUT_DIR", str(HERE))) / "exports"
DEFAULT_FIELD_CROSSWALK = HERE / "config" / "field_crosswalk_166.csv"


def _validate_field_crosswalk(path: pathlib.Path) -> dict:
    """Prove that the generated 166-field reconciliation reaches this registry.

    The crosswalk used to be merely declared as a run-plan input while this
    command never opened it.  That allowed a stale or wrong mapping to coexist
    with a green field-status export.  Validate the complete target set before
    opening the database or any output path, and retain the legacy 166-row
    population as an explicit invariant rather than a headline count.
    """
    required = {"source_doc", "source_row", "template", "disposition", "successors"}
    with pathlib.Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = sorted(required - set(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"field crosswalk has wrong schema; missing {missing}: {path}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"field crosswalk contains zero data rows: {path}")
    audited = [r for r in rows
               if (r.get("source_doc") or "").startswith("audit 2026-09-08")]
    lost = [r.get("source_row", "") for r in rows
            if r.get("disposition") in {"lost", "partially_lost"}]
    targets = {((r.get("template") or "").strip(), successor.strip())
               for r in rows
               for successor in (r.get("successors") or "").split(";")
               if successor.strip()}
    expected = {(template, metric.id)
                for metric in REGISTRY for template in metric.templates}
    if len(audited) != 166 or lost or targets != expected:
        missing_targets = sorted(expected - targets)
        extra_targets = sorted(targets - expected)
        raise ValueError(
            "field crosswalk reconciliation failed: "
            f"audited={len(audited)}/166, lost={lost[:5]}, "
            f"targets={len(targets)}/{len(expected)}, "
            f"missing={missing_targets[:5]}, extra={extra_targets[:5]}")
    return {"rows": len(rows), "audited_rows": len(audited),
            "target_keys": len(targets)}


def _entity_templates(query) -> dict[str, str]:
    """entity_key -> the template its asset sits in.

    An observation belongs to the template of the filer that produced it. No
    entity in the delivered universe maps to two templates, so this is a
    function, not a fan-out; if one ever does, the mapping row carries the scope
    and the ambiguity must be resolved there rather than silently duplicated.
    """
    out: dict[str, str] = {}
    for r in query("SELECT m.entity_key ek, a.template t FROM asset_entity_map m "
                   "JOIN assets a USING(asset_id)"):
        out.setdefault(r["ek"], r["t"])

    # INSTRUMENT ENTITIES HAVE NO ASSET, and must still be visible here.
    #
    # The FERC Oil Pipeline Index is one industry-wide instrument, not a filer,
    # so it is deliberately absent from `assets` and `asset_entity_map` -- giving
    # it a synthetic asset row would corrupt the 109-asset / 104-entity roster
    # reconciliation. But the join above is the only route into a template, so
    # its 60 observations were invisible and the two index fields were reported
    # as "adapter not run": a field with data described as unimplemented, which
    # is the mirror image of the A07 defect this module exists to fix.
    #
    # An instrument's observations belong to the template its METRICS declare,
    # which is the only template that requests them.
    for r in query("SELECT DISTINCT o.entity_key ek, o.metric_id mid FROM observations o "
                   "LEFT JOIN asset_entity_map m ON m.entity_key = o.entity_key "
                   "WHERE m.entity_key IS NULL"):
        m = BY_ID.get(r["mid"])
        if m is not None and m.templates:
            out.setdefault(r["ek"], m.templates[0])
    return out


def _local_counts(query, ent_template):
    """(template, metric_id) -> Counter of (availability, validation), plus entities."""
    counts: dict[tuple, Counter] = defaultdict(Counter)
    entities: dict[tuple, set] = defaultdict(set)
    regimes: dict[tuple, set] = defaultdict(set)
    for r in query("SELECT entity_key, metric_id, source_regime, availability, "
                   "validation, COUNT(*) n FROM observations "
                   "GROUP BY 1,2,3,4,5"):
        t = ent_template.get(r["entity_key"])
        if not t:
            continue
        key = (t, r["metric_id"])
        counts[key][(r["availability"], r["validation"])] += r["n"]
        entities[key].add(r["entity_key"])
        regimes[key].add(r["source_regime"])
    return counts, entities, regimes


def _selector_failures(query, ent_template) -> dict[tuple, int]:
    """(template, metric_id) -> observations that are OUR selector missing a fact.

    The adapters record these as `parse_failed`, which puts them in
    `Availability.OUR_GAP` alongside genuine retrieval and parse problems -- and
    the field report then called them `retrieval_or_access_failure`. They are
    nothing of the kind. The reason text says so in as many words: the fact
    matches the requested period grain and no selector matched it. Nothing failed
    to arrive and nothing failed to parse; our selector did not recognise what we
    already had. Reporting that as an access failure blames the source for our
    own unfinished work, which is the one thing the field report exists to
    prevent.
    """
    out: dict[tuple, int] = defaultdict(int)
    for r in query("SELECT entity_key, metric_id, COUNT(*) n FROM observations "
                   "WHERE missing_reason LIKE 'SELECTOR FAILURE%' GROUP BY 1,2"):
        t = ent_template.get(r["entity_key"])
        if t:
            out[(t, r["metric_id"])] += r["n"]
    return dict(out)


def _template_requirement(query) -> dict[tuple, Counter]:
    """(template, metric_id) -> requirement classes the frozen grid assigned.

    A field the form does not collect in this template is NOT unfinished
    engineering, and a field with no slot at all in this template is not a
    validated one either.
    """
    req: dict[tuple, Counter] = defaultdict(Counter)
    try:
        rows = query("SELECT template, metric_id, requirement, COUNT(*) n "
                     "FROM coverage_expected GROUP BY 1,2,3")
    except sqlite3.Error:
        return req
    for r in rows:
        req[(r["template"], r["metric_id"])][r["requirement"]] += r["n"]
    return req


def classify(m, template, counts, n_entities, req, blockers, selector_failures=0):
    """Outcome for ONE (template, field), from that template's own evidence."""
    present_ok = sum(n for (a, v), n in counts.items()
                     if a == Availability.PRESENT and v == Validation.PASS)
    present_flagged = sum(n for (a, v), n in counts.items()
                          if a == Availability.PRESENT and v in Validation.MUST_PROPAGATE)
    present_other = sum(n for (a, v), n in counts.items()
                        if a == Availability.PRESENT) - present_ok - present_flagged
    source_absent = sum(n for (a, v), n in counts.items()
                        if a in Availability.SOURCE_CONDITION)
    our_gap = sum(n for (a, v), n in counts.items() if a in Availability.OUR_GAP)
    semantic = sum(n for (a, v), n in counts.items() if a in Availability.SEMANTIC_GATE)
    blocked = sum(n for (a, v), n in counts.items()
                  if v in (Validation.BLOCKED_AMBIGUITY, Validation.SCOPE_INCOMPATIBLE))
    total = sum(counts.values())
    core = sum(n for rq, n in req.items() if rq in cov.CORE)
    not_required = req.get(cov.NOT_REQUIRED, 0)
    unknown = req.get(cov.UNKNOWN, 0)

    if total == 0:
        if not_required and not core:
            return (FieldOutcome.IMPLEMENTED_SOURCE_ABSENT, "implemented",
                    f"{not_required} frozen slots in {template} class the concept as not "
                    "collected by this template's form; the adapter produced nothing "
                    "because there is nothing to produce", "")
        if unknown and not core:
            return (FieldOutcome.INTERPRETATION_BLOCKED, "implemented",
                    f"{unknown} frozen slots in {template} are APPLICABILITY_UNKNOWN; "
                    "whether this template's form collects the field is unresolved", "")
        return (FieldOutcome.NOT_IMPLEMENTED, "not_implemented",
                f"adapter '{m.adapter}' produced no observation for this metric in the "
                f"{template} template in this run"
                + (f", although {core} slots are requested of it" if core else ""),
                "; ".join(b for b in blockers[:2]) or "adapter not run or metric not reached")
    if present_ok:
        return (FieldOutcome.IMPLEMENTED_VALIDATED, "implemented",
                f"{present_ok} validated + {present_flagged} flagged values across "
                f"{n_entities} {template} entities", "")
    if present_flagged or present_other or semantic or blocked:
        # There is a value here, and it is not usable unqualified. That is an
        # interpretation gate, and it is NOT readiness -- which is the whole of
        # A07: a metric passing in interstate gas cannot validate a storage field
        # whose only local values are review-state.
        return (FieldOutcome.INTERPRETATION_BLOCKED, "implemented",
                f"{present_flagged + present_other + semantic} values retained in "
                f"{template} across {n_entities} entities, none of which passed its "
                f"quality gate here",
                m.gate_reason or "no present, passing value exists within this template")
    if source_absent and not our_gap:
        return (FieldOutcome.IMPLEMENTED_SOURCE_ABSENT, "implemented",
                f"{source_absent} observations across {n_entities} {template} entities, all "
                "recording an evidenced source condition (blank, nil, not required, "
                "not applicable or not yet due)", "")
    if our_gap:
        if selector_failures >= our_gap:
            # Every one of them is our selector failing to match a fact we
            # already hold. That is unfinished engineering, and the vocabulary
            # has one term for it.
            return (FieldOutcome.NOT_IMPLEMENTED, "partial",
                    f"{selector_failures} observations in {template} record a SELECTOR "
                    "FAILURE: the fact matches the requested period grain and no selector "
                    "matched it. The source arrived and parsed; our selector did not "
                    "recognise it, so this is unfinished engineering, not an access failure",
                    "no selector matches the filed fact for this metric")
        return (FieldOutcome.RETRIEVAL_FAILURE, "partial",
                f"{our_gap} observations in {template} record a retrieval or parse failure"
                + (f", of which {selector_failures} are selector failures rather than "
                   "source problems" if selector_failures else ""),
                "; ".join(b for b in blockers[:2]) or "see blockers table")
    located = sum(n for (a, _v), n in counts.items()
                  if a == Availability.EXPECTED_NOT_LOCATED)
    if located:
        # "The record should exist and the index shows nothing" is a location
        # failure, not proof that FERC holds nothing (contract rule 7) and not
        # unfinished engineering: the adapter ran and searched.
        return (FieldOutcome.RETRIEVAL_FAILURE, "partial",
                f"{located} observations across {n_entities} {template} entities record "
                "EXPECTED_NOT_LOCATED: a record was searched for and the index returned "
                "nothing; absence of a document is not proof of non-compliance",
                "; ".join(b for b in blockers[:2])
                or "no indexed record located for this template's filers")
    unverified = sum(n for (a, _v), n in counts.items()
                     if a == Availability.UNVERIFIED_AVAILABILITY)
    if unverified:
        # The adapter ran and returned a considered answer: it could not
        # establish whether FERC holds such a record for these filers. That is
        # an access/verification gap of ours, not unfinished engineering (the
        # code exists and executed) and not a FERC data gap (nothing has been
        # shown absent).
        return (FieldOutcome.RETRIEVAL_FAILURE, "partial",
                f"{unverified} observations across {n_entities} {template} entities record "
                "UNVERIFIED availability: whether a FERC record exists was never "
                "established for this template's filers",
                "; ".join(b for b in blockers[:2])
                or "availability not established; absence is not evidence of absence")
    return (FieldOutcome.NOT_IMPLEMENTED, "not_implemented",
            f"{total} observations in {template}, none in a recognised terminal state", "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("FERC_STAGING_DB", str(DB)))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--field-crosswalk", type=pathlib.Path,
                    default=DEFAULT_FIELD_CROSSWALK)
    ap.add_argument("--no-write", action="store_true",
                    help="read the database without opening it for writing")
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)

    # Preflight the mapping before a writable database connection or output
    # file is opened.  A bad prerequisite therefore preserves last-good data.
    try:
        crosswalk_identity = _validate_field_crosswalk(args.field_crosswalk)
    except (OSError, ValueError) as exc:
        print(f"field crosswalk preflight failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if args.no_write:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        query, db = (lambda sql, p=(): con.execute(sql, p).fetchall()), None
    else:
        db = Staging(pathlib.Path(args.db), create=False)
        query = db.query

    ent_template = _entity_templates(query)
    counts, entities, regimes = _local_counts(query, ent_template)
    selfail = _selector_failures(query, ent_template)
    req = _template_requirement(query)
    blockers: dict[str, list[str]] = defaultdict(list)
    for r in query("SELECT adapter, summary FROM blockers WHERE resolved_at IS NULL"):
        blockers[r["adapter"]].append(r["summary"])

    rows = []
    for m in REGISTRY:
        for template in m.templates:
            key = (template, m.id)
            c = counts.get(key, Counter())
            ents = entities.get(key, set())
            outcome, impl, evidence, blocker = classify(
                m, template, c, len(ents), req.get(key, Counter()),
                blockers.get(m.adapter, []), selfail.get(key, 0))
            present = sum(n for (a, _v), n in c.items() if a == Availability.PRESENT)
            present_pass = sum(n for (a, v), n in c.items()
                               if a == Availability.PRESENT and v == Validation.PASS)
            rows.append({
                "template": template, "field_id": m.id, "metric_id": m.id,
                "adapter": m.adapter, "implementation": impl, "outcome": outcome,
                "evidence": evidence, "blocker": blocker,
                "note": f"role={m.role}; regimes={';'.join(r for r, _ in m.regimes)}",
                # the three facts kept deliberately apart
                "adapter_implemented": int(m.implementation == "implemented"),
                "has_data_in_template": int(present > 0),
                "validated_in_template": int(present_pass > 0),
                "within_template_observations": sum(c.values()),
                "within_template_present": present,
                "within_template_present_pass": present_pass,
                "within_template_entities": len(ents),
                "within_template_regimes": ";".join(sorted(regimes.get(key, ()))),
                "selector_failures_in_template": selfail.get(key, 0),
                "core_slots_in_template": sum(
                    n for rq, n in req.get(key, {}).items() if rq in cov.CORE),
                # what the whole-universe count would have said, kept only as a
                # contrast so the correction is visible rather than silent
                "cross_template_present_pass": sum(
                    v for (t, mid), cc in counts.items() if mid == m.id
                    for (a, val), v in cc.items()
                    if a == Availability.PRESENT and val == Validation.PASS),
            })

    if db is not None:
        db.replace_field_status(rows)
    cov.write_csv(out / "field_status.csv", rows)

    by_outcome = Counter(r["outcome"] for r in rows)
    by_template: dict[str, Counter] = {}
    for r in rows:
        by_template.setdefault(r["template"], Counter())[r["outcome"]] += 1
    corrected = [r for r in rows
                 if r["outcome"] == FieldOutcome.IMPLEMENTED_VALIDATED
                 and not r["validated_in_template"]]
    summary = {"field_rows": len(rows), "distinct_metrics": len(REGISTRY),
               "by_outcome": dict(by_outcome),
               "by_template": {k: dict(v) for k, v in sorted(by_template.items())},
               "readiness_basis": "within template, adapter, applicability and grain",
               "rows_validated_locally": sum(r["validated_in_template"] for r in rows),
               "rows_with_local_data": sum(r["has_data_in_template"] for r in rows),
               "rows_with_implemented_adapter": sum(r["adapter_implemented"] for r in rows),
               "labelled_validated_without_local_pass": len(corrected),
               "field_crosswalk": crosswalk_identity}
    out.mkdir(parents=True, exist_ok=True)
    (out / "field_status_summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))

    unfinished = sorted({(r["template"], r["field_id"]) for r in rows
                         if r["outcome"] == FieldOutcome.NOT_IMPLEMENTED})
    if unfinished:
        print(f"\nNOT IMPLEMENTED -- unfinished engineering, not a FERC gap ({len(unfinished)}):")
        for t, f in unfinished:
            b = next(r["blocker"] for r in rows
                     if r["field_id"] == f and r["template"] == t)
            print(f"  {t:16s} {f:38s} {b[:70]}")
    if db is not None:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
