"""Deterministic operating-universe obligation and coverage-grid construction.

This module is the production boundary around :mod:`ferclib.applicability` and
:mod:`ferclib.coverage`.  It deliberately does not open SQLite, inspect an
observation population, fetch a taxonomy, or write an export.  A caller supplies
one coherent evidence snapshot; the returned rows can then be published inside
the caller's database/output generation boundary.

The important separations are structural:

* the current operating universe establishes *who* is in scope, not whether a
  fetch happened to succeed;
* a FERC-index/file occurrence establishes a form obligation, while retrieval
  health says what happened after discovery;
* taxonomy pins and applicability rows are frozen inputs, never inferred from
  whichever filing parsed successfully;
* ``as_of`` decides due/not-yet-due, while ``built_at`` records when this grid
  was produced; neither is read from the wall clock;
* occurrences before the coverage window may establish forward continuity but
  never create historical periodic slots; event-driven document metrics remain
  separate, while declared regulatory-instrument intervals enter through their
  own ingestion-independent slot population.

No percentage is targeted here.  Unknown eligibility, source failure,
not-required concepts and review-gated values remain separate all the way to
``measure_grid``.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import coverage, periods
from .applicability import (FAMILY_OF_FORM, FORM_PERIODS, EvidenceKind,
                            FormObligation, IN_FORM_NO, IN_FORM_UNKNOWN,
                            IN_FORM_YES, ObligationRegister, ObligationState,
                            SourceHealth)
from .registry import REGISTRY, REGISTRY_VERSION
from .staging import slot_id


DEFAULT_UNIVERSE = pathlib.Path(__file__).resolve().parent.parent / "config" / "universe.csv"
OPERATING_TEMPLATES = frozenset(
    {"interstate_gas", "gas_storage", "liquids", "intrastate_549d", "lng"}
)

# Names in the reviewed roster predate the executable adapter vocabulary.  The
# normalisation is exact and deliberately small; an unknown name is refused.
FORM_ALIASES = {
    "Form 2": "Form 2",
    "Form 2A": "Form 2A",
    "Form 2-A": "Form 2A",
    "Form 3Q Gas": "Form 3Q Gas",
    "Form 3-Q": "Form 3Q Gas",
    "Form 3-Q Gas": "Form 3Q Gas",
    "Form 6": "Form 6",
    "Form 6Q": "Form 6Q",
    "Form 6-Q": "Form 6Q",
    "Form 549B IOC": "Form 549B IOC",
    "Form 549B Capacity": "Form 549B Capacity",
    "Form 549D": "Form 549D",
}

# These forms follow from the adapter/operating-template eligibility that the
# universe run already applies.  They seed a ROSTER_DECLARED obligation only;
# that is APPLICABILITY_UNKNOWN until an indexed occurrence establishes it.
# Thus this mapping prevents a failed fetch from deleting a slot without
# pretending the reviewed roster is itself a FERC filing occurrence.
TEMPLATE_ELIGIBILITY_FORMS = {
    "interstate_gas": ("Form 549B IOC", "Form 549B Capacity"),
    "gas_storage": ("Form 549B IOC", "Form 549B Capacity"),
    "intrastate_549d": ("Form 549D",),
    "liquids": (),
    "lng": (),
}

_KNOWN_HEALTH = frozenset({
    SourceHealth.OK,
    SourceHealth.NOT_INDEXED,
    SourceHealth.HEALTH_NOT_RECORDED,
    SourceHealth.INDEX_FAILED,
    SourceHealth.RETRIEVAL_FAILED,
    SourceHealth.PARSE_FAILED,
    SourceHealth.NOT_RETRIEVED,
    SourceHealth.CACHE_CORRUPT,
})
_OCCURRENCE_HEALTH = _KNOWN_HEALTH - {SourceHealth.NOT_INDEXED,
                                      SourceHealth.INDEX_FAILED}
_PERIODIC_FORMS = frozenset(FAMILY_OF_FORM)
_NONPERIODIC_FORMS = frozenset({"eLibrary document", "Federal Register notice"})
_TAXONOMY_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _date(value: str | dt.date, label: str) -> dt.date:
    if isinstance(value, dt.datetime):
        raise ValueError(f"{label} must be a date, not a datetime")
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD): {exc}") from None


def _timestamp(value: str | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"built_at must be an ISO timestamp: {exc}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("built_at must include an explicit UTC offset")
    return parsed.isoformat(timespec="seconds")


def normalise_form(value: str) -> str:
    form = FORM_ALIASES.get((value or "").strip())
    if not form:
        raise ValueError(f"unknown periodic FERC form name {value!r}")
    return form


def _occurrence_form(value: str) -> str:
    raw = (value or "").strip()
    if raw in _NONPERIODIC_FORMS:
        return raw
    return normalise_form(raw)


def _period(form: str, value: str) -> str:
    raw = (value or "").strip()
    if raw.upper() in {"ANNUAL", "FY", "YEAR"}:
        raw = "Q4"
    if raw and raw not in FORM_PERIODS[form]:
        raise ValueError(
            f"{form} reporting period {raw!r} is outside its declared calendar "
            f"{FORM_PERIODS[form]}"
        )
    return raw


@dataclass(frozen=True)
class EntityEligibility:
    """One filing entity after asset-level roster rows are deduplicated."""

    entity_key: str
    template: str
    asset_ids: tuple[str, ...]
    primary_asset_id: str
    roster_forms: tuple[str, ...]
    eligibility_forms: tuple[str, ...]

    @property
    def all_forms(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.roster_forms) | set(self.eligibility_forms)))


@dataclass(frozen=True)
class UniverseSnapshot:
    path: str
    sha256: str
    assets: tuple[dict, ...]
    entities: tuple[EntityEligibility, ...]
    exclusions: tuple[dict, ...]

    @property
    def entity_template(self) -> dict[str, str]:
        return {entity.entity_key: entity.template for entity in self.entities}

    @property
    def entity_asset(self) -> dict[str, str]:
        return {entity.entity_key: entity.primary_asset_id for entity in self.entities}


@dataclass(frozen=True)
class OccurrenceEvidence:
    """A filing occurrence discovered through an official FERC source.

    ``source_health`` describes retrieval/parsing after discovery.  A searched
    index with no occurrence is *not* an OccurrenceEvidence; represent that as a
    :class:`SourceHealthEvidence` with ``not_indexed`` instead.
    """

    entity_key: str
    form: str
    reporting_year: int
    reporting_period: str = ""
    source_health: str = SourceHealth.HEALTH_NOT_RECORDED
    detail: str = ""
    indexed_only: bool = False
    as_of: str = ""
    occurrence_id: str = ""
    source_system: str = ""

    @classmethod
    def from_value(cls, value: "OccurrenceEvidence | Mapping[str, Any]") -> "OccurrenceEvidence":
        if isinstance(value, cls):
            raw = value
        elif isinstance(value, Mapping):
            raw = cls(
                entity_key=str(value.get("entity_key") or value.get("entity_cid") or ""),
                form=str(value.get("form") or ""),
                reporting_year=int(value.get("reporting_year") or value.get("year") or 0),
                reporting_period=str(value.get("reporting_period") or value.get("period") or ""),
                source_health=str(value.get("source_health") or
                                  SourceHealth.HEALTH_NOT_RECORDED),
                detail=str(value.get("detail") or value.get("health_detail") or ""),
                indexed_only=bool(value.get("indexed_only", False)),
                as_of=str(value.get("as_of") or value.get("snapshot_date") or ""),
                occurrence_id=str(value.get("occurrence_id") or value.get("filing_id") or
                                  value.get("accession_number") or ""),
                source_system=str(value.get("source_system") or ""),
            )
        else:
            raise TypeError("occurrences must contain OccurrenceEvidence or mappings")
        form = _occurrence_form(raw.form)
        period = (_period(form, raw.reporting_period)
                  if form in _PERIODIC_FORMS else raw.reporting_period)
        if (not raw.entity_key or raw.reporting_year < 1900
                or not raw.occurrence_id or not raw.source_system):
            raise ValueError(f"invalid occurrence identity: {raw!r}")
        if raw.source_health not in _OCCURRENCE_HEALTH:
            raise ValueError(
                f"an occurrence cannot have source_health={raw.source_health!r}; "
                "not_indexed/index_failed describe a search, not an occurrence"
            )
        if raw.as_of:
            _date(raw.as_of, "occurrence as_of")
        return dataclasses.replace(raw, form=form, reporting_period=period)


@dataclass(frozen=True)
class SourceHealthEvidence:
    """Generation-scoped, durable retrieval state for one obligation period."""

    record_id: str
    generation_id: str
    entity_key: str
    form: str
    reporting_year: int
    reporting_period: str
    source_health: str
    detail: str
    recorded_at: str
    as_of: str = ""

    @classmethod
    def from_value(cls, value: "SourceHealthEvidence | Mapping[str, Any]") -> "SourceHealthEvidence":
        if isinstance(value, cls):
            raw = value
        elif isinstance(value, Mapping):
            raw = cls(
                record_id=str(value.get("record_id") or ""),
                generation_id=str(value.get("generation_id") or value.get("run_id") or ""),
                entity_key=str(value.get("entity_key") or value.get("entity_cid") or ""),
                form=str(value.get("form") or ""),
                reporting_year=int(value.get("reporting_year") or value.get("year") or 0),
                reporting_period=str(value.get("reporting_period") or value.get("period") or ""),
                source_health=str(value.get("source_health") or ""),
                detail=str(value.get("detail") or value.get("health_detail") or ""),
                recorded_at=str(value.get("recorded_at") or value.get("observed_at") or ""),
                as_of=str(value.get("as_of") or value.get("snapshot_date") or ""),
            )
        else:
            raise TypeError("source_health must contain SourceHealthEvidence or mappings")
        form = normalise_form(raw.form)
        period = _period(form, raw.reporting_period)
        if not all((raw.record_id, raw.generation_id, raw.entity_key, raw.recorded_at)):
            raise ValueError(
                "source-health rows require record_id, generation_id, entity_key and recorded_at"
            )
        if raw.reporting_year < 1900 or not period:
            raise ValueError(f"invalid source-health obligation key: {raw!r}")
        if raw.source_health not in _KNOWN_HEALTH:
            raise ValueError(f"unknown source_health {raw.source_health!r}")
        _timestamp(raw.recorded_at)
        if raw.as_of:
            _date(raw.as_of, "source-health as_of")
        return dataclasses.replace(raw, form=form, reporting_period=period)

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.entity_key, self.form, self.reporting_year, self.reporting_period)


@dataclass(frozen=True)
class TaxonomyPin:
    form: str
    reporting_year: int
    taxonomy_version: str
    evidence_ref: str

    @classmethod
    def from_value(cls, value: "TaxonomyPin | Mapping[str, Any]") -> "TaxonomyPin":
        if isinstance(value, cls):
            raw = value
        elif isinstance(value, Mapping):
            raw = cls(
                form=str(value.get("form") or ""),
                reporting_year=int(value.get("reporting_year") or value.get("year") or 0),
                taxonomy_version=str(value.get("taxonomy_version") or value.get("version") or ""),
                evidence_ref=str(value.get("evidence_ref") or value.get("evidence") or ""),
            )
        else:
            raise TypeError("taxonomy_pins must contain TaxonomyPin or mappings")
        form = normalise_form(raw.form)
        if not _TAXONOMY_VERSION.fullmatch(raw.taxonomy_version) or not raw.evidence_ref:
            raise ValueError(f"taxonomy pin needs a dated version and evidence: {raw!r}")
        return dataclasses.replace(raw, form=form)


class FrozenApplicability:
    """Read-only applicability resolver over a frozen, hash-identifiable row set."""

    def __init__(self, rows: Iterable[Mapping[str, Any]], *, snapshot_id: str):
        if not snapshot_id:
            raise ValueError("FrozenApplicability requires a snapshot_id")
        self.snapshot_id = snapshot_id
        self._rows: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        canonical = []
        for source in rows:
            form = normalise_form(str(source.get("form") or ""))
            version = str(source.get("taxonomy_version") or "")
            concept = str(source.get("concept_local") or "")
            status = str(source.get("in_form") or IN_FORM_UNKNOWN)
            page = str(source.get("schedule_page") or "")
            evidence = str(source.get("evidence") or "")
            if status not in {IN_FORM_YES, IN_FORM_NO, IN_FORM_UNKNOWN}:
                raise ValueError(f"unknown in_form value {status!r}")
            if not (_TAXONOMY_VERSION.fullmatch(version) and concept and evidence):
                raise ValueError(f"incomplete applicability row: {dict(source)!r}")
            key = (form, version, concept)
            value = (status, page, evidence)
            if key in self._rows and self._rows[key] != value:
                raise ValueError(f"conflicting applicability rows for {key}")
            self._rows[key] = value
            canonical.append((*key, *value))
        self.identity = _digest({"snapshot_id": snapshot_id, "rows": sorted(canonical)})

    def status(self, form: str, version: str, concept: str, *, schema_ref: str = ""):
        del schema_ref
        key = (normalise_form(form), version, concept)
        return self._rows.get(
            key,
            (IN_FORM_UNKNOWN, "",
             f"no frozen applicability row for {key[0]} {version} {concept}; "
             "absence is APPLICABILITY_UNKNOWN, never NOT_REQUIRED"),
        )


@dataclass(frozen=True)
class CoverageGrid:
    run_id: str
    as_of: str
    built_at: str
    year_from: int
    year_to: int
    registry_version: str
    input_digest: str
    universe: UniverseSnapshot
    obligations: tuple[FormObligation, ...]
    slots: tuple[dict, ...]
    prior_eligibility_occurrences: tuple[OccurrenceEvidence, ...]
    window_occurrences: tuple[OccurrenceEvidence, ...]
    separated_occurrences: tuple[dict, ...]
    unused_source_health: tuple[SourceHealthEvidence, ...]
    zero_slot_obligations: tuple[dict, ...]
    event_driven_metric_ids: tuple[str, ...]
    instrument_metric_ids: tuple[str, ...]

    def manifest(self) -> dict:
        requirements = Counter(slot["requirement"] for slot in self.slots)
        states = Counter(slot.get("slot_state") or "unstated" for slot in self.slots)
        health = Counter(slot.get("source_health") or "unstated" for slot in self.slots)
        return {
            "run_id": self.run_id,
            "as_of": self.as_of,
            "built_at": self.built_at,
            "coverage_window": [self.year_from, self.year_to],
            "registry_version": self.registry_version,
            "input_digest": self.input_digest,
            "universe": {
                "path": self.universe.path,
                "sha256": self.universe.sha256,
                "included_assets": len(self.universe.assets),
                "included_entities": len(self.universe.entities),
                "excluded_assets": len(self.universe.exclusions),
            },
            "obligations": len(self.obligations),
            "slots": len(self.slots),
            "requirements": dict(sorted(requirements.items())),
            "slot_states": dict(sorted(states.items())),
            "source_health": dict(sorted(health.items())),
            "prior_eligibility_occurrences": len(self.prior_eligibility_occurrences),
            "window_occurrences": len(self.window_occurrences),
            "separated_occurrences": len(self.separated_occurrences),
            "unused_source_health": len(self.unused_source_health),
            "zero_slot_obligations": len(self.zero_slot_obligations),
            "event_driven_metric_ids": list(self.event_driven_metric_ids),
            "instrument_metric_ids": list(self.instrument_metric_ids),
            "instrument_slots": sum(
                1 for row in self.slots
                if row.get("metric_id") in set(self.instrument_metric_ids)),
            "document_slots": sum(
                1 for row in self.slots
                if row.get("metric_id") in set(self.event_driven_metric_ids)
                and row.get("source_regime") == "eLibrary document"),
        }


@dataclass(frozen=True)
class CoverageMeasurement:
    grid_input_digest: str
    rows: tuple[dict, ...]
    statistics: dict


def load_universe(path: pathlib.Path | str = DEFAULT_UNIVERSE) -> UniverseSnapshot:
    """Load the reviewed post-COD roster and deduplicate it to filing entities."""
    source = pathlib.Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"operating universe is absent: {source}")
    with source.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"asset_id", "entity_key", "status", "cod_group", "template",
                    "roster_forms"}
        if not reader.fieldnames or required - set(reader.fieldnames):
            raise ValueError(f"universe is missing columns {sorted(required-set(reader.fieldnames or []))}")
        rows = list(reader)

    seen_assets: set[str] = set()
    included: list[dict] = []
    excluded: list[dict] = []
    by_entity: dict[str, list[dict]] = {}
    for row in rows:
        asset_id = (row.get("asset_id") or "").strip()
        if not asset_id or asset_id in seen_assets:
            raise ValueError(f"universe has a missing or duplicate asset_id {asset_id!r}")
        seen_assets.add(asset_id)
        reasons = []
        if row.get("status") != "operating":
            reasons.append(f"status={row.get('status') or '(blank)'}")
        if row.get("cod_group") != "post-cod":
            reasons.append(f"cod_group={row.get('cod_group') or '(blank)'}")
        template = row.get("template") or ""
        if template == "OUT_OF_TEMPLATE":
            reasons.append("outside the five operating templates")
        elif template not in OPERATING_TEMPLATES:
            raise ValueError(f"asset {asset_id} has unknown template {template!r}")
        if reasons:
            excluded.append({"asset_id": asset_id, "entity_key": row.get("entity_key") or "",
                             "reason": "; ".join(reasons)})
            continue
        entity_key = (row.get("entity_key") or "").strip()
        if not entity_key:
            raise ValueError(f"included asset {asset_id} has no entity_key")
        frozen = dict(row)
        included.append(frozen)
        by_entity.setdefault(entity_key, []).append(frozen)

    entities = []
    for entity_key, members in sorted(by_entity.items()):
        templates = {row["template"] for row in members}
        if len(templates) != 1:
            raise ValueError(f"entity {entity_key} maps to incompatible templates {templates}")
        template = next(iter(templates))
        roster: set[str] = set()
        for row in members:
            try:
                raw_forms = json.loads(row.get("roster_forms") or "[]")
            except json.JSONDecodeError as exc:
                raise ValueError(f"asset {row['asset_id']} has invalid roster_forms: {exc}") from None
            if not isinstance(raw_forms, list) or not all(isinstance(x, str) for x in raw_forms):
                raise ValueError(f"asset {row['asset_id']} roster_forms must be a JSON string list")
            roster.update(normalise_form(form) for form in raw_forms)
        asset_ids = tuple(sorted(row["asset_id"] for row in members))
        entities.append(EntityEligibility(
            entity_key=entity_key,
            template=template,
            asset_ids=asset_ids,
            # Coverage is at legal-entity grain.  A stable representative keeps
            # the legacy asset_id column useful without duplicating a whole
            # entity total once per investor/physical-asset row.
            primary_asset_id=asset_ids[0],
            roster_forms=tuple(sorted(roster)),
            eligibility_forms=tuple(TEMPLATE_ELIGIBILITY_FORMS[template]),
        ))

    return UniverseSnapshot(
        path=str(source), sha256=_sha256(source), assets=tuple(included),
        entities=tuple(entities), exclusions=tuple(excluded)
    )


def _pins(values: Iterable[TaxonomyPin | Mapping[str, Any]]) -> tuple[dict, tuple[TaxonomyPin, ...]]:
    rows = [TaxonomyPin.from_value(value) for value in values]
    resolved: dict[tuple[str, int], str] = {}
    for row in rows:
        key = (row.form, row.reporting_year)
        if key in resolved and resolved[key] != row.taxonomy_version:
            raise ValueError(f"conflicting taxonomy pins for {key}: {resolved[key]} / {row.taxonomy_version}")
        resolved[key] = row.taxonomy_version
    return resolved, tuple(sorted(set(rows), key=lambda r: (r.form, r.reporting_year)))


def _health(values: Iterable[SourceHealthEvidence | Mapping[str, Any]]
            ) -> tuple[dict, tuple[SourceHealthEvidence, ...], str]:
    rows = [SourceHealthEvidence.from_value(value) for value in values]
    generations = {row.generation_id for row in rows}
    if len(generations) > 1:
        raise ValueError(
            f"source-health rows mix generations {sorted(generations)}; select one coherent snapshot"
        )
    resolved: dict[tuple[str, str, int, str], SourceHealthEvidence] = {}
    for row in rows:
        if row.key in resolved and resolved[row.key] != row:
            raise ValueError(f"conflicting source-health rows for {row.key}")
        resolved[row.key] = row
    ordered = tuple(sorted(resolved.values(), key=lambda r: r.key))
    return resolved, ordered, next(iter(generations), "")


def build_coverage_grid(
        *,
        year_from: int,
        year_to: int,
        as_of: str | dt.date,
        built_at: str | dt.datetime,
        run_id: str,
        universe_path: pathlib.Path | str = DEFAULT_UNIVERSE,
        metrics: Sequence = REGISTRY,
        occurrences: Iterable[OccurrenceEvidence | Mapping[str, Any]] = (),
        source_health: Iterable[SourceHealthEvidence | Mapping[str, Any]] = (),
        taxonomy_pins: Iterable[TaxonomyPin | Mapping[str, Any]] = (),
        instrument_slots: Iterable[Mapping[str, Any]] = (),
        document_slots: Iterable[Mapping[str, Any]] = (),
        applicability=None,
        applicability_identity: str = "") -> CoverageGrid:
    """Build a periodic coverage grid from one explicitly identified snapshot.

    The function is pure with respect to project state: it reads only
    ``universe_path`` and mutates no input object.  ``applicability`` must expose
    the existing ``status(form, version, concept)`` API.  A custom resolver must
    also provide an identity (attribute or argument), otherwise the build is
    refused because its requirement classifications could not be reproduced.
    """
    y0, y1 = int(year_from), int(year_to)
    if y0 > y1:
        raise ValueError(f"coverage year_from {y0} is after year_to {y1}")
    today = _date(as_of, "as_of")
    stamp = _timestamp(built_at)
    if not run_id:
        raise ValueError("run_id is required")
    universe = load_universe(universe_path)
    metric_rows = tuple(metrics)
    if len({metric.id for metric in metric_rows}) != len(metric_rows):
        raise ValueError("metric registry contains duplicate ids")

    pin_map, pin_rows = _pins(taxonomy_pins)
    health_map, health_rows, health_generation = _health(source_health)
    resolver_identity = applicability_identity or getattr(applicability, "identity", "")
    if applicability is not None and not resolver_identity:
        raise ValueError("a supplied applicability resolver needs a frozen identity")

    occurrence_rows = tuple(OccurrenceEvidence.from_value(value) for value in occurrences)
    occurrence_ids: set[tuple[str, str]] = set()
    for row in occurrence_rows:
        key = (row.source_system, row.occurrence_id)
        if key in occurrence_ids:
            raise ValueError(f"duplicate filing occurrence identity {key}")
        occurrence_ids.add(key)
    ordered_occurrences = tuple(sorted(
        occurrence_rows,
        key=lambda row: (row.entity_key, row.form, row.reporting_year,
                         row.reporting_period, row.source_system, row.occurrence_id),
    ))
    entities = {entity.entity_key: entity for entity in universe.entities}
    register = ObligationRegister(y0, y1, today, taxonomy_pins=pin_map)
    for entity in universe.entities:
        register.note_roster(entity.entity_key, entity.all_forms)

    prior: list[OccurrenceEvidence] = []
    window: list[OccurrenceEvidence] = []
    separated: list[dict] = []
    for occurrence in ordered_occurrences:
        if occurrence.entity_key not in entities:
            separated.append({"reason": "entity_outside_current_operating_universe",
                              **dataclasses.asdict(occurrence)})
            continue
        if occurrence.form not in _PERIODIC_FORMS:
            separated.append({"reason": "event_or_nonperiodic_feed",
                              **dataclasses.asdict(occurrence)})
            continue
        if occurrence.reporting_year > y1:
            separated.append({"reason": "after_coverage_window",
                              **dataclasses.asdict(occurrence)})
            continue
        register.note_occurrence(
            occurrence.entity_key, occurrence.form, occurrence.reporting_year,
            occurrence.reporting_period, health=occurrence.source_health,
            detail=occurrence.detail, indexed_only=occurrence.indexed_only,
            as_of=occurrence.as_of,
        )
        if occurrence.reporting_year < y0:
            prior.append(occurrence)
        else:
            window.append(occurrence)

    raw_obligations = register.obligations()
    # ObligationRegister predates time-varying major/non-major status and keeps
    # one entity-wide nonmajor flag. Recompute Form 3-Q deadlines from the
    # governing annual form in the *same year*: Form 2 uses 60 days; Form 2-A
    # uses 70. This changes no form eligibility and uses periods.due_date's
    # existing cited rule.
    annual_form = {(row.entity_key, row.year): row.form for row in raw_obligations
                   if row.family == "gas_annual"}
    obligations: list[FormObligation] = []
    used_health: set[tuple[str, str, int, str]] = set()
    for obligation in raw_obligations:
        if obligation.form == "Form 3Q Gas":
            due = periods.due_date(
                obligation.form, obligation.year, obligation.period,
                nonmajor=annual_form.get((obligation.entity_key, obligation.year)) == "Form 2A",
            )
            if due is None:
                obligation = dataclasses.replace(
                    obligation, due_date="", state=ObligationState.DEADLINE_UNRESOLVED)
            else:
                obligation = dataclasses.replace(
                    obligation,
                    due_date=due.isoformat(),
                    state=(ObligationState.FUTURE_NOT_DUE
                           if today <= due else ObligationState.DUE),
                )
        row = health_map.get((obligation.entity_key, obligation.form,
                              obligation.year, obligation.period))
        if row is not None:
            used_health.add(row.key)
            obligation = dataclasses.replace(
                obligation,
                source_health=row.source_health,
                health_detail=(f"[{row.record_id} generation={row.generation_id} "
                               f"recorded={row.recorded_at}] {row.detail}"),
                as_of=row.as_of or obligation.as_of,
            )
        obligations.append(obligation)

    slots = coverage.calendar_slots(
        obligations, metric_rows,
        entity_template=universe.entity_template,
        entity_asset=universe.entity_asset,
        run_id=run_id,
        applicability=applicability,
    )
    # Regulatory instruments are neither entity filing obligations nor
    # event-driven discoveries.  Their expected intervals therefore arrive as
    # a separately declared, deterministic input.  Requiring those rows here
    # prevents the previous failure mode where the registry advertised an
    # implemented Oil Pipeline Index while the denominator silently contained
    # zero index slots.
    instrument_metrics = {metric.id: metric for metric in metric_rows
                          if metric.adapter == "oil_index"}
    supplied_instruments = [dict(row) for row in instrument_slots]
    if instrument_metrics and not supplied_instruments:
        raise ValueError(
            "oil_index metrics are in the registry but no independent instrument slots "
            "were supplied")
    required_instrument_columns = {
        "slot_id", "entity_key", "template", "metric_id", "source_regime",
        "period_basis", "period_start", "period_end", "reporting_year",
        "reporting_period", "scope", "unit_rule", "requirement",
        "requirement_evidence", "applicability_version", "due_date",
        "slot_state", "obligation_form", "obligation_authority",
        "obligation_evidence_kind", "source_health", "source_health_detail",
        "denominator_origin",
    }
    normalised_instruments: list[dict] = []
    seen_instrument_slots: set[str] = set()
    for row in supplied_instruments:
        missing = sorted(required_instrument_columns - set(row))
        if missing:
            raise ValueError(f"instrument slot is missing columns {missing}")
        metric = instrument_metrics.get(str(row.get("metric_id") or ""))
        if metric is None:
            raise ValueError(
                f"instrument slot names non-oil or undeclared metric {row.get('metric_id')!r}")
        if row.get("period_basis") != periods.INTERVAL:
            raise ValueError("regulatory instrument slots require exact interval basis")
        start = _date(row.get("period_start"), "instrument period_start")
        end = _date(row.get("period_end"), "instrument period_end")
        if start > end:
            raise ValueError("instrument period_start is after period_end")
        if int(row.get("reporting_year") or 0) != start.year:
            raise ValueError("instrument reporting_year must equal interval start year")
        if row.get("reporting_period") != "index_year":
            raise ValueError("oil-index instrument reporting_period must be index_year")
        if row.get("scope") != metric.scope or row.get("unit_rule") != metric.unit_rule:
            raise ValueError("instrument slot scope/unit contract differs from the registry")
        regimes = {regime for regime, _basis in metric.regimes}
        if row.get("source_regime") not in regimes:
            raise ValueError("instrument slot source regime differs from the registry")
        if row.get("requirement") not in coverage.CORE:
            raise ValueError("instrument slots must retain a core requirement classification")
        if row.get("source_health") not in _KNOWN_HEALTH:
            raise ValueError(f"unknown instrument source health {row.get('source_health')!r}")
        if row.get("denominator_origin") != "regulatory_instrument_history":
            raise ValueError("instrument denominator origin is not independently declared")
        expected_id = slot_id(
            str(row["entity_key"]), metric.id, str(row["source_regime"]),
            periods.INTERVAL, start.isoformat(), end.isoformat(), "", metric.scope)
        if row.get("slot_id") != expected_id:
            raise ValueError(f"instrument slot_id differs from its exact grain: {metric.id}")
        if expected_id in seen_instrument_slots:
            raise ValueError(f"duplicate instrument slot {expected_id}")
        seen_instrument_slots.add(expected_id)
        normalised_instruments.append(dict(
            row,
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            frozen_at=stamp,
            frozen_run_id=run_id,
        ))
    normalised_instruments.sort(key=lambda row: (
        row["entity_key"], row["period_start"], row["period_end"],
        row["metric_id"], row["slot_id"]))
    missing_instrument_metrics = sorted(
        set(instrument_metrics) - {row["metric_id"] for row in normalised_instruments})
    if missing_instrument_metrics:
        raise ValueError(
            f"instrument history has no slots for metrics {missing_instrument_metrics}")
    slots.extend(normalised_instruments)

    # eLibrary assertions are event/search driven, not periodic filing duties.
    # The coverage denominator therefore receives exactly one independently
    # frozen route anchor per eligible entity/metric.  Dated occurrences and
    # explicit search statuses are audited in a separate quality census; they
    # must never create or delete denominator rows as parser output changes.
    document_metrics = {
        metric.id: metric for metric in metric_rows
        if metric.adapter != "oil_index"
        and any(regime == "eLibrary document" for regime, _basis in metric.regimes)
    }
    supplied_documents = [dict(row) for row in document_slots]
    if document_metrics and not supplied_documents:
        raise ValueError(
            "eLibrary document metrics are in the registry but no independent "
            "route anchors were supplied")
    required_document_columns = {
        "slot_id", "entity_key", "asset_id", "template", "metric_id",
        "source_regime", "period_basis", "period_start", "period_end",
        "instant_date", "reporting_year", "reporting_period", "scope",
        "unit_rule", "requirement", "requirement_evidence",
        "applicability_version", "due_date", "slot_state", "obligation_form",
        "obligation_authority", "obligation_evidence_kind", "source_health",
        "source_health_detail", "denominator_origin",
    }
    normalised_documents: list[dict] = []
    seen_document_slots: set[str] = set()
    seen_document_pairs: set[tuple[str, str]] = set()
    allowed_requirements = {
        coverage.REQUIRED, coverage.CONDITIONAL, coverage.OPTIONAL,
        coverage.NOT_REQUIRED, coverage.UNKNOWN,
    }
    allowed_evidence = {
        EvidenceKind.FILED_OCCURRENCE, EvidenceKind.INDEXED_OCCURRENCE,
        EvidenceKind.CARRIED_FORWARD, EvidenceKind.ROSTER_DECLARED,
        EvidenceKind.NONE,
    }
    allowed_origins = {"document_adapter_anchor"}
    for row in supplied_documents:
        missing = sorted(required_document_columns - set(row))
        if missing:
            raise ValueError(f"document slot is missing columns {missing}")
        metric = document_metrics.get(str(row.get("metric_id") or ""))
        if metric is None:
            raise ValueError(
                f"document slot names non-document or undeclared metric "
                f"{row.get('metric_id')!r}")
        entity = entities.get(str(row.get("entity_key") or ""))
        if entity is None:
            raise ValueError("document slot entity is outside the operating universe")
        if row.get("template") != entity.template or entity.template not in metric.templates:
            raise ValueError("document slot template/entity contract differs from the registry")
        asset_id = str(row.get("asset_id") or "")
        if asset_id and asset_id not in entity.asset_ids:
            raise ValueError("document route anchor names an asset outside its entity")
        if row.get("source_regime") != "eLibrary document" \
                or row.get("period_basis") != periods.AS_OF:
            raise ValueError("document slots require the exact eLibrary/as_of regime")
        if row.get("period_start") or row.get("period_end"):
            raise ValueError("document as-of slots cannot carry duration dates")
        instant = str(row.get("instant_date") or "")
        if instant:
            raise ValueError(
                "document denominator accepts route anchors only; dated occurrences "
                "belong in the separate occurrence-quality census")
        if not y0 <= int(row.get("reporting_year") or 0) <= y1:
            raise ValueError("document route-anchor reporting_year is outside the window")
        if row.get("unit_rule") != metric.unit_rule:
            raise ValueError("document slot unit contract differs from the registry")
        if row.get("requirement") not in allowed_requirements:
            raise ValueError("document slot has an unknown requirement classification")
        if row.get("source_health") not in _KNOWN_HEALTH:
            raise ValueError(f"unknown document source health {row.get('source_health')!r}")
        if row.get("obligation_evidence_kind") not in allowed_evidence:
            raise ValueError("document slot has an unknown evidence kind")
        if row.get("denominator_origin") not in allowed_origins:
            raise ValueError("document denominator origin is not independently declared")
        actual_scope = row.get("scope")
        if actual_scope is None:
            raise ValueError("document slot scope must be explicit (empty means unresolved)")
        if actual_scope != metric.scope:
            raise ValueError(
                "document route-anchor scope must be the registry route contract; "
                "occurrence scope is measured separately")
        expected_id = slot_id(
            str(row["entity_key"]), metric.id, "eLibrary document", periods.AS_OF,
            "", "", instant, str(actual_scope))
        if row.get("slot_id") != expected_id:
            raise ValueError(f"document slot_id differs from its exact grain: {metric.id}")
        if expected_id in seen_document_slots:
            raise ValueError(f"duplicate document slot {expected_id}")
        seen_document_slots.add(expected_id)
        pair = (str(row["entity_key"]), metric.id)
        if pair in seen_document_pairs:
            raise ValueError(f"duplicate document route anchor {pair}")
        seen_document_pairs.add(pair)
        normalised_documents.append(dict(
            row,
            instant_date=instant or None,
            frozen_at=stamp,
            frozen_run_id=run_id,
        ))
    normalised_documents.sort(key=lambda row: (
        row["entity_key"], row["metric_id"], row.get("instant_date") or "",
        row.get("scope") or "", row["slot_id"]))
    expected_document_pairs = {
        (entity.entity_key, metric.id)
        for entity in universe.entities
        for metric in document_metrics.values()
        if entity.template in metric.templates
    }
    if seen_document_pairs != expected_document_pairs:
        missing_document_pairs = sorted(expected_document_pairs - seen_document_pairs)
        extra_document_pairs = sorted(seen_document_pairs - expected_document_pairs)
        raise ValueError(
            "document route-anchor population does not equal the eligible universe: "
            f"missing={missing_document_pairs[:20]} extra={extra_document_pairs[:20]}")
    slots.extend(normalised_documents)

    # coverage.build_expected records the wall clock.  That helper predates the
    # clean-build contract; replace it with the caller's explicit build time so
    # a hidden current-date dependency cannot enter semantic comparisons.
    slots = [dict(slot, frozen_at=stamp, frozen_run_id=run_id) for slot in slots]
    slots.sort(key=lambda row: (
        row["entity_key"], row["source_regime"], int(row.get("reporting_year") or 0),
        row.get("reporting_period") or "", row["metric_id"], row["period_basis"],
        row.get("period_start") or "", row.get("instant_date") or ""))

    slot_keys = Counter((slot["entity_key"], slot.get("obligation_form"),
                         slot.get("reporting_year"), slot.get("reporting_period"))
                        for slot in slots)
    zero_slot = []
    for obligation in obligations:
        key = (obligation.entity_key, obligation.form, obligation.year, obligation.period)
        if slot_keys[key]:
            continue
        template = entities[obligation.entity_key].template
        if obligation.form in {"Form 549B IOC", "Form 549B Capacity"} and not obligation.as_of:
            reason = "snapshot/as-of grain unresolved; an undated slot is not emitted"
        else:
            reason = f"no {template} metric uses this periodic form"
        zero_slot.append({**obligation.as_row(), "as_of": obligation.as_of,
                          "reason": reason})

    event_ids = tuple(sorted({metric.id for metric in metric_rows
                              if metric.adapter != "oil_index"
                              and any(regime == "eLibrary document"
                                      for regime, _basis in metric.regimes)}))
    instrument_ids = tuple(sorted({metric.id for metric in metric_rows
                                   if metric.adapter == "oil_index"}))

    identity_payload = {
        "run_id": run_id,
        "as_of": today.isoformat(),
        "built_at": stamp,
        "window": [y0, y1],
        "universe_sha256": universe.sha256,
        "registry_version": REGISTRY_VERSION,
        "metrics": [{"id": metric.id, "templates": list(metric.templates),
                     "regimes": [list(regime) for regime in metric.regimes],
                     "concept": metric.concept, "scope": metric.scope,
                     "unit_rule": metric.unit_rule}
                    for metric in metric_rows],
        "occurrences": [dataclasses.asdict(row) for row in ordered_occurrences],
        "source_health_generation": health_generation,
        "source_health": [dataclasses.asdict(row) for row in health_rows],
        "taxonomy_pins": [dataclasses.asdict(row) for row in pin_rows],
        "applicability_identity": resolver_identity,
        "instrument_slots": normalised_instruments,
        "document_slots": normalised_documents,
    }
    return CoverageGrid(
        run_id=run_id,
        as_of=today.isoformat(),
        built_at=stamp,
        year_from=y0,
        year_to=y1,
        registry_version=REGISTRY_VERSION,
        input_digest=_digest(identity_payload),
        universe=universe,
        obligations=tuple(obligations),
        slots=tuple(slots),
        prior_eligibility_occurrences=tuple(prior),
        window_occurrences=tuple(window),
        separated_occurrences=tuple(separated),
        unused_source_health=tuple(row for row in health_rows if row.key not in used_health),
        zero_slot_obligations=tuple(zero_slot),
        event_driven_metric_ids=event_ids,
        instrument_metric_ids=instrument_ids,
    )


def measure_grid(grid: CoverageGrid, observations: Sequence[dict], *,
                 canonical: dict | None = None,
                 lineage: dict | None = None) -> CoverageMeasurement:
    """Measure one frozen grid without collapsing review or source states."""
    rows = coverage.measure(list(grid.slots), list(observations), grid.run_id,
                            canonical=canonical, lineage=lineage)
    return CoverageMeasurement(
        grid_input_digest=grid.input_digest,
        rows=tuple(rows),
        statistics=coverage.summarise(list(grid.slots), rows),
    )


__all__ = [
    "CoverageGrid", "CoverageMeasurement", "DEFAULT_UNIVERSE", "EntityEligibility",
    "FORM_ALIASES", "FrozenApplicability", "OccurrenceEvidence", "SourceHealthEvidence",
    "TEMPLATE_ELIGIBILITY_FORMS", "TaxonomyPin", "UniverseSnapshot",
    "build_coverage_grid", "load_universe", "measure_grid", "normalise_form",
]
