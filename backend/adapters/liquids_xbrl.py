"""
Liquids regime configuration: Forms 6 and 6-Q, including Page 700.

Implemented from the Form 6 / 6-Q taxonomy and live filings in their own right,
not as gas with different labels. What differs from gas is configuration, and
what differs SEMANTICALLY is enforced by the registry:

  * Page 700 is an ANNUAL, INTERSTATE, carrier-reported panel. Its entry point
    does not import sched-700 for Form 6-Q, so there is no quarterly Page 700 at
    all -- the expected grid must never ask for one.
  * Page 700 figures may never be mixed with whole-entity Form 6 figures in a
    ratio. Their `scope` strings differ, and the ratio builder refuses a scope
    mismatch outright.
  * The Page 700 return component is an ALLOWANCE inside a cost-of-service
    calculation. It is not an achieved return on capital, and revenue minus cost
    of service is a regulatory screen, not realised profit.
  * All barrel-mile concepts are filed with unitRef -> xbrli:pure rather than a
    barrel-mile unit. That is a source fact, recorded as filed; the registry's
    unit rule carries the real meaning.
  * FERC permits reduced schedules for smaller carriers, so the presence of
    Page 700 does NOT prove a full Form 6 was filed. Completeness is decided by
    a schedule census against the form's own taxonomy.

Form 6 index rows carry period="Q4", so annual versus quarterly comes from
`formName`, never from the period label.
"""

from __future__ import annotations

import hashlib
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import ecollection, xbrl_adapter                       # noqa: E402
from ferclib.status import (Availability, Method, Origin, Validation,  # noqa: E402
                            VersionStatus)

ADAPTER = "liquids_xbrl"

CFG = xbrl_adapter.XbrlConfig(
    adapter=ADAPTER,
    forms=ecollection.LIQUID_FORMS,          # {"Form 6", "Form 6Q"}
    annual_forms={"Form 6"},
    quarterly_forms={"Form 6Q"},
    # Form 6 carries no narrative physical-profile textblock equivalent to the
    # Form 2-A p.211.1 disclosure, so no narrative fallback is configured. That
    # is a deliberate absence, not an oversight: inventing patterns for a
    # textblock this form does not collect would manufacture false profile rows.
    textblock_concept="",
    textblock_patterns=[],
    # Page 700 is filed inside the annual Form 6 instance but is its own
    # interstate, carrier-reported panel. Naming it separately keeps its scope
    # distinct so it can never be mixed with whole-entity Form 6 figures.
    sub_regimes={"Form 6": ("Form 6 Page 700",)},
)


# 18 CFR 357.2's reduced filing is page 1 + page 700, or pages 1 + 301 +
# 700.  These five schedules are deliberately chosen because they are outside
# both reduced sets and carry filing-specific facts in a full Form 6.  They are
# the independently documented sentinels in discovery/liquids_discovery.md; no
# carrier name, filing id, source value or candidate output is encoded here.
_FULL_ONLY_SCHEDULES = frozenset({"110", "114", "120", "212", "600"})
_REDUCED_SIGNALS = frozenset({"ScheduleExemption", "ScheduleWaiver"})
_METRIC_ID = "liq_form6_filing_completeness"


def _page(value) -> str:
    """Normalise a taxonomy schedule token without inventing page aliases."""
    text = str(value or "").strip().lower()
    for prefix in ("schedule-", "schedule ", "sched-", "page ", "page-"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.lstrip("0") or ("0" if text else "")


def _reported(fact: dict) -> bool:
    """A filed zero is populated; nil and empty text are not."""
    return not bool(fact.get("is_nil")) and str(fact.get("value_as_filed") or "").strip() != ""


def _taxonomy_source_digest(taxonomy) -> str:
    """Stable identity of the taxonomy artefacts used for the census."""
    rows = [{
        "artefact": str(r.get("artefact") or ""),
        "url": str(r.get("url") or ""),
        "content_hash": str(r.get("content_hash") or ""),
        "retrieved": int(bool(r.get("retrieved"))),
    } for r in getattr(taxonomy, "sources", ())]
    raw = json.dumps(sorted(rows, key=lambda r: tuple(r.values())),
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _schedule_census(facts: list[dict], taxonomy) -> dict:
    """Classify one Form 6 occurrence from its facts and own taxonomy.

    A positive full-only schedule is enough to prove a full filing.  Absence is
    stronger evidence and is used to call a reduced filing only when the
    taxonomy census is complete and contains every sentinel schedule.  Page
    700 by itself therefore remains unresolved instead of being promoted to a
    full Form 6.
    """
    concepts = getattr(taxonomy, "concepts", {}) or {}
    taxonomy_pages = {_page(p) for p, _folder in (getattr(taxonomy, "schedules", ()) or ())}
    by_page: dict[str, list[dict]] = {}
    reported = [dict(f) for f in facts if _reported(f)]
    for fact in reported:
        mapped = concepts.get(fact.get("concept_local") or "")
        if not mapped:
            continue
        page = _page(mapped[0])
        if page:
            by_page.setdefault(page, []).append(fact)

    def ordered(rows):
        return sorted(rows, key=lambda r: (int(r.get("document_order") or 0),
                                           str(r.get("source_fact_id") or "")))

    representatives = {p: ordered(rows)[0] for p, rows in sorted(by_page.items())}
    counts = {p: len(rows) for p, rows in sorted(by_page.items())}
    full_pages = sorted(_FULL_ONLY_SCHEDULES.intersection(by_page))
    form_type_values = sorted({str(f.get("value_as_filed") or "").strip()
                               for f in reported if f.get("concept_local") == "FormType"})
    form6_marker = any(v.upper().replace(" ", "") in {"6", "FORM6", "FERCFORM6"}
                       for v in form_type_values)
    incompatible_form_type = bool(form_type_values) and not form6_marker
    reduced_signals = sorted({f["concept_local"] for f in reported
                              if f.get("concept_local") in _REDUCED_SIGNALS})
    rule_taxonomy_complete = (bool(getattr(taxonomy, "complete", False))
                              and _FULL_ONLY_SCHEDULES.issubset(taxonomy_pages))
    page1 = "1" in by_page
    page700 = "700" in by_page

    status = "unresolved"
    validation = Validation.BLOCKED_AMBIGUITY
    if incompatible_form_type:
        reason = ("the filing occurrence is labelled Form 6 but its reported FormType "
                  f"value(s) are {form_type_values!r}; schedule classification is refused")
    elif full_pages and reduced_signals:
        reason = ("contradictory filing evidence: reduced-filing signal(s) "
                  f"{reduced_signals} coexist with full-only populated schedule(s) "
                  f"{full_pages}")
    elif full_pages:
        status, validation = "full", Validation.PASS
        reason = ("full Form 6 proven by populated full-only schedule(s) "
                  f"{full_pages}; Page 700 was not used as proof of full status")
    elif reduced_signals:
        status, validation = "reduced_schedule", Validation.PASS
        reason = ("reduced-schedule filing confirmed by reported taxonomy signal(s) "
                  f"{reduced_signals}; no full-only sentinel schedule is populated")
    elif rule_taxonomy_complete and page1 and form6_marker and page700:
        status, validation = "reduced_schedule", Validation.PASS
        reason = ("reduced-schedule filing: the complete Form 6 taxonomy census finds "
                  "reported page 1/FormType and page 700 evidence but zero facts on "
                  f"full-only schedules {sorted(_FULL_ONLY_SCHEDULES)}")
    elif page700 and not (page1 and form6_marker):
        reason = ("Page 700 alone is insufficient to distinguish a full Form 6 from a "
                  "reduced-schedule filing; no taxonomy-mapped, compatible page 1/FormType "
                  "evidence was found")
    elif not rule_taxonomy_complete:
        reason = ("the filing cannot be classified by absence because the Form 6 taxonomy "
                  "schedule census is incomplete or omits one or more full-only sentinels")
    else:
        reason = ("neither a populated full-only schedule nor the page 1 + page 700 reduced "
                  "filing pattern was established")

    return {
        "status": status,
        "validation": validation,
        "reason": reason,
        "reported_fact_count": len(reported),
        "mapped_fact_count": sum(counts.values()),
        "taxonomy_schedule_count": len(taxonomy_pages),
        "taxonomy_complete_for_rule": rule_taxonomy_complete,
        "populated_schedule_counts": counts,
        "populated_pages": sorted(by_page),
        "full_only_pages": full_pages,
        "form_type_values": form_type_values,
        "reduced_signals": reduced_signals,
        "representatives": representatives,
    }


def _filing_metadata(ctx, filing: dict) -> dict:
    """Fill replay-safe metadata from the stored occurrence when needed."""
    meta = dict(filing)
    rows = ctx.staging.query(
        "SELECT taxonomy_version,schema_ref,version_status,data_origin FROM filings "
        "WHERE source_system=? AND filing_id=?",
        (ecollection.SOURCE_SYSTEM, filing["filing_id"]))
    if rows:
        stored = dict(rows[0])
        for key, value in stored.items():
            if meta.get(key) in (None, ""):
                meta[key] = value
    parsed = filing.get("_parsed") or {}
    tax = ecollection.taxonomy_version(parsed.get("taxonomy_ns") or "")
    meta["taxonomy_version"] = tax or meta.get("taxonomy_version") or ""
    meta["schema_ref"] = parsed.get("schema_ref") or meta.get("schema_ref") or ""
    return meta


def _completeness_observation(ctx, entity: dict, filing: dict, slot: dict,
                              metric) -> tuple[dict, list[dict]]:
    """Build the categorical observation and occurrence-specific fact edges."""
    meta = _filing_metadata(ctx, filing)
    facts = [dict(r) for r in ctx.staging.query(
        "SELECT * FROM source_facts WHERE source_system=? AND filing_id=?",
        (ecollection.SOURCE_SYSTEM, filing["filing_id"]))]
    taxonomy = ctx.applicability.build(
        "Form 6", meta["taxonomy_version"], schema_ref=meta["schema_ref"])
    census = _schedule_census(facts, taxonomy)
    origin = (Origin.FERC_MIGRATED if meta.get("data_origin") == "ferc_migrated"
              else Origin.NATIVE_XBRL)
    version = meta.get("version_status") or VersionStatus.ORIGINAL
    legal = entity.get("legal_name") or entity["entity_key"]
    scope = (f"FERC filing entity {legal} ({entity['entity_key']}); Form 6 filing "
             f"occurrence {filing['filing_id']}; filing-level schedule census")
    source_digest = _taxonomy_source_digest(taxonomy)
    detail = {
        key: census[key] for key in (
            "reported_fact_count", "mapped_fact_count", "taxonomy_schedule_count",
            "taxonomy_complete_for_rule", "populated_schedule_counts",
            "full_only_pages", "form_type_values", "reduced_signals")
    }
    notes = (f"{census['reason']}; taxonomy_entry_point="
             f"{getattr(taxonomy, 'entry_point_url', '')}; "
             f"taxonomy_source_set_sha256={source_digest}; "
             f"schedule_census={json.dumps(detail, sort_keys=True, separators=(',', ':'))}")
    present = census["status"] != "unresolved"
    obs = xbrl_adapter._obs(
        entity["entity_key"], metric, slot["source_regime"], slot["period_basis"],
        slot.get("period_start") or "", slot.get("period_end") or "",
        slot.get("instant_date") or "", slot["reporting_year"],
        slot["reporting_period"],
        value_text=census["status"] if present else None, value_num=None,
        **{"unit": "categorical" if present else None},
        availability=(Availability.PRESENT if present
                      else Availability.INTERPRETATION_BLOCKED),
        origin=origin, method=Method.DERIVED, version_status=version,
        validation=census["validation"],
        qa_flags="" if present else census["reason"],
        missing_reason="" if present else census["reason"],
        filing_id=filing["filing_id"], selector="schedule_census",
        derivation="taxonomy_schedule_census(source_facts)",
        tax=meta["taxonomy_version"],
        applicability_evidence=slot.get("requirement_evidence") or "",
        candidate_count=census["reported_fact_count"], notes=notes, scope=scope)

    missing_fact_ids = sum(1 for f in facts if not f.get("source_fact_id"))
    if missing_fact_ids:
        raise ValueError(
            f"Form 6 filing {filing['filing_id']} has {missing_fact_ids} source fact(s) "
            "without occurrence-specific identity; schedule-census lineage refused")
    candidate_keys = sorted(
        f"{filing['filing_id']}:{f['source_fact_id']}" for f in facts)
    reported_keys = sorted(
        f"{filing['filing_id']}:{f.get('source_fact_id')}"
        for f in facts if _reported(f))
    member_digest = hashlib.sha256("\n".join(reported_keys).encode("utf-8")).hexdigest()
    population_id = "pop-" + hashlib.sha256(
        f"{obs['observation_id']}|Form6-schedule-census|{member_digest}".encode("utf-8")
    ).hexdigest()[:32]
    population = {
        "population_id": population_id,
        "observation_id": obs["observation_id"],
        "source_system": ecollection.SOURCE_SYSTEM,
        "source_table": "source_facts",
        "filing_ids": json.dumps([filing["filing_id"]]),
        "inclusion_rule": ("all source_facts from this exact Form 6 filing occurrence whose "
                           "XBRL value is non-nil and non-empty; each concept is mapped to "
                           "its schedule by this occurrence's taxonomy presentation census"),
        "exclusion_rule": ("xsi:nil facts and empty filed values are excluded from populated-"
                           "schedule counts; unmapped facts remain candidates but do not prove "
                           "a schedule page; numeric zero is included"),
        "row_count": len(reported_keys),
        "candidate_count": len(candidate_keys),
        "excluded_count": len(candidate_keys) - len(reported_keys),
        "member_key": "filing_id:source_fact_id",
        "member_digest": member_digest,
        "members_sample": json.dumps(reported_keys[:25]),
        "aggregate_value": census["status"],
        "aggregate_unit": "categorical",
        "empty_reason": (None if reported_keys else
                         "the filing occurrence contains no non-nil, non-empty source fact; "
                         "no filing classification is published"),
        "note": ("Form 6 filing completeness schedule-census population; "
                 f"taxonomy_source_set_sha256={source_digest}"),
        "created_at": ecollection.utcnow(),
    }
    obs["_populations"] = [population]
    edges = [{
        "observation_id": obs["observation_id"], "input_order": 0,
        "input_role": "population", "operator_sign": "", "coefficient": 1.0,
        "input_source_system": ecollection.SOURCE_SYSTEM,
        "input_filing_id": filing["filing_id"],
        "input_source_fact_id": None, "input_observation_id": None,
        "input_context_id": None, "input_concept": "source_facts",
        "input_period": f"FY{slot['reporting_year']}",
        "input_value": census["status"], "input_unit": "categorical",
        "input_version_status": version, "input_population_id": population_id,
    }]
    for i, (page, fact) in enumerate(sorted(census["representatives"].items()), 1):
        edges.append({
            "observation_id": obs["observation_id"], "input_order": i,
            "input_role": "schedule_evidence", "operator_sign": "",
            "coefficient": None,
            "input_source_system": ecollection.SOURCE_SYSTEM,
            "input_filing_id": filing["filing_id"],
            "input_source_fact_id": fact.get("source_fact_id"),
            "input_observation_id": None,
            "input_context_id": fact.get("context_id"),
            "input_concept": fact.get("concept_local"),
            "input_period": (fact.get("instant") or
                             f"{fact.get('period_start') or ''}..{fact.get('period_end') or ''}"),
            "input_value": fact.get("value_as_filed"),
            "input_unit": fact.get("unit_text"),
            "input_version_status": version,
            "input_population_id": None,
        })
    return obs, edges


def retrieve(ctx, entity, *, year_from, year_to):
    return xbrl_adapter.retrieve(CFG, ctx, entity, year_from=year_from, year_to=year_to)


def freeze_expected(ctx, entity, filings, assets):
    return xbrl_adapter.freeze_expected(CFG, ctx, entity, filings, assets)


def canonicalise(ctx, entity, filings, expected):
    observations, edges = xbrl_adapter.canonicalise(CFG, ctx, entity, filings, expected)

    # `schedule_census` is a filing-level classification, not a selector for a
    # single FormType fact.  Remove the generic placeholder and replace it with
    # one result per expected annual occurrence, retaining every unrelated
    # observation and edge from the shared XBRL engine.
    generic_completeness = [o for o in observations if o.get("metric_id") == _METRIC_ID]
    removed = {o["observation_id"] for o in generic_completeness}
    observations = [o for o in observations if o.get("metric_id") != _METRIC_ID]
    edges = [e for e in edges if e.get("observation_id") not in removed]

    metric = next((m for m in CFG.metrics() if m.id == _METRIC_ID), None)
    if metric is None:
        return observations, edges
    canonical = {(f["form"], int(f["reporting_year"]), f["reporting_period"]): f
                 for f in filings if f.get("is_canonical") and f.get("form") == "Form 6"}
    for slot in expected:
        if slot.get("metric_id") != _METRIC_ID:
            continue
        key = ("Form 6", int(slot["reporting_year"]), slot["reporting_period"])
        filing = canonical.get(key) or canonical.get(("Form 6", key[1], "Q4"))
        if filing is None:
            # The shared engine already produced the exact expected-not-located
            # status.  Retain it: a schedule census cannot exist without a
            # filing occurrence and manufacturing one here would change a
            # retrieval gap into a semantic answer.
            prior = next((o for o in generic_completeness
                          if (o.get("reporting_year"), o.get("reporting_period")) ==
                          (slot["reporting_year"], slot["reporting_period"])), None)
            if prior is not None:
                observations.append(prior)
            continue
        obs, census_edges = _completeness_observation(ctx, entity, filing, slot, metric)
        observations.append(obs)
        edges.extend(census_edges)
    return observations, edges
