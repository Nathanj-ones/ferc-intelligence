"""
The shared XBRL adapter engine.

Gas (Forms 2 / 2-A / 3-Q) and liquids (Forms 6 / 6-Q, including Page 700) are the
same machine with different configuration: the same verified instance parser, the
same taxonomy-driven applicability, the same period-grain rules, the same
selectors, the same derivations and the same five status dimensions.

`adapters/gas_xbrl.py` and `adapters/liquids_xbrl.py` are therefore thin config
objects, not two script trees. Everything that could drift between regimes lives
here exactly once.

Corrections from the 7 September independent check, all implemented here:

  A. period grain -- expected slots and matching key on basis + exact interval,
     so a year-to-date fact can never satisfy a requested quarter, and
     sequential-YTD derivation is available to every regime, not just liquids;
  B. warnings reach consumers -- every observation carries availability, origin,
     method, version and validation, and derived values inherit their inputs'
     must-propagate flags;
  C. narrative fallback -- textblock disclosures become span-backed profile
     observations with their own scope, never dressed up as schedule totals;
  D. applicability per form AND taxonomy version, UNKNOWN where the taxonomy
     could not be completely resolved.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import coverage, ecollection, periods, registry, selectors                # noqa: E402
from ferclib.applicability import IN_FORM_NO, IN_FORM_UNKNOWN, IN_FORM_YES   # noqa: E402
from ferclib.http import FetchError                                          # noqa: E402
from ferclib.registry import BY_ADAPTER, BY_ID, REGISTRY_VERSION             # noqa: E402
from ferclib.staging import observation_id                                   # noqa: E402
from ferclib.status import Availability, Method, Origin, Validation, VersionStatus  # noqa: E402
from ferclib.xbrl import classify_period, parse_instance                     # noqa: E402

SOURCE_SYSTEM = ecollection.SOURCE_SYSTEM

#: |numerator / denominator| above which a ratio stops being an economic
#: quantity and becomes an artefact of a negligible basis. 100 means "the
#: numerator is more than 10,000% of the denominator" -- a margin beyond that is
#: not a margin. Chosen to sit far outside every legitimate value in the
#: delivered data (the widest genuine margin is -307%) while catching the
#: Fayetteville Express case, whose $3,499 revenue basis produced -555,785%.
#: The value is still published; the threshold only decides whether it carries a
#: review flag instead of a clean pass.
_RATIO_SANITY = 100


def _dec(x):
    """Exact decimal from a stored value, preferring the as-filed text.

    `value_num` is a float and has already lost precision; `value_text` is what
    the filer wrote. Derivations read the text so the arithmetic matches the
    source rather than its binary approximation.
    """
    from decimal import Decimal
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x).replace(",", "").strip())


class XbrlConfig:
    """One regime's configuration. Everything regime-specific lives in here."""

    def __init__(self, adapter: str, forms: set[str], annual_forms: set[str],
                 quarterly_forms: set[str], *, textblock_concept: str = "",
                 textblock_regime: str = "XBRL textblock",
                 textblock_patterns: list | None = None,
                 textblock_page: str = "",
                 sub_regimes: dict[str, tuple[str, ...]] | None = None):
        self.adapter = adapter
        self.forms = forms
        self.annual_forms = annual_forms
        self.quarterly_forms = quarterly_forms
        self.textblock_concept = textblock_concept
        self.textblock_regime = textblock_regime
        self.textblock_patterns = textblock_patterns or []
        self.textblock_page = textblock_page
        #: Sub-regimes carried INSIDE a form's own instance. Page 700 is filed
        #: within the annual Form 6 document but is a separate reporting panel
        #: with its own interstate scope, so it is named separately in the
        #: registry and resolved against the host form's filing here.
        self.sub_regimes = sub_regimes or {}

    def host_form(self, regime: str) -> str:
        """The form whose filing actually carries a (possibly sub-) regime."""
        for form, subs in self.sub_regimes.items():
            if regime in subs:
                return form
        return regime

    def metrics(self):
        return BY_ADAPTER.get(self.adapter, [])


# ---------------------------------------------------------------- retrieval

def retrieve(cfg, ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """Fetch and parse every eligible filing for one entity.

    One filing is one atomic unit: parse failure or download failure leaves that
    filing absent rather than half-written, and the other filings continue.
    """
    inv = ctx.index.inventory(entity["entity_key"], entity["legal_name"], cfg.forms,
                              year_from=year_from, year_to=year_to)
    if not inv:
        ctx.log("info", f"{entity['entity_key']}: no Form 2/2-A/3-Q filings in the index",
                adapter=cfg.adapter)
        return []

    kept = []
    for row in inv:
        fid = row["filing_id"]
        scope_key = f"{row['form']}:{row['reporting_year']}:{row['reporting_period']}:{fid}"
        if ctx.staging.is_done(cfg.adapter, entity["entity_key"], scope_key) and not ctx.force:
            kept.append(row)
            continue
        ctx.staging.checkpoint(cfg.adapter, entity["entity_key"], scope_key, "in_progress")
        try:
            body, entry, url = ecollection.fetch_instance(ctx.client, fid)
        except FetchError as exc:
            ctx.staging.checkpoint(cfg.adapter, entity["entity_key"], scope_key, "failed",
                                   error=exc.detail)
            ctx.staging.open_blocker(cfg.adapter, "source",
                                     f"{entity['entity_key']} {row['form']} "
                                     f"{row['reporting_year']}{row['reporting_period']}: "
                                     f"instance not retrieved",
                                     scope=scope_key, attempts=str(exc.attempts),
                                     exact_error=exc.detail)
            ctx.log("error", f"{fid}: {exc.detail}", adapter=cfg.adapter,
                    entity_cid=entity["entity_key"])
            continue

        tmp = ctx.workdir / f"{fid}.xbrl"
        tmp.write_bytes(body)
        try:
            parsed = parse_instance(str(tmp))
        except Exception as exc:                                       # noqa: BLE001
            ctx.staging.checkpoint(cfg.adapter, entity["entity_key"], scope_key, "failed",
                                   error=f"parse: {exc}")
            ctx.staging.open_blocker(cfg.adapter, "source", f"{fid}: XBRL parse failed",
                                     scope=scope_key, exact_error=str(exc)[:400])
            continue
        finally:
            tmp.unlink(missing_ok=True)

        _persist(cfg, ctx, entity, row, parsed, entry, url)
        row["_parsed"] = parsed
        row["_content_hash"] = entry["content_hash"]
        kept.append(row)
        ctx.staging.checkpoint(cfg.adapter, entity["entity_key"], scope_key, "done")
    return kept


def _persist(cfg, ctx, entity, row, parsed, entry, url) -> None:
    """Write one filing and everything parsed from it, atomically."""
    fid, form = row["filing_id"], row["form"]
    year = row["reporting_year"]
    tax_ns = parsed["taxonomy_ns"]
    version_status, supersedes = ctx.staging.classify_version(
        SOURCE_SYSTEM, entity["entity_key"], form, year, row["reporting_period"],
        fid, entry["content_hash"], submitted_on=row.get("submitted_on") or "")

    filing = {
        "source_system": SOURCE_SYSTEM, "filing_id": fid,
        "entity_key": entity["entity_key"], "form": form,
        "accession_number": row.get("accession_number"),
        "reporting_year": year, "reporting_period": row["reporting_period"],
        "period_start": row["period_start"], "period_end": row["period_end"],
        "filed_date": None, "posted_date": None, "issued_date": None,
        "effective_date": None, "submitted_on": row["submitted_on"],
        "snapshot_date": None, "acceptance_status": row["acceptance_status"],
        "taxonomy_version": ecollection.taxonomy_version(tax_ns),
        "schema_ref": parsed["schema_ref"], "content_hash": entry["content_hash"],
        "is_canonical": row["is_canonical"], "canonical_reason": row["canonical_reason"],
        "version_status": version_status, "supersedes_filing_id": supersedes,
        "data_origin": row["data_origin"],
        "retrieved_at": entry["last_seen_at"], "first_seen_at": entry["first_seen_at"],
        "source_url": url,
    }

    contexts, dims, units, facts = [], [], [], []
    for cid_, c in parsed["contexts"].items():
        pcls, cypy, dur = classify_period(c, year)
        contexts.append({
            "source_system": SOURCE_SYSTEM, "filing_id": fid, "context_id": cid_,
            "entity_identifier": c["entity_identifier"], "scheme": c["scheme"],
            "instant": c["instant"], "period_start": c["start"], "period_end": c["end"],
            "period_class": pcls, "duration_days": str(dur),
            "containers": ";".join(c["containers"]),
            "n_explicit": len(c["explicit"]), "n_typed": len(c["typed"])})
        seq = 0
        for d in c["explicit"]:
            dims.append({"source_system": SOURCE_SYSTEM, "filing_id": fid, "context_id": cid_,
                         "seq": seq, "dim_kind": "explicit", "container": d["container"],
                         "axis_qname": d["axis_qname"], "axis_local": d["axis_local"],
                         "member_qname": d["member_qname"], "member_local": d["member_local"],
                         "typed_value": ""})
            seq += 1
        for d in c["typed"]:
            dims.append({"source_system": SOURCE_SYSTEM, "filing_id": fid, "context_id": cid_,
                         "seq": seq, "dim_kind": "typed", "container": d["container"],
                         "axis_qname": d["axis_qname"], "axis_local": d["axis_local"],
                         "member_qname": d["domain_qname"], "member_local": d["domain_local"],
                         "typed_value": d["value"]})
            seq += 1
    for uid, u in parsed["units"].items():
        units.append({"source_system": SOURCE_SYSTEM, "filing_id": fid, "unit_id": uid,
                      "numerators": "*".join(u["numerators"]),
                      "denominators": "*".join(u["denominators"]),
                      "normalized": u["text"]})

    for order, f in enumerate(parsed["facts"]):
        c = parsed["contexts"].get(f["context_ref"],
                                   {"instant": "", "start": "", "end": "",
                                    "explicit": [], "typed": []})
        pcls, cypy, dur = classify_period(c, year)
        facts.append({
            "source_system": SOURCE_SYSTEM, "filing_id": fid,
            "source_fact_id": ecollection.fact_id(entry["content_hash"], order, f),
            "document_order": order, "concept_qname": f["qname"],
            "concept_local": f["local"], "context_id": f["context_ref"],
            "unit_id": f["unit_ref"],
            "unit_text": parsed["units"].get(f["unit_ref"], {}).get("text", ""),
            "decimals": f["decimals"], "precision": f["precision"],
            "value_as_filed": f["value"], "is_nil": int(f["nil"]),
            "period_class": pcls, "instant": c["instant"], "period_start": c["start"],
            "period_end": c["end"], "duration_days": str(dur), "current_or_prior": cypy,
            "explicit_dims_json": json.dumps(
                [{"axis": d["axis_qname"], "member": d["member_qname"]} for d in c["explicit"]])
                if c["explicit"] else "",
            "typed_dims_json": json.dumps(
                [{"axis": d["axis_qname"], "domain": d["domain_qname"], "value": d["value"]}
                 for d in c["typed"]]) if c["typed"] else "",
            "taxonomy_version": ecollection.taxonomy_version(tax_ns)})

    ctx.staging.write_filing_bundle(filing, facts=facts, contexts=contexts,
                                    dimensions=dims, units=units)
    ctx.log("info", f"{fid} {form} {year}{row['reporting_period']} "
                    f"({row['data_origin']}, {version_status}): {len(facts):,} facts",
            adapter=cfg.adapter, entity_cid=entity["entity_key"])


# ---------------------------------------------------------------- expected

def freeze_expected(cfg, ctx, entity, filings: list[dict], assets: list[dict]) -> list[dict]:
    """Build the frozen expected grid BEFORE any canonical result is examined.

    Requirement class comes from the form's OWN taxonomy for the version that
    filing actually used -- not from the current roster form, and not from
    whether our selector later found anything.
    """
    metrics = [m for m in cfg.metrics()]
    asset_id = assets[0]["asset_id"] if assets else ""
    template = assets[0]["template"] if assets else "interstate_gas"
    out: list[dict] = []
    seen: set[str] = set()

    canonical = [f for f in filings if f["is_canonical"]]
    for f in canonical:
        form, year, period = f["form"], f["reporting_year"], f["reporting_period"]
        tax = ecollection.taxonomy_version(
            (f.get("_parsed") or {}).get("taxonomy_ns", "")) or ""
        schema_ref = (f.get("_parsed") or {}).get("schema_ref", "")
        for m in metrics:
            # Peer-form expansion. A metric declared for one annual form is
            # REQUESTED of every annual form -- whether the form actually
            # collects it is then decided by that form's own taxonomy below.
            # Without this, Form 2-A would simply have no slot for p.514 miles
            # and the answer "this form does not collect it, here is the
            # evidence" could never be produced.
            declared = set(m.regimes)
            for reg, bas in list(declared):
                peers = (cfg.annual_forms if reg in cfg.annual_forms
                         else cfg.quarterly_forms if reg in cfg.quarterly_forms else set())
                for peer in peers - {reg}:
                    declared.add((peer, bas))
            # Narrative-profile metrics are carried by the annual filing's own
            # textblock, so they are requested of any annual form that collects
            # the textblock concept -- not of a form named "XBRL textblock".
            if m.selector == "textblock_span":
                if not cfg.textblock_concept or form not in cfg.annual_forms:
                    continue
                declared = {(cfg.textblock_regime, periods.ANNUAL_OBSERVATION)}
            for regime, basis in sorted(declared):
                is_textblock = (m.selector == "textblock_span"
                                and regime == cfg.textblock_regime)
                if cfg.host_form(regime) != form and not is_textblock:
                    continue
                # a quarterly form reports quarters and YTD; an annual form the year
                if m.selector == "textblock_span":
                    period_used = "Q4"
                elif form in cfg.annual_forms and basis in (periods.QUARTER, periods.YTD):
                    # a Q4 derived from annual inputs is still a requested quarter
                    if basis == periods.YTD:
                        continue
                    period_used = "Q4"
                else:
                    period_used = period

                if m.concept and tax:
                    in_form, page, evidence = ctx.applicability.status(
                        form, tax, m.concept, schema_ref=schema_ref)
                    if in_form == IN_FORM_NO and m.aliases:
                        for a in m.aliases:
                            alt, _p, alt_ev = ctx.applicability.status(
                                form, tax, a["concept"], schema_ref=schema_ref)
                            if alt == IN_FORM_YES:
                                in_form, evidence = alt, alt_ev
                                break
                    requirement = {
                        IN_FORM_YES: (coverage.CONDITIONAL
                                      if (regime, basis) in m.optional_regimes
                                      else coverage.REQUIRED),
                        IN_FORM_NO: coverage.NOT_REQUIRED,
                        IN_FORM_UNKNOWN: coverage.UNKNOWN,
                    }[in_form]
                elif m.selector == "textblock_span":
                    # the textblock itself must be in the form for the narrative
                    # profile items to be expected at all
                    in_form, _p, evidence = ctx.applicability.status(
                        form, tax, cfg.textblock_concept, schema_ref=schema_ref) if tax else (
                        IN_FORM_UNKNOWN, "", "taxonomy version unresolved")
                    requirement = (coverage.OPTIONAL if in_form == IN_FORM_YES
                                   else coverage.NOT_REQUIRED if in_form == IN_FORM_NO
                                   else coverage.UNKNOWN)
                else:
                    requirement = coverage.CONDITIONAL
                    evidence = "derived metric: required when its inputs are present"

                slot = coverage.build_expected(
                    entity["entity_key"], asset_id, template, m, regime, basis,
                    year, period_used, requirement, evidence, tax or "unresolved",
                    ctx.staging.run_id)
                if slot["slot_id"] not in seen:
                    seen.add(slot["slot_id"])
                    out.append(slot)
    return out


# ---------------------------------------------------------------- selection

def _obs(entity_key, m, regime, basis, start, end, instant, year, period, *,
         value_text, value_num, unit, availability, origin, method, version_status,
         validation, qa_flags="", missing_reason="", fact=None, filing_id="",
         selector="", derivation="", tax="", applicability_evidence="",
         normalized_iso="", candidate_count=None, notes="", scope="") -> dict:
    actual_scope = scope or (
        f"FERC filing entity {entity_key}; actual regulatory/facility subset unresolved")
    label = (f"FY{year}" if basis in (periods.ANNUAL, periods.ANNUAL_OBSERVATION)
             else f"{year}{period}")
    return {
        "observation_id": observation_id(entity_key, m.id, regime, basis, start, end,
                                         instant, actual_scope, unit or "", method),
        "entity_key": entity_key, "metric_id": m.id, "source_regime": regime,
        "period_basis": basis, "period_start": start or None, "period_end": end or None,
        "instant_date": instant or None, "reporting_year": year,
        "reporting_period": period, "period_label": label,
        "scope": actual_scope, "scope_rule": m.scope,
        "unit": unit, "value_text": value_text, "value_num": value_num,
        "normalized_iso": normalized_iso or None,
        "availability": availability, "origin": origin, "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": SOURCE_SYSTEM, "filing_id": filing_id or None,
        "source_fact_id": (fact or {}).get("source_fact_id"),
        "source_context_id": (fact or {}).get("context_id"),
        "document_id": None, "accession_number": None,
        "candidate_count": candidate_count,
        "selector": selector or m.selector, "derivation": derivation,
        "concept_local": (fact or {}).get("concept_local") or m.concept,
        "concept_qname": (fact or {}).get("concept_qname"),
        "taxonomy_version": tax, "registry_version": REGISTRY_VERSION,
        "applicability_version": tax,
        "schedule_page": m.schedule, "taxonomy_label": None,
        "qa_flags": qa_flags, "review_status": "open" if qa_flags and "review" in qa_flags else "",
        "missing_reason": missing_reason,
        "applicability_evidence": applicability_evidence, "notes": notes,
    }


def _source_scope(ctx, entity: dict, metric, filing: dict, fact: dict) -> str:
    """Describe the scope evidenced by the exact XBRL occurrence.

    Registry `metric.scope` is an acceptance rule (for example "identical to
    both inputs"), not a legal/facility scope. The stored scope is built from
    the filing entity, regulatory subset and the occurrence's own context and
    dimensions. A display schedule is evidence metadata (`schedule_page`), not
    legal scope: p.114 and p.300 can support the same consolidated filing-entity
    quantity. Nothing is inferred from value equality or the first input.
    """
    entity_key = entity["entity_key"]
    legal = entity.get("legal_name") or entity_key
    context_id = fact.get("context_id") or ""
    context_entity = ""
    if context_id and filing.get("filing_id"):
        rows = ctx.staging.query(
            "SELECT entity_identifier FROM source_contexts WHERE source_system=? "
            "AND filing_id=? AND context_id=?",
            (SOURCE_SYSTEM, filing["filing_id"], context_id))
        if rows:
            context_entity = rows[0]["entity_identifier"] or ""

    dimensions = []
    for field in ("explicit_dims_json", "typed_dims_json"):
        raw = fact.get(field)
        if not raw:
            continue
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, dict):
            parsed = [parsed]
        for dim in parsed or []:
            if not isinstance(dim, dict):
                continue
            axis = dim.get("axis_local") or dim.get("axis") or "axis"
            member = (dim.get("member_local") or dim.get("member") or
                      dim.get("typed_value") or dim.get("value") or
                      "unresolved member")
            domain = dim.get("domain_local") or dim.get("domain") or ""
            dimensions.append(f"{axis}={member}" +
                              (f" (domain {domain})" if domain else ""))

    parts = [f"FERC filing entity {legal} ({entity_key})"]
    if context_entity:
        parts.append(f"XBRL entity identifier {context_entity}")
    regimes = {name for name, _basis in (metric.regimes or ())}
    if registry.P700 in regimes:
        # Page 700 is an actual interstate regulatory subset carried inside a
        # Form 6 filing. It must remain distinct from the carrier-wide income
        # statement even when both facts use an undimensioned context.
        parts.append("regulatory subset Form 6 Page 700 interstate panel")
    else:
        parts.append("regulatory subset filing entity represented by this XBRL context")
    parts.append("context dimensions " + ", ".join(sorted(dimensions))
                 if dimensions else "consolidated source context (no explicit/typed dimensions)")
    return "; ".join(parts)


def canonicalise(cfg, ctx, entity, filings: list[dict], expected: list[dict]) -> tuple[list, list]:
    """Produce observations for every frozen slot, filed first, then derived."""
    observations: list[dict] = []
    edges: list[dict] = []
    entity_key = entity["entity_key"]
    metrics = {m.id: m for m in cfg.metrics()}

    # Facts indexed by (filing, concept) once. Scanning every fact for every
    # requested slot is O(facts x slots) -- fine for one filer, hopeless across
    # the universe, where a single filer can carry 100k facts.
    facts_by_filing: dict[str, list[dict]] = {}
    facts_by_concept: dict[str, dict[str, list[dict]]] = {}
    for f in filings:
        if not f["is_canonical"]:
            continue
        rows = [dict(r) for r in ctx.staging.query(
            "SELECT * FROM source_facts WHERE source_system=? AND filing_id=?",
            (SOURCE_SYSTEM, f["filing_id"]))]
        facts_by_filing[f["filing_id"]] = rows
        idx: dict[str, list[dict]] = {}
        for r in rows:
            idx.setdefault(r["concept_local"], []).append(r)
        facts_by_concept[f["filing_id"]] = idx

    filing_by_slot: dict[tuple, dict] = {}
    for f in filings:
        if f["is_canonical"]:
            filing_by_slot[(f["form"], f["reporting_year"], f["reporting_period"])] = f

    annots = _load_annotations(cfg, ctx, entity_key)

    for slot in expected:
        m = metrics.get(slot["metric_id"])
        if m is None or m.selector in ("derived_ratio", "derived_difference", "textblock_span"):
            continue
        if slot["requirement"] == coverage.NOT_REQUIRED:
            observations.append(_obs(
                entity_key, m, slot["source_regime"], slot["period_basis"],
                slot.get("period_start") or "", slot.get("period_end") or "",
                slot.get("instant_date") or "", slot["reporting_year"],
                slot["reporting_period"],
                value_text=None, value_num=None, unit=None,
                availability=Availability.NOT_REQUIRED, origin=Origin.TAXONOMY,
                method=Method.FILED, version_status=VersionStatus.ORIGINAL,
                validation=Validation.PASS,
                missing_reason="the filed form does not collect this concept",
                applicability_evidence=slot["requirement_evidence"],
                tax=slot["applicability_version"]))
            continue
        # APPLICABILITY_UNKNOWN means "we could not establish whether the form
        # requires this", NOT "do not look". Selection is still attempted: if the
        # filer reported the concept, that settles the question empirically and
        # the value is real. Only when nothing is found does the slot fall back
        # to UNVERIFIED_AVAILABILITY -- because then we can distinguish neither a
        # source blank from a concept the form never collected.
        applicability_unknown = slot["requirement"] == coverage.UNKNOWN

        # locate the filing that should carry this slot
        year, period = slot["reporting_year"], slot["reporting_period"]
        host = cfg.host_form(slot["source_regime"])
        filing = filing_by_slot.get((host, year, period))
        if filing is None and host in cfg.annual_forms:
            filing = filing_by_slot.get((host, year, "Q4"))
        if filing is None:
            observations.append(_obs(
                entity_key, m, slot["source_regime"], slot["period_basis"],
                slot.get("period_start") or "", slot.get("period_end") or "",
                slot.get("instant_date") or "", year, period,
                value_text=None, value_num=None, unit=None,
                availability=Availability.EXPECTED_NOT_LOCATED, origin=Origin.NATIVE_XBRL,
                method=Method.FILED, version_status=VersionStatus.UNRESOLVED,
                validation=Validation.NOT_YET_VALIDATED,
                missing_reason=f"no canonical {host} filing for {year} {period}",
                tax=slot["applicability_version"]))
            continue

        idx = facts_by_concept.get(filing["filing_id"], {})
        wanted = [m.concept] + [a["concept"] for a in m.aliases]
        facts = [r for c in wanted for r in idx.get(c, [])]
        hits, rejects = selectors.candidates(
            facts, m, slot["period_basis"], slot.get("period_start") or "",
            slot.get("period_end") or "", slot.get("instant_date") or "")
        chosen, n, why = selectors.pick(hits)
        tax = filing.get("_parsed", {}).get("taxonomy_ns", "")
        tax = ecollection.taxonomy_version(tax)
        origin = (Origin.FERC_MIGRATED if filing["data_origin"] == "ferc_migrated"
                  else Origin.NATIVE_XBRL)
        vstat = _version_status(cfg, ctx, filing)

        if chosen is None and _aggregate_kind(m):
            # An aggregate metric is not a single filed cell: it is a sum over
            # per-facility, subtotal or detail rows. The sum is only taken when
            # the members are proven MUTUALLY EXCLUSIVE by their own labels --
            # overlapping cuts are refused rather than silently double-counted --
            # and every input gets a lineage edge.
            agg, agg_edges, agg_note, agg_fail = _aggregate(
                cfg, m, idx.get(m.concept, []), slot, ctx, entity, filing, tax, origin,
                vstat, all_facts=facts_by_filing.get(filing["filing_id"], []))
            if agg is not None:
                observations.append(agg)
                edges.extend(agg_edges)
                continue
            if agg_fail:
                observations.append(_obs(
                    entity_key, m, slot["source_regime"], slot["period_basis"],
                    slot.get("period_start") or "", slot.get("period_end") or "",
                    slot.get("instant_date") or "", year, period,
                    value_text=None, value_num=None, unit=None,
                    availability=Availability.UNVERIFIED_AVAILABILITY, origin=origin,
                    method=Method.DERIVED, version_status=vstat,
                    validation=Validation.BLOCKED_AMBIGUITY,
                    missing_reason=agg_fail, filing_id=filing["filing_id"], tax=tax,
                    applicability_evidence=slot["requirement_evidence"]))
                continue

        if chosen is None:
            # Distinguish a genuine selector failure from a source condition:
            # is the concept filed at all in this filing, on ANY basis?
            present_any = idx.get(m.concept, [])
            if not present_any:
                avail, reason = Availability.SOURCE_BLANK, (
                    f"the concept is not present in filing {filing['filing_id']}; "
                    "the form collects it but the filer reported nothing")
            elif n and "ambiguous" in why:
                avail, reason = Availability.UNVERIFIED_AVAILABILITY, why
            else:
                same_basis = [f for f in present_any
                              if selectors.period_matches(
                                  f, slot["period_basis"], slot.get("period_start") or "",
                                  slot.get("period_end") or "",
                                  slot.get("instant_date") or "")[0]]
                if same_basis:
                    avail, reason = Availability.PARSE_FAILED, (
                        f"SELECTOR FAILURE: {len(same_basis)} fact(s) match the requested "
                        f"period grain but no selector matched. "
                        f"First rejections: {'; '.join(rejects[:2])}")
                else:
                    avail, reason = Availability.SOURCE_BLANK, (
                        f"the concept is filed in {filing['filing_id']} but only on another "
                        f"period basis; a year-to-date fact cannot satisfy a requested "
                        f"{slot['period_basis']}")
            if applicability_unknown:
                avail = Availability.UNVERIFIED_AVAILABILITY
                reason = ("applicability could not be resolved for this form/taxonomy "
                          "version AND no fact was found, so a source blank cannot be "
                          f"distinguished from a concept the form does not collect. {reason}")
            observations.append(_obs(
                entity_key, m, slot["source_regime"], slot["period_basis"],
                slot.get("period_start") or "", slot.get("period_end") or "",
                slot.get("instant_date") or "", year, period,
                value_text=None, value_num=None, unit=None,
                availability=avail, origin=origin, method=Method.FILED,
                version_status=vstat, validation=Validation.NOT_YET_VALIDATED,
                missing_reason=reason, filing_id=filing["filing_id"], tax=tax,
                applicability_evidence=slot["requirement_evidence"]))
            continue

        qa: list[str] = []
        if origin == Origin.FERC_MIGRATED:
            qa.append("ferc_migrated")
        if n > 1:
            qa.append(f"duplicate_source_facts: {n} identical facts; lowest document order selected")
        unit = chosen.get("unit_text") or ""
        validation = Validation.PASS

        # UNIT VALIDATION IS A GENERAL PREDICATE, NOT AN ALLOW-LIST.
        #
        # This used to be two hardcoded `if m.id == ...` branches, so any metric
        # nobody had remembered to name was validated PASS whatever unit it
        # filed. w3-financial found the consequence: coverage REFUSES `utr:bbl`
        # under the barrel-mile rule while validation called those same two
        # observations `pass`. One subsystem said the unit was inadmissible and
        # the other said the value was validated, which is worse than either
        # answer alone.
        #
        # Both now consult `registry.unit_admissible()`, so they agree by
        # construction. The two original special cases survive as data rather
        # than as code: `certificated_horsepower` fails because utr:MW is power
        # where the rendered column is horsepower, and `storage_capacity` /
        # `max_day_withdrawal` pass because their unit_rule explicitly admits
        # the untyped `pure` that FERC's rendered p.512-513 schedule supplies.
        if unit:
            admissible, why = registry.unit_admissible(m.id, unit)
            if not admissible:
                qa.append(f"unit_warning: {why}")
                validation = Validation.UNIT_WARNING
            elif "NOT dimensionally verified" in why:
                # admitted because the metric is as-filed; say so rather than
                # letting an unverifiable unit read as a checked one
                qa.append(f"unit_note: {why}")
        elif m.unit_rule and not m.unit_rule.startswith("("):
            qa.append("unit_warning: the fact was filed with no unit at all, but the "
                      f"metric requires {m.unit_rule!r}")
            validation = Validation.UNIT_WARNING

        # TWO SEMANTIC MISMATCHES THE DIMENSIONAL TEST CANNOT SEE, kept as
        # declared exceptions rather than lost to the generalisation.
        #
        # Neither is a family error, so `unit_admissible` passes both -- and
        # dropping them would have QUIETLY RAISED the validated count by
        # reclassifying two known source problems as clean, which is exactly
        # what contract rule 6 forbids. Generalising a check must not be a way
        # to delete the specific knowledge it replaced.
        if m.id == "certificated_horsepower" and "MW" in unit.upper():
            # utr:MW and horsepower are both `power`, so the families agree.
            # The defect is that FERC's TAG says megawatts while the rendered
            # column is horsepower -- a labelling error inside one family.
            qa.append("unit_warning: filed XBRL unit is utr:MW; the rendered column "
                      "is horsepower")
            validation = Validation.UNIT_WARNING
        if unit in ("pure", "xbrli:pure", "") and m.id in ("storage_capacity",
                                                          "max_day_withdrawal"):
            # The metric's own rule admits untyped `pure`, so this is admissible
            # -- but the Dth meaning comes from the RENDERED p.512-513 schedule
            # rather than from the instance, and a consumer is entitled to know
            # the number's unit was supplied by a different document.
            qa.append("unit_note: XBRL unit is pure/unitless; the rendered p.512-513 "
                      "schedule supplies the Dth meaning")
            validation = Validation.UNIT_WARNING

        value_text = chosen["value_as_filed"]
        nil = bool(chosen["is_nil"])
        norm = ""
        if m.id.endswith("_date") or m.unit_rule.startswith("(date"):
            norm = _normalise_peak_date(value_text)
            if norm and not _peak_window_ok(norm, year):
                qa.append(f"implausible_peak_date: normalised {norm} lies outside the "
                          f"FY{year} peak season window; preserved as filed, never repaired")
                validation = Validation.SOURCE_DATE_WARNING

        if applicability_unknown:
            qa.append("applicability_unresolved_for_taxonomy_version: the concept IS filed "
                      "here, which establishes empirically that the form collects it")
        ann = annots.get((filing["filing_id"], chosen["source_fact_id"], m.id))
        if ann and ann["filed_text"] == value_text:
            qa.append(f"{ann['review_status']}: {ann['rationale']}")
            validation = Validation.SOURCE_ANOMALY_REVIEW
            ctx.applied_annotations += 1

        observation = _obs(
            entity_key, m, slot["source_regime"], slot["period_basis"],
            slot.get("period_start") or "", slot.get("period_end") or "",
            slot.get("instant_date") or "", year, period,
            value_text=value_text,
            value_num=_num(value_text) if not nil else None,
            unit=unit,
            availability=Availability.FILED_NIL if nil else Availability.PRESENT,
            origin=origin, method=Method.FILED, version_status=vstat,
            validation=validation, qa_flags="; ".join(qa),
            fact=chosen, filing_id=filing["filing_id"],
            selector=chosen.get("_selector", m.selector), tax=tax,
            normalized_iso=norm, candidate_count=n,
            applicability_evidence=slot["requirement_evidence"],
            notes=why if n > 1 else "",
            scope=_source_scope(ctx, entity, m, filing, chosen))
        if ann and ann["filed_text"] == value_text:
            # Annotation workflow status is separate from the warning itself.
            # A reviewed-final anomaly remains a warning, but is no longer an
            # open review item; open annotations remain explicitly open.
            observation["review_status"] = _annotation_review_status(
                ann["review_status"])
        observations.append(observation)

    derived, derived_edges = _derive(cfg, ctx, entity, filings, expected, observations,
                                    facts_by_concept=facts_by_concept,
                                    filing_by_slot=filing_by_slot)
    observations.extend(derived)
    edges.extend(derived_edges)
    observations.extend(_textblock_profile(cfg, ctx, entity, filings, expected))
    observations.extend(_account_for_remainder(cfg, entity_key, metrics, expected, observations))
    return observations, edges


def _account_for_remainder(cfg, entity_key, metrics, expected, produced) -> list[dict]:
    """Every frozen slot must end in a measured status with a reason.

    A slot left silently empty is indistinguishable from unfinished engineering,
    so anything still unmatched after selection and derivation gets an explicit
    observation naming WHY. For a derived metric that means naming the missing
    input; for a narrative item it means saying the disclosure was not present in
    the filed text.
    """
    have = {(o["entity_key"], o["metric_id"], o["source_regime"], o["period_basis"],
             o.get("period_start") or "", o.get("period_end") or "",
             o.get("instant_date") or "") for o in produced}
    by_metric_period = {(o["metric_id"], o["reporting_year"], o["reporting_period"]): o
                        for o in produced}
    out = []
    for slot in expected:
        key = (slot["entity_key"], slot["metric_id"], slot["source_regime"],
               slot["period_basis"], slot.get("period_start") or "",
               slot.get("period_end") or "", slot.get("instant_date") or "")
        if key in have:
            continue
        m = metrics.get(slot["metric_id"])
        if m is None:
            continue
        year, period = slot["reporting_year"], slot["reporting_period"]

        if m.dependencies:
            missing = []
            for dep in m.dependencies:
                d = by_metric_period.get((dep, year, period))
                if d is None or d["availability"] != Availability.PRESENT:
                    missing.append(f"{dep} ({d['availability'] if d else 'no observation'})")
            reason = (f"derived metric: input unavailable -- {', '.join(missing)}"
                      if missing else
                      "derived metric: inputs present but the derivation gate was not met")
            avail = Availability.SOURCE_BLANK if missing else Availability.UNVERIFIED_AVAILABILITY
        elif m.selector == "textblock_span":
            reason = ("the filed narrative textblock was retrieved and parsed, but contains "
                      "no disclosure of this item")
            avail = Availability.SOURCE_BLANK
        else:
            reason = "no observation produced at the requested grain"
            avail = Availability.NOT_IMPLEMENTED

        out.append(_obs(
            entity_key, m, slot["source_regime"], slot["period_basis"],
            slot.get("period_start") or "", slot.get("period_end") or "",
            slot.get("instant_date") or "", year, period,
            value_text=None, value_num=None, unit=None,
            availability=avail, origin=Origin.NATIVE_XBRL,
            method=Method.DERIVED if m.dependencies else Method.FILED,
            version_status=VersionStatus.ORIGINAL,
            validation=Validation.NOT_YET_VALIDATED,
            missing_reason=reason, tax=slot["applicability_version"],
            applicability_evidence=slot["requirement_evidence"]))
    return out


def _version_status(cfg, ctx, filing) -> str:
    row = ctx.staging.query(
        "SELECT version_status FROM filings WHERE source_system=? AND filing_id=?",
        (SOURCE_SYSTEM, filing["filing_id"]))
    return row[0]["version_status"] if row else VersionStatus.ORIGINAL


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


MONTHS = {m.upper(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _normalise_peak_date(text: str) -> str:
    t = re.sub(r"^Dates?:\s*", "", (text or "").strip(), flags=re.I).strip()
    try:
        return dt.date.fromisoformat(t).isoformat()
    except ValueError:
        pass
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})\s*(?:-\s*\d{1,2})?\s*,?\s+(\d{4})$", t)
    if m and m.group(1).upper() in MONTHS:
        return dt.date(int(m.group(3)), MONTHS[m.group(1).upper()], int(m.group(2))).isoformat()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", t)
    if m:
        y = int(m.group(3))
        return dt.date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2))).isoformat()
    return ""


def _peak_window_ok(iso: str, year: int) -> bool:
    if not iso:
        return True
    d = dt.date.fromisoformat(iso)
    return dt.date(year, 1, 1) <= d <= dt.date(year + 1, 3, 31)


def _load_annotations(cfg, ctx, entity_key: str) -> dict:
    rows = ctx.staging.query(
        "SELECT * FROM reviewed_source_annotations WHERE source_system=? AND entity_key=?",
        (SOURCE_SYSTEM, entity_key))
    return {(r["filing_id"], r["source_fact_id"], r["metric_id"]): dict(r) for r in rows}


def _annotation_review_status(annotation_status: str) -> str:
    """Map annotation workflow vocabulary to the consumer-facing status."""
    return ("resolved" if annotation_status in
            ("reviewed_final", "reviewed_resolved") else "open")




def _fact_as_term(metric, filing, fact, scope) -> dict:
    """Wrap a raw source fact as a derivation term.

    Some derivation inputs are facts the registry never requests as observations
    in their own right -- a single month, for instance. They still need full
    lineage, so they are presented to the derivation machinery in the same shape
    as an observation.
    """
    return {"metric_id": metric.id, "value_num": _num(fact["value_as_filed"]),
            "value_text": fact["value_as_filed"], "unit": fact.get("unit_text") or "",
            "validation": Validation.PASS, "origin": Origin.NATIVE_XBRL,
            "version_status": VersionStatus.ORIGINAL,
            "observation_id": None, "filing_id": fact["filing_id"],
            "source_fact_id": fact["source_fact_id"],
            "source_context_id": fact.get("context_id"),
            "concept_local": fact.get("concept_local"),
            "period_start": fact.get("period_start"), "period_end": fact.get("period_end"),
            "taxonomy_version": fact.get("taxonomy_version"),
            "reporting_year": None, "reporting_period": None,
            "source_regime": filing["form"] if filing else "", "scope": scope}


# ---------------------------------------------------------------- aggregates

AGGREGATE_KINDS = ("facility_sum", "subtotal_sum", "filed_aggregate_sum")


def _aggregate_kind(m) -> str:
    """The aggregate derivation a metric declares, if any."""
    for k in AGGREGATE_KINDS:
        if k in (m.derivation or "") or k in (m.selector or ""):
            return k
    return ""


def _aggregate(cfg, m, concept_facts, slot, ctx, entity, filing, tax, origin, vstat,
               all_facts=None):
    """(observation, edges, note, failure_reason) for an aggregate metric.

    Order of preference, and why:
      1. a filed TOTAL row -- the filer's own total always beats our arithmetic;
      2. a disjoint sum of SUBTOTAL rows;
      3. a disjoint sum of DETAIL rows.
    Subtotals and details are never mixed, because a subtotal already contains
    the details it summarises. Two members sharing a label are an overlapping
    cut and the sum is refused outright.
    """
    entity_key = entity["entity_key"]
    kind = _aggregate_kind(m)
    aggregate_scope = (f"FERC filing entity {entity.get('legal_name') or entity_key} "
                       f"({entity_key}); regulatory schedule {m.schedule or m.concept}; "
                       "aggregate of explicitly persisted mutually exclusive source members")
    rows = [f for f in concept_facts
            if selectors.period_matches(f, slot["period_basis"],
                                        slot.get("period_start") or "",
                                        slot.get("period_end") or "",
                                        slot.get("instant_date") or "")[0]]
    if not rows:
        return None, [], "", ""

    tol0 = selectors.tolerance_for(m.unit_rule)
    labels = selectors.companion_labels(all_facts or rows)

    # ARITHMETIC FIRST. If the rows partition into two halves of equal sum, the
    # schedule has restated its own details as totals and the answer is one half.
    # That is decisive evidence: a caption reading "Total" may belong to a
    # SUBGROUP, and trusting it over the arithmetic silently drops the subgroups
    # it excludes -- TGP's compressor schedule loses exactly one 5,280 hp storage
    # subgroup that way. Captions are consulted only when arithmetic is
    # inconclusive, and they remain authoritative for the details+subtotals+grand
    # total layout, which does NOT split into equal halves.
    _det0, _agg0, _split0 = selectors.split_detail_aggregate(
        rows, tol0["abs"], tol0.get("rel", 0.0))
    totalish, detail_rows = (([], []) if _det0
                             else selectors.classify_rows(rows, labels))
    import os
    if os.environ.get("FERC_DEBUG_AGG") and m.id == "certificated_horsepower":
        print("DEBUG all_facts=", len(all_facts or []), "rows=", len(rows),
              "labels=", len(labels), "totalish=",
              [(r.get("_caption"), r["value_as_filed"]) for r in totalish], flush=True)
    if totalish:
        grand, subgroups, residual = selectors.resolve_filed_total(
            totalish, tol0["abs"], tol0.get("rel", 0.0))
        if grand is None and len(subgroups) > 1:
            # No grand total is filed: sum the disjoint subgroup totals. Picking
            # the largest subtotal instead would report one part of the system
            # as the whole of it.
            sub_total, sub_note = selectors.disjoint_sum(subgroups)
            if sub_total is not None:
                note = (f"no grand total is filed; summed {len(subgroups)} disjoint "
                        f"subgroup total(s) captioned "
                        f"{[selectors.row_label(r, labels)[:28] for r in subgroups]}; {sub_note}")
                obs = _obs(entity_key, m, slot["source_regime"], slot["period_basis"],
                           slot.get("period_start") or "", slot.get("period_end") or "",
                           slot.get("instant_date") or "", slot["reporting_year"],
                           slot["reporting_period"],
                           value_text=f"{sub_total:.10g}", value_num=sub_total,
                           unit=next((r.get("unit_text") for r in subgroups
                                      if r.get("unit_text")), ""),
                           availability=Availability.PRESENT, origin=origin,
                           method=Method.DERIVED, version_status=vstat,
                           validation=Validation.PASS, qa_flags=note,
                           filing_id=filing["filing_id"], derivation="subgroup_total_sum",
                           selector="subgroup_total_sum", tax=tax,
                           applicability_evidence=slot["requirement_evidence"],
                           scope=aggregate_scope)
                sub_edges = [{
                    "observation_id": obs["observation_id"], "input_order": i,
                    "input_role": "group_member", "operator_sign": "+", "coefficient": 1.0,
                    "input_source_system": SOURCE_SYSTEM, "input_filing_id": r["filing_id"],
                    "input_source_fact_id": r["source_fact_id"],
                    "input_observation_id": None, "input_context_id": r.get("context_id"),
                    "input_concept": r.get("concept_local"),
                    "input_period": r.get("instant") or f"{r.get('period_start')}..{r.get('period_end')}",
                    "input_value": r.get("value_as_filed"), "input_unit": r.get("unit_text"),
                    "input_version_status": selectors.row_label(r, labels)}
                    for i, r in enumerate(sorted(
                        subgroups, key=lambda x: int(x.get("document_order") or 0)), 1)]
                return obs, sub_edges, note, ""
        if grand is not None:
            note = (f"filed grand total selected: '{selectors.row_label(grand, labels)}'"
                    + (f", which dominates {len(subgroups)} subgroup total(s)"
                       f"{f' with a residual of {residual:,.4g} from rows having no subgroup total' if residual else ''}"
                       if subgroups else ""))
            obs = _obs(entity_key, m, slot["source_regime"], slot["period_basis"],
                       slot.get("period_start") or "", slot.get("period_end") or "",
                       slot.get("instant_date") or "", slot["reporting_year"],
                       slot["reporting_period"],
                       value_text=grand["value_as_filed"],
                       value_num=_num(grand["value_as_filed"]),
                       unit=grand.get("unit_text") or "",
                       availability=Availability.PRESENT, origin=origin,
                       method=Method.FILED, version_status=vstat,
                       validation=Validation.PASS,
                       qa_flags=note, fact=grand, filing_id=filing["filing_id"],
                       selector="filed_total_by_caption", tax=tax,
                       applicability_evidence=slot["requirement_evidence"],
                       scope=_source_scope(ctx, entity, m, filing, grand))
            return obs, [], note, ""

    totals, subs, details = selectors.partition_totals(rows)
    if totals:
        chosen, n, why = selectors.pick(totals)
        if chosen is not None:
            obs = _obs(entity_key, m, slot["source_regime"], slot["period_basis"],
                       slot.get("period_start") or "", slot.get("period_end") or "",
                       slot.get("instant_date") or "", slot["reporting_year"],
                       slot["reporting_period"],
                       value_text=chosen["value_as_filed"],
                       value_num=_num(chosen["value_as_filed"]),
                       unit=chosen.get("unit_text") or "",
                       availability=Availability.PRESENT, origin=origin,
                       method=Method.FILED, version_status=vstat,
                       validation=Validation.PASS,
                       qa_flags="filed TOTAL row selected in preference to summing members",
                       fact=chosen, filing_id=filing["filing_id"],
                       selector="typed-label:TOTAL", tax=tax, candidate_count=n,
                       applicability_evidence=slot["requirement_evidence"],
                       scope=_source_scope(ctx, entity, m, filing, chosen))
            return obs, [], why, ""

    members = subs or details
    layer = "subtotal" if subs else "detail"
    if not members:
        return None, [], "", ""

    # Recover the schedule's structure arithmetically: its typed members are
    # opaque codes, so neither the axis nor the member name says which rows are
    # details and which are the totals that summarise them.
    fold_applied = ""
    det, agg, split_note = selectors.split_detail_aggregate(
        members, tol0["abs"], tol0.get("rel", 0.0))
    if det:
        # The two halves balance, so they are alternative statements of ONE
        # quantity and the detail half is already the complete answer. Sum it
        # directly and return: passing it on to the signature partition would
        # re-split a set that is by construction whole, and publish whichever
        # sub-cut happened to have the most rows -- dropping, for instance, the
        # single explicit-member 5,280 hp storage row out of TGP's 84.
        total_d, note_d = selectors.disjoint_sum(det)
        if total_d is not None:
            unit_d = next((r.get("unit_text") for r in det if r.get("unit_text")), "")
            obs = _obs(entity_key, m, slot["source_regime"], slot["period_basis"],
                       slot.get("period_start") or "", slot.get("period_end") or "",
                       slot.get("instant_date") or "", slot["reporting_year"],
                       slot["reporting_period"],
                       value_text=f"{total_d:.10g}", value_num=total_d, unit=unit_d,
                       availability=Availability.PRESENT, origin=origin,
                       method=Method.DERIVED, version_status=vstat,
                       validation=Validation.PASS,
                       qa_flags=f"{kind}: {split_note}; {note_d}. The filed total rows are "
                                f"retained as raw evidence and are NOT added to this sum.",
                       filing_id=filing["filing_id"], derivation=kind, selector=kind,
                       tax=tax, applicability_evidence=slot["requirement_evidence"],
                       scope=aggregate_scope)
            edges_d = [{
                "observation_id": obs["observation_id"], "input_order": i,
                "input_role": "group_member", "operator_sign": "+", "coefficient": 1.0,
                "input_source_system": SOURCE_SYSTEM, "input_filing_id": r["filing_id"],
                "input_source_fact_id": r["source_fact_id"],
                "input_observation_id": None, "input_context_id": r.get("context_id"),
                "input_concept": r.get("concept_local"),
                "input_period": r.get("instant") or f"{r.get('period_start')}..{r.get('period_end')}",
                "input_value": r.get("value_as_filed"), "input_unit": r.get("unit_text"),
                "input_version_status": selectors.row_label(r, labels)}
                for i, r in enumerate(sorted(det, key=lambda x: int(x.get("document_order") or 0)), 1)]
            return obs, edges_d, split_note, ""
    if False:
        pass
    else:
        folded_total, top_rows, fold_note = selectors.fold_aggregates(
            members, tol0["abs"], tol0.get("rel", 0.0))
        if folded_total is not None and len(top_rows) < len(members):
            members, layer, fold_applied = top_rows, "folded", fold_note

    # A schedule may ALSO report the same quantity under two alternative
    # dimensional cuts. Sum within ONE cut; use any other as a cross-check.
    groups = selectors.partition_by_signature(members)
    sums = {}
    for sig, rows_in in groups.items():
        t, n = selectors.disjoint_sum(rows_in)
        if t is not None:
            sums[sig] = (t, n, rows_in)
    if not sums:
        _, why = selectors.disjoint_sum(members)
        return None, [], "", (f"{kind}: {why}. The sum is refused rather than "
                              "double-counting an overlapping cut.")

    tol = selectors.tolerance_for(m.unit_rule)
    # finest cut first: the most members
    ordered = sorted(sums.items(), key=lambda kv: -len(kv[1][2]))
    sig, (total, note, members) = ordered[0]
    cross = []
    disagreement = ""
    for other_sig, (other_total, _, other_rows) in ordered[1:]:
        ok, cmp_note = selectors.check_aggregate(total, other_total, tol["abs"],
                                                 tol.get("rel", 0.0))
        label = (f"{len(other_rows)}-member "
                 f"{'/'.join(other_sig[1] or other_sig[3]) or 'undimensioned'} cut")
        if ok:
            cross.append(f"cross-checked against the {label} ({cmp_note})")
            continue
        if len(other_rows) == len(members):
            # Equal-sized cuts that disagree give no basis to prefer either.
            return None, [], "", (
                f"{kind}: two equally complete dimensional cuts disagree -- "
                f"{cmp_note}. Neither is published, and they are NOT added together.")
        # Unequal cuts: the FINER enumeration is the more complete one, so it is
        # published and the coarser cut's shortfall is reported as a warning.
        # Choosing on completeness is a stated reason; choosing on magnitude
        # would be the forbidden largest-value shortcut. Suppressing a defensible
        # entity value because a secondary classification is incomplete would
        # lose real data.
        disagreement = (f"the {label} disagrees: {cmp_note}. The finer "
                        f"{len(members)}-member enumeration is published because it is "
                        "the complete one; the coarser cut appears not to classify "
                        "every member.")
    if cross:
        note += "; " + "; ".join(cross)
    if disagreement:
        note += "; " + disagreement
    if fold_applied:
        note = f"{fold_applied}; {note}"

    # Where BOTH a subtotal layer and a detail layer exist, they must reconcile.
    if subs and details:
        d_total, _ = selectors.disjoint_sum(details)
        if d_total is not None:
            tol = selectors.tolerance_for(m.unit_rule)
            ok, detail_note = selectors.check_aggregate(total, d_total, tol["abs"],
                                                        tol.get("rel", 0.0))
            if not ok:
                return None, [], "", (f"{kind}: subtotal and detail layers disagree -- "
                                      f"{detail_note}. Neither is published.")
            note += f"; reconciles with the detail layer ({detail_note})"

    unit = next((r.get("unit_text") for r in members if r.get("unit_text")), "")
    obs = _obs(entity_key, m, slot["source_regime"], slot["period_basis"],
               slot.get("period_start") or "", slot.get("period_end") or "",
               slot.get("instant_date") or "", slot["reporting_year"],
               slot["reporting_period"],
               value_text=f"{total:.10g}", value_num=total, unit=unit,
               availability=Availability.PRESENT, origin=origin,
               method=Method.DERIVED, version_status=vstat,
               validation=(Validation.ROUNDING_WARNING if disagreement else Validation.PASS),
               qa_flags=(f"{kind}: {note}; summed over the {layer} layer only, "
                         f"{len(members)} member(s). This is a sum of filed members, "
                         "not a filer-certified system total."),
               filing_id=filing["filing_id"], derivation=kind, selector=kind,
               tax=tax, applicability_evidence=slot["requirement_evidence"],
               scope=aggregate_scope)
    edges = []
    for i, r in enumerate(sorted(members, key=lambda x: int(x.get("document_order") or 0)), 1):
        ty = selectors.typed_dims(r)
        edges.append({
            "observation_id": obs["observation_id"], "input_order": i,
            "input_role": "group_member", "operator_sign": "+", "coefficient": 1.0,
            "input_source_system": SOURCE_SYSTEM, "input_filing_id": r["filing_id"],
            "input_source_fact_id": r["source_fact_id"],
            "input_observation_id": None, "input_context_id": r.get("context_id"),
            "input_concept": r.get("concept_local"),
            "input_period": r.get("instant") or f"{r.get('period_start')}..{r.get('period_end')}",
            "input_value": r.get("value_as_filed"), "input_unit": r.get("unit_text"),
            "input_version_status": (ty[0].get("value") if ty else "")})
    return obs, edges, note, ""


# ---------------------------------------------------------------- derivation

def _derive(cfg, ctx, entity, filings, expected, filed_obs, *,
           facts_by_concept=None, filing_by_slot=None) -> tuple[list[dict], list[dict]]:
    """Sequential-YTD and Q4 derivations, for gas as well as liquids.

    A derived quarter is kept STRICTLY separate from a blank filed quarter: it
    gets method=derived and its own lineage, and it never overwrites the filed
    observation that says the quarter was blank.
    """
    entity_key = entity["entity_key"]
    metrics = {m.id: m for m in cfg.metrics()}
    out, edges = [], []

    by_key: dict[tuple, dict] = {}
    for o in filed_obs:
        if o["availability"] != Availability.PRESENT:
            continue
        by_key[(o["metric_id"], o["period_basis"], o.get("period_start"),
                o.get("period_end"))] = o

    facts_by_concept = facts_by_concept or {}
    filing_by_slot = filing_by_slot or {}
    facts_cache: dict[str, list[dict]] = {}

    def facts_for(filing_id):
        if filing_id not in facts_cache:
            facts_cache[filing_id] = [dict(r) for r in ctx.staging.query(
                "SELECT * FROM source_facts WHERE source_system=? AND filing_id=?",
                (SOURCE_SYSTEM, filing_id))]
        return facts_cache[filing_id]

    wanted = [s for s in expected
              if s["period_basis"] == periods.QUARTER
              and s["requirement"] in coverage.CORE]

    for slot in wanted:
        m = metrics.get(slot["metric_id"])
        if m is None or not m.derivation:
            continue
        key = (slot["metric_id"], periods.QUARTER, slot.get("period_start"),
               slot.get("period_end"))
        if key in by_key:
            continue                        # a filed quarter always wins
        year, period = slot["reporting_year"], slot["reporting_period"]

        plan = None
        # 1. Monthly sum, where the form carries a monthly schedule. This is the
        #    most direct route and it is exact: three filed monthly facts, no
        #    subtraction. Form 2-A has no p.299, which is why the algebraic
        #    routes below exist at all.
        if "q4_monthly_sum" in m.derivation:
            # Monthly facts are read from the FILING, not from by_key: the
            # registry declares no monthly slot (nobody requests a month on its
            # own), so no monthly observation exists to look up. The facts are
            # there all the same, and three filed months are a better Q4 than any
            # subtraction -- no annual rounding is inherited.
            host_filing = filing_by_slot.get((slot["source_regime"], year, "Q4"))
            monthly = []
            if host_filing is not None:
                idx_m = facts_by_concept.get(host_filing["filing_id"], {})
                for a, b in periods.monthly_sum_inputs(year, period):
                    hit = [f for f in idx_m.get(m.concept, [])
                           if f.get("period_start") == a and f.get("period_end") == b
                           and not f.get("explicit_dims_json")
                           and not f.get("typed_dims_json")]
                    chosen_m, n_m, _ = selectors.pick(hit)
                    monthly.append(chosen_m)
            if monthly and all(monthly):
                plan = ("q4_monthly_sum",
                        [_fact_as_term(
                            m, host_filing, f,
                            _source_scope(ctx, entity, m, host_filing, f))
                         for f in monthly],
                        "sum of the three filed monthly facts for the quarter; no "
                        "subtraction is involved, so this does not inherit an annual "
                        "total's rounding")
        # 2. Annual minus the three filed quarters.
        if plan is None and period == "Q4" and "q4_minus_quarters" in m.derivation:
            ann = periods.annual_interval(year)
            a = by_key.get((m.id, periods.ANNUAL, ann[0], ann[1]))
            qs = [by_key.get((m.id, periods.QUARTER) + periods.quarter_interval(year, q))
                  for q in ("Q1", "Q2", "Q3")]
            if a and all(qs):
                plan = ("q4_minus_quarters", [a] + [("-", q) for q in qs],
                        "ALGEBRAIC: filed annual total minus the three filed quarters. "
                        "A Q4 derived this way sums back to the annual total by "
                        "construction and is not independent confirmation of Q4.")
        if plan is None and period == "Q4" and "q4_annual_minus_q3_ytd" in m.derivation:
            ann, q3 = periods.q4_from_annual_minus_q3_ytd(year)
            a = by_key.get((m.id, periods.ANNUAL, ann[0], ann[1]))
            b = by_key.get((m.id, periods.YTD, q3[0], q3[1]))
            if a and b:
                plan = ("q4_annual_minus_q3_ytd", a, b,
                        "ALGEBRAIC: annual total minus Q3 year-to-date. A Q4 derived this "
                        "way sums back to the annual total by construction and is not "
                        "independent confirmation of the Q4 figure.")
        if plan is None and "sequential_ytd" in m.derivation:
            pair = periods.sequential_ytd_inputs(year, period)
            if pair:
                (ts, te), (ps, pe) = pair
                a = by_key.get((m.id, periods.YTD, ts, te))
                b = by_key.get((m.id, periods.YTD, ps, pe))
                if a and b:
                    plan = ("sequential_ytd", a, b,
                            "sequential year-to-date difference: this YTD minus the "
                            "preceding YTD, both filed")
        if plan is None:
            continue

        if len(plan) == 4:
            kind, minuend, subtrahend, note = plan
            terms = [("+", minuend), ("-", subtrahend)]
        else:
            kind, raw_terms, note = plan
            terms = [t if isinstance(t, tuple) else ("+", t) for t in raw_terms]
        srcs = [o for _, o in terms]
        if any(o is None or o["value_num"] is None for o in srcs):
            continue
        source_scopes = {o.get("scope") for o in srcs if o.get("scope")}
        if len(source_scopes) != 1 or any(not o.get("scope") for o in srcs):
            scope_detail = "; ".join(
                f"{o.get('metric_id') or m.id}={o.get('scope') or 'unresolved'}"
                for o in srcs)
            out.append(_obs(
                entity_key, m, slot["source_regime"], periods.QUARTER,
                slot["period_start"], slot["period_end"], "", year, period,
                value_text=None, value_num=None, unit=None,
                availability=Availability.NOT_APPLICABLE,
                origin=srcs[0]["origin"], method=Method.DERIVED,
                version_status=srcs[0]["version_status"],
                validation=Validation.SCOPE_INCOMPATIBLE,
                missing_reason=("derivation refused because input scopes are not "
                                f"identical: {scope_detail}"),
                derivation=kind, selector="derived",
                tax=srcs[0].get("taxonomy_version") or "",
                applicability_evidence=slot["requirement_evidence"],
                scope=(f"FERC filing entity {entity.get('legal_name') or entity_key} "
                       f"({entity_key}); derived scope unresolved because inputs differ")))
            continue
        common_scope = next(iter(source_scopes))
        # a derivation must not silently launder an input under review
        inherited = [o["validation"] for o in srcs
                     if o["validation"] in Validation.MUST_PROPAGATE]
        # Decimal, not float: a cent of binary rounding in a nine-figure total
        # shows up as an off-by-one against the reference and is indistinguishable
        # from a real selection error.
        from decimal import Decimal
        acc = Decimal("0")
        for sign, o in terms:
            d = Decimal(str(o["value_text"]).replace(",", "").strip())
            acc = acc + d if sign == "+" else acc - d
        value = float(acc)
        minuend = srcs[0]
        start, end = slot["period_start"], slot["period_end"]
        obs = _obs(entity_key, m, slot["source_regime"], periods.QUARTER, start, end, "",
                   year, period,
                   value_text=repr(value) if isinstance(value, float) else str(value),
                   value_num=value, unit=minuend["unit"],
                   availability=Availability.PRESENT,
                   origin=minuend["origin"], method=Method.DERIVED,
                   version_status=minuend["version_status"],
                   validation=inherited[0] if inherited else Validation.PASS,
                   qa_flags="; ".join(["derivation: " + note] +
                                      [f"inherited_from_input: {v}" for v in inherited]),
                   derivation=kind, selector="derived",
                   tax=minuend["taxonomy_version"],
                   applicability_evidence=slot["requirement_evidence"],
                   scope=common_scope)
        obs["value_text"] = (str(int(acc)) if acc == acc.to_integral_value()
                             else f"{value:.10g}")
        out.append(obs)

        roles = [("addend" if sg == "+" else "subtrahend", sg, o) for sg, o in terms]
        if kind in ("q4_minus_quarters", "q4_annual_minus_q3_ytd", "sequential_ytd"):
            roles[0] = ("minuend", roles[0][1], roles[0][2])
        for i, (role, sign, src) in enumerate(roles, 1):
            fr = next((f for f in facts_for(src["filing_id"])
                       if f["source_fact_id"] == src["source_fact_id"]), {})
            edges.append({
                "observation_id": obs["observation_id"], "input_order": i,
                "input_role": role, "operator_sign": sign, "coefficient": 1.0,
                "input_source_system": SOURCE_SYSTEM, "input_filing_id": src["filing_id"],
                "input_source_fact_id": src["source_fact_id"],
                "input_observation_id": src["observation_id"],
                "input_context_id": src.get("source_context_id"),
                "input_concept": src.get("concept_local"),
                "input_period": f"{src.get('period_start')}..{src.get('period_end')}",
                "input_value": src.get("value_text"), "input_unit": src.get("unit"),
                "input_version_status": src.get("version_status")})

    out.extend(_ratios(cfg, ctx, entity_key, metrics, filed_obs + out, edges))
    return out, edges


def _ratios(cfg, ctx, entity_key, metrics, all_obs, edges) -> list[dict]:
    """Scope-checked ratios. Refused outright when the two inputs do not share
    an identical scope and interval."""
    out = []
    index: dict[tuple, list[dict]] = {}
    for observation in all_obs:
        if observation["availability"] != Availability.PRESENT:
            continue
        key = (observation["metric_id"], observation["period_basis"],
               observation.get("period_start"), observation.get("period_end"),
               observation.get("instant_date"))
        index.setdefault(key, []).append(observation)
    for m in metrics.values():
        if m.selector not in ("derived_ratio", "derived_difference") or len(m.dependencies) != 2:
            continue
        is_ratio = m.selector == "derived_ratio"
        num_id, den_id = m.dependencies
        for key, numerators in list(index.items()):
            if key[0] != num_id:
                continue
            denominators = index.get((den_id,) + key[1:], [])
            if not denominators:
                continue
            for num in numerators:
                basis, start, end, inst = key[1], key[2], key[3], key[4]
                num_scope = str(num.get("scope") or "")
                compatible = []
                for den in denominators:
                    if not num_scope or "unresolved" in num_scope.lower():
                        continue
                    # Keep this as an explicit join predicate: a shared unit or
                    # interval can never make different regulatory scopes
                    # interchangeable (notably Page 700 vs whole Form 6).
                    if num["scope"] != den["scope"]:
                        continue
                    if "unresolved" in str(den.get("scope") or "").lower():
                        continue
                    compatible.append(den)
                if len(compatible) != 1:
                    # Scope compatibility is a join condition, not a tiebreaker.
                    # Never select the first denominator, and never let dict
                    # insertion order decide between regulatory subsets.
                    den_scopes = sorted({str(den.get("scope") or "unresolved")
                                         for den in denominators})
                    detail = ("none" if not compatible else str(len(compatible)))
                    out.append(_obs(
                        entity_key, m, num["source_regime"], basis, start or "", end or "",
                        inst or "", num["reporting_year"], num["reporting_period"],
                        value_text=None, value_num=None, unit=None,
                        availability=Availability.NOT_APPLICABLE, origin=num["origin"],
                        method=Method.DERIVED, version_status=num["version_status"],
                        validation=Validation.SCOPE_INCOMPATIBLE,
                        missing_reason=(f"the ratio is refused: scope join refused: {num_id} covers "
                                        f"'{num.get('scope') or 'unresolved'}'; {den_id} "
                                        f"candidate scopes are {den_scopes}; exactly one "
                                        f"compatible input is required, found {detail}"),
                        derivation=f"ratio({num_id},{den_id})", selector=m.selector,
                        scope=(f"FERC filing entity {entity_key}; derived scope unresolved "
                               "because inputs differ or are ambiguous")))
                    continue
                den = compatible[0]
                if is_ratio and not den["value_num"]:
                    # A zero denominator is a real source condition, not a gap
                    # in our engineering: the ratio is undefined and is
                    # recorded as such.
                    out.append(_obs(
                        entity_key, m, num["source_regime"], basis, start or "", end or "",
                        inst or "", num["reporting_year"], num["reporting_period"],
                        value_text=None, value_num=None, unit=None,
                        availability=Availability.NOT_APPLICABLE, origin=num["origin"],
                        method=Method.DERIVED, version_status=num["version_status"],
                        validation=Validation.PASS,
                        missing_reason=(
                            f"{den_id} is filed as {den['value_text']} for this period; "
                            "the ratio is undefined and is not published"),
                        qa_flags="denominator filed as zero; ratio undefined",
                        derivation=f"ratio({num_id},{den_id})", selector="derived_ratio",
                        tax=num["taxonomy_version"], scope=num["scope"]))
                    continue

                # ARITHMETIC IN DECIMAL, then the UNIT FROM THE REGISTRY.
                #
                # The 8 September audit found 1,251 margins storing a fraction
                # while declaring `percent` (A04). The arithmetic was never
                # wrong; the unit/value contract was. The unit now comes from
                # the metric and the value is scaled exactly once.
                dnum = _dec(num.get("value_text") or num["value_num"])
                dden = _dec(den.get("value_text") or den["value_num"])
                if is_ratio:
                    dval = dnum / dden
                    if m.canonical_unit == "percent":
                        dval *= _dec(100)             # the ONE percent conversion
                    unit = m.canonical_unit or "ratio"
                else:
                    dval = dnum - dden
                    unit = m.canonical_unit or num["unit"]
                value = float(dval)
                inherited = [o["validation"] for o in (num, den)
                             if o["validation"] in Validation.MUST_PROPAGATE]

                # DENOMINATOR PLAUSIBILITY. Fayetteville Express's tiny filed
                # revenue basis creates an arithmetically correct but unusable
                # margin. Preserve the number with a warning; never suppress it.
                implausible = ""
                if is_ratio and dden != 0 and abs(dnum / dden) > _RATIO_SANITY:
                    implausible = (
                        f"denominator {den_id}={den['value_text']} is negligible beside "
                        f"numerator {num_id}={num['value_text']}; the ratio "
                        f"({value:,.1f} {unit}) is arithmetically correct but is not a "
                        "meaningful economic quantity and must not be used as a headline "
                        "or as an input to another calculation")
                obs = _obs(
                    entity_key, m, num["source_regime"], basis, start or "", end or "",
                    inst or "", num["reporting_year"], num["reporting_period"],
                    value_text=f"{dval:.6f}", value_num=value, unit=unit,
                    availability=Availability.PRESENT, origin=num["origin"],
                    method=Method.DERIVED, version_status=num["version_status"],
                    validation=(Validation.SOURCE_ANOMALY_REVIEW if implausible
                                else inherited[0] if inherited else Validation.PASS),
                    qa_flags="; ".join(
                        [(f"ratio of {num_id} over {den_id}" if is_ratio
                          else f"{num_id} minus {den_id}") + " on identical scope"]
                        + ([implausible] if implausible else [])
                        + [f"inherited_from_input: {v}" for v in inherited]),
                    derivation=(f"{'ratio' if is_ratio else 'difference'}"
                                f"({num_id},{den_id})"),
                    selector=m.selector, tax=num["taxonomy_version"], scope=num["scope"])
                out.append(obs)
                roles = (("numerator", num), ("denominator", den)) if is_ratio else (
                         ("minuend", num), ("subtrahend", den))
                for i, (role, src) in enumerate(roles, 1):
                    edges.append({
                        "observation_id": obs["observation_id"], "input_order": i,
                        "input_role": role,
                        "operator_sign": ("/" if is_ratio else "-") if i == 2 else "+",
                        "coefficient": 1.0, "input_source_system": SOURCE_SYSTEM,
                        "input_filing_id": src.get("filing_id"),
                        "input_source_fact_id": src.get("source_fact_id"),
                        "input_observation_id": src["observation_id"],
                        "input_context_id": src.get("source_context_id"),
                        "input_concept": src.get("concept_local"),
                        "input_period":
                            f"{src.get('period_start')}..{src.get('period_end')}",
                        "input_value": src.get("value_text"),
                        "input_unit": src.get("unit"),
                        "input_version_status": src.get("version_status")})
    return out


# ---------------------------------------------------------------- textblock

def _textblock_profile(cfg, ctx, entity, filings, expected) -> list[dict]:
    if not cfg.textblock_concept or not cfg.textblock_patterns:
        return []
    """Evidence-backed profile observations from FERC-filed narrative.

    Form 2-A has no p.508/512/514/518 schedule, but its p.211.1 textblock is a
    real FERC disclosure. Extracting it closes a template field WITHOUT
    pretending a narrative is a certified schedule total.
    """
    entity_key = entity["entity_key"]
    slots = {(s["metric_id"], s["reporting_year"]): s for s in expected
             if s["source_regime"] == "XBRL textblock"}
    if not slots:
        return []
    out = []
    for f in filings:
        if not f["is_canonical"]:
            continue
        rows = ctx.staging.query(
            "SELECT * FROM source_facts WHERE source_system=? AND filing_id=? "
            "AND concept_local=?", (SOURCE_SYSTEM, f["filing_id"], cfg.textblock_concept))
        if not rows:
            continue
        year = f["reporting_year"]
        for r in rows:
            text = re.sub(r"<[^>]+>", " ", r["value_as_filed"] or "")
            text = re.sub(r"&#?\w+;", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            for metric_id, pattern, unit in cfg.textblock_patterns:
                slot = slots.get((metric_id, year))
                if slot is None:
                    continue
                m_obj = pattern.search(text)
                if not m_obj:
                    continue
                metric = BY_ID[metric_id]
                span_start = max(0, m_obj.start() - 60)
                span_end = min(len(text), m_obj.end() + 60)
                verbatim = text[span_start:span_end]
                groups = [g for g in m_obj.groups() if g]
                qualifier = ""
                if groups and str(groups[0]).lower() in ("approximately", "about", "roughly"):
                    qualifier, groups = groups[0], groups[1:]
                value_text = " / ".join(str(g) for g in groups) if groups else m_obj.group(0)
                value_num = _num(groups[0]) if len(groups) == 1 else None
                obs = _obs(
                    entity_key, metric, "XBRL textblock", periods.ANNUAL_OBSERVATION,
                    "", "", slot.get("instant_date") or "", year, "Q4",
                    value_text=value_text, value_num=value_num, unit=unit,
                    availability=Availability.PRESENT, origin=Origin.NATIVE_XBRL,
                    method=Method.DOCUMENT_EXTRACTED, version_status=VersionStatus.ORIGINAL,
                    validation=Validation.SCOPE_INCOMPATIBLE,
                    qa_flags=("narrative disclosure extracted from the filer's own p.211.1 "
                              "textblock; NOT a p.514/p.508 schedule total and not a "
                              "certified figure; described sections are never summed and "
                              "no unit conversion is applied"
                              + (f"; qualifier as filed: '{qualifier}'" if qualifier else "")),
                    fact=dict(r), filing_id=f["filing_id"],
                    selector="textblock_span",
                    tax=r["taxonomy_version"],
                    applicability_evidence=slot["requirement_evidence"],
                    notes=f"span[{span_start}:{span_end}]",
                    scope=_source_scope(ctx, entity, metric, f, dict(r)))
                obs["_document_fact"] = {
                    "document_fact_id": f"dfact-{f['filing_id']}-{metric_id}-{year}",
                    "document_id": None, "source_system": SOURCE_SYSTEM,
                    "filing_id": f["filing_id"], "source_fact_id": r["source_fact_id"],
                    "entity_key": entity_key, "assertion_type": metric_id,
                    "metric_id": metric_id, "value_text": value_text,
                    "value_num": value_num, "unit": unit, "qualifier": qualifier,
                    "scope_note": metric.scope, "page": cfg.textblock_page, "paragraph": "",
                    "char_start": span_start, "char_end": span_end,
                    "verbatim_span": verbatim,
                    "extraction_method": "xbrl_textblock_regex",
                    "content_hash": f.get("_content_hash", ""),
                    "confidence": "verified_span", "review_state": "",
                    "reviewer_note": "", "first_seen_at": ecollection.utcnow()}
                out.append(obs)
    return out
