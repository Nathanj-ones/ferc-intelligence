"""
Consumer-facing exports.

The 7 September independent check found that a flagged canonical value reached a
human-facing CSV with its source-review warning stripped: FY2025 total throughput
999,999 was shown, but the QA cell carried only unrelated derivation notes.

So this module enforces one rule mechanically, in `_check_flags_survive`: every
export row that carries a value whose observation has a must-propagate
validation MUST also carry that flag. The writer raises rather than emit a
silently unqualified figure. A warning is not decoration; it is what stops the
number being used as an unqualified headline or a ratio input.
"""

from __future__ import annotations

import csv
import json
import pathlib
import re

from ferclib.http import assert_no_secrets
from ferclib.registry import BY_ID, to_rows as registry_rows, unit_family_of
from ferclib.status import Availability, Validation

#: columns every consumer export carries, so status can never be dropped
STATUS_COLUMNS = ["availability", "origin", "method", "version_status", "validation",
                  "qa_flags", "review_status", "missing_reason"]


class FlagStripped(AssertionError):
    """Raised when an export would publish a flagged value without its flag."""


class UnitContractViolation(AssertionError):
    """Raised when a stored value's unit disagrees with its metric's contract."""


class CSVControlCharacterViolation(AssertionError):
    """Raised rather than publishing binary control codes as consumer text."""


#: control characters that have no business in a scope, unit or label. A PDF
#: whose font lacks a usable /ToUnicode map decodes to glyph indices rather than
#: letters, and those land in the extracted text as C0 controls.
_UNDECODABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _check_csv_text_is_portable(rows: list[dict], label: str) -> None:
    """Fail closed if a consumer cell still contains a binary C0 control.

    Source extraction removes these before persistence, while the immutable raw
    FERC object remains in the cache.  This second boundary is deliberate: a
    future adapter or migration must not reintroduce NUL-bearing CSV that passes
    on CPython 3.14 but fails on the declared CPython 3.9 runtime.
    """
    bad = []
    for row_number, row in enumerate(rows, 2):
        for column, value in row.items():
            if not isinstance(value, str):
                continue
            controls = sorted({ord(m.group()) for m in _UNDECODABLE.finditer(value)})
            if controls:
                identity = (row.get("observation_id") or row.get("document_fact_id")
                            or row.get("filing_id") or "")
                bad.append({"row": row_number, "column": column, "identity": identity,
                            "controls": [f"U+{code:04X}" for code in controls]})
                if len(bad) >= 20:
                    break
        if len(bad) >= 20:
            break
    if bad:
        raise CSVControlCharacterViolation(
            f"{label}: consumer text contains binary C0 controls; refusing to publish. "
            f"First affected cells: {bad}")


def _gate_undecodable_scopes(rows: list[dict], label: str) -> list[dict]:
    """Refuse to publish a value whose SCOPE cannot be read.

    Scope is what makes a figure comparable -- it is the difference between a
    winter design capacity and a summer one, between Plymouth LNG and Jackson
    Prairie. A value whose scope decodes to glyph indices instead of letters is
    arithmetically fine and semantically unusable: nobody can tell what it
    measures, so nobody can safely compare it to anything.

    Found in the rebuilt data on two `cap_reported_capacity` observations for
    C001685, whose capacity PDF uses a font with no usable /ToUnicode map:

        'as reported: narrative statement :: orage Capacity 0Dth 0Dth\\x12d ...'

    Both were `present` / `pass`. They are now downgraded at the export boundary
    rather than suppressed: the value and its provenance are retained, because
    the figure really was filed, and the scope is kept verbatim as evidence of
    what the extraction produced. What changes is that it stops being offered as
    a clean, comparable number.

    This lives at the export boundary on purpose, alongside the unit-contract
    check: it is a general guarantee about what may leave the system, not a
    per-adapter patch, so a future extractor with the same failure is caught
    without anyone remembering to look.
    """
    gated = []
    for r in rows:
        if r.get("value") in (None, ""):
            continue
        if not _UNDECODABLE.search(str(r.get("scope") or "")):
            continue
        gated.append({"observation_id": r.get("observation_id"),
                      "metric_id": r.get("metric_id"),
                      "entity_key": r.get("entity_key")})
        r["validation"] = Validation.BLOCKED_AMBIGUITY
        note = ("scope_undecodable: the source document's font supplies no usable "
                "character map, so this figure's scope extracted as glyph indices "
                "rather than text. The value is as filed and is retained, but what "
                "it measures cannot be established, so it must not be compared or "
                "aggregated")
        r["qa_flags"] = f"{note}; {r.get('qa_flags') or ''}".strip("; ")
    return gated


def _apply_display_contract(rows: list[dict], label: str) -> None:
    """Attach the ONE documented display conversion, and check the stored unit.

    Audit A04: 1,251 margins stored a fraction and declared `percent`, so a
    consumer would render them 100 times too small. The repair puts the unit
    under the metric's control (`canonical_unit`) and gives every consumer the
    rendering rule explicitly rather than leaving it to be guessed:

        display_value = value * display_scale, expressed in display_unit

    Both columns are written even when the scale is 1, so a consumer never has
    to decide for itself whether a conversion applies -- deciding for itself is
    what the audit found going wrong.

    The stored unit is also checked against the contract here, at the last point
    before the value leaves the system. A mismatch raises: shipping a number
    whose unit we know to be wrong is worse than shipping nothing.
    """
    bad = []
    for r in rows:
        m = BY_ID.get(r.get("metric_id"))
        if m is None:
            continue
        if m.canonical_unit and r.get("value") not in (None, "") and r.get("unit"):
            # An observation may legitimately carry a source-specific spelling
            # (utr:dth for an energy metric), so the FAMILY must agree, not the
            # exact token. A percent stored where a fraction is declared -- or
            # the reverse -- is a different family and is refused.
            if unit_family_of(r["unit"]) != unit_family_of(m.canonical_unit):
                bad.append(f"{r.get('observation_id')} {m.id}: stored unit "
                           f"{r['unit']!r} ({unit_family_of(r['unit']) or 'no family'}) "
                           f"contradicts declared canonical unit {m.canonical_unit!r} "
                           f"({unit_family_of(m.canonical_unit)})")
                continue
        r["display_unit"] = m.display_unit or r.get("unit") or ""
        r["display_scale"] = m.display_scale
        v = r.get("value_num")
        r["display_value"] = ("" if v in (None, "")
                              else f"{float(v) * m.display_scale:.6f}".rstrip("0").rstrip("."))
    if bad:
        raise UnitContractViolation(
            f"{label}: {len(bad)} value(s) whose stored unit contradicts the metric's "
            "declared unit contract; refusing to export\n  " + "\n  ".join(bad[:20]))


def _check_flags_survive(rows: list[dict], label: str) -> None:
    for r in rows:
        if r.get("value") in (None, ""):
            continue
        if r.get("validation") in Validation.MUST_PROPAGATE:
            # Look ONLY at the human-facing narrative columns. Including the
            # `validation` column itself made this tautological -- the value was
            # always found in a string built from the column it came from, so the
            # check could never fail and the negative control never fired.
            narrative = " ".join(str(r.get(c) or "")
                                 for c in ("qa_flags", "review_status", "missing_reason"))
            if not narrative.strip():
                raise FlagStripped(
                    f"{label}: observation {r.get('observation_id')} carries "
                    f"validation={r['validation']} but no human-readable explanation "
                    "travels with it")


def _write(path: pathlib.Path, rows: list[dict], columns: list[str] | None = None) -> int:
    cols: list[str] = list(columns or [])
    if not rows and not cols:
        path.write_text("", encoding="utf-8")
        return 0
    _check_csv_text_is_portable(rows, path.name)
    for r in rows:
        for k in r:
            if k not in cols and not k.startswith("_"):
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    assert_no_secrets(path.read_text(encoding="utf-8")[:200000])
    return len(rows)


def _rows(ctx, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in ctx.staging.query(sql, params)]


def entity_summary_rows(ctx) -> list[dict]:
    """Summarise each filing entity without multiplying shared-filer facts.

    A filing entity can legitimately map to more than one reviewed roster asset
    (for example, the two owner views of Overland Pass).  Joining observations
    directly to ``asset_entity_map`` therefore fans every fact out once per
    asset.  Select the template independently and aggregate observations only
    at their native entity grain.
    """
    return _rows(ctx, """
        SELECT o.entity_key, e.legal_name,
               (SELECT MIN(a.template)
                  FROM asset_entity_map m
                  JOIN assets a ON a.asset_id = m.asset_id
                 WHERE m.entity_key = o.entity_key) AS template,
               COUNT(*) AS observations,
               SUM(o.availability='present') AS populated,
               SUM(o.validation='pass' AND o.availability='present') AS validated,
               SUM(o.validation IN ('source_anomaly_review','blocked_ambiguity',
                                    'scope_incompatible','unit_warning','rounding_warning',
                                    'source_date_warning')) AS in_review,
               SUM(o.availability='not_required') AS not_required,
               SUM(o.availability='source_blank') AS source_blank,
               SUM(o.availability='not_implemented') AS not_implemented
        FROM observations o
        LEFT JOIN entities e ON e.entity_key = o.entity_key
        GROUP BY o.entity_key
        ORDER BY template, o.entity_key""")


def write_all(ctx, out: pathlib.Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    n = 0

    # ---------------------------------------------------------- identity/contract
    # These are part of the consumer contract, not incidental database tables.
    # Stable IDs and reviewed mappings let a later read-only adapter serve the
    # existing frontend without opening this staging database directly.
    n += bool(_write(out / "entities.csv", _rows(
        ctx, "SELECT * FROM entities ORDER BY entity_key")))
    n += bool(_write(out / "assets.csv", _rows(
        ctx, "SELECT * FROM assets ORDER BY asset_id")))
    n += bool(_write(out / "asset_entity_map.csv", _rows(
        ctx, "SELECT * FROM asset_entity_map ORDER BY asset_id,entity_key")))
    n += bool(_write(out / "ownership.csv", _rows(
        ctx, "SELECT * FROM ownership ORDER BY entity_key,parent")))
    n += bool(_write(out / "asset_dockets.csv", _rows(
        ctx, "SELECT * FROM asset_dockets ORDER BY asset_id,docket,role")))
    n += bool(_write(out / "metric_registry.csv", registry_rows()))

    # ---------------------------------------------------------- canonical
    obs = _rows(ctx, """
        SELECT o.observation_id, o.entity_key, e.legal_name, o.metric_id, o.source_regime,
               o.period_basis, o.period_start, o.period_end, o.instant_date,
               o.reporting_year, o.reporting_period, o.period_label, o.scope, o.scope_rule,
               o.unit,
               o.value_text AS value, o.value_num, o.normalized_iso,
               o.availability, o.origin, o.method, o.version_status, o.validation,
               o.qa_flags, o.review_status, o.missing_reason, o.applicability_evidence,
               o.source_system, o.filing_id, o.source_fact_id, o.source_context_id,
               o.accession_number, o.document_id, o.selector, o.derivation,
               o.concept_local, o.taxonomy_version, o.registry_version, o.schedule_page,
               o.candidate_count, o.notes
        FROM observations o LEFT JOIN entities e ON e.entity_key = o.entity_key
        ORDER BY o.entity_key, o.metric_id, o.reporting_year, o.reporting_period""")
    _gated = _gate_undecodable_scopes(obs, "canonical_observations.csv")
    _check_flags_survive(obs, "canonical_observations.csv")
    _apply_display_contract(obs, "canonical_observations.csv")
    n += bool(_write(out / "canonical_observations.csv", obs))

    # ---------------------------------------------------------- lineage
    n += bool(_write(out / "lineage_edges.csv", _rows(ctx, """
        SELECT l.*, o.metric_id, o.entity_key, o.reporting_year, o.reporting_period,
               o.value_text AS derived_value, o.derivation
        FROM lineage_edges l JOIN observations o USING(observation_id)
        ORDER BY o.entity_key, o.metric_id, l.input_order""")))
    n += bool(_write(out / "lineage_populations.csv", _rows(ctx, """
        SELECT p.*,o.entity_key,o.metric_id,o.source_regime,o.period_basis,
               o.period_start,o.period_end,o.instant_date,o.scope,o.scope_rule,o.unit
        FROM lineage_populations p JOIN observations o USING(observation_id)
        ORDER BY o.entity_key,o.metric_id,p.observation_id,p.population_id""")))
    version_columns = [
        "observation_id", "version_seq", "superseded_at", "superseded_by_run_id",
        "source_system", "filing_id", "value_text", "value_num", "version_status",
        "validation", "qa_flags", "review_status", "row_json", "entity_key",
        "metric_id", "source_regime", "period_basis", "period_start", "period_end",
        "instant_date", "scope", "scope_rule", "unit",
    ]
    n += bool(_write(out / "observation_versions.csv", _rows(ctx, """
        SELECT v.*,o.entity_key,o.metric_id,o.source_regime,o.period_basis,
               o.period_start,o.period_end,o.instant_date,o.scope,o.scope_rule,o.unit
        FROM observation_versions v LEFT JOIN observations o USING(observation_id)
        ORDER BY v.observation_id,v.version_seq"""), version_columns))

    # ---------------------------------------------------------- quarterly view
    # The human-facing table. Every value travels with its status; a flagged
    # value is never rendered bare.
    q = _rows(ctx, """
        SELECT o.observation_id, o.entity_key, e.legal_name, o.metric_id, o.reporting_year,
               o.reporting_period, o.period_basis, o.period_start, o.period_end,
               o.value_text AS value, o.value_num, o.unit, o.scope, o.scope_rule,
               o.availability, o.origin, o.method, o.version_status, o.validation,
               o.qa_flags, o.review_status, o.missing_reason,
               o.filing_id, o.source_fact_id, o.derivation
        FROM observations o LEFT JOIN entities e ON e.entity_key = o.entity_key
        WHERE o.period_basis IN ('quarter','ytd')
        ORDER BY o.entity_key, o.metric_id, o.reporting_year, o.reporting_period,
                 o.period_basis""")
    _gated = _gate_undecodable_scopes(q, "quarterly_key_metrics.csv")
    _check_flags_survive(q, "quarterly_key_metrics.csv")
    _apply_display_contract(q, "quarterly_key_metrics.csv")
    n += bool(_write(out / "quarterly_key_metrics.csv", q))

    a = _rows(ctx, """
        SELECT o.observation_id, o.entity_key, e.legal_name, o.metric_id, o.reporting_year,
               o.period_basis, o.instant_date, o.value_text AS value, o.value_num,
               o.unit, o.scope, o.scope_rule,
               o.availability, o.origin, o.method, o.version_status, o.validation,
               o.qa_flags, o.review_status, o.missing_reason, o.filing_id, o.source_fact_id
        FROM observations o LEFT JOIN entities e ON e.entity_key = o.entity_key
        WHERE o.period_basis IN ('annual','instant','annual_observation')
        ORDER BY o.entity_key, o.metric_id, o.reporting_year""")
    _gated = _gate_undecodable_scopes(a, "annual_key_metrics.csv")
    _check_flags_survive(a, "annual_key_metrics.csv")
    _apply_display_contract(a, "annual_key_metrics.csv")
    n += bool(_write(out / "annual_key_metrics.csv", a))

    # ---------------------------------------------------------- documents
    n += bool(_write(out / "document_facts.csv", _rows(ctx, """
        SELECT d.*, e.legal_name FROM document_facts d
        LEFT JOIN entities e ON e.entity_key = d.entity_key
        ORDER BY d.entity_key, d.assertion_type""")))
    n += bool(_write(out / "documents.csv", _rows(ctx,
        "SELECT * FROM documents ORDER BY source_system, filing_id, attachment_id")))
    n += bool(_write(out / "events.csv", _rows(ctx,
        "SELECT * FROM events "
        "ORDER BY COALESCE(source_filed_date, reporting_date, '') DESC, event_id")))

    # ---------------------------------------------------------- provenance
    n += bool(_write(out / "filing_inventory.csv", _rows(ctx, """
        SELECT f.*, e.legal_name,
               (SELECT group_concat(entity_key, '|') FROM
                  (SELECT entity_key FROM filing_entities fe
                   WHERE fe.source_system=f.source_system AND fe.filing_id=f.filing_id
                   ORDER BY entity_key)) AS associated_entity_keys,
               (SELECT group_concat(entity_key || ':' || association_role, '|') FROM
                  (SELECT entity_key,association_role FROM filing_entities fe
                   WHERE fe.source_system=f.source_system AND fe.filing_id=f.filing_id
                   ORDER BY entity_key)) AS associated_entity_roles
        FROM filings f
        LEFT JOIN entities e ON e.entity_key = f.entity_key
        ORDER BY f.entity_key, f.form, f.reporting_year, f.reporting_period""")))
    n += bool(_write(out / "source_manifest.csv", ctx.cache.manifest_rows()))
    n += bool(_write(out / "applicability.csv", _rows(ctx,
        "SELECT * FROM applicability ORDER BY form, taxonomy_version, concept_local")))
    n += bool(_write(out / "blockers.csv", _rows(ctx,
        "SELECT * FROM blockers ORDER BY adapter, kind")))
    n += bool(_write(out / "reviewed_source_annotations.csv", _rows(ctx,
        "SELECT * FROM reviewed_source_annotations ORDER BY entity_key, filing_id")))

    # ---------------------------------------------------------- summaries
    entity_summary = entity_summary_rows(ctx)
    n += bool(_write(out / "coverage_by_entity.csv", entity_summary))

    n += bool(_write(out / "coverage_by_metric.csv", _rows(ctx, """
        SELECT metric_id, source_regime, COUNT(*) AS observations,
               COUNT(DISTINCT entity_key) AS entities,
               SUM(availability='present') AS populated,
               SUM(validation='pass' AND availability='present') AS validated,
               SUM(availability='source_blank') AS source_blank,
               SUM(availability='not_required') AS not_required,
               SUM(availability='not_implemented') AS not_implemented
        FROM observations GROUP BY metric_id, source_regime
        ORDER BY metric_id, source_regime""")))

    n += bool(_write(out / "field_status.csv", _rows(ctx,
        "SELECT * FROM field_status ORDER BY template, field_id")))

    return n


def api_payload(ctx, entity_key: str) -> dict:
    """The minimal read-only interface payload for one entity.

    Every value carries its five status dimensions, so a consumer cannot render a
    figure without also having what qualifies it.
    """
    rows = _rows(ctx, """
        SELECT metric_id, source_regime, period_basis, period_start, period_end,
               instant_date, reporting_year, reporting_period, scope, scope_rule, unit,
               value_text, value_num, availability, origin, method, version_status,
               validation, qa_flags, review_status, missing_reason,
               source_system, filing_id, source_fact_id, accession_number, derivation
        FROM observations WHERE entity_key=? ORDER BY metric_id, reporting_year""",
        (entity_key,))
    # The same single display conversion the CSVs use. A consumer of this
    # envelope must never have to infer whether a `percent` metric holds a
    # percent or a fraction -- that inference is what audit A04 recorded.
    for r in rows:
        r["value"] = r.get("value_text")
    _apply_display_contract(rows, f"api_payload({entity_key})")
    for r in rows:
        r.pop("value", None)
    ent = _rows(ctx, "SELECT * FROM entities WHERE entity_key=?", (entity_key,))
    assets = _rows(ctx, """
        SELECT a.*, m.mapping_scope FROM assets a
        JOIN asset_entity_map m ON m.asset_id = a.asset_id WHERE m.entity_key=?""",
        (entity_key,))
    own = _rows(ctx, "SELECT * FROM ownership WHERE entity_key=?", (entity_key,))
    return {
        "entity": ent[0] if ent else None,
        "assets": assets,
        # Ownership is a LABEL. Entity figures are 100% of the entity's own
        # system and are never multiplied by a percentage.
        "ownership": own,
        "ownership_note": ("percentages label the parent's interest; reported figures are "
                           "the filing entity's own 100% system data and are never "
                           "apportioned"),
        "observations": rows,
        "status_contract": {
            "availability": "which kind of present/absent this is",
            "origin": "which system the bytes came from",
            "method": "filed, normalised, derived or document-extracted",
            "version_status": "original, identical resubmission, revised, superseded",
            "validation": "comparability and quality; MUST travel with the value",
        },
        "unit_contract": {
            "unit": "the unit the STORED value is in; the authority for comparison",
            "display_unit": "what to render",
            "display_scale": "the single factor from stored to displayed; already "
                             "applied in display_value",
            "display_value": "value * display_scale, for rendering only",
            "note": ("Derived percentages are stored percent-valued (54.6861 percent). "
                     "Values FILED by the carrier as fractions -- p700_wacc and the "
                     "capital-structure components arrive as xbrli:pure 0.0913 -- are "
                     "stored exactly as filed and carry display_scale 100. Never infer "
                     "the convention from a metric name; read unit and display_scale."),
        },
    }
