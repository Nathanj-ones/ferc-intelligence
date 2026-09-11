"""Versioned, read-only consumer exports for the existing asset frontend.

The current frontend's SQLite schema is a separate application store.  This
module deliberately does not mimic or mutate that store.  It emits a small
adapter contract: stable asset slugs, entity payloads, exact FERC scope/status/
source fields, and explicit comparison gates.  A later integration can serve
these files behind the existing routes without pointing the app at this DB.
"""

from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping

from ferclib import coverage as cov
from ferclib.http import assert_no_secrets
from ferclib.registry import (
    BY_ID,
    REGISTRY_VERSION,
    unit_admissible,
    unit_family_of,
    unit_scale_of,
)
from ferclib.status import Availability, Validation


SCHEMA = "ferc_operating_assets_frontend_v1"
CONTRACT_VERSION = "1.1.0"
COMPARISON_ID_VERSION = "v1"
MAX_COMPARISON_SERIES = 4
_UNRESOLVED_SCOPE = "actual regulatory/facility subset unresolved"
_UNDECODABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CORE = frozenset({cov.REQUIRED, cov.CONDITIONAL})


class FrontendContractViolation(ValueError):
    """Raised when a detached consumer generation is not self-consistent."""


def _rows(ctx, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in ctx.staging.query(sql, params)]


def _json_value(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _write_json(path: pathlib.Path, payload) -> int:
    # Provenance-rich entity payloads are numerous; compact deterministic JSON
    # keeps the detached candidate practical without dropping any fields.
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":")) + "\n"
    assert_no_secrets(body[:200000])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return len(body.encode("utf-8"))


def _entity_filename(entity_key: str) -> str:
    """Stable safe filename; the full source identity stays inside the file."""
    if re.fullmatch(r"C\d{6}", entity_key) or entity_key == "FERC-OIL-INDEX":
        return f"{entity_key}.json"
    digest = hashlib.sha256(entity_key.encode("utf-8")).hexdigest()[:20]
    return f"local-{digest}.json"


def _asset_detail_mappings(mappings: list[dict]) -> list[dict]:
    """Convert contract-root entity paths to asset-document-relative paths."""
    return [
        {**mapping,
         "entity_payload_path": f"../{mapping['entity_payload_path']}"}
        for mapping in mappings
    ]


def _load_universe(path: pathlib.Path) -> dict[str, dict]:
    with pathlib.Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "asset_id" not in rows[0]:
        raise ValueError(f"universe has no usable asset rows: {path}")
    return {row["asset_id"]: row for row in rows if row.get("asset_id")}


def actual_scope_resolved(scope: str | None) -> bool:
    text = str(scope or "").strip()
    return bool(text and text != _UNRESOLVED_SCOPE and not _UNDECODABLE.search(text))


def _stable_id(prefix: str, *parts) -> str:
    """Return a compact, versioned ID without delimiter-collision ambiguity."""
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def comparison_decision(row: dict, *, template: str,
                        unique_asset_mapping: bool = True) -> dict:
    """Explain whether one observation is safe for a same-type chart.

    The key uses the registry contract (`scope_rule`) rather than the legal
    entity name embedded in actual scope.  Actual scope is still required and
    remains in the payload.  Consumers compare only observations with equal
    keys, never by metric label or unit-looking text alone.
    """
    reasons: list[str] = []
    metric_id = str(row.get("metric_id") or "")
    metric = BY_ID.get(metric_id)
    entity_key = str(row.get("entity_key") or "").strip()
    if template == "OUT_OF_TEMPLATE":
        reasons.append("asset_type_out_of_operating_templates")
    if metric is None:
        reasons.append("metric_unregistered")
    elif template not in metric.templates:
        reasons.append("metric_not_in_asset_type")
    if not entity_key:
        reasons.append("comparison_subject_missing")
    if not unique_asset_mapping:
        reasons.append("filing_entity_maps_to_multiple_asset_rows")
    if row.get("availability") != Availability.PRESENT:
        reasons.append(f"availability:{row.get('availability') or 'unknown'}")
    if row.get("validation") != Validation.PASS:
        reasons.append(f"validation:{row.get('validation') or 'unknown'}")
    if row.get("version_status") == "superseded":
        reasons.append("superseded_occurrence")
    if row.get("review_status") not in (None, "", "resolved"):
        reasons.append(f"review_status:{row.get('review_status')}")
    if row.get("value_num") is None:
        reasons.append("non_numeric_value")
    unit = str(row.get("unit") or "")
    unit_family = unit_family_of(unit)
    unit_scale = unit_scale_of(unit)
    if not unit_family:
        reasons.append("unit_family_unresolved")
    elif unit_scale is None:
        reasons.append("unit_scale_unresolved")
    if metric is not None:
        admitted, _unit_reason = unit_admissible(metric_id, unit)
        if not admitted:
            reasons.append("unit_not_admissible_for_metric")
    if not actual_scope_resolved(row.get("scope")):
        reasons.append("actual_scope_unresolved")
    contract_scope = str(row.get("scope_rule") or row.get("scope") or "").strip()
    if not contract_scope:
        reasons.append("scope_contract_missing")
    key = None
    group_id = None
    series_id = None
    comparison_value_base = None
    if not reasons:
        key = "|".join((template, metric_id,
                        str(row.get("period_basis") or ""), unit_family,
                        contract_scope))
        group_id = _stable_id(
            f"comparison-group-{COMPARISON_ID_VERSION}", CONTRACT_VERSION,
            template, metric_id, row.get("period_basis") or "", unit_family,
            contract_scope)
        # Actual filed scope is deliberately part of the subject's series
        # identity while the registry contract is the cross-subject group.  A
        # facility/direction/season therefore cannot silently collapse into a
        # sibling series merely because the two share a registry rule.
        series_id = _stable_id(
            f"comparison-series-{COMPARISON_ID_VERSION}", CONTRACT_VERSION,
            group_id, entity_key, row.get("scope") or "")
        comparison_value_base = float(row["value_num"]) * float(unit_scale)
    return {
        "eligible": not reasons,
        "key": key,
        "group_id": group_id,
        "series_id": series_id,
        "subject_id": entity_key or None,
        "identity_version": COMPARISON_ID_VERSION,
        "comparison_value_base": comparison_value_base,
        "base_unit_family": unit_family or None,
        "source_unit_scale_to_base": unit_scale,
        "reasons": reasons,
        "same_type_required": True,
        "exact_unit_family_required": True,
        "exact_period_basis_required": True,
        "scope_contract": contract_scope or None,
    }


def pair_comparison_decision(left: dict, right: dict) -> dict:
    """Pure negative/positive control used by the contract validator/tests."""
    reasons = []
    for label, item in (("left", left), ("right", right)):
        if not item.get("eligible"):
            reasons.append(f"{label}_ineligible")
    if left.get("group_id") != right.get("group_id"):
        reasons.append("comparison_group_mismatch")
    if left.get("key") != right.get("key"):
        reasons.append("comparison_key_mismatch")
    return {"eligible": not reasons, "reasons": reasons}


def comparison_selection_decision(items: list[dict]) -> dict:
    """Validate a user selection of comparison *series*, never data points.

    Callers deduplicate observations by ``series_id`` before invoking this
    control.  Rejecting duplicate series and duplicate legal-entity subjects
    here prevents aliases or two scopes for one filer from masquerading as two
    independent assets.
    """
    rows = list(items)
    reasons: list[str] = []
    if len(rows) < 2:
        reasons.append("fewer_than_two_series")
    if len(rows) > MAX_COMPARISON_SERIES:
        reasons.append(f"more_than_{MAX_COMPARISON_SERIES}_series")
    if any(not row.get("eligible") for row in rows):
        reasons.append("ineligible_series_selected")
    group_ids = {row.get("group_id") for row in rows if row.get("group_id")}
    if len(group_ids) != 1 or any(not row.get("group_id") for row in rows):
        reasons.append("comparison_group_mismatch")
    series_ids = [row.get("series_id") for row in rows]
    if any(not value for value in series_ids):
        reasons.append("series_id_missing")
    elif len(series_ids) != len(set(series_ids)):
        reasons.append("duplicate_series")
    subjects = [row.get("subject_id") for row in rows]
    if any(not value for value in subjects):
        reasons.append("comparison_subject_missing")
    elif len(subjects) != len(set(subjects)):
        reasons.append("duplicate_subject")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "selected_series": len(rows),
        "maximum_series": MAX_COMPARISON_SERIES,
        "group_id": next(iter(group_ids)) if len(group_ids) == 1 else None,
    }


def _asset_comparison_decision(*, unique_asset_mapping: bool,
                               comparison_eligible_series: int) -> dict:
    if not unique_asset_mapping:
        return {"eligible": False,
                "reason": "filing_entity_maps_to_multiple_asset_rows"}
    if not comparison_eligible_series:
        return {"eligible": False, "reason": "no_comparison_eligible_series"}
    return {"eligible": True, "reason": None}


def _display(row: dict) -> dict:
    metric = BY_ID.get(row.get("metric_id"))
    scale = metric.display_scale if metric else 1.0
    display_unit = (metric.display_unit if metric else None) or row.get("unit")
    numeric = row.get("value_num")
    return {
        "as_filed": row.get("value_text"),
        "numeric": numeric,
        "normalized_iso": row.get("normalized_iso") or None,
        "unit": row.get("unit"),
        "display_value": None if numeric is None else float(numeric) * scale,
        "display_unit": display_unit,
        "display_scale": scale,
    }


def _quality(row: dict) -> dict:
    validation = row.get("validation")
    flags = row.get("qa_flags") or ""
    if row.get("value_num") is not None and _UNDECODABLE.search(str(row.get("scope") or "")):
        validation = Validation.BLOCKED_AMBIGUITY
        warning = ("scope_undecodable: retained as filed but excluded from comparison "
                   "because the source font did not yield readable scope text")
        flags = f"{warning}; {flags}".strip("; ")
    return {
        "availability": row.get("availability"),
        "origin": row.get("origin"),
        "method": row.get("method"),
        "version_status": row.get("version_status"),
        "validation": validation,
        "qa_flags": flags or None,
        "review_status": row.get("review_status") or None,
        "missing_reason": row.get("missing_reason") or None,
        "applicability_evidence": row.get("applicability_evidence") or None,
    }


def _filing_ref(source_system: str | None, filing_id: str | None) -> str | None:
    if not source_system or not filing_id:
        return None
    return _stable_id(
        "filing-occurrence-v1", str(source_system), str(filing_id))


def _document_assertion_catalog(rows: list[dict]) -> tuple[dict, dict]:
    """Return compact assertion records and occurrence-aware lookup buckets."""
    catalog = {}
    lookup = defaultdict(set)
    for row in rows:
        assertion_id = row.get("document_fact_id")
        if not assertion_id:
            continue
        catalog[assertion_id] = {k: row.get(k) for k in (
            "document_id", "source_system", "filing_id", "source_fact_id",
            "entity_key", "assertion_type", "metric_id", "value_text",
            "value_num", "unit", "qualifier", "scope_note", "page",
            "paragraph", "char_start", "char_end", "verbatim_span",
            "extraction_method", "content_hash", "confidence", "review_state",
            "reviewer_note", "first_seen_at")}
        common = (row.get("entity_key"), row.get("metric_id"))
        if row.get("document_id"):
            lookup[("document", row.get("document_id"), *common)].add(assertion_id)
        if row.get("source_system") and row.get("filing_id"):
            lookup[("filing", row.get("source_system"), row.get("filing_id"),
                    *common)].add(assertion_id)
        if (row.get("source_system") and row.get("filing_id")
                and row.get("source_fact_id")):
            lookup[("fact", row.get("source_system"), row.get("filing_id"),
                    row.get("source_fact_id"), *common)].add(assertion_id)
    return catalog, lookup


def _document_assertion_refs(row: dict, lookup: dict) -> list[str]:
    """Resolve assertion references from the narrowest supported identity.

    A source fact identifies a particular fact occurrence, and a document ID
    identifies a particular attachment.  Falling through to the filing-level
    bucket is useful only when neither narrower identity has a matching
    assertion; unioning all three buckets can attach a sibling document's
    operative passage to the wrong observation.
    """
    common = (row.get("entity_key"), row.get("metric_id"))
    if (row.get("source_system") and row.get("filing_id")
            and row.get("source_fact_id")):
        refs = lookup.get(("fact", row.get("source_system"),
                           row.get("filing_id"), row.get("source_fact_id"),
                           *common), ())
        if refs:
            return sorted(refs)
    if row.get("document_id"):
        refs = lookup.get(("document", row.get("document_id"), *common), ())
        if refs:
            return sorted(refs)
    if row.get("source_system") and row.get("filing_id"):
        refs = lookup.get(("filing", row.get("source_system"),
                           row.get("filing_id"), *common), ())
        if refs:
            return sorted(refs)
    return []


def _source_index_payload(filings: list[dict], documents: list[dict],
                          assertions: dict, *, as_of=None, run_id=None) -> dict:
    filing_index = {}
    for row in filings:
        ref = _filing_ref(row.get("source_system"), row.get("filing_id"))
        if ref:
            filing_index[ref] = {k: row.get(k) for k in (
                "source_system", "filing_id", "entity_key", "form",
                "accession_number", "reporting_year", "reporting_period",
                "period_start", "period_end", "filed_date", "posted_date",
                "issued_date", "effective_date", "submitted_on", "snapshot_date",
                "acceptance_status", "taxonomy_version", "content_hash",
                "is_canonical", "canonical_reason", "version_status",
                "supersedes_filing_id", "data_origin", "retrieved_at",
                "first_seen_at", "source_url")}
    document_index = {}
    for row in documents:
        document_id = row.get("document_id")
        if not document_id:
            continue
        document_index[document_id] = {k: row.get(k) for k in (
            "source_system", "filing_id", "accession_number", "attachment_id",
            "title", "class_type", "media_type", "byte_size", "content_hash",
            "text_layer", "availability", "retrieved_at", "source_url")}
        document_index[document_id]["filing_ref"] = _filing_ref(
            row.get("source_system"), row.get("filing_id"))
    return {
        "schema": f"{SCHEMA}_source_index",
        "contract_version": CONTRACT_VERSION,
        "as_of": as_of,
        "run_id": run_id,
        "filing_identity": "source_system plus native filing_id; filing_ref is deterministic",
        "filings": filing_index,
        "documents": document_index,
        "document_assertions": assertions,
        "counts": {
            "filings": len(filing_index),
            "documents": len(document_index),
            "document_assertions": len(assertions),
        },
    }


def _enrich_event_source(event: dict, filings: dict, documents: dict) -> dict:
    """Attach a resolvable official-FERC link while preserving raw event keys."""
    item = dict(event)
    document = documents.get(item.get("document_id")) or {}
    filing = filings.get((item.get("source_system"), item.get("filing_id"))) or {}
    document_url = document.get("source_url")
    filing_url = filing.get("source_url")
    item["source_url"] = document_url or filing_url
    item["source_resolution_status"] = (
        "resolved" if item["source_url"] else "unresolved")
    item["source"] = {
        "system": item.get("source_system"),
        "filing_id": item.get("filing_id"),
        "filing_ref": _filing_ref(item.get("source_system"), item.get("filing_id")),
        "document_id": item.get("document_id"),
        "accession_number": item.get("accession_number"),
        "filing_url": filing_url,
        "document_url": document_url,
        "resolved_url": document_url or filing_url,
    }
    return item


def _observation_payload(row: dict, *, template: str,
                         unique_asset_mapping: bool, edges: list[dict],
                         populations: list[dict], versions: list[dict],
                         document_assertion_refs: list[str] | None = None) -> dict:
    quality = _quality(row)
    comparison_row = dict(row)
    comparison_row["validation"] = quality["validation"]
    edge_sample = [{k: edge.get(k) for k in (
        "input_order", "input_role", "operator_sign", "coefficient",
        "input_source_system", "input_filing_id", "input_source_fact_id",
        "input_observation_id", "input_context_id", "input_concept",
        "input_period", "input_value", "input_unit", "input_version_status",
        "input_population_id")} for edge in edges[:8]]
    population_sample = [{
        "population_id": population.get("population_id"),
        "source_system": population.get("source_system"),
        "source_table": population.get("source_table"),
        "filing_ids": _json_value(population.get("filing_ids")),
        "row_count": population.get("row_count"),
        "candidate_count": population.get("candidate_count"),
        "excluded_count": population.get("excluded_count"),
        "member_digest": population.get("member_digest"),
        "aggregate_value": population.get("aggregate_value"),
        "aggregate_unit": population.get("aggregate_unit"),
        "empty_reason": population.get("empty_reason"),
    } for population in populations[:4]]
    return {
        "observation_id": row.get("observation_id"),
        "entity_key": row.get("entity_key"),
        "metric_id": row.get("metric_id"),
        "source_regime": row.get("source_regime"),
        "period": {
            "basis": row.get("period_basis"),
            "start": row.get("period_start"),
            "end": row.get("period_end"),
            "instant": row.get("instant_date"),
            "reporting_year": row.get("reporting_year"),
            "reporting_period": row.get("reporting_period"),
            "label": row.get("period_label"),
        },
        "scope": {
            "actual": row.get("scope"),
            "contract_rule": row.get("scope_rule") or None,
            "resolved": actual_scope_resolved(row.get("scope")),
        },
        "value": _display(row),
        "quality": quality,
        "comparison": comparison_decision(
            comparison_row, template=template,
            unique_asset_mapping=unique_asset_mapping),
        "source": {
            "system": row.get("source_system"),
            "filing_id": row.get("filing_id"),
            "filing_ref": _filing_ref(row.get("source_system"), row.get("filing_id")),
            "source_fact_id": row.get("source_fact_id"),
            "context_id": row.get("source_context_id"),
            "document_id": row.get("document_id"),
            "document_assertion_refs": sorted(document_assertion_refs or []),
            "accession_number": row.get("accession_number"),
            "selector": row.get("selector"),
            "concept_local": row.get("concept_local"),
            "concept_qname": row.get("concept_qname"),
            "taxonomy_version": row.get("taxonomy_version"),
            "schedule_page": row.get("schedule_page"),
            "filing": {
                "form": row.get("filing_form"),
                "filed_date": row.get("filing_filed_date"),
                "posted_date": row.get("filing_posted_date"),
                "issued_date": row.get("filing_issued_date"),
                "effective_date": row.get("filing_effective_date"),
                "submitted_on": row.get("filing_submitted_on"),
                "retrieved_at": row.get("filing_retrieved_at"),
                "first_seen_at": row.get("filing_first_seen_at"),
                "source_url": row.get("filing_source_url"),
                "canonical": bool(row.get("filing_is_canonical"))
                    if row.get("filing_is_canonical") is not None else None,
            },
            "document": {
                "title": row.get("document_title"),
                "media_type": row.get("document_media_type"),
                "availability": row.get("document_availability"),
                "retrieved_at": row.get("document_retrieved_at"),
                "source_url": row.get("document_source_url"),
            } if row.get("document_id") else None,
            "fact": {
                "value_as_filed": row.get("source_value_as_filed"),
                "is_nil": bool(row.get("source_is_nil"))
                    if row.get("source_is_nil") is not None else None,
                "unit_text": row.get("source_unit_text"),
                "decimals": row.get("source_decimals"),
                "precision": row.get("source_precision"),
                "period_class": row.get("source_period_class"),
                "period_start": row.get("source_period_start"),
                "period_end": row.get("source_period_end"),
                "instant": row.get("source_instant"),
                "explicit_dimensions": _json_value(row.get("source_explicit_dims")),
                "typed_dimensions": _json_value(row.get("source_typed_dims")),
            } if row.get("source_fact_id") else None,
        },
        "lineage": {
            "derivation": row.get("derivation") or None,
            "edge_count": len(edges),
            "population_count": len(populations),
            "edge_sample": edge_sample,
            "edge_sample_complete": len(edges) <= len(edge_sample),
            "population_sample": population_sample,
            "population_sample_complete": len(populations) <= len(population_sample),
            "edges_export": "../../lineage_edges.csv",
            "populations_export": "../../lineage_populations.csv",
        },
        "archived_versions": versions,
        "dates": {
            "source_reporting_start": row.get("period_start"),
            "source_reporting_end": row.get("period_end"),
            "source_reporting_instant": row.get("instant_date"),
            "source_filed": row.get("filing_filed_date"),
            "source_posted": row.get("filing_posted_date"),
            "source_issued": row.get("filing_issued_date"),
            "regulatory_effective": row.get("filing_effective_date"),
            "retrieved": row.get("filing_retrieved_at")
                or row.get("document_retrieved_at"),
            "first_seen": row.get("first_seen_at")
                or row.get("filing_first_seen_at"),
        },
        "notes": row.get("notes") or None,
    }


def _headline_availability(ctx, observations: list[dict]) -> dict:
    expected = _rows(ctx, "SELECT * FROM coverage_expected ORDER BY slot_id")
    measured = _rows(ctx, "SELECT * FROM coverage_measured ORDER BY slot_id")
    stats = cov.summarise(expected, measured)
    measured_by_slot = {row["slot_id"]: row for row in measured}

    template_rows = cov.group_summary(expected, measured, "template")
    metric_rows = cov.group_summary(expected, measured, "metric_id")
    entity_template = {}
    for row in _rows(ctx, "SELECT m.entity_key,a.template FROM asset_entity_map m "
                          "JOIN assets a USING(asset_id) ORDER BY m.entity_key,a.asset_id"):
        entity_template.setdefault(row["entity_key"], row["template"])
    entity_template.setdefault("FERC-OIL-INDEX", "liquids")

    obs_by_template: dict[str, Counter] = defaultdict(Counter)
    periods: dict[str, list[str]] = defaultdict(list)
    metric_availability: dict[tuple[str, str], Counter] = defaultdict(Counter)
    metric_entities: dict[tuple[str, str], set[str]] = defaultdict(set)
    metric_periods: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in observations:
        template = entity_template.get(row.get("entity_key"), "unmapped")
        key = (template, row.get("metric_id") or "")
        obs_by_template[template]["observations"] += 1
        metric_availability[key][f"availability:{row.get('availability')}"] += 1
        metric_availability[key][f"validation:{row.get('validation')}"] += 1
        metric_entities[key].add(row.get("entity_key"))
        stamp = row.get("instant_date") or row.get("period_end") or row.get("period_start")
        if stamp:
            periods[template].append(stamp)
            metric_periods[key].add(stamp)
        if row.get("availability") == Availability.PRESENT:
            obs_by_template[template]["present"] += 1
            if row.get("validation") == Validation.PASS \
                    and row.get("version_status") != "superseded":
                obs_by_template[template]["usable"] += 1
                metric_availability[key]["usable"] += 1
            elif row.get("validation") in Validation.MUST_PROPAGATE:
                obs_by_template[template]["review"] += 1
                metric_availability[key]["review"] += 1

    unmatched = Counter()
    refusal = Counter()
    for slot in expected:
        if slot.get("requirement") not in _CORE:
            continue
        result = measured_by_slot.get(slot["slot_id"], {})
        outcome = result.get("outcome", "unmeasured")
        if outcome not in ("populated_validated", "populated_review", "not_yet_due",
                           "not_applicable"):
            unmatched[(slot.get("template"), slot.get("source_regime"),
                       slot.get("metric_id"), outcome,
                       (result.get("reason") or "")[:240])] += 1
        if result.get("candidates_refused"):
            refusal[result.get("refusal_gates") or "unspecified"] += int(
                result.get("candidates_refused") or 0)

    metric_detail = []
    grouped_metric = {row["group"]: row for row in metric_rows}
    for (template, metric_id), counts in sorted(metric_availability.items()):
        base = grouped_metric.get(metric_id, {})
        stamps = sorted(metric_periods[(template, metric_id)])
        metric_detail.append({
            "template": template,
            "metric_id": metric_id,
            "observation_entities": len(metric_entities[(template, metric_id)]),
            "observation_periods": len(stamps),
            "history_from": stamps[0] if stamps else None,
            "history_to": stamps[-1] if stamps else None,
            "usable_observations": counts.get("usable", 0),
            "review_observations": counts.get("review", 0),
            "availability": {k.removeprefix("availability:"): v for k, v in counts.items()
                             if k.startswith("availability:")},
            "coverage_all_templates": base,
        })

    template_detail = []
    for base in template_rows:
        template = base["group"]
        counts = obs_by_template.get(template, Counter())
        stamps = sorted(periods.get(template, []))
        template_detail.append({
            **base,
            "assets": len({r["asset_id"] for r in _rows(
                ctx, "SELECT asset_id FROM assets WHERE template=?", (template,))}),
            "entities": len({r["entity_key"] for r in _rows(
                ctx, "SELECT DISTINCT m.entity_key FROM asset_entity_map m "
                     "JOIN assets a USING(asset_id) WHERE a.template=?", (template,))}),
            "observations": counts.get("observations", 0),
            "present_observations": counts.get("present", 0),
            "usable_observations": counts.get("usable", 0),
            "review_observations": counts.get("review", 0),
            "history_from": stamps[0] if stamps else None,
            "history_to": stamps[-1] if stamps else None,
        })

    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of": getattr(ctx, "as_of_iso", None),
        "coverage": stats,
        "templates": template_detail,
        "metrics": metric_detail,
        "largest_unmatched_core_buckets": [
            {"template": key[0], "source_regime": key[1], "metric_id": key[2],
             "outcome": key[3], "reason": key[4], "slots": count}
            for key, count in unmatched.most_common(75)
        ],
        "refused_candidate_gates": dict(refusal.most_common()),
        "definitions": {
            "usable_observation": ("availability=present, validation=pass, and the "
                                   "occurrence is not superseded"),
            "core_slot": "REQUIRED or CONDITIONALLY_REQUIRED coverage obligation",
            "due_to_date": "core slot whose stored slot_state is not future_not_yet_due",
            "review": "present observation carrying a must-propagate validation state",
        },
    }


def _contract() -> dict:
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "integration_mode": "read_only_export_adapter; never open the frontend app database",
        "files": {
            "asset_directory": "assets.json",
            "asset_routes": "asset_payloads/<asset_id>.json",
            "entity_history": "entity_payloads/<stable-safe-entity-file>.json",
            "instruments": "instruments.json",
            "headline_availability": "headline_availability.json",
            "source_index": "source_index.json",
        },
        # These files live one directory above contract.json and are mandatory
        # dependencies whenever a consumer requests complete, rather than
        # sampled, lineage or the machine-readable registry contract.
        "external_csv_dependencies": {
            "metric_registry": "../metric_registry.csv",
            "filing_inventory": "../filing_inventory.csv",
            "documents": "../documents.csv",
            "document_facts": "../document_facts.csv",
            "lineage_edges": "../lineage_edges.csv",
            "lineage_populations": "../lineage_populations.csv",
            "observation_versions": "../observation_versions.csv",
            "reviewed_source_annotations": "../reviewed_source_annotations.csv",
            "source_manifest": "../source_manifest.csv",
        },
        "comparison_identity": {
            "version": COMPARISON_ID_VERSION,
            "maximum_selected_series": MAX_COMPARISON_SERIES,
            "group": ("same template, registered metric, period basis, base unit family "
                      "and registry scope contract"),
            "series": ("group plus filing-entity subject and actual filed scope; actual scope "
                       "is never replaced by the registry rule"),
            "value": ("comparison_value_base equals numeric value multiplied by the stored "
                      "unit's declared scale to its dimensional-family base"),
        },
        "frontend_mapping": [
            {"requirement": "company and asset directory", "existing_frontend":
             "AssetView/id,ticker,displayName,cid", "backend":
             "assets.json stable slugs plus explicit entity mappings", "status": "mapped"},
            {"requirement": "single-asset history", "existing_frontend":
             "CID-level factsSeries", "backend":
             "asset route -> entity history with actual scope and shared-entity warning",
             "status": "mapped_with_scope_gate"},
            {"requirement": "same-type comparison", "existing_frontend":
             "chart primitives; no selection route or four-item cap", "backend":
             "stable comparison group/series IDs, base values and a tested max-four control; "
             "UI selection remains future work",
             "status": "data_contract_ready_ui_not_implemented"},
            {"requirement": "periods and units", "existing_frontend":
             "period/year/unit; UI may infer unit from concept", "backend":
             "exact period grain, stored unit, display unit/scale", "status": "mapped"},
            {"requirement": "scope and missing/review states", "existing_frontend":
             "not represented", "backend":
             "actual scope, registry contract, five quality dimensions and warnings",
             "status": "new_adapter_fields_required"},
            {"requirement": "source detail and dates", "existing_frontend":
             "filing accession links only", "backend":
             "filing/fact/document identity and reporting/filed/posted/issued/effective/"
             "retrieved/first-seen dates plus compact source and operative-assertion index",
             "status": "new_adapter_fields_required"},
            {"requirement": "derivation inputs", "existing_frontend":
             "not represented", "backend":
             "lineage edges and verifiable population descriptors per observation",
             "status": "new_source_detail_component_required"},
            {"requirement": "related project links", "existing_frontend":
             "groupKey, entity and docket mappings", "backend":
             "related asset context is exported; no reviewed pre-COD crosswalk is supplied",
             "status": "genuine_gap"},
            {"requirement": "change feed", "existing_frontend":
             "company-filtered filing feed", "backend":
             "entity payload events retain destination/backfill/date/source distinctions",
             "status": "mapped_adapter_required"},
        ],
        "consumer_rules": {
            "status": "quality object must travel with every displayed value",
            "unit": ("render display_value/display_unit; for comparisons use "
                     "comparison_value_base/base_unit_family; never infer scale"),
            "scope": "do not label entity-level data as facility-specific",
            "comparison": ("select two to four distinct subjects; every series must be "
                           "eligible and share exact comparison.group_id"),
            "shared_entity": ("asset payloads marked shared_filer_entity_context provide entity "
                              "context only and are not asset-comparison eligible"),
            "dates": "reporting, filing, effective and retrieval dates are not interchangeable",
            "source": ("retain source-system, occurrence, fact/document and URL together; "
                       "resolve source/document assertion refs through source_index.json and "
                       "use the declared CSV dependencies for complete lineage"),
        },
        "deliberate_limits": [
            "No frontend database migration, API route or connection is included.",
            "The current frontend has no user-selected up-to-four comparison workflow.",
            ("Entity histories remain monolithic compatibility payloads; API pagination or "
             "history sharding is not included in this contract revision."),
            "Entity-level FERC totals are not allocated to multiple asset rows.",
            "Related-project mapping status is not_supplied; group/entity links are not relabelled as projects.",
            "Unavailable or ambiguous FERC evidence remains explicit rather than imputed.",
        ],
    }


def _resolve_export_member(base_name: str, target: str) -> str:
    """Resolve a POSIX path as a consumer would, without escaping export root."""
    if not isinstance(target, str) or not target or "\\" in target:
        raise FrontendContractViolation(f"unsafe or empty consumer path: {target!r}")
    relative = pathlib.PurePosixPath(target)
    if relative.is_absolute():
        raise FrontendContractViolation(f"absolute consumer path: {target!r}")
    parts = list(pathlib.PurePosixPath(base_name).parent.parts)
    for part in relative.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise FrontendContractViolation(
                    f"consumer path escapes export root: {base_name!r} -> {target!r}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise FrontendContractViolation(f"consumer path resolves to export root: {target!r}")
    return "/".join(parts)


def _json_export_member(files: Mapping[str, bytes], name: str):
    if name not in files:
        raise FrontendContractViolation(f"required consumer member is absent: {name}")
    body = files[name]
    if not isinstance(body, (bytes, bytearray)):
        raise FrontendContractViolation(f"consumer member is not bytes: {name}")
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontendContractViolation(
            f"consumer JSON is invalid: {name}: {type(exc).__name__}: {exc}") from None


def validate_frontend_contract_files(files: Mapping[str, bytes]) -> dict:
    """Validate every detached frontend route and declared dependency.

    This is deliberately a generation-level check.  Green unit tests cannot
    compensate for a missing dynamic payload, a stale compatibility route, or
    a contract that points outside the published set.
    """
    unsafe = sorted(name for name in files
                    if not isinstance(name, str)
                    or pathlib.PurePosixPath(name).is_absolute()
                    or ".." in pathlib.PurePosixPath(name).parts
                    or "\\" in name)
    if unsafe:
        raise FrontendContractViolation(f"unsafe export member name(s): {unsafe[:10]}")

    prefix = "frontend_v1/"
    contract_name = prefix + "contract.json"
    contract = _json_export_member(files, contract_name)
    if contract.get("schema") != SCHEMA or contract.get("contract_version") != CONTRACT_VERSION:
        raise FrontendContractViolation("frontend contract schema/version is not current")

    declarations = contract.get("files")
    if not isinstance(declarations, dict):
        raise FrontendContractViolation("frontend contract has no files declaration")
    fixed_declarations = {
        "asset_directory": "assets.json",
        "instruments": "instruments.json",
        "headline_availability": "headline_availability.json",
        "source_index": "source_index.json",
    }
    for key, expected in fixed_declarations.items():
        if declarations.get(key) != expected:
            raise FrontendContractViolation(
                f"frontend contract declaration changed unexpectedly: {key}")
    if declarations.get("asset_routes") != "asset_payloads/<asset_id>.json" \
            or declarations.get("entity_history") \
            != "entity_payloads/<stable-safe-entity-file>.json":
        raise FrontendContractViolation("frontend dynamic route declaration is invalid")

    fixed_frontend = {contract_name}
    for target in fixed_declarations.values():
        name = _resolve_export_member(contract_name, target)
        fixed_frontend.add(name)
        if name not in files:
            raise FrontendContractViolation(f"declared frontend member is absent: {name}")

    dependencies = contract.get("external_csv_dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        raise FrontendContractViolation("frontend contract has no external dependencies")
    dependency_members = set()
    for label, target in dependencies.items():
        name = _resolve_export_member(contract_name, target)
        if not name.endswith(".csv") or name.startswith(prefix):
            raise FrontendContractViolation(
                f"invalid external CSV dependency {label!r}: {target!r}")
        if name not in files:
            raise FrontendContractViolation(
                f"declared external dependency is absent: {label}: {name}")
        dependency_members.add(name)

    directory_name = prefix + "assets.json"
    directory = _json_export_member(files, directory_name)
    instruments = _json_export_member(files, prefix + "instruments.json")
    headline = _json_export_member(files, prefix + "headline_availability.json")
    source_index = _json_export_member(files, prefix + "source_index.json")
    if directory.get("schema") != SCHEMA \
            or directory.get("contract_version") != CONTRACT_VERSION:
        raise FrontendContractViolation("asset directory schema/version is not current")
    if not isinstance(instruments, list):
        raise FrontendContractViolation("instruments.json is not a list")
    if directory.get("instruments") != instruments:
        raise FrontendContractViolation(
            "assets.json instruments differ from instruments.json")
    if not isinstance(headline, dict):
        raise FrontendContractViolation("headline availability is not an object")

    assets = directory.get("assets")
    if not isinstance(assets, list):
        raise FrontendContractViolation("asset directory assets is not a list")
    asset_ids = [row.get("id") for row in assets if isinstance(row, dict)]
    if len(asset_ids) != len(assets) or any(not value for value in asset_ids) \
            or len(set(asset_ids)) != len(asset_ids):
        raise FrontendContractViolation("asset IDs are empty, malformed or duplicated")
    counts = directory.get("counts") or {}
    if counts.get("assets") != len(assets):
        raise FrontendContractViolation("asset directory count disagrees with rows")

    expected_asset_files: set[str] = set()
    expected_entity_files: set[str] = set()
    entity_keys_by_file: dict[str, str] = {}

    def register_entity(mapping: dict, base: str) -> str:
        if not isinstance(mapping, dict):
            raise FrontendContractViolation("entity mapping is not an object")
        entity_key = mapping.get("entity_key")
        target = mapping.get("entity_payload_path")
        if not entity_key or not target:
            raise FrontendContractViolation("entity mapping lacks identity or path")
        name = _resolve_export_member(base, target)
        if not name.startswith(prefix + "entity_payloads/") or not name.endswith(".json"):
            raise FrontendContractViolation(f"entity payload path is outside route root: {name}")
        prior = entity_keys_by_file.setdefault(name, entity_key)
        if prior != entity_key:
            raise FrontendContractViolation(
                f"two entity identities share payload route {name}: {prior}, {entity_key}")
        expected_entity_files.add(name)
        return name

    for asset in assets:
        detail_name = _resolve_export_member(directory_name, asset.get("detailPath"))
        if not detail_name.startswith(prefix + "asset_payloads/") \
                or not detail_name.endswith(".json"):
            raise FrontendContractViolation(
                f"asset detail path is outside route root: {detail_name}")
        if detail_name in expected_asset_files:
            raise FrontendContractViolation(f"duplicate asset detail route: {detail_name}")
        expected_asset_files.add(detail_name)
        root_entities = {
            register_entity(mapping, directory_name)
            for mapping in asset.get("entityMappings", [])
        }
        detail = _json_export_member(files, detail_name)
        detail_asset = detail.get("asset") or {}
        if detail_asset.get("id") != asset.get("id"):
            raise FrontendContractViolation(f"asset detail identity mismatch: {detail_name}")
        detail_entities = {
            register_entity(mapping, detail_name)
            for mapping in detail_asset.get("entityMappings", [])
        }
        listed_entities = {
            _resolve_export_member(detail_name, target)
            for target in detail.get("entity_payloads", [])
        }
        if root_entities != detail_entities or detail_entities != listed_entities:
            raise FrontendContractViolation(
                f"asset detail entity routes disagree: {detail_name}")
        if _resolve_export_member(detail_name, detail.get("source_index_path")) \
                != prefix + "source_index.json":
            raise FrontendContractViolation(
                f"asset detail source index route is invalid: {detail_name}")

    for instrument in instruments:
        register_entity(instrument, directory_name)

    for name in sorted(expected_entity_files):
        payload = _json_export_member(files, name)
        entity = payload.get("entity") or {}
        if entity.get("entity_key") != entity_keys_by_file[name]:
            raise FrontendContractViolation(f"entity payload identity mismatch: {name}")
        if _resolve_export_member(name, payload.get("source_index_path")) \
                != prefix + "source_index.json":
            raise FrontendContractViolation(f"entity source index route is invalid: {name}")

    expected_frontend = fixed_frontend | expected_asset_files | expected_entity_files
    actual_frontend = {name for name in files if name.startswith(prefix)}
    if actual_frontend != expected_frontend:
        missing = sorted(expected_frontend - actual_frontend)
        extra = sorted(actual_frontend - expected_frontend)
        raise FrontendContractViolation(
            f"frontend route closure mismatch; missing={missing[:10]} extra={extra[:10]}")

    source_counts = source_index.get("counts") or {}
    for label in ("filings", "documents", "document_assertions"):
        rows = source_index.get(label)
        if not isinstance(rows, dict) or source_counts.get(label) != len(rows):
            raise FrontendContractViolation(
                f"source index count disagrees for {label}")
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "frontend_files": len(expected_frontend),
        "asset_payloads": len(expected_asset_files),
        "entity_payloads": len(expected_entity_files),
        "external_csv_dependencies": len(dependency_members),
        "source_filings": source_counts["filings"],
        "source_documents": source_counts["documents"],
        "source_document_assertions": source_counts["document_assertions"],
    }


def write_frontend_contract(ctx, out: pathlib.Path,
                            universe_path: pathlib.Path) -> dict:
    """Write all `frontend_v1/` files inside the staged export directory."""
    root = pathlib.Path(out) / "frontend_v1"
    universe = _load_universe(universe_path)
    assets_raw = _rows(ctx, """
        SELECT a.*,m.entity_key,m.mapping_scope,m.effective_from,m.effective_to,
               m.note AS mapping_note,e.legal_name,e.cid,e.local_key,e.parent,
               e.jurisdiction
        FROM assets a JOIN asset_entity_map m USING(asset_id)
        JOIN entities e USING(entity_key)
        ORDER BY a.asset_id,m.entity_key""")
    dockets = defaultdict(list)
    for row in _rows(ctx, "SELECT * FROM asset_dockets ORDER BY asset_id,docket,role"):
        dockets[row["asset_id"]].append({k: row.get(k) for k in
                                         ("docket", "role", "evidence_ref")})
    ownership = defaultdict(list)
    for row in _rows(ctx, "SELECT * FROM ownership ORDER BY entity_key,parent"):
        ownership[row["entity_key"]].append({k: row.get(k) for k in
            ("parent", "ticker", "pct", "basis", "qualifier", "effective_from",
             "effective_to")})

    entity_assets = defaultdict(list)
    asset_rows = defaultdict(list)
    for row in assets_raw:
        entity_assets[row["entity_key"]].append(row["asset_id"])
        asset_rows[row["asset_id"]].append(row)

    filing_rows = _rows(ctx, "SELECT * FROM filings ORDER BY source_system,filing_id")
    filings_by_key = {(row.get("source_system"), row.get("filing_id")): row
                      for row in filing_rows}
    document_rows = _rows(ctx, "SELECT * FROM documents ORDER BY document_id")
    documents_by_id = {row.get("document_id"): row for row in document_rows
                       if row.get("document_id")}
    document_fact_rows = _rows(
        ctx, "SELECT * FROM document_facts ORDER BY document_fact_id")
    assertion_catalog, assertion_lookup = _document_assertion_catalog(document_fact_rows)

    filings_summary = {row["entity_key"]: row for row in _rows(ctx, """
        SELECT entity_key,COUNT(*) AS filings,MAX(filed_date) AS last_filed
        FROM filings GROUP BY entity_key""")}
    forms = defaultdict(list)
    for row in _rows(ctx, "SELECT DISTINCT entity_key,form FROM filings ORDER BY entity_key,form"):
        forms[row["entity_key"]].append(row["form"])

    obs_rows = _rows(ctx, """
        SELECT o.*,
               f.form AS filing_form,f.filed_date AS filing_filed_date,
               f.posted_date AS filing_posted_date,f.issued_date AS filing_issued_date,
               f.effective_date AS filing_effective_date,
               f.submitted_on AS filing_submitted_on,
               f.retrieved_at AS filing_retrieved_at,
               f.first_seen_at AS filing_first_seen_at,
               f.source_url AS filing_source_url,f.is_canonical AS filing_is_canonical,
               d.title AS document_title,d.media_type AS document_media_type,
               d.availability AS document_availability,
               d.retrieved_at AS document_retrieved_at,
               d.source_url AS document_source_url,
               sf.value_as_filed AS source_value_as_filed,sf.is_nil AS source_is_nil,
               sf.unit_text AS source_unit_text,sf.decimals AS source_decimals,
               sf.precision AS source_precision,sf.period_class AS source_period_class,
               sf.period_start AS source_period_start,sf.period_end AS source_period_end,
               sf.instant AS source_instant,
               sf.explicit_dims_json AS source_explicit_dims,
               sf.typed_dims_json AS source_typed_dims
        FROM observations o
        LEFT JOIN filings f ON f.source_system=o.source_system AND f.filing_id=o.filing_id
        LEFT JOIN documents d ON d.document_id=o.document_id
        LEFT JOIN source_facts sf ON sf.source_system=o.source_system
          AND sf.filing_id=o.filing_id AND sf.source_fact_id=o.source_fact_id
        ORDER BY o.entity_key,o.metric_id,o.reporting_year,o.reporting_period,
                 o.period_start,o.period_end,o.instant_date,o.observation_id""")
    by_entity_obs = defaultdict(list)
    for row in obs_rows:
        by_entity_obs[row["entity_key"]].append(row)

    edges = defaultdict(list)
    for row in _rows(ctx, """
        SELECT l.*,o.entity_key FROM lineage_edges l
        JOIN observations o USING(observation_id)
        ORDER BY o.entity_key,l.observation_id,l.input_order"""):
        edges[row["observation_id"]].append(row)
    populations = defaultdict(list)
    for row in _rows(ctx, """
        SELECT p.*,o.entity_key FROM lineage_populations p
        JOIN observations o USING(observation_id)
        ORDER BY o.entity_key,p.observation_id,p.population_id"""):
        populations[row["observation_id"]].append(row)
    events = defaultdict(list)
    for row in _rows(ctx, "SELECT * FROM events ORDER BY entity_key,event_id"):
        item = _enrich_event_source(dict(row), filings_by_key, documents_by_id)
        item["asset_ids"] = _json_value(item.get("asset_ids"))
        events[row.get("entity_key")].append(item)
    annotations = defaultdict(list)
    for row in _rows(ctx, "SELECT * FROM reviewed_source_annotations "
                          "ORDER BY entity_key,filing_id,source_fact_id,metric_id"):
        annotations[row["entity_key"]].append(row)
    versions = defaultdict(list)
    for row in _rows(ctx, "SELECT * FROM observation_versions "
                          "ORDER BY observation_id,version_seq"):
        item = dict(row)
        item["row"] = _json_value(item.pop("row_json", None))
        versions[row["observation_id"]].append(item)

    template_by_entity = {}
    for row in assets_raw:
        template_by_entity.setdefault(row["entity_key"], row["template"])
    template_by_entity["FERC-OIL-INDEX"] = "liquids"
    all_entities = {row["entity_key"]: row for row in _rows(
        ctx, "SELECT * FROM entities ORDER BY entity_key")}
    entity_payloads = {}
    entity_summaries = {}
    written_bytes = 0
    for entity_key, entity in all_entities.items():
        template = template_by_entity.get(entity_key, "instrument")
        mapped_count = len(entity_assets.get(entity_key, []))
        unique = mapped_count <= 1
        payload_obs = [
            _observation_payload(row, template=template,
                                 unique_asset_mapping=unique,
                                 edges=edges.get(row["observation_id"], []),
                                 populations=populations.get(row["observation_id"], []),
                                 versions=versions.get(row["observation_id"], []),
                                 document_assertion_refs=_document_assertion_refs(
                                     row, assertion_lookup))
            for row in by_entity_obs.get(entity_key, [])
        ]
        summary = Counter()
        comparison_series = set()
        history_stamps = []
        for item in payload_obs:
            summary["observations"] += 1
            quality = item["quality"]
            if quality["availability"] == Availability.PRESENT:
                summary["present"] += 1
                if quality["validation"] == Validation.PASS \
                        and quality["version_status"] != "superseded":
                    summary["usable"] += 1
                elif quality["validation"] in Validation.MUST_PROPAGATE:
                    summary["review"] += 1
            if item["comparison"]["eligible"]:
                summary["comparison_eligible_observations"] += 1
                comparison_series.add(item["comparison"]["series_id"])
            stamp = (item["period"]["instant"] or item["period"]["end"]
                     or item["period"]["start"])
            if stamp:
                history_stamps.append(stamp)
        entity_summaries[entity_key] = {
            **dict(summary),
            "metrics": len({item["metric_id"] for item in payload_obs}),
            "comparison_eligible_series": len(comparison_series),
            "history_from": min(history_stamps) if history_stamps else None,
            "history_to": max(history_stamps) if history_stamps else None,
        }
        entity_file = _entity_filename(entity_key)
        entity_payloads[entity_key] = f"entity_payloads/{entity_file}"
        payload = {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "as_of": getattr(ctx, "as_of_iso", None),
            "run_id": getattr(ctx.staging, "run_id", None),
            "entity": entity,
            "asset_ids": sorted(entity_assets.get(entity_key, [])),
            "entity_scope_relation": ("regulatory_instrument" if mapped_count == 0 else
                                      "one_to_one_filing_entity" if unique else
                                      "shared_filer_entity_context"),
            "source_index_path": "../source_index.json",
            "observations": payload_obs,
            "events": events.get(entity_key, []),
            "reviewed_annotations": annotations.get(entity_key, []),
            "summary": entity_summaries[entity_key],
        }
        written_bytes += _write_json(root / "entity_payloads" / entity_file, payload)

    related = defaultdict(set)
    group_assets = defaultdict(list)
    for asset_id, rows in asset_rows.items():
        group = rows[0].get("group_key")
        if group:
            group_assets[group].append(asset_id)
    for ids in group_assets.values():
        for asset_id in ids:
            related[asset_id].update(other for other in ids if other != asset_id)
    for entity_key, ids in entity_assets.items():
        for asset_id in ids:
            related[asset_id].update(other for other in ids if other != asset_id)

    asset_directory = []
    for asset_id in sorted(asset_rows):
        mappings = asset_rows[asset_id]
        first = mappings[0]
        config = universe.get(asset_id, {})
        entity_key = first["entity_key"]
        unique = len(entity_assets[entity_key]) == 1 and len(mappings) == 1
        summary = entity_summaries.get(entity_key, {})
        capacity = _json_value(config.get("capacity"))
        reports_forms = _json_value(config.get("roster_forms")) or forms.get(entity_key, [])
        mapping_list = [{
            "entity_key": row["entity_key"],
            "legal_name": row["legal_name"],
            "cid": row["cid"],
            "mapping_scope": row["mapping_scope"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "note": row["mapping_note"],
            "entity_payload_path": entity_payloads[row["entity_key"]],
        } for row in mappings]
        status = ("live" if summary.get("usable", 0) else
                  "watch" if first.get("cod_group") == "pre-cod" else "no-data")
        asset_comparison = _asset_comparison_decision(
            unique_asset_mapping=unique,
            comparison_eligible_series=summary.get("comparison_eligible_series", 0))
        item = {
            # Direct compatibility aliases for the existing AssetView interface.
            "id": asset_id,
            "ticker": first.get("ticker"),
            "displayName": first.get("display_name"),
            "groupKey": first.get("group_key") or None,
            "authority": first.get("authority"),
            "status": first.get("status"),
            "statusNote": config.get("status_note") or None,
            "codGroup": first.get("cod_group"),
            "interestDisplay": config.get("interest_display") or None,
            "note": first.get("note") or config.get("note") or None,
            "entityName": first.get("legal_name"),
            "parent": first.get("parent"),
            "cid": first.get("cid"),
            "reportsForms": reports_forms,
            "interests": ownership.get(entity_key, []),
            "dockets": dockets.get(asset_id, []),
            "capacity": capacity,
            "filings": (filings_summary.get(entity_key) or {}).get("filings", 0),
            "lastFiled": (filings_summary.get(entity_key) or {}).get("last_filed"),
            "dataStatus": status,
            # Rich adapter fields.
            "inScope": True,
            "exclusionReason": None,
            "assetType": first.get("template"),
            "entityMappings": mapping_list,
            "scopeRelation": ("one_to_one_filing_entity" if unique
                              else "shared_filer_entity_context"),
            "comparisonEligible": asset_comparison["eligible"],
            "comparisonBlockedReason": asset_comparison["reason"],
            "dataSummary": summary,
            "relatedAssetIds": sorted(related.get(asset_id, set())),
            "relatedProjectIds": [],
            "relatedProjectMappingStatus": "not_supplied",
            "detailPath": f"asset_payloads/{asset_id}.json",
        }
        asset_directory.append(item)

    # Preserve all 120 reviewed roster identifiers. Eleven power/QF rows sit
    # outside the five operating templates; keep them explicit and empty rather
    # than silently shrinking the frontend directory or fabricating coverage.
    for asset_id, config in sorted(universe.items()):
        if asset_id in asset_rows:
            continue
        if config.get("template") != "OUT_OF_TEMPLATE":
            raise ValueError(f"in-scope universe asset was not seeded: {asset_id}")
        entity_key = config.get("entity_key") or ""
        asset_directory.append({
            "id": asset_id,
            "ticker": config.get("ticker") or None,
            "displayName": config.get("display_name") or asset_id,
            "groupKey": config.get("group_key") or None,
            "authority": config.get("authority") or None,
            "status": config.get("status") or None,
            "statusNote": config.get("status_note") or None,
            "codGroup": config.get("cod_group") or None,
            "interestDisplay": config.get("interest_display") or None,
            "note": config.get("note") or None,
            "entityName": config.get("entity_name") or None,
            "parent": config.get("parent") or None,
            "cid": entity_key if re.fullmatch(r"C\d{6}", entity_key) else None,
            "reportsForms": _json_value(config.get("roster_forms")) or [],
            "interests": [],
            "dockets": [],
            "capacity": _json_value(config.get("capacity")),
            "filings": 0,
            "lastFiled": None,
            "dataStatus": "no-data",
            "inScope": False,
            "exclusionReason": "outside the five operating-asset templates",
            "assetType": "OUT_OF_TEMPLATE",
            "entityMappings": [],
            "scopeRelation": "out_of_scope",
            "comparisonEligible": False,
            "comparisonBlockedReason": "asset_type_out_of_operating_templates",
            "dataSummary": {"observations": 0, "present": 0, "usable": 0,
                            "review": 0, "metrics": 0,
                            "comparison_eligible_observations": 0,
                            "comparison_eligible_series": 0,
                            "history_from": None, "history_to": None},
            "relatedAssetIds": [],
            "relatedProjectIds": [],
            "relatedProjectMappingStatus": "not_supplied",
            "detailPath": f"asset_payloads/{asset_id}.json",
        })
    asset_directory.sort(key=lambda row: row["id"])

    for item in asset_directory:
        # Paths in assets.json are relative to the frontend_v1 contract root.
        # The same mappings embedded one directory lower in an asset-detail
        # document must instead be relative to asset_payloads/, just like the
        # source-index reference below.
        detail_mappings = _asset_detail_mappings(item["entityMappings"])
        detail_item = {**item, "entityMappings": detail_mappings}
        payload = {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "as_of": getattr(ctx, "as_of_iso", None),
            "asset": detail_item,
            "entity_payloads": [m["entity_payload_path"]
                                for m in detail_mappings],
            "source_index_path": "../source_index.json",
            "scope_note": ("This roster row is outside the five operating templates and "
                           "carries no operating-pipeline facts." if not item["inScope"] else
                           "FERC observations are legal-entity context, not an allocation "
                           "to this asset row" if item["scopeRelation"] ==
                           "shared_filer_entity_context" else
                           "This asset has one filing-entity mapping; each observation still "
                           "carries its narrower filed scope where applicable."),
        }
        written_bytes += _write_json(root / item["detailPath"], payload)

    companies = []
    for ticker in sorted({row.get("ticker") for row in asset_directory if row.get("ticker")}):
        rows = [row for row in asset_directory if row.get("ticker") == ticker]
        companies.append({
            "ticker": ticker,
            "parent": sorted({row.get("parent") for row in rows if row.get("parent")}),
            "asset_ids": [row["id"] for row in rows],
            "asset_types": dict(Counter(row["assetType"] for row in rows)),
        })
    instruments = [{
        "entity_key": key,
        "legal_name": all_entities[key].get("legal_name"),
        "entity_payload_path": entity_payloads[key],
        "summary": entity_summaries[key],
    } for key in sorted(all_entities) if key not in entity_assets]
    headline = _headline_availability(ctx, obs_rows)
    contract = _contract()
    source_index = _source_index_payload(
        filing_rows, document_rows, assertion_catalog,
        as_of=getattr(ctx, "as_of_iso", None),
        run_id=getattr(ctx.staging, "run_id", None))
    directory = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of": getattr(ctx, "as_of_iso", None),
        "run_id": getattr(ctx.staging, "run_id", None),
        "registry_version": REGISTRY_VERSION,
        "companies": companies,
        "assets": asset_directory,
        "instruments": instruments,
        "counts": {"assets": len(asset_directory),
                   "in_scope_assets": sum(row["inScope"] for row in asset_directory),
                   "out_of_scope_assets": sum(not row["inScope"] for row in asset_directory),
                   "mapped_entities": len(entity_assets),
                   "instrument_entities": len(instruments)},
    }
    written_bytes += _write_json(root / "contract.json", contract)
    written_bytes += _write_json(root / "assets.json", directory)
    written_bytes += _write_json(root / "instruments.json", instruments)
    written_bytes += _write_json(root / "headline_availability.json", headline)
    written_bytes += _write_json(root / "source_index.json", source_index)
    return {
        "files": 5 + len(all_entities) + len(asset_directory),
        "bytes": written_bytes,
        "assets": len(asset_directory),
        "entities": len(entity_assets),
        "instruments": len(instruments),
        "observations": len(obs_rows),
    }
