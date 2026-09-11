"""
Honest coverage measurement at the requested grain.

Three rules, and the three defects the 8 September independent audit found in
the way this module implemented them:

  1. THE DENOMINATOR IS FROZEN AND EXTERNAL -- but the shipped grid was not
     external at all. `xbrl_adapter.freeze_expected` iterated
     `[f for f in filings if f["is_canonical"]]`, so an obligation only entered
     the denominator if its filing had been retrieved AND parsed AND accepted.
     A broken parser therefore deleted its own dated obligations and coverage
     went UP (A05). `calendar_slots` below builds the grid from entity
     eligibility, time-varying form obligations, pinned taxonomy versions and
     official FERC deadlines, none of which depend on a fetch succeeding.

  2. THE KEY INCLUDES PERIOD BASIS, EXACT INTERVAL, SCOPE AND UNIT -- but unit
     and filing version were absent from the match key, so a wrong-unit or
     superseded observation satisfied a slot (A06). Unit family, version
     currency and lineage are now admissibility gates, each of which records
     why a candidate was refused.

  3. FOUR COUNTS ARE REPORTED SEPARATELY: populated, source-matched, validated
     and review-free. The selection order used to be
     `(method != "filed", validation != PASS)`, which ranks a BLANK filed
     placeholder above a valid same-grain derived quarter -- 528 core slots
     picked the blank (A06). Ranking is now by what the observation actually
     IS: present and validated, then present with its warning attached, then an
     explained source condition, then our own defects. A review-gated value
     still never counts as validated.

Nothing here may improve a number by weakening a basis. Every gate that refuses
a candidate is recorded on the measured row, so a coverage fall is explainable
rather than merely smaller.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping

from . import periods
from .applicability import (EvidenceKind, FAMILY_OF_FORM, FORM_FAMILIES,
                            HOSTED_REGIMES, ObligationState, SourceHealth)
from .staging import slot_id
from .status import Availability, CoverageOutcome, Validation, coverage_outcome

REQUIRED = "REQUIRED"
CONDITIONAL = "CONDITIONALLY_REQUIRED"
OPTIONAL = "OPTIONAL_OR_SUPPLEMENTAL"
NOT_REQUIRED = "NOT_REQUIRED"
UNKNOWN = "APPLICABILITY_UNKNOWN"

#: requirement classes that belong in the core denominator
CORE = {REQUIRED, CONDITIONAL}

#: Slot lifecycle, kept separate from the requirement class. "Not yet due" is a
#: statement about the calendar; "applicability unknown" is a statement about
#: the law; "source blank" is a statement about the filing. Collapsing any two
#: of them is how an unmet obligation gets reported as a satisfied one.
FUTURE = "future_not_yet_due"
OVERDUE = "overdue"
UNKNOWN_APPLICABILITY = "applicability_unknown"
TECHNICAL_FAILURE = "technical_failure"
SLOT_OPEN = "due_and_open"
OUTSIDE_WINDOW = "outside_declared_window"


# ==================================================================== units

#: unit_rule tokens that state "whatever the source reports", which is a real
#: editorial position for narrative and as-filed metrics, and rules for values
#: that HAVE no unit (a peak date, a refund window, a reporting period). Such a
#: slot cannot be unit-gated, and says so, rather than silently accepting
#: anything or refusing everything.
#: `by <something>` is deliberately NOT here. It was, for the sake of
#: `i311_contract_expiry`'s rule `"by weight"` -- and that was the same permissive
#: default this module spent the day removing, written into the regex that removes
#: them. "By weight" describes the AGGREGATION BASIS of a distribution, not the
#: unit of its values: the metric's 232 present observations carry six different
#: unit strings (`contract rows and Usage_BU, both stated`, `... Annual_Volume_BU`,
#: `... Reservation_BU`, and three more), none of which resolves to a family. So
#: the phrase never licensed skipping the unit check; it only looked as though it
#: did. An unparseable rule is now an unresolved contract -- a gate, not an absence
#: of constraint. Found by w2-ioc's unit census disagreeing with mine by one.
_OPEN_UNIT_TOKENS = re.compile(
    r"^(as (stated|reported|filed)\b.*"
    r"|\(?(date|period)\b.*|\(date range\)|no unit)$", re.I)

#: A trailing "by <something>" states how a figure is BUCKETED, not what it is
#: measured in: "Dth/day by bucket" is dekatherms per day, reported per expiry
#: bucket. Reading the qualifier as though it made the rule open removed the unit
#: check from the metric entirely -- so `ioc_expiry_profile` admitted values with
#: no unit at all, while `ioc_firm_transport_mdq` refused them for exactly the
#: same reason. The qualifier is stripped and the unit underneath is resolved.
_UNIT_QUALIFIER = re.compile(r"\s+by\s+\w+\s*$", re.I)


def _unit_family(unit):
    """Dimensional family of a stored unit, via the registry's own table."""
    try:
        from .registry import unit_family_of
    except Exception:                                    # pragma: no cover
        return ""
    return unit_family_of(unit)


def _unit_known(unit) -> bool:
    """Whether the registry recognises the token at all, dimensional or not."""
    try:
        from .registry import known_unit
    except Exception:                                    # pragma: no cover
        return bool(_unit_family(unit))
    return bool(unit) and known_unit(unit)


def admissible_unit_families(unit_rule: str, canonical_unit: str = "") -> tuple[frozenset, bool]:
    """(families a candidate may be in, whether the rule is deliberately open).

    `unit_rule` is a vocabulary, not a single unit: "utr:dth | ferc:dth
    (migrated-era unit vocabulary)" accepts either spelling of a dekatherm, and
    "utr:dth | pure (rendered p.512-513 supplies Dth)" accepts the rendered
    dimensionless variant too. Every alternative is resolved separately and the
    accepted set is their union.

    The registry's own `unit_family_of` also understands a pipe-joined string,
    but it returns a family only when exactly ONE alternative resolves, silently
    discarding the rest -- so asking it about a whole rule would drop the
    "| pure" alternative and refuse the rendered storage figures that FERC
    genuinely files that way. The alternatives are therefore split here first,
    and the registry is asked one token at a time.

    `canonical_unit` wins when the metric declares one, because a metric like
    `p700_wacc` has unit_rule "percent" and canonical_unit "fraction": FERC
    files it as 0.0913, and reading the rule instead of the contract would
    refuse the filer's own number.

    An open rule returns `(frozenset(), True)`: the slot's unit is whatever the
    source states, or there is no unit to state, so unit cannot refuse a
    candidate. An unresolvable rule returns `(frozenset(), False)`, reported as
    an unresolved unit contract -- an explicit gate, never missing data.
    """
    if canonical_unit:
        fam = _unit_family(canonical_unit)
        if fam:
            return frozenset({fam}), False
    rule = (unit_rule or "").strip()
    if not rule:
        return frozenset(), True
    if "|" not in rule:
        fam = _unit_family(rule)
        if fam:
            return frozenset({fam}), False
    families, open_rule = set(), False
    for tok in rule.split("|"):
        tok = tok.strip()
        bare = re.sub(r"\s*\([^)]*\)\s*", " ", tok).strip()
        if not bare:
            bare = tok
        if not bare:
            continue
        fam = (_unit_family(bare) or _unit_family(tok)
               or _unit_family(_UNIT_QUALIFIER.sub("", bare)))
        if fam:
            families.add(fam)
        elif _OPEN_UNIT_TOKENS.match(bare):
            open_rule = True
    return frozenset(families), open_rule and not families


def _slot_unit_contract(slot: dict) -> tuple[frozenset, bool]:
    """Families this slot accepts, widened by any units the registry declares.

    `Metric.admissible_units` names units FERC is KNOWN to file for a metric that
    are not its canonical dimension, each with recorded evidence. It is not a
    loophole: an entry is a factual claim about the source, and everything
    unlisted stays refused. The two barrel-mile metrics list `xbrli:pure` (the
    Form 6 taxonomy declares no barrel-mile unit) and deliberately do NOT list
    `utr:bbl`, which is a filer's wrong declaration on a barrel-mile concept.

    `ioc_expiry_profile` is the interesting one: it profiles contract expiry
    under two weights taken from different items of the IOC's own D record --
    transportation MDQ (item o, a per-day rate) and contracted storage quantity
    (item p, not per-day). Both dimensions are correct for it. Admitting both is
    safe because the weight is named in `scope`, and scope is part of the slot
    key -- so a rate and a quantity are different SLOTS, never two answers to
    one. Verified against the delivered data at the full slot grain: zero
    (entity, scope, instant) groups carry more than one unit family.
    """
    canonical, declared = "", ()
    try:
        from .registry import BY_ID
        m = BY_ID.get(slot.get("metric_id", ""))
        if m is not None:
            canonical = getattr(m, "canonical_unit", "") or ""
            declared = getattr(m, "admissible_units", ()) or ()
    except Exception:                                    # pragma: no cover
        pass
    families, open_rule = admissible_unit_families(slot.get("unit_rule") or "", canonical)
    if declared:
        extra = {f for f in (_unit_family(u) for u in declared) if f}
        if extra:
            return frozenset(families | extra), False
    return families, open_rule


# ==================================================================== slots

def build_expected(entity_key: str, asset_id: str, template: str, metric,
                   regime: str, basis: str, year: int, period: str,
                   requirement: str, evidence: str, applicability_version: str,
                   run_id: str, *, as_of: str = "", interval_start: str = "",
                   interval_end: str = "", actual_scope: str | None = None,
                   **extra) -> dict:
    """One frozen expected slot at the full requested grain.

    `extra` carries the calendar's own columns (due date, lifecycle state,
    obligation authority and evidence, source health). They are additive: the
    adapters that call this positionally are unaffected.
    """
    start = end = instant = ""
    if basis == periods.QUARTER:
        start, end = periods.quarter_interval(year, period)
    elif basis == periods.YTD:
        start, end = periods.ytd_interval(year, period)
    elif basis == periods.ANNUAL:
        start, end = periods.annual_interval(year)
    elif basis == periods.INTERVAL:
        start, end = interval_start, interval_end
        if (not start or not end or periods.days_between(start, end) is None
                or start > end):
            raise ValueError(
                "interval coverage slots require ordered ISO interval_start/interval_end")
    elif basis in (periods.INSTANT,):
        instant = periods.quarter_interval(year, period)[1]
    elif basis == periods.ANNUAL_OBSERVATION:
        instant = periods.annual_interval(year)[1]
    elif basis in (periods.SNAPSHOT, periods.AS_OF):
        # A snapshot source files many as-of dates, and each is its own requested
        # observation. Leaving the instant blank hashed every snapshot onto ONE
        # slot, so a filer with twelve quarterly indexes reported a denominator
        # of one -- understating the work actually requested of it.
        instant = as_of

    # Registry scope is an instruction/contract.  Document and typed-dimension
    # observations carry the scope that the source actually supports.  Conflating
    # the two leaked rule prose into stored observation scope and also let one
    # docket/facility answer another.  ``None`` means "use the registry contract";
    # an explicitly supplied empty string stays empty and therefore unresolved.
    scope = metric.scope if actual_scope is None else actual_scope
    sid = slot_id(entity_key, metric.id, regime, basis, start, end, instant, scope)
    row = {
        "slot_id": sid, "entity_key": entity_key, "asset_id": asset_id,
        "template": template, "metric_id": metric.id, "source_regime": regime,
        "period_basis": basis, "period_start": start or None, "period_end": end or None,
        "instant_date": instant or None, "reporting_year": year, "reporting_period": period,
        "scope": scope, "unit_rule": metric.unit_rule,
        "requirement": requirement, "requirement_evidence": evidence,
        "applicability_version": applicability_version,
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "frozen_run_id": run_id,
    }
    row.update(extra)
    return row


def _peer_forms(form: str) -> set[str]:
    return set(FORM_FAMILIES.get(FAMILY_OF_FORM.get(form, ""), ()))


def calendar_slots(obligations, metrics, *, entity_template, entity_asset,
                   run_id: str, applicability=None, textblock_concepts=None) -> list[dict]:
    """The eligible-universe grid: expected slots that no fetch can delete.

    For each obligation the register established -- one entity, one governing
    form, one reporting period, whether or not anything was retrieved -- every
    registry metric that the entity's template requests and that form family can
    supply becomes a dated slot. The requirement class comes from the taxonomy
    pinned to the reporting YEAR, not from the namespace of a filing we happened
    to parse; when the pin is unresolved the class is APPLICABILITY_UNKNOWN and
    the slot still exists.

    Retrieval health rides along on `source_health`, so an eleven-filing parse
    failure shows up as eleven live obligations with a defect against them, not
    as eleven obligations that stopped existing.
    """
    textblock_concepts = textblock_concepts or {}
    by_template: dict[str, list] = defaultdict(list)
    for m in metrics:
        for t in m.templates:
            by_template[t].append(m)

    out: list[dict] = []
    seen: set[str] = set()
    for ob in obligations:
        if ob.entity_key not in entity_template:
            # An unclassified filer is a SCOPE question, and contract rule 3.3
            # makes scope non-interchangeable, so there is no defensible default
            # here in either direction. Guessing a template would build a whole
            # metric grid -- and therefore a whole denominator -- for an entity
            # nobody classified. Silently skipping (what this did before) would
            # delete every one of that filer's obligations instead, which is the
            # exact A05 failure this module exists to prevent. Neither is
            # publishable, so it is refused.
            raise ValueError(
                f"no template mapping for entity {ob.entity_key!r}: its "
                f"{ob.form} {ob.year} {ob.period} obligation can be neither "
                "classified nor dropped. Map the entity in asset_entity_map, or "
                "exclude it from the obligation register deliberately.")
        template = entity_template[ob.entity_key]
        asset_id = entity_asset.get(ob.entity_key, "")
        governing = ob.form
        for m in by_template.get(template, ()):
            declared = set(m.regimes)
            # Peer-form expansion inside the family only. A metric declared for
            # Form 2 is requested of the Form 2-A filer too -- whether 2-A
            # actually collects it is then decided by 2-A's own taxonomy, which
            # is how "this form does not collect it, here is the evidence"
            # becomes a publishable answer instead of a silent gap.
            for reg, bas in list(declared):
                for peer in _peer_forms(reg):
                    declared.add((peer, bas))
            for regime, basis in sorted(declared):
                host = HOSTED_REGIMES.get(regime)
                if host is not None:
                    # a hosted sub-regime rides the governing annual filing
                    if host and host != governing:
                        continue
                    if not host and ob.family != "gas_annual":
                        continue
                    if ob.period != "Q4":
                        continue
                elif regime != governing:
                    continue
                elif regime == "eLibrary document":
                    continue                # event-driven; see document_slots
                period_used = ob.period
                if basis in (periods.QUARTER, periods.YTD) and ob.period == "Q4" \
                        and ob.family in ("gas_annual", "oil_annual"):
                    if basis == periods.YTD:
                        continue            # an annual form files no YTD column
                    period_used = "Q4"
                if basis in (periods.SNAPSHOT, periods.AS_OF) and not ob.as_of:
                    # An undated snapshot slot is satisfiable by any date, so it
                    # measures nothing and is never emitted. The obligation is
                    # reported unresolved instead of being quietly weakened.
                    continue
                requirement, evidence = _calendar_requirement(
                    m, ob, applicability, governing)
                state = _slot_state(ob, requirement)
                slot = build_expected(
                    ob.entity_key, asset_id, template, m, regime, basis,
                    ob.year, period_used, requirement, evidence,
                    ob.taxonomy_version or "unresolved", run_id, as_of=ob.as_of,
                    due_date=ob.due_date or None, slot_state=state,
                    obligation_form=governing, obligation_authority=ob.authority,
                    obligation_evidence_kind=ob.evidence_kind,
                    source_health=ob.source_health,
                    source_health_detail=ob.health_detail,
                    denominator_origin="calendar")
                if slot["slot_id"] not in seen:
                    seen.add(slot["slot_id"])
                    out.append(slot)
    return out


def _calendar_requirement(m, ob, applicability, form) -> tuple[str, str]:
    """Requirement class for one metric under one obligation.

    An unresolved taxonomy pin, or an applicability lookup that could not
    complete, can only produce APPLICABILITY_UNKNOWN. Absence of evidence is
    never converted into NOT_REQUIRED (contract rule 7).
    """
    if ob.evidence_kind == EvidenceKind.NONE:
        return UNKNOWN, (ob.evidence or
                         f"no FERC evidence establishes a {form} obligation for "
                         f"{ob.entity_key} in {ob.year}")
    if ob.evidence_kind == EvidenceKind.ROSTER_DECLARED:
        return UNKNOWN, ob.evidence
    if not m.concept:
        # Nothing to look up in a taxonomy: the metric is derived, or its source
        # is a structured dataset or a filed report with no XBRL concept at all
        # (549D, the index of customers, the capacity report). Its applicability
        # rests on the cited authority, and demanding a taxonomy pin for it would
        # convert a perfectly evidenced obligation into "unknown" -- which shrinks
        # the core denominator, which is the failure mode this module exists to
        # prevent.
        return CONDITIONAL, (f"{ob.authority}; derived or non-concept metric, required "
                             "when its inputs are present")
    if ob.taxonomy_pin_state != "pinned":
        return UNKNOWN, (f"no taxonomy version is pinned for {form} {ob.year}; "
                         "whether the form collects this concept is UNKNOWN, not "
                         "'not required'")
    if applicability is None:
        return UNKNOWN, (f"no applicability resolver was supplied for {form} "
                         f"{ob.taxonomy_version}; concept membership is UNKNOWN")
    try:
        in_form, _page, evidence = applicability.status(form, ob.taxonomy_version, m.concept)
    except Exception as exc:                             # pragma: no cover
        return UNKNOWN, f"applicability lookup failed for {m.concept}: {exc}"
    return ({"yes": REQUIRED, "no": NOT_REQUIRED}.get(in_form, UNKNOWN), evidence)


def _slot_state(ob, requirement: str) -> str:
    if requirement == UNKNOWN:
        return UNKNOWN_APPLICABILITY
    if ob.state == ObligationState.FUTURE_NOT_DUE:
        return FUTURE
    if ob.source_health in SourceHealth.DEFECT:
        return TECHNICAL_FAILURE
    if ob.state == ObligationState.DUE and ob.source_health == SourceHealth.NOT_INDEXED:
        return OVERDUE
    return SLOT_OPEN


def document_slots(occurrences, metrics_by_id, *, run_id: str) -> list[dict]:
    """Dated document expectations, one per evidenced facility/docket fact.

    Deliberately NOT a periodic grid. FERC imposes no duty to file a fixed
    number of inspections, orders or tariff records in a year, so a calendar of
    N-per-year document slots would be invented, and an undated generic slot
    would then be satisfiable by any dated fact for the same filer -- which is
    exactly the scope loosening the audit refused. Each occurrence must carry
    its own `as_of` date and its own docket or facility identity; one without a
    date produces no slot and is returned by the caller as an unresolved
    obligation instead.
    """
    out, seen = [], set()
    for occ in occurrences:
        m = metrics_by_id.get(occ.get("metric_id", ""))
        as_of = (occ.get("as_of") or "").strip()
        if m is None or not as_of:
            continue
        requirement = occ.get("requirement") or CONDITIONAL
        requirement_evidence = occ.get("requirement_evidence") or (
            f"{occ.get('authority', 'docket record')}: a dated record exists for "
            f"{occ.get('docket') or occ.get('facility') or occ['entity_key']} as of {as_of}")
        slot = build_expected(
            occ["entity_key"], occ.get("asset_id", ""), occ["template"], m,
            occ.get("source_regime", "eLibrary document"), periods.AS_OF,
            int(as_of[:4]), occ.get("reporting_period", "Q4"),
            requirement, requirement_evidence,
            occ.get("applicability_version", "authority unresolved"), run_id, as_of=as_of,
            actual_scope=occ["scope"] if "scope" in occ else None,
            due_date=occ.get("due_date"),
            slot_state=occ.get("slot_state") or SLOT_OPEN,
            obligation_form=occ.get("obligation_form") or "eLibrary document",
            # Two permissive defaults removed here. `authority` used to fall back
            # to the literal string "docket record", which asserted a legal basis
            # nobody had established -- the exact "leaning on a generic eLibrary
            # Class/Type" the audit refused. And `source_health` defaulted to OK,
            # asserting the artefact was retrieved and fine because the caller
            # said nothing about it.
            obligation_authority=occ.get("authority") or "authority unresolved",
            obligation_evidence_kind=(occ.get("obligation_evidence_kind")
                                      or EvidenceKind.INDEXED_OCCURRENCE),
            source_health=occ.get("source_health") or SourceHealth.HEALTH_NOT_RECORDED,
            source_health_detail=(occ.get("source_health_detail")
                                  or occ.get("health_detail", "")),
            docket=occ.get("docket", ""), facility=occ.get("facility", ""),
            denominator_origin=(occ.get("denominator_origin")
                                or "document_occurrence"))
        if slot["slot_id"] not in seen:
            seen.add(slot["slot_id"])
            out.append(slot)
    return out


def reconcile_expected(calendar: list[dict], shipped: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge the independent calendar with a previously frozen grid.

    Neither side may delete the other. A calendar slot the shipped grid lacks is
    an obligation retrieval failure had removed; a shipped slot the calendar
    lacks is kept and labelled, because dropping it would shrink a denominator,
    which contract rule 6 forbids outright. The returned bridge explains every
    row on both sides.
    """
    by_id = {s["slot_id"]: dict(s) for s in shipped}
    merged: dict[str, dict] = {}
    bridge: list[dict] = []
    for c in calendar:
        sid = c["slot_id"]
        prior = by_id.get(sid)
        row = dict(c)
        if prior is None:
            disposition = "added_by_calendar"
            note = ("this dated obligation was absent from the shipped grid: it exists "
                    "only when a filing is retrieved and parsed")
        else:
            disposition = "kept"
            note = ""
            if prior.get("requirement") != c["requirement"]:
                disposition = "reclassified"
                note = (f"requirement {prior.get('requirement')} -> {c['requirement']} "
                        f"on the year-pinned taxonomy")
            row["shipped_requirement"] = prior.get("requirement")
        merged[sid] = row
        bridge.append({"slot_id": sid, "entity_key": c["entity_key"],
                       "template": c["template"], "metric_id": c["metric_id"],
                       "source_regime": c["source_regime"], "reporting_year": c["reporting_year"],
                       "reporting_period": c["reporting_period"],
                       "period_basis": c["period_basis"], "requirement": c["requirement"],
                       "slot_state": c.get("slot_state", ""),
                       "source_health": c.get("source_health", ""),
                       "disposition": disposition, "note": note})
    for sid, s in by_id.items():
        if sid in merged:
            continue
        row = dict(s)
        row.setdefault("denominator_origin", "shipped_only")
        # Its own requirement class stands; what is unknown is only whether the
        # declared eligibility window reaches it. Labelling it
        # APPLICABILITY_UNKNOWN would overstate the doubt.
        row.setdefault("slot_state", OUTSIDE_WINDOW)
        merged[sid] = row
        bridge.append({"slot_id": sid, "entity_key": s.get("entity_key", ""),
                       "template": s.get("template", ""), "metric_id": s.get("metric_id", ""),
                       "source_regime": s.get("source_regime", ""),
                       "reporting_year": s.get("reporting_year", ""),
                       "reporting_period": s.get("reporting_period", ""),
                       "period_basis": s.get("period_basis", ""),
                       "requirement": s.get("requirement", ""),
                       "slot_state": row["slot_state"], "source_health": "",
                       "disposition": "retained_outside_calendar",
                       "note": ("present in the shipped grid but outside the declared "
                                "eligibility window or form family; retained because a "
                                "denominator is never shrunk to improve a percentage")})
    return list(merged.values()), bridge


# ==================================================================== matching

class Gate:
    """Why an otherwise same-grain candidate was refused.

    The distinction the gates draw is the one contract rule 8 insists on. A
    dekatherm figure offered for a dollar slot is a DEFECT: two dimensional
    families disagree and one of them is wrong. A figure the filer reported with
    an untyped or absent unit is NOT a defect and not missing data either -- the
    value exists and its unit contract is unresolved, which is an explicit gate.
    Collapsing those two into "no data" would be as dishonest as accepting both.
    """

    UNIT_FAMILY = "unit_family_mismatch"              # dimensional conflict: a defect
    UNIT_UNRELATABLE = "unit_needs_unavailable_constant"   # see below
    UNIT_UNDIMENSIONED = "unit_undimensioned"         # value present, unit untyped
    UNIT_UNRECOGNISED = "unit_unrecognised"           # unit token we cannot place
    UNIT_RULE_UNRESOLVED = "slot_unit_rule_unresolved"   # OUR registry contract gap
    SCOPE_CONTRACT = "scope_contract_mismatch"
    SCOPE_UNRESOLVED = "actual_scope_unresolved"
    SUPERSEDED = "superseded_version"
    NONCANONICAL = "non_canonical_filing"
    LINEAGE = "derived_without_lineage"

    #: gates that mean "a value is here and we cannot yet say what it is in"
    UNRESOLVED_CONTRACT = {UNIT_UNDIMENSIONED, UNIT_UNRECOGNISED, UNIT_RULE_UNRESOLVED,
                           UNIT_UNRELATABLE, SCOPE_CONTRACT, SCOPE_UNRESOLVED}
    #: gates that mean a wrong value was offered for this slot
    DEFECT = {UNIT_FAMILY}


#: Dimension pairs that describe the SAME physical thing through a conversion
#: factor we do not hold. A gas volume and a gas energy differ by heat content
#: (Btu per cubic foot), which varies by stream and is not carried on the record;
#: an LNG mass and an energy differ the same way. So a filer who reports IOC
#: header item `g` as `F` (Mcf) against a metric declaring dekatherms has filed a
#: real quantity in a unit we cannot relate to the requested one WITHOUT
#: inventing a constant -- which contract rule 8 forbids.
#:
#: That is categorically different from a dollar figure offered for a dekatherm
#: slot: no constant relates those, so something is simply wrong and the slot is
#: a defect. Keeping the two apart stops a filer's unit declaration being
#: reported as our selector's failure, and stops a conversion being invented to
#: make a percentage move.
_UNRELATABLE_PAIRS = frozenset({
    frozenset({"energy", "volume"}),
    frozenset({"energy", "volume_metric"}),
    frozenset({"energy", "mass_rate"}),
    frozenset({"energy_rate", "volume_rate"}),
    frozenset({"energy_rate", "mass_rate"}),
})


#: selection tiers, best first. The tier decides what the slot IS; `filed`
#: versus `derived` only breaks ties WITHIN a tier, because provenance is not a
#: reason to prefer a blank placeholder to a real value.
_USABLE_WARNINGS = {Validation.SOURCE_ANOMALY_REVIEW, Validation.UNIT_WARNING,
                    Validation.ROUNDING_WARNING, Validation.SOURCE_DATE_WARNING}
_GATED_VALIDATIONS = {Validation.BLOCKED_AMBIGUITY, Validation.SCOPE_INCOMPATIBLE}
_METHOD_ORDER = {"filed": 0, "normalised": 1, "derived": 2,
                 "document_extracted": 3, "manually_curated": 4}


def _tier(o: dict) -> int:
    av, val = o.get("availability"), o.get("validation")
    if av == Availability.PRESENT:
        if val == Validation.PASS:
            return 0
        if val in _USABLE_WARNINGS:
            return 1
        if val == Validation.NOT_YET_VALIDATED:
            return 2
        return 3                                  # present but comparability-gated
    if av in Availability.SOURCE_CONDITION:
        return 4
    if av in Availability.SEMANTIC_GATE:
        return 5
    if av == Availability.UNVERIFIED_AVAILABILITY:
        return 6
    return 7                                      # our own retrieval/parse gap


def _rank(o: dict) -> tuple:
    return (_tier(o), _METHOD_ORDER.get(o.get("method"), 9), o.get("observation_id") or "")


def _scope_contract(o: dict) -> str:
    """Return the registry contract an observation was selected against.

    Newer XBRL observations deliberately separate the actual legal/facility
    scope in ``scope`` from the registry's acceptance instruction in
    ``scope_rule``.  Legacy/document observations predate that split and retain
    their complete contract directly in ``scope``.  Falling back only when the
    explicit rule is empty preserves both representations without replacing the
    source-backed scope consumers need to see.
    """
    return str(o.get("scope_rule") or "").strip() or str(o.get("scope") or "")


def _actual_scope_resolved(o: dict) -> bool:
    actual = str(o.get("scope") or "").strip()
    return bool(actual and "actual regulatory/facility subset unresolved" not in actual.lower())


def _actual_scope_supports_contract(o: dict) -> bool:
    """Require the actual scope to retain its generating evidence envelope.

    ``scope_rule`` says what the selector was allowed to accept.  It cannot make
    an arbitrary train, terminal or storage-module label satisfy an entity slot.
    eCollection observations carry a deterministic, source-context description
    including both the filing CID and XBRL entity identifier.  Document adapters
    retain the registry rule as the leading clause before their docket/facility
    detail.  Checking those two maintained formats rejects scope substitution
    without pretending that the registry instruction itself is actual scope.
    """
    actual = str(o.get("scope") or "").strip()
    rule = str(o.get("scope_rule") or "").strip()
    if not rule:
        return True                 # legacy representation already matched exactly
    if o.get("source_system") == "eCollection_XBRL":
        entity_key = str(o.get("entity_key") or "").strip()
        return bool(
            entity_key
            and actual.startswith("FERC filing entity ")
            and f"({entity_key});" in actual
            and (
                (f"XBRL entity identifier {entity_key}" in actual
                 and "regulatory subset " in actual)
                or ("; regulatory schedule " in actual
                    and "; aggregate of explicitly persisted " in actual)
                or ("; Form 6 filing occurrence " in actual
                    and actual.endswith("; filing-level schedule census"))
            )
        )
    return actual == rule or actual.startswith(rule + " | ")


def _superseded_operative_input(o: dict, lineage) -> bool:
    """Whether a superseded lineage input can make *o* non-current.

    Lineage has two distinct jobs in the store.  An observation with
    ``method=derived`` depends on its operand edges, so a superseded operand
    makes that result inadmissible.  A direct observation, however, can also
    retain a ``basis`` edge solely to preserve an earlier regulatory state.
    That historical edge is not an operand and cannot supersede the direct
    source fact that the observation itself names.

    New callers may supply edge mappings containing ``input_role`` and
    ``input_version_status``.  The production coverage command historically
    supplied only status strings; for that representation we use the same
    semantic boundary: a row that names its own source fact/document and is not
    labelled derived is direct, so its supplemental lineage does not determine
    its currency.  Unknown or derived rows remain conservative and are refused.
    """
    entries = lineage.get(o.get("observation_id"), ())
    direct = (o.get("method") != "derived"
              and bool(o.get("source_fact_id") or o.get("document_id")))

    for edge in entries:
        role = None
        if isinstance(edge, Mapping):
            status = edge.get("input_version_status", edge.get("version_status", ""))
            role = edge.get("input_role")
        elif isinstance(edge, (tuple, list)) and len(edge) == 2:
            # A compact role/status pair is useful to callers that do not want
            # to materialise every lineage column.  Status-only lists continue
            # to work because each member is a string, not a two-item sequence.
            role, status = edge
        else:
            status = edge

        if status != "superseded":
            continue
        if direct and (role is None or role == "basis"):
            # Direct current fact + historical basis.  The standing Oil
            # Pipeline Index factor is the occupied example: its basis edge
            # preserves a vacated factor without making the reinstated factor
            # itself superseded.
            continue
        return True
    return False


def admissible(slot: dict, o: dict, *, canonical=None, lineage=None) -> tuple[bool, str]:
    """Whether an observation may satisfy this slot at all. (ok, gate_reason).

    Grain has already been matched by the index key. What is checked here is
    everything the shipped matcher left out: that the value is in a unit the
    slot accepts, that it comes from the CURRENT filing occurrence, and that a
    derived value can actually show its working.

    A non-present observation is not unit-gated -- a record that the filer left
    the cell blank has no unit and is not claiming one.
    """
    present = o.get("availability") == Availability.PRESENT

    if _scope_contract(o) != str(slot.get("scope") or ""):
        return False, Gate.SCOPE_CONTRACT
    # A registry rule proves what the selector intended to accept; it does not
    # itself prove what the filed occurrence covered.  Present XBRL values must
    # therefore retain a resolved actual scope as well.  Non-present source
    # records may keep the explicit unresolved marker because they carry no
    # value into a comparison.
    if present and str(o.get("scope_rule") or "").strip() \
            and not _actual_scope_resolved(o):
        return False, Gate.SCOPE_UNRESOLVED
    if present and not _actual_scope_supports_contract(o):
        return False, Gate.SCOPE_CONTRACT

    if o.get("version_status") == "superseded":
        return False, Gate.SUPERSEDED
    fid, sys_ = o.get("filing_id"), o.get("source_system")
    if canonical is not None and fid and (sys_, str(fid)) in canonical \
            and not canonical[(sys_, str(fid))]:
        return False, Gate.NONCANONICAL
    if lineage is not None and _superseded_operative_input(o, lineage):
        return False, Gate.SUPERSEDED

    if not present:
        return True, ""
    families, open_rule = _slot_unit_contract(slot)
    if open_rule:
        return True, ""
    if not families:
        return False, Gate.UNIT_RULE_UNRESOLVED
    fam = _unit_family(o.get("unit"))
    if not fam:
        # An untyped or unknown unit is a reason to refuse, never a wildcard --
        # but it is refused as an unresolved contract, not as a wrong value.
        return False, (Gate.UNIT_UNDIMENSIONED if _unit_known(o.get("unit"))
                       else Gate.UNIT_UNRECOGNISED)
    if fam not in families:
        if any(frozenset({fam, want}) in _UNRELATABLE_PAIRS for want in families):
            # A real quantity in a unit that only an unavailable physical
            # constant could relate to the requested one. Refused, and refused as
            # an unresolved contract rather than as somebody's defect.
            return False, Gate.UNIT_UNRELATABLE
        return False, Gate.UNIT_FAMILY
    return True, ""


def measure(expected: list[dict], observations: list[dict], run_id: str,
            *, canonical: dict | None = None, lineage: dict | None = None,
            record_rejects: bool = True) -> list[dict]:
    """Match each frozen slot against what the run actually produced.

    Matching is on the full grain: entity, metric, regime, basis and exact
    interval or instant; scope compatibility is then checked against either the
    observation's explicit ``scope_rule`` or, for legacy rows, its actual
    ``scope``. Unit family, version currency and lineage are checked alongside
    it. Method is deliberately NOT part of the match key, so a validly
    derived quarter can satisfy a requested quarter -- but it is recorded on the
    result, so a derived quarter is never confused with a filed one.

    `canonical` maps (source_system, filing_id) -> bool. `lineage` maps an
    observation id either to input-version status strings (the legacy command
    representation) or to role-aware edge mappings/pairs. Both are optional:
    when absent those gates are reported as unchecked rather than as passed.
    """
    index: dict[tuple, list[dict]] = defaultdict(list)
    for o in observations:
        key = (o["entity_key"], o["metric_id"], o["source_regime"], o["period_basis"],
               o.get("period_start") or "", o.get("period_end") or "",
               o.get("instant_date") or "")
        index[key].append(o)

    # Genuine equivalences: a Q1 year-to-date interval IS the Q1 quarter. Nothing
    # else may be normalised, and in particular no other YTD period.
    for o in observations:
        if o["period_basis"] != periods.YTD:
            continue
        yr = o.get("reporting_year")
        if yr and periods.is_q1_equivalent(int(yr), periods.YTD,
                                           o.get("period_start") or "", o.get("period_end") or ""):
            key = (o["entity_key"], o["metric_id"], o["source_regime"], periods.QUARTER,
                   o.get("period_start") or "", o.get("period_end") or "",
                   "")
            index[key].append(dict(o, _equivalence="quarter (also YTD): Q1 interval coincides"))

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out = []
    for slot in expected:
        key = (slot["entity_key"], slot["metric_id"], slot["source_regime"],
               slot["period_basis"], slot.get("period_start") or "",
               slot.get("period_end") or "", slot.get("instant_date") or "")
        # Snapshot and document slots match on the full grain like everything
        # else. They used to have a special case that ignored the date; that let
        # ANY snapshot of a metric satisfy EVERY snapshot slot for it -- a 2024
        # slot answered by a 2026 value, and slots that should read NOT
        # APPLICABLE reported as populated off another date's number.
        candidates = index.get(key, [])
        matches, refused = [], []
        for o in candidates:
            ok, gate = admissible(slot, o, canonical=canonical, lineage=lineage)
            (matches if ok else refused).append((o, gate))

        refusal = "; ".join(sorted({g for _o, g in refused})) if record_rejects else ""
        if not matches:
            outcome = (CoverageOutcome.NOT_REQUIRED if slot["requirement"] == NOT_REQUIRED
                       else CoverageOutcome.APPLICABILITY_UNKNOWN
                       if slot["requirement"] == UNKNOWN
                       else CoverageOutcome.NOT_IMPLEMENTED)
            gates = {g for _o, g in refused}
            if gates & Gate.UNRESOLVED_CONTRACT:
                # A value IS here at this grain; what it is measured in is not
                # established. That is a gate on our own unit contract, not a
                # FERC data gap and not unfinished retrieval.
                outcome = CoverageOutcome.INTERPRETATION_BLOCKED
            elif gates & Gate.DEFECT:
                # A value exists at this grain in a unit whose dimension is not
                # the slot's. Something is wrong and it is not the filer's
                # absence, so the slot is a defect rather than an explained gap.
                # WHOSE defect is deliberately not asserted: our selector may
                # have produced the wrong unit, or the filer may have declared
                # one -- Form 6 has barrel-mile values filed under utr:bbl, which
                # is a filer error, and blaming our selector for it would be as
                # wrong as accepting the value.
                outcome = CoverageOutcome.SELECTOR_FAILED
            elif slot.get("slot_state") == FUTURE:
                outcome = CoverageOutcome.NOT_YET_DUE
            elif slot.get("source_health") in SourceHealth.DEFECT:
                # A corrupt cache object is reported as a retrieval failure --
                # the least wrong term in the controlled vocabulary, since the
                # artefact could not be obtained -- but `source_health` keeps
                # saying `cache_corrupt`, so it is never read as a parser defect
                # and never as an absence of FERC data.
                outcome = (CoverageOutcome.PARSE_FAILED
                           if slot["source_health"] == SourceHealth.PARSE_FAILED
                           else CoverageOutcome.RETRIEVAL_FAILED)
            reason = ("no observation was produced for this requested grain"
                      if outcome == CoverageOutcome.NOT_IMPLEMENTED
                      else slot.get("source_health_detail")
                      or slot.get("requirement_evidence") or "")
            if refused:
                reason = (f"{len(refused)} same-grain candidate(s) refused: {refusal}; "
                          f"{reason}").strip("; ")
            out.append({
                "slot_id": slot["slot_id"], "run_id": run_id, "observation_id": None,
                "outcome": outcome, "populated": 0, "source_matched": 0, "validated": 0,
                "in_review": 0, "reason": reason,
                "candidates_refused": len(refused), "refusal_gates": refusal,
                "evidence": slot.get("requirement_evidence", ""), "measured_at": stamp})
            continue

        best = sorted((o for o, _g in matches), key=_rank)[0]
        outcome = coverage_outcome(best["availability"], best["validation"])
        populated = int(best["availability"] == Availability.PRESENT)
        traced = bool(best.get("source_fact_id") or best.get("document_id"))
        lineage_checked = True
        if not traced and best.get("method") == "derived":
            # A derived value is source-matched only if it can show its working.
            # The shipped code counted `method == "derived"` as proof by itself.
            #
            # This branch used to read `bool(lineage is None or lineage.get(...))`,
            # so a caller that supplied no lineage map got every unlineaged derived
            # value counted as source-matched -- "we did not check, therefore it
            # passes". The docstring already claimed the opposite. 703 derived
            # observations in the delivered baseline have neither a source fact id
            # nor a single lineage edge, and `run.py` does not yet pass the map, so
            # the default was permissive AND occupied. Not checking is now its own
            # answer: not traced, and flagged as unchecked rather than as proven.
            if lineage is None:
                traced, lineage_checked = False, False
            else:
                traced = bool(lineage.get(best.get("observation_id")))
        source_matched = int(populated and traced)
        validated = int(populated and best["validation"] == Validation.PASS)
        in_review = int(best.get("validation") in Validation.MUST_PROPAGATE)
        reason = best.get("missing_reason") or best.get("qa_flags") or ""
        if best.get("_equivalence"):
            reason = f"{best['_equivalence']}; {reason}".strip("; ")
        out.append({
            "slot_id": slot["slot_id"], "run_id": run_id,
            "observation_id": best["observation_id"], "outcome": outcome,
            "populated": populated, "source_matched": source_matched,
            "validated": validated, "in_review": in_review,
            "reason": reason,
            "candidates_refused": len(refused), "refusal_gates": refusal,
            "evidence": f"method={best.get('method')}; origin={best.get('origin')}; "
                        f"filing={best.get('filing_id')}; unit={best.get('unit')}"
                        + ("" if lineage_checked
                           else "; lineage NOT CHECKED (no map supplied): this derived "
                                "value is not counted as source-matched"),
            "measured_at": stamp})
    return out


# ==================================================================== reporting

def summarise(expected: list[dict], measured: list[dict]) -> dict:
    """Headline counts. Every number here is a count of frozen slots."""
    by_slot = {m["slot_id"]: m for m in measured}
    core = [s for s in expected if s["requirement"] in CORE]
    stats = {
        "expected_total": len(expected),
        "expected_core": len(core),
        "expected_not_required": sum(1 for s in expected if s["requirement"] == NOT_REQUIRED),
        "expected_optional": sum(1 for s in expected if s["requirement"] == OPTIONAL),
        "expected_unknown": sum(1 for s in expected if s["requirement"] == UNKNOWN),
        "slot_states": dict(Counter(s.get("slot_state", "unstated") for s in expected)),
        "source_health": dict(Counter(s.get("source_health", "unstated") for s in expected)),
        "denominator_origin": dict(Counter(s.get("denominator_origin", "unstated")
                                           for s in expected)),
    }
    for label, pool in (("core", core), ("all", expected)):
        rows = [by_slot.get(s["slot_id"], {}) for s in pool]
        stats[f"{label}_populated"] = sum(r.get("populated", 0) for r in rows)
        stats[f"{label}_source_matched"] = sum(r.get("source_matched", 0) for r in rows)
        stats[f"{label}_validated"] = sum(r.get("validated", 0) for r in rows)
        stats[f"{label}_in_review"] = sum(r.get("in_review", 0) for r in rows)
        stats[f"{label}_refused_candidates"] = sum(r.get("candidates_refused", 0) for r in rows)
        stats[f"{label}_outcomes"] = dict(Counter(r.get("outcome", "missing") for r in rows))
    # A slot that is not yet due is an obligation that has not arisen. It is
    # reported separately and never counted as either a hit or a miss.
    due_core = [s for s in core if s.get("slot_state") != FUTURE]
    stats["expected_core_due"] = len(due_core)
    for name in ("populated", "validated"):
        stats[f"core_due_{name}"] = sum(
            by_slot.get(s["slot_id"], {}).get(name, 0) for s in due_core)
    if stats["expected_core"]:
        stats["core_populated_pct"] = round(
            100.0 * stats["core_populated"] / stats["expected_core"], 4)
        stats["core_validated_pct"] = round(
            100.0 * stats["core_validated"] / stats["expected_core"], 4)
    if stats["expected_core_due"]:
        stats["core_due_populated_pct"] = round(
            100.0 * stats["core_due_populated"] / stats["expected_core_due"], 4)
        stats["core_due_validated_pct"] = round(
            100.0 * stats["core_due_validated"] / stats["expected_core_due"], 4)
    return stats


def group_summary(expected: list[dict], measured: list[dict], key: str,
                  keyer=None) -> list[dict]:
    """Per-template / per-adapter / per-metric coverage, on the same slot basis."""
    by_slot = {m["slot_id"]: m for m in measured}
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in expected:
        groups[str((keyer(s) if keyer else s.get(key, "")) or "")].append(s)
    out = []
    for g, pool in sorted(groups.items()):
        core = [s for s in pool if s["requirement"] in CORE]
        due = [s for s in core if s.get("slot_state") != FUTURE]
        rows = [by_slot.get(s["slot_id"], {}) for s in core]
        pop = sum(r.get("populated", 0) for r in rows)
        val = sum(r.get("validated", 0) for r in rows)
        rev = sum(r.get("in_review", 0) for r in rows)
        out.append({
            "group": g, "all_slots": len(pool), "core_slots": len(core),
            "core_due_slots": len(due), "populated": pop, "validated": val,
            "in_review": rev,
            "populated_pct": round(100.0 * pop / len(core), 4) if core else 0.0,
            "validated_pct": round(100.0 * val / len(core), 4) if core else 0.0,
        })
    return out


# ---------------------------------------------------------------- persistence
#
# `staging._upsert` builds its column list from `rows[0]`, so any key a row
# carries that the table lacks becomes `no such column` at insert time. The
# calendar's obligation columns are exactly such keys. Rather than drop them at
# the source -- which would throw away the due date, the lifecycle state and the
# source health that make a fallen percentage explainable -- they are projected
# out on the way to a table that has not got them yet, and the projection
# reports what it dropped so the loss is visible rather than silent.

#: columns `coverage_expected` has in the repaired schema.  Keep the base
#: identity columns and the calendar/evidence columns together so a caller
#: cannot accidentally persist a denominator after projecting away the very
#: due-state and source-health evidence that made it independent of ingestion.
EXPECTED_COLUMNS = (
    "slot_id", "entity_key", "asset_id", "template", "metric_id", "source_regime",
    "period_basis", "period_start", "period_end", "instant_date", "reporting_year",
    "reporting_period", "scope", "unit_rule", "requirement", "requirement_evidence",
    "applicability_version", "frozen_at", "frozen_run_id",
    "due_date", "slot_state", "obligation_form", "obligation_authority",
    "obligation_evidence_kind", "source_health", "source_health_detail",
    "denominator_origin", "docket", "facility", "shipped_requirement")

#: columns `coverage_measured` has in the delivered schema
MEASURED_COLUMNS = (
    "slot_id", "run_id", "observation_id", "outcome", "populated", "source_matched",
    "validated", "in_review", "reason", "evidence", "measured_at",
    "candidates_refused", "refusal_gates")

#: what the calendar adds. Requested as real columns; see
#: requests/w4-coverage_shared_requests.md R10.
CALENDAR_COLUMNS = (
    "due_date", "slot_state", "obligation_form", "obligation_authority",
    "obligation_evidence_kind", "source_health", "source_health_detail",
    "denominator_origin", "docket", "facility", "shipped_requirement")
GATE_COLUMNS = ("candidates_refused", "refusal_gates")


def project(rows: list[dict], columns) -> tuple[list[dict], list[str]]:
    """(rows restricted to `columns`, the keys that had to be dropped).

    Always check the second return value. A silently narrowed row is how
    provenance goes missing.
    """
    keep = tuple(columns)
    dropped = sorted({k for r in rows for k in r
                      if k not in keep and not k.startswith("_")})
    return [{c: r.get(c) for c in keep if c in r} for r in rows], dropped


def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols and not k.startswith("_"):
                cols.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
