"""
Which concepts each FERC form actually collects, per taxonomy version.

This replaces the single-version, regex-based builder that the 7 September
independent check found three defects in:

  1. VERSION-BLIND. Applicability was generated from 2025-04-01 only, although
     the sampled filings spanned five taxonomy namespaces. Decisions are now
     keyed by (form, taxonomy_version) and generated for every version actually
     encountered.

  2. REGEX SCHEDULE CENSUS. `schemaLocation` was matched with a regex whose
     schedule-name pattern was numeric-only, so `sched-230a`, `sched-230b` and
     `sched-230c` were missed and the Form 2 entry point was counted as 75
     schedules instead of 78. Parsing is now namespace-aware XML.

  3. ABSENCE INFERRED FROM A FAILED FETCH. If a presentation linkbase could not
     be retrieved, the builder carried on and could then report a concept as
     "not in the form". An incomplete dependency set now yields
     APPLICABILITY_UNKNOWN, never NOT_REQUIRED.

A fourth distinction the check asked for is kept explicit throughout: a concept
appearing in a presentation linkbase proves the form COLLECTS it. It does not
prove an unconditional legal duty to report a non-blank value in every case.
That is `conditionality`, which comes from official instructions, not from the
schema, and defaults to "unknown".
"""

from __future__ import annotations

import collections
import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .http import Client, FetchError

XS = "http://www.w3.org/2001/XMLSchema"
XLINK = "http://www.w3.org/1999/xlink"

IN_FORM_YES = "yes"
IN_FORM_NO = "no"
IN_FORM_UNKNOWN = "unknown"

TAXONOMY_ROOT = "https://eCollection.ferc.gov/taxonomy"

#: entry-point layout per form family. Verified live for form2; form6 confirmed
#: reachable at the analogous path. A form absent from this map resolves its
#: entry point from a filing's own schemaRef instead, which is authoritative.
ENTRY_POINTS = {
    "Form 2":      ("form2", "form2",  "form-2_{v}.xsd"),
    "Form 2A":     ("form2", "form2A", "form-2A_{v}.xsd"),
    "Form 3Q Gas": ("form2", "form2Q", "form-2Q_{v}.xsd"),
    "Form 6":      ("form6", "form6",  "form-6_{v}.xsd"),
    "Form 6Q":     ("form6", "form6Q", "form-6Q_{v}.xsd"),
}


@dataclass
class FormTaxonomy:
    form: str
    version: str
    entry_point_url: str = ""
    schedules: list[tuple[str, str]] = field(default_factory=list)   # (page, folder)
    concepts: dict[str, tuple[str, str]] = field(default_factory=dict)  # concept -> (page, folder)
    complete: bool = False
    missing: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    error: str = ""


def version_from_namespace(ns: str) -> str:
    """'http://ferc.gov/form/2025-04-01/ferc' -> '2025-04-01'."""
    m = re.search(r"/form/(\d{4}-\d{2}-\d{2})/", ns or "")
    return m.group(1) if m else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_imports(xsd_bytes: bytes) -> list[str]:
    """Every schemaLocation on an xs:import/xs:include, namespace-aware.

    Returns raw relative or absolute locations in document order. No regex: an
    alphanumeric schedule name such as sched-230a is a normal attribute value
    and is returned like any other.
    """
    root = ET.fromstring(xsd_bytes)
    out = []
    for el in root.iter():
        if _local(el.tag) in ("import", "include", "redefine"):
            loc = el.get("schemaLocation")
            if loc:
                out.append(loc)
    return out


def parse_presentation_concepts(xml_bytes: bytes) -> set[str]:
    """Concept local-names referenced by a presentation linkbase's locators."""
    root = ET.fromstring(xml_bytes)
    found: set[str] = set()
    for el in root.iter():
        if _local(el.tag) != "loc":
            continue
        href = el.get(f"{{{XLINK}}}href") or ""
        frag = href.split("#", 1)[-1]
        if not frag:
            continue
        # fragments look like ferc-core_ConceptName or ferc_ConceptName or ConceptName
        found.add(frag.split("_", 1)[1] if "_" in frag else frag)
    return found


class ApplicabilityBuilder:
    """Builds and caches per-(form, taxonomy version) applicability."""

    def __init__(self, client: Client, logger=None):
        self.client = client
        self._cache: dict[tuple[str, str], FormTaxonomy] = {}
        self._log = logger or (lambda level, msg: None)

    # ----------------------------------------------------------- entry point

    def entry_point_url(self, form: str, version: str, schema_ref: str = "") -> str:
        """Prefer the form-family layout; fall back to a filing's own schemaRef,
        which is the authoritative statement of what that filing was built on."""
        ep = ENTRY_POINTS.get(form)
        if ep:
            family, folder, fname = ep
            return f"{TAXONOMY_ROOT}/{family}/{version}/form/{folder}/{fname.format(v=version)}"
        if schema_ref.startswith("http"):
            return schema_ref
        return ""

    # ----------------------------------------------------------- build

    def build(self, form: str, version: str, *, schema_ref: str = "") -> FormTaxonomy:
        key = (form, version)
        if key in self._cache:
            return self._cache[key]

        ft = FormTaxonomy(form=form, version=version)
        url = self.entry_point_url(form, version, schema_ref)
        ft.entry_point_url = url
        if not url:
            ft.error = (f"no entry point could be resolved for {form} {version}; "
                        "applicability is UNKNOWN, not 'not required'")
            self._cache[key] = ft
            return ft

        try:
            body, entry = self.client.get(url, source_system="taxonomy")
        except FetchError as exc:
            ft.error = f"entry point unavailable ({exc.detail}); applicability is UNKNOWN"
            self._log("warn", f"applicability {form} {version}: {ft.error}")
            self._cache[key] = ft
            return ft
        ft.sources.append({"artefact": "entry_point", "url": entry["source_url"],
                           "content_hash": entry["content_hash"], "retrieved": 1})

        # Resolve each schemaLocation against the entry point's own URL rather
        # than matching a path pattern. FERC has used at least two directory
        # layouts: `schedules/<folder>/sched-<page>_<ver>.xsd` from 2022 onward,
        # and `schedule/<Name>/form-2-sched-<Name>_<ver>.xsd` in the 2021
        # migration taxonomy. Pattern-matching one of them silently produced a
        # zero-schedule census for the other -- which then made every migrated-era
        # concept APPLICABILITY_UNKNOWN. Relative-URL resolution is layout-agnostic.
        for loc in parse_imports(body):
            if not loc.endswith(".xsd") or "sched" not in loc.lower():
                continue                    # the form's own default/core schema
            sched_url = urljoin(url, loc)
            stem = sched_url.rsplit("/", 1)[-1][:-4]
            folder = sched_url.rsplit("/", 2)[-2]
            page_m = re.search(r"sched-([0-9A-Za-z.]+?)_\d{4}-\d{2}-\d{2}$", stem)
            page = page_m.group(1) if page_m else re.sub(
                r"^form-\d[A-Za-z]*-sched-|_\d{4}-\d{2}-\d{2}$", "", stem)
            ft.schedules.append((page, folder))

            pre_url = sched_url[:-4] + "_pre.xml"
            try:
                pre_body, pre_entry = self.client.get(pre_url, source_system="taxonomy")
            except FetchError as exc:
                ft.missing.append(f"{stem}_pre.xml ({exc.detail})")
                ft.sources.append({"artefact": "presentation_linkbase", "url": pre_url,
                                   "content_hash": "", "retrieved": 0,
                                   "note": exc.detail})
                continue
            ft.sources.append({"artefact": "presentation_linkbase", "url": pre_entry["source_url"],
                               "content_hash": pre_entry["content_hash"], "retrieved": 1})
            try:
                for c in parse_presentation_concepts(pre_body):
                    ft.concepts.setdefault(c, (page, folder))
            except ET.ParseError as exc:
                ft.missing.append(f"{stem}_pre.xml (unparseable: {exc})")

        # An incomplete dependency set can never support a NOT_REQUIRED verdict.
        ft.complete = bool(ft.schedules) and not ft.missing
        if not ft.complete:
            ft.error = (f"{len(ft.missing)} of {len(ft.schedules)} presentation linkbases "
                        "unavailable; concept absence is UNKNOWN for this version")
        self._log("info", f"applicability {form} {version}: {len(ft.schedules)} schedules, "
                          f"{len(ft.concepts):,} concepts, complete={ft.complete}")
        self._cache[key] = ft
        return ft

    # ----------------------------------------------------------- query

    def status(self, form: str, version: str, concept: str,
               *, schema_ref: str = "") -> tuple[str, str, str]:
        """(in_form, schedule_page, evidence) for one concept."""
        ft = self.build(form, version, schema_ref=schema_ref)
        if concept in ft.concepts:
            page, folder = ft.concepts[concept]
            return (IN_FORM_YES, page,
                    f"present in presentation linkbase sched-{page}_{version} "
                    f"({folder}) of the {form} {version} entry point")
        if not ft.complete:
            return (IN_FORM_UNKNOWN, "",
                    f"APPLICABILITY_UNKNOWN for {form} {version}: {ft.error or 'incomplete taxonomy'}")
        return (IN_FORM_NO, "",
                f"absent from all {len(ft.schedules)} presentation linkbases imported by the "
                f"{form} {version} entry point; the schedule is not part of this form")

    def rows(self, form: str, version: str, concepts: list[str],
             *, schema_ref: str = "") -> list[dict]:
        """Rows for the `applicability` table."""
        ft = self.build(form, version, schema_ref=schema_ref)
        out = []
        for c in concepts:
            in_form, page, evidence = self.status(form, version, c, schema_ref=schema_ref)
            out.append({
                "form": form, "taxonomy_version": version, "concept_local": c,
                "in_form": in_form, "schedule_page": page,
                "schedule_name": ft.concepts.get(c, ("", ""))[1],
                # Presence in a schedule is not proof of an unconditional duty to
                # report a value. Conditionality comes from official instructions.
                "conditionality": "unknown",
                "evidence": evidence, "resolved_at": None})
        return out

    def taxonomy_source_rows(self, form: str, version: str) -> list[dict]:
        ft = self._cache.get((form, version))
        if not ft:
            return []
        return [{"form": form, "taxonomy_version": version, "artefact": s["artefact"],
                 "url": s["url"], "content_hash": s.get("content_hash", ""),
                 "retrieved": s.get("retrieved", 0), "note": s.get("note", "")}
                for s in ft.sources]


# ==================================================================== A05
# The eligible-universe obligation calendar.
#
# The defect this section exists to remove: the shipped denominator was built by
# `freeze_expected`, which iterates `[f for f in filings if f["is_canonical"]]`.
# A filing that could not be retrieved, or whose parser failed, produced no
# canonical row, produced no expected slot, and therefore *removed its own dated
# obligations from the denominator*. Northern Border Pipeline's eleven Index of
# Customers filings are all indexed in eLibrary and all fail to parse (a byte
# order mark before the required `H` record); the shipped grid contains eight
# undated APPLICABILITY_UNKNOWN slots for them instead of eleven dated ones.
#
# Everything below is derived from things that are true whether or not a fetch
# succeeded: who the filer is, which form family it is in and when that changed,
# what period the form covers, and when FERC says the filing is due. Retrieval
# health is recorded ALONGSIDE the obligation, never in place of it.


class ObligationState:
    """Where a dated obligation sits relative to its own official deadline."""

    FUTURE_NOT_DUE = "future_not_due"       # the deadline has not been reached
    DUE = "due"                             # the deadline has passed
    DEADLINE_UNRESOLVED = "deadline_unresolved"   # no cited FERC deadline rule


class SourceHealth:
    """What happened to the artefact that should answer an obligation.

    This is deliberately separate from the obligation itself. A parse failure is
    an engineering defect against a live obligation; it is never a reason to stop
    expecting the filing (contract rule 7).
    """

    OK = "ok"                               # indexed and parsed
    NOT_INDEXED = "not_indexed"             # the index WAS searched and showed nothing
    #: We never recorded what happened to this period's artefact. Distinct from
    #: NOT_INDEXED, which is a finding about FERC's index; this is a statement
    #: about our own bookkeeping. Defaulting to NOT_INDEXED asserted the search
    #: happened and came back empty -- a positive claim about FERC's holdings
    #: derived from nothing but a missing dictionary key.
    HEALTH_NOT_RECORDED = "health_not_recorded"
    INDEX_FAILED = "index_failed"           # the index search itself failed
    RETRIEVAL_FAILED = "retrieval_failed"   # located, download failed
    PARSE_FAILED = "parse_failed"           # retrieved, parser failed
    NOT_RETRIEVED = "not_retrieved"         # located, retrieval not attempted
    #: The filing was retrieved once and our own captured copy is no longer the
    #: bytes it claims to be -- a content-addressed object whose contents do not
    #: hash to its name. That is OUR storage failing, and it is deliberately
    #: distinct from PARSE_FAILED: the parser never saw the real file, so calling
    #: it a parse failure would blame the wrong component, and calling it a
    #: source condition would blame FERC for our lost bytes.
    CACHE_CORRUPT = "cache_corrupt"

    #: health states that mean OUR pipeline owes work, not that FERC has no data
    DEFECT = {INDEX_FAILED, RETRIEVAL_FAILED, PARSE_FAILED, NOT_RETRIEVED,
              CACHE_CORRUPT}
    #: not a defect and not a source condition -- an unanswered question
    UNDETERMINED = {HEALTH_NOT_RECORDED}


class EvidenceKind:
    """How an entity-year form obligation was established, strongest first."""

    FILED_OCCURRENCE = "filed_occurrence"       # a filing occurrence exists for that year
    INDEXED_OCCURRENCE = "indexed_occurrence"   # indexed but not parseable -- still evidence
    CARRIED_FORWARD = "carried_forward"         # established earlier, no evidenced change
    ROSTER_DECLARED = "roster_declared"         # the roster names the form, year unevidenced
    NONE = "unevidenced"                        # nothing establishes it -> UNKNOWN

    #: evidence that supports placing a slot in the CORE denominator
    CORE = {FILED_OCCURRENCE, INDEXED_OCCURRENCE, CARRIED_FORWARD}


#: Form families. Within a family a filer is on exactly ONE form in a given year
#: -- 18 CFR 260.1 (major) and 260.2 (non-major) are mutually exclusive, and the
#: threshold is crossed in a specific year. Modelling the family is what makes
#: Fayetteville Express's Form 2 -> Form 2-A move a *transition* rather than two
#: unrelated obligations, one of which would otherwise look permanently unmet.
FORM_FAMILIES: dict[str, tuple[str, ...]] = {
    "gas_annual":     ("Form 2", "Form 2A"),
    "gas_quarterly":  ("Form 3Q Gas",),
    "oil_annual":     ("Form 6",),
    "oil_quarterly":  ("Form 6Q",),
    "ioc":            ("Form 549B IOC",),
    "capacity":       ("Form 549B Capacity",),
    "intrastate":     ("Form 549D",),
}
FAMILY_OF_FORM = {f: fam for fam, forms in FORM_FAMILIES.items() for f in forms}

#: Which reporting periods each form actually has, and on what deadline rule.
#: Form 3-Q and Form 6-Q have NO fourth quarter: the annual report covers it.
#: Inventing a Q4 quarterly slot would manufacture a permanent 25% shortfall.
FORM_PERIODS: dict[str, tuple[str, ...]] = {
    "Form 2":              ("Q4",),          # annual, labelled Q4 by the pipeline
    "Form 2A":             ("Q4",),
    "Form 6":              ("Q4",),
    "Form 3Q Gas":         ("Q1", "Q2", "Q3"),
    "Form 6Q":             ("Q1", "Q2", "Q3"),
    "Form 549B IOC":       ("Q1", "Q2", "Q3", "Q4"),
    "Form 549B Capacity":  ("Q4",),
    "Form 549D":           ("Q1", "Q2", "Q3", "Q4"),
}

#: Sub-regimes carried INSIDE a host form's own filing. They are not separate
#: obligations and never get their own deadline; they inherit the host's.
HOSTED_REGIMES: dict[str, str] = {
    "Form 6 Page 700": "Form 6",
    "XBRL textblock":  "",          # host resolved per template's annual form
}

#: The obligation's legal authority. A slot whose authority is only "we filtered
#: eLibrary on a Class/Type" is NOT an established obligation and is classed
#: APPLICABILITY_UNKNOWN -- an index facet is a search convenience, not a duty.
FORM_AUTHORITY: dict[str, str] = {
    "Form 2":     "18 CFR 260.1 (major natural gas company annual report)",
    "Form 2A":    "18 CFR 260.2 (non-major natural gas company annual report)",
    "Form 3Q Gas": "18 CFR 260.300 (quarterly financial report, Q1-Q3)",
    "Form 6":     "18 CFR 357.2 (oil pipeline annual report)",
    "Form 6Q":    "18 CFR 357.2(a)(2) (oil pipeline quarterly report, Q1-Q3)",
    "Form 6 Page 700": "18 CFR 357.2; Page 700 is a schedule of the Form 6 filing",
    "Form 549B IOC": "18 CFR 284.13(c) (quarterly index of customers)",
    "Form 549B Capacity": "18 CFR 284.13(d)(2), Docket RM85-1-000 (capacity report)",
    "Form 549D":  "18 CFR 284.126(b) (NGPA 311 / Hinshaw quarterly report)",
}

#: Deadlines we can cite. Anything absent here yields DEADLINE_UNRESOLVED and is
#: never called overdue. Observed filing lag is a scheduling statistic and is
#: deliberately NOT used -- a median lag is not a legal deadline.
DEADLINE_UNRESOLVED_FORMS = {"Form 549B IOC", "Form 549D"}

#: first day of each quarter, for snapshot as-of derivation
QUARTER_START_MD = {"Q1": (1, 1), "Q2": (4, 1), "Q3": (7, 1), "Q4": (10, 1)}


@dataclass
class FormObligation:
    """One entity's duty to file one form for one reporting period."""

    entity_key: str
    form: str
    family: str
    year: int
    period: str
    authority: str
    evidence_kind: str
    evidence: str
    due_date: str = ""
    state: str = ObligationState.DEADLINE_UNRESOLVED
    source_health: str = SourceHealth.NOT_INDEXED
    health_detail: str = ""
    taxonomy_version: str = ""
    taxonomy_pin_state: str = "unresolved"
    #: for snapshot regimes, the as-of date the obligation is reported at. Taken
    #: from the occurrence FERC's index shows; where none was indexed it falls
    #: back to the date the rule itself names (18 CFR 284.13(c): the index of
    #: customers is stated as of the first day of the quarter). Never blank for a
    #: snapshot obligation, because an undated snapshot slot is satisfiable by
    #: any date and so measures nothing.
    as_of: str = ""

    @property
    def established(self) -> bool:
        return self.evidence_kind in EvidenceKind.CORE

    def as_row(self) -> dict:
        return {"entity_key": self.entity_key, "form": self.form, "family": self.family,
                "reporting_year": self.year, "reporting_period": self.period,
                "authority": self.authority, "evidence_kind": self.evidence_kind,
                "evidence": self.evidence, "due_date": self.due_date, "state": self.state,
                "source_health": self.source_health, "health_detail": self.health_detail,
                "taxonomy_version": self.taxonomy_version,
                "taxonomy_pin_state": self.taxonomy_pin_state}


class ObligationRegister:
    """Time-varying form obligations, built independently of retrieval success.

    Evidence goes in through `note_occurrence` (an occurrence seen in a FERC
    index, whatever later happened to it) and `note_roster`. `resolve` then turns
    that into one obligation per entity/form-family/year/period across the whole
    requested window, applying the continuity rule: an established obligation
    persists until evidence shows it changed, and a year with no evidence at all
    is APPLICABILITY_UNKNOWN -- never silently absent, and never assumed met.
    """

    def __init__(self, year_from: int, year_to: int, today: "dt.date",
                 *, taxonomy_pins: dict[tuple[str, int], str] | None = None):
        self.year_from, self.year_to = int(year_from), int(year_to)
        self.today = today
        self.pins = dict(taxonomy_pins or {})
        # (entity, family, year) -> {form: (kind, detail)}
        self._years: dict[tuple[str, str, int], dict[str, tuple[str, str]]] = {}
        # (entity, form, year, period) -> (health, detail)
        self._health: dict[tuple[str, str, int, str], tuple[str, str]] = {}
        self._as_of: dict[tuple[str, str, int, str], str] = {}
        self._roster: dict[str, set[str]] = {}
        self._nonmajor: set[str] = set()
        #: forms whose occurrences carry no periodic obligation, and how many.
        #: Inspect it: a form here that you expected to be in FORM_FAMILIES is a
        #: silently missing calendar, not an absence of duty.
        self.ignored_occurrences: "collections.Counter[str]" = collections.Counter()

    # ------------------------------------------------------------- evidence

    #: Snapshot regimes and the date their own rule states the report is made
    #: as of. 18 CFR 284.13(c) and the Form 549B manual put the index of
    #: customers as of the first day of the quarter, which is exactly the
    #: `snapshot_date` every parsed filing in the universe carries.
    SNAPSHOT_AS_OF = {"Form 549B IOC": "quarter_start"}

    def note_occurrence(self, entity_key: str, form: str, year: int | None,
                        period: str = "",
                        *, health: str = SourceHealth.HEALTH_NOT_RECORDED,
                        detail: str = "", indexed_only: bool = False,
                        as_of: str = "") -> None:
        """Record that FERC's own index shows this filer filed this form for this
        year. `health` says what happened to the artefact afterwards, which never
        affects whether the obligation exists.

        `health` defaults to HEALTH_NOT_RECORDED, not OK. A caller that knows the
        artefact was retrieved and parsed must say so, because only the caller
        knows: a row in the `filings` table is that evidence, an entry in an index
        listing is not. Defaulting to OK asserted a successful retrieval on behalf
        of every caller who never mentioned one."""
        fam = FAMILY_OF_FORM.get(form)
        if not fam or year is None:
            # Legitimately not a periodic-form occurrence -- 'eLibrary document'
            # is the big one, 4,977 filings in the delivered universe. But
            # returning in silence meant a form we had simply forgotten to map
            # looked identical to one deliberately out of scope. Counted, so the
            # caller can see what was skipped and why.
            self.ignored_occurrences[form or "(no form)"] += 1
            return
        kind = (EvidenceKind.INDEXED_OCCURRENCE if indexed_only or health in SourceHealth.DEFECT
                else EvidenceKind.FILED_OCCURRENCE)
        slot = self._years.setdefault((entity_key, fam, int(year)), {})
        prev = slot.get(form)
        if prev is None or prev[0] == EvidenceKind.INDEXED_OCCURRENCE:
            slot[form] = (kind, detail or f"{form} {year} occurrence in the FERC index")
        if period:
            key = (entity_key, form, int(year), period)
            cur = self._health.get(key)
            # a defect is never overwritten by a later OK for a DIFFERENT artefact
            if cur is None or (cur[0] == SourceHealth.OK and health != SourceHealth.OK):
                self._health[key] = (health, detail)
            if as_of:
                self._as_of[key] = as_of
        if form == "Form 2A":
            self._nonmajor.add(entity_key)

    def _snapshot_as_of(self, entity_key: str, form: str, year: int, period: str) -> str:
        seen = self._as_of.get((entity_key, form, year, period))
        if seen:
            return seen
        if self.SNAPSHOT_AS_OF.get(form) == "quarter_start":
            ms, ds = QUARTER_START_MD[period]
            return f"{year:04d}-{ms:02d}-{ds:02d}"
        return ""

    def note_roster(self, entity_key: str, forms) -> None:
        self._roster.setdefault(entity_key, set()).update(
            f for f in (forms or ()) if f in FAMILY_OF_FORM)

    # ------------------------------------------------------------- resolve

    def _form_for_year(self, entity_key: str, family: str, year: int
                       ) -> tuple[str, str, str]:
        """(form, evidence_kind, evidence) for one entity-family-year."""
        direct = self._years.get((entity_key, family, year))
        if direct:
            if len(direct) > 1:
                form = sorted(direct)[0]
                return (form, EvidenceKind.NONE,
                        f"two forms of the {family} family are evidenced for {year} "
                        f"({', '.join(sorted(direct))}); the governing form is unresolved")
            form, (kind, detail) = next(iter(direct.items()))
            return form, kind, detail
        # continuity: carry the most recent EVIDENCED form forward, never backward
        prior = [(y, d) for (e, fam, y), d in self._years.items()
                 if e == entity_key and fam == family and y < year and len(d) == 1]
        if prior:
            y, d = max(prior, key=lambda p: p[0])
            form = next(iter(d))
            return (form, EvidenceKind.CARRIED_FORWARD,
                    f"{form} is the most recently evidenced {family} obligation for this "
                    f"filer ({y}); 18 CFR obligations persist until the filer's own record "
                    f"shows a change, so {year} carries it forward")
        roster = {f for f in self._roster.get(entity_key, ()) if FAMILY_OF_FORM[f] == family}
        if len(roster) == 1:
            form = next(iter(roster))
            return (form, EvidenceKind.ROSTER_DECLARED,
                    f"the roster declares {form} for this filer, but no filing occurrence "
                    f"evidences the obligation in {year}; applicability is UNKNOWN")
        return "", EvidenceKind.NONE, ""

    def obligations(self) -> list[FormObligation]:
        from . import periods as _periods                       # local: avoid cycle

        entities = {e for (e, _f, _y) in self._years} | set(self._roster)
        families = sorted(FORM_FAMILIES)
        out: list[FormObligation] = []
        for entity_key in sorted(entities):
            for family in families:
                for year in range(self.year_from, self.year_to + 1):
                    form, kind, detail = self._form_for_year(entity_key, family, year)
                    if not form:
                        continue
                    nonmajor = entity_key in self._nonmajor
                    if form not in FORM_PERIODS:
                        # We classified the form into a family but never declared
                        # which periods it reports. Defaulting to () emitted zero
                        # obligations for it, which is a silent denominator
                        # shrink dressed as an absence of duty.
                        raise ValueError(
                            f"{form} is mapped to family {family!r} but has no "
                            "FORM_PERIODS entry, so its reporting calendar is "
                            "undeclared. Add it rather than emitting no obligation.")
                    for period in FORM_PERIODS[form]:
                        due = None
                        if form not in DEADLINE_UNRESOLVED_FORMS:
                            due = _periods.due_date(form, year, period, nonmajor=nonmajor)
                        if due is None:
                            state, due_s = ObligationState.DEADLINE_UNRESOLVED, ""
                        elif self.today <= due:
                            state, due_s = ObligationState.FUTURE_NOT_DUE, due.isoformat()
                        else:
                            state, due_s = ObligationState.DUE, due.isoformat()
                        health, hdetail = self._health.get(
                            (entity_key, form, year, period),
                            (SourceHealth.HEALTH_NOT_RECORDED,
                             "no retrieval outcome was recorded for this period; this "
                             "is a gap in our bookkeeping, not a finding about what "
                             "FERC holds"))
                        pin = self.pins.get((form, year), "")
                        out.append(FormObligation(
                            entity_key=entity_key, form=form, family=family, year=year,
                            period=period,
                            authority=FORM_AUTHORITY.get(form, "authority unresolved"),
                            evidence_kind=kind, evidence=detail, due_date=due_s, state=state,
                            source_health=health, health_detail=hdetail,
                            taxonomy_version=pin,
                            taxonomy_pin_state="pinned" if pin else "unresolved",
                            as_of=self._snapshot_as_of(entity_key, form, year, period)))
        return out
