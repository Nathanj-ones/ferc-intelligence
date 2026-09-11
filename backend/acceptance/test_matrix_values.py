"""
Adversarial matrix, points 6 and 12: reviewed annotations, and scope leakage.

Point 6 is audit A09. `exporters._check_flags_survive` catches a row whose
warning TEXT was stripped while its `validation` still says a warning is due.
It cannot catch the case the audit actually found: when the flag text and the
review status are removed TOGETHER, the row no longer claims anything, so
nothing on the row contradicts anything else and the export is clean.

The only thing that can detect that is something OUTSIDE the row: the declared
review input in `config/annotations/`. Those seven annotations name exactly which
(entity, filing, metric, filed value) facts a human reviewed and why. A fact
named there that comes out of a rebuild carrying no review flag has lost an
upstream annotation, and the annotation file is the witness. That cross-check is
implemented here because it is an acceptance question, not an export question.

Point 12 is the storage/transportation scope boundary (A16). w3-financial's
adapter tests cover the parser; this covers the published data independently.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

from .harness import TREE, Tier, acceptance, must_reject, require, require_population

ANNOTATION_INPUT = TREE / "config" / "annotations" / "reviewed_source_annotations.json"

#: A fact carries its review when EITHER the machine status says so or a human
#: sentence travels with it. Requiring both would fail correct rows; requiring
#: neither is the A09 defect.
def _carries_review(row: dict) -> bool:
    from ferclib.status import Validation
    if (row.get("validation") or "") in Validation.MUST_PROPAGATE:
        return True
    narrative = " ".join(str(row.get(c) or "")
                         for c in ("qa_flags", "review_status", "missing_reason"))
    return bool(narrative.strip())


def _declared_annotations() -> list[dict]:
    raw = json.loads(ANNOTATION_INPUT.read_bytes())
    rows = raw if isinstance(raw, list) else (
        raw.get("rows") or raw.get("annotations") or list(raw.values()))
    if rows and not isinstance(rows[0], dict):
        rows = list(raw.values())
    return [r for r in rows if isinstance(r, dict)]


def check_annotated_facts_keep_their_flags(observations: list[dict],
                                           annotations: list[dict]) -> list[dict]:
    """The cross-check the export layer cannot do from a row alone.

    Returns the annotated facts that reached the output with no review of any
    kind. A non-empty result is a lost upstream annotation -- the exact A09
    condition, where a clean cached rebuild silently dropped four Fayetteville
    flags and every row still looked internally consistent.
    """
    by_key: dict[tuple, list[dict]] = {}
    for o in observations:
        key = (o.get("entity_key"), str(o.get("filing_id") or ""), o.get("metric_id"))
        by_key.setdefault(key, []).append(o)

    lost = []
    for a in annotations:
        key = (a.get("entity_key"), str(a.get("filing_id") or ""), a.get("metric_id"))
        matches = by_key.get(key, [])
        for o in matches:
            filed = str(a.get("filed_text") or "")
            if filed and str(o.get("value_text") or o.get("value") or "") != filed:
                continue
            if not _carries_review(o):
                lost.append({"entity_key": key[0], "filing_id": key[1],
                             "metric_id": key[2], "filed_text": filed,
                             "observation_id": o.get("observation_id"),
                             "required_by": a.get("review_status"),
                             "rationale": (a.get("rationale") or "")[:120]})
    return lost


# ------------------------------------------------------------------ point 6

@acceptance(issue="M6.1", group="annotations", owner="w1-runtime",
            mutation="removing a warning's text and its review status together")
def t_losing_text_and_status_together_is_detected(env):
    """removing warning text AND review status together fails against the input"""
    import exporters

    require(ANNOTATION_INPUT.is_file(),
            f"the declared review input is absent: {ANNOTATION_INPUT}")
    annotations = _declared_annotations()
    require_population(annotations, "declared reviewed-source annotations", minimum=1)

    con, which = env.any_db()
    rows = []
    for a in annotations:
        for r in con.execute("""
                SELECT * FROM observations
                WHERE entity_key=? AND CAST(filing_id AS TEXT)=? AND metric_id=?""",
                (a.get("entity_key"), str(a.get("filing_id") or ""),
                 a.get("metric_id"))).fetchall():
            rows.append(dict(r, value=r["value_text"]))
    require_population(rows, "observations matching the declared annotations")

    # POSITIVE CONTROL: as published, every annotated fact carries its review.
    lost_now = check_annotated_facts_keep_their_flags(rows, annotations)
    require(not lost_now,
            f"{len(lost_now)} annotated fact(s) already reach the output with NO review "
            f"of any kind, against the declared input: {lost_now[:2]}")

    # MUTATION A: strip the text only. The export layer must catch this alone.
    text_only = [dict(r, qa_flags="", review_status="", missing_reason="") for r in rows]
    caught_by_exporter = False
    try:
        exporters._check_flags_survive(text_only, "W6ACC_SYNTHETIC_TEXT_ONLY")
    except exporters.FlagStripped:
        caught_by_exporter = True

    # MUTATION B: strip the text AND the status. Nothing on the row contradicts
    # anything, so the export layer is blind -- the declared input is the witness.
    both = [dict(r, qa_flags="", review_status="", missing_reason="",
                 validation="pass") for r in rows]
    exporter_blind = True
    try:
        exporters._check_flags_survive(both, "W6ACC_SYNTHETIC_BOTH")
    except exporters.FlagStripped:
        exporter_blind = False

    lost = check_annotated_facts_keep_their_flags(both, annotations)
    require(lost,
            "stripping BOTH the warning text and the review status was NOT detected, "
            "even against the declared annotation input. That is A09: a clean rebuild "
            "can drop a reviewed flag and every row still looks self-consistent.")
    return (f"[{which}] {len(rows)} annotated fact(s). Text-only strip caught by the "
            f"exporter: {caught_by_exporter}. Text+status strip is invisible to the "
            f"exporter ({exporter_blind}) and is caught only against the declared "
            f"input: {len(lost)} lost annotation(s) identified.")


@acceptance(issue="M6.2", group="annotations", owner="integrator",
            mutation="a rebuild that drops a reviewed flag")
def t_reviewed_facts_retain_flags_after_rebuild(env, ):
    """all reviewed facts retain their flags in the published data"""
    annotations = _declared_annotations()
    require_population(annotations, "declared reviewed-source annotations")
    con, which = env.any_db()

    staged = con.execute("SELECT COUNT(*) n FROM reviewed_source_annotations").fetchone()["n"]
    require(staged >= len(annotations),
            f"the database holds {staged} reviewed annotations but the declared input "
            f"names {len(annotations)}; {len(annotations) - staged} were never applied")

    rows = [dict(r, value=r["value_text"]) for r in con.execute("""
        SELECT o.* FROM observations o
        WHERE EXISTS (SELECT 1 FROM reviewed_source_annotations a
                      WHERE a.entity_key=o.entity_key
                        AND CAST(a.filing_id AS TEXT)=CAST(o.filing_id AS TEXT)
                        AND a.metric_id=o.metric_id)""").fetchall()]
    require_population(rows, "observations covered by a staged annotation")
    lost = check_annotated_facts_keep_their_flags(rows, annotations)
    require(not lost,
            f"{len(lost)} reviewed fact(s) carry no review flag in the published data: "
            f"{lost[:3]}")
    return (f"[{which}] {staged} staged annotation(s); {len(rows)} covered observation(s), "
            "all retaining a review flag")


# ------------------------------------------------------------------ point 12

@acceptance(issue="M12.1", group="scope", owner="w3-financial",
            mutation="storage revenue summed into a 549D transportation total")
def t_storage_revenue_cannot_leak_into_transport(env):
    """storage revenue cannot leak into a 549D transportation total"""
    con, which = env.any_db()
    transport = con.execute("""
        SELECT * FROM observations
        WHERE metric_id='i311_annual_transport_revenue' AND availability='present'
          AND value_num IS NOT NULL""").fetchall()
    if not transport:
        raise __import__("acceptance.harness", fromlist=["TierUnavailable"]).TierUnavailable(
            "no 549D transportation-revenue observations are staged in this database")
    components = {(_r["entity_key"], _r["reporting_year"]): _r for _r in con.execute("""
        SELECT * FROM observations WHERE metric_id='i311_revenue_components'
          AND availability='present'""").fetchall()}

    checked = leaks = 0
    for t in transport:
        comp = components.get((t["entity_key"], t["reporting_year"]))
        if not comp or not comp["value_text"]:
            continue
        try:
            payload = json.loads(comp["value_text"])
        except json.JSONDecodeError:
            continue
        by_type = payload.get("revenue_by_service_type_as_filed") or {}
        storage = Decimal(str(by_type.get("Storage", 0) or 0))
        if storage <= 0:
            continue
        checked += 1
        # INDEPENDENT arithmetic: the published transportation figure must not
        # equal the all-services total. Decimal, so no float rounding can make a
        # leak look like a match or a match look like a leak.
        published = Decimal(str(t["value_num"]))
        everything = sum((Decimal(str(v or 0)) for v in by_type.values()), Decimal(0))
        if published == everything:
            leaks += 1
        require(published != everything,
                f"{t['entity_key']} {t['reporting_year']}: the transportation figure "
                f"{published} equals the ALL-SERVICES total {everything}, so storage "
                f"revenue of {storage} is inside a transportation total. Order No. 735-A "
                "excludes it by the field's own definition.")
        require(published <= everything,
                f"{t['entity_key']} {t['reporting_year']}: transportation {published} "
                f"exceeds all services {everything}")
    require(checked > 0,
            "no entity carried both a transportation total and a non-zero filed storage "
            "component, so the leak could not be tested on real data")
    return (f"[{which}] {checked} entity-year(s) with non-zero filed storage revenue: "
            f"{leaks} leak(s); every transportation total is strictly below the "
            "all-services total")


@acceptance(issue="M12.2", group="scope", owner="w3-financial",
            mutation="a multiplier accepted where a fractional change is required")
def t_index_factor_cannot_satisfy_index_change(env):
    """the oil index factor cannot satisfy a slot requiring the change"""
    from ferclib import coverage
    from ferclib.registry import REGISTRY

    by_id = {m.id: m for m in REGISTRY}
    factor = by_id.get("liq_oil_pipeline_index_factor")
    change = by_id.get("liq_oil_pipeline_index_change")
    require(factor is not None and change is not None,
            "the oil index metrics are not both declared; w3-financial split "
            "liq_oil_price_index into a factor and a change")
    require(factor.canonical_unit != change.canonical_unit,
            f"the factor and the change share canonical unit "
            f"{factor.canonical_unit!r}; 1.014290 and 0.014290 are different "
            "quantities and must not be interchangeable")

    fam_factor, _ = coverage.admissible_unit_families(factor.unit_rule,
                                                      factor.canonical_unit)
    fam_change, _ = coverage.admissible_unit_families(change.unit_rule,
                                                      change.canonical_unit)
    require(not (fam_factor & fam_change),
            f"the factor's admissible unit families {sorted(fam_factor)} overlap the "
            f"change's {sorted(fam_change)}, so a multiplier could satisfy a slot that "
            "requires the fractional change")
    return (f"factor {factor.canonical_unit!r} families {sorted(fam_factor)} are "
            f"disjoint from change {change.canonical_unit!r} families "
            f"{sorted(fam_change)}; a multiplier cannot satisfy a fraction slot")
