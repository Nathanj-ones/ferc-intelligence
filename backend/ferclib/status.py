"""
The five independent status dimensions, and the coverage/field outcome vocabularies.

Revised template section 9: one overloaded label cannot carry availability,
origin, method, version and comparability at once. A single value can legally be
"migrated + derived + revised + valid with a unit warning". Collapsing those into
one column is what makes a source blank look like an extraction failure.

Every string here is a controlled value. Adapters import these constants; they
never invent status text, because the coverage report and the consumer exports
both switch on them.
"""

from __future__ import annotations


class Availability:
    """Is the value there, and if not, WHY not? Never a catch-all."""

    PRESENT = "present"
    FILED_NIL = "filed_nil"                       # xsi:nil — the filer explicitly said nothing
    SOURCE_BLANK = "source_blank"                 # required cell left empty. NOT zero.
    NOT_REQUIRED = "not_required"                 # the form does not collect it (evidence needed)
    NOT_APPLICABLE = "not_applicable"             # collected, but not applicable to this entity
    NOT_YET_DUE = "not_yet_due"                   # period exists, deadline not reached
    EXPECTED_NOT_LOCATED = "expected_not_located"  # should exist; index shows nothing
    KNOWN_NOT_RETRIEVED = "known_not_retrieved"   # source identified, download not done
    PARSE_FAILED = "parse_failed"                 # retrieved, could not be parsed
    RETRIEVAL_FAILED = "retrieval_failed"         # download/API failure, with the exact error
    NONPUBLIC = "nonpublic"                       # CEII/privileged — never bypass
    UNVERIFIED_AVAILABILITY = "unverified_availability"
    #: The source was retrieved and is COMPLETE, but its meaning is unresolved,
    #: so no value may be published. Distinct from UNVERIFIED_AVAILABILITY, which
    #: means we could not establish whether the source should exist at all --
    #: reporting a resolved-but-ambiguous aggregate as "applicability unknown"
    #: tells the reader the wrong story about which question is open.
    INTERPRETATION_BLOCKED = "interpretation_blocked"
    NOT_IMPLEMENTED = "not_implemented"           # OUR gap, not FERC's. Counted as unfinished work.

    #: availability values that mean "a real value is attached"
    POPULATED = {PRESENT}
    #: values that are a legitimate source condition, not an engineering failure
    SOURCE_CONDITION = {FILED_NIL, SOURCE_BLANK, NOT_REQUIRED, NOT_APPLICABLE,
                        NOT_YET_DUE, NONPUBLIC}
    #: values that indicate work we still owe
    OUR_GAP = {PARSE_FAILED, RETRIEVAL_FAILED, NOT_IMPLEMENTED, KNOWN_NOT_RETRIEVED}
    #: retrieved and complete, but semantically gated -- neither a source gap
    #: nor unfinished engineering
    SEMANTIC_GATE = {INTERPRETATION_BLOCKED}


class Origin:
    """Which system the bytes came from."""

    NATIVE_XBRL = "native_xbrl"
    FERC_MIGRATED = "ferc_migrated"               # migrated history is not "missing"
    DATAFERC_STRUCTURED = "dataferc_structured"
    ELIBRARY_DOCUMENT = "elibrary_document"
    ECOLLECTION_DOCUMENT = "ecollection_document"
    TAXONOMY = "taxonomy"


class Method:
    """How the number in front of the reader was arrived at."""

    FILED = "filed"                               # taken verbatim from a filed fact
    NORMALISED = "normalised"                     # relabelled/unit-normalised, value unchanged
    DERIVED = "derived"                           # computed from filed inputs; edges required
    DOCUMENT_EXTRACTED = "document_extracted"     # span-backed narrative/PDF extraction
    MANUALLY_CURATED = "manually_curated"         # reviewed from a cited FERC record

    #: derived values are NOT estimates or interpolations — they have full lineage
    NEEDS_LINEAGE = {DERIVED}


class VersionStatus:
    """Filing-occurrence version. Identical resubmission is not an economic change."""

    ORIGINAL = "original"
    IDENTICAL_RESUBMISSION = "identical_resubmission"
    REVISED = "revised"
    SUPERSEDED = "superseded"
    UNRESOLVED = "unresolved"


class Validation:
    """Comparability and quality. Separate from availability, deliberately:
    a comparison limitation does not make a valid filed value unavailable."""

    PASS = "pass"
    ROUNDING_WARNING = "rounding_warning"         # e.g. TGP's source-backed $2 identity gap
    UNIT_WARNING = "unit_warning"
    SOURCE_DATE_WARNING = "source_date_warning"
    SOURCE_ANOMALY_REVIEW = "source_anomaly_review"   # e.g. FEP's 999,999 — value kept as filed
    BLOCKED_AMBIGUITY = "blocked_ambiguity"       # grain/semantics unresolved
    SCOPE_INCOMPATIBLE = "scope_incompatible"     # valid value, invalid comparison
    NOT_YET_VALIDATED = "not_yet_validated"

    #: a value carrying one of these must never be published as an unqualified
    #: headline or fed into a ratio without its warning travelling with it
    MUST_PROPAGATE = {SOURCE_ANOMALY_REVIEW, BLOCKED_AMBIGUITY, SCOPE_INCOMPATIBLE,
                      UNIT_WARNING, ROUNDING_WARNING, SOURCE_DATE_WARNING}


class CoverageOutcome:
    """What happened to one frozen expected slot."""

    POPULATED_VALIDATED = "populated_validated"
    POPULATED_REVIEW = "populated_review"          # value present, carries a must-propagate flag
    POPULATED_UNVALIDATED = "populated_unvalidated"
    SOURCE_BLANK = "source_blank"
    NOT_REQUIRED = "not_required"
    NOT_APPLICABLE = "not_applicable"
    NOT_YET_DUE = "not_yet_due"
    APPLICABILITY_UNKNOWN = "applicability_unknown"
    INTERPRETATION_BLOCKED = "interpretation_blocked"
    RETRIEVAL_FAILED = "retrieval_failed"
    PARSE_FAILED = "parse_failed"
    SELECTOR_FAILED = "selector_failed"            # concept IS filed on a compatible basis and
                                                   # we still produced nothing — a real defect
    NOT_IMPLEMENTED = "not_implemented"

    #: counts toward the usable numerator
    USABLE = {POPULATED_VALIDATED, POPULATED_REVIEW}
    #: legitimately explained absences
    EXPLAINED_ABSENT = {SOURCE_BLANK, NOT_REQUIRED, NOT_APPLICABLE, NOT_YET_DUE,
                        INTERPRETATION_BLOCKED}
    #: our unfinished work or defects
    DEFECT = {RETRIEVAL_FAILED, PARSE_FAILED, SELECTOR_FAILED, NOT_IMPLEMENTED}


class FieldOutcome:
    """Task section 12: every requested template field ends in one of these."""

    IMPLEMENTED_VALIDATED = "implemented_retrieved_validated"
    IMPLEMENTED_SOURCE_ABSENT = "implemented_source_blank_or_not_required"
    INTERPRETATION_BLOCKED = "source_exists_interpretation_blocked"
    RETRIEVAL_FAILURE = "retrieval_or_access_failure"
    NOT_IMPLEMENTED = "not_implemented_unfinished_work"


# ---------------------------------------------------------------- helpers

def is_usable(availability: str, validation: str) -> bool:
    """A value the product may show. A warning does not make it unusable — it
    makes it a value that must carry its warning."""
    return availability in Availability.POPULATED and validation != Validation.BLOCKED_AMBIGUITY


def coverage_outcome(availability: str, validation: str) -> str:
    """Map an observation's status pair onto a coverage outcome."""
    if availability == Availability.PRESENT:
        if validation == Validation.PASS:
            return CoverageOutcome.POPULATED_VALIDATED
        if validation == Validation.NOT_YET_VALIDATED:
            return CoverageOutcome.POPULATED_UNVALIDATED
        return CoverageOutcome.POPULATED_REVIEW
    return {
        Availability.FILED_NIL: CoverageOutcome.SOURCE_BLANK,
        Availability.SOURCE_BLANK: CoverageOutcome.SOURCE_BLANK,
        Availability.NOT_REQUIRED: CoverageOutcome.NOT_REQUIRED,
        Availability.NOT_APPLICABLE: CoverageOutcome.NOT_APPLICABLE,
        Availability.NOT_YET_DUE: CoverageOutcome.NOT_YET_DUE,
        Availability.UNVERIFIED_AVAILABILITY: CoverageOutcome.APPLICABILITY_UNKNOWN,
        Availability.INTERPRETATION_BLOCKED: CoverageOutcome.INTERPRETATION_BLOCKED,
        Availability.EXPECTED_NOT_LOCATED: CoverageOutcome.RETRIEVAL_FAILED,
        Availability.KNOWN_NOT_RETRIEVED: CoverageOutcome.RETRIEVAL_FAILED,
        Availability.RETRIEVAL_FAILED: CoverageOutcome.RETRIEVAL_FAILED,
        Availability.NONPUBLIC: CoverageOutcome.NOT_APPLICABLE,
        Availability.PARSE_FAILED: CoverageOutcome.PARSE_FAILED,
        Availability.NOT_IMPLEMENTED: CoverageOutcome.NOT_IMPLEMENTED,
    }.get(availability, CoverageOutcome.APPLICABILITY_UNKNOWN)


ALL_AVAILABILITY = {v for k, v in vars(Availability).items()
                    if not k.startswith("_") and isinstance(v, str)}
ALL_VALIDATION = {v for k, v in vars(Validation).items()
                  if not k.startswith("_") and isinstance(v, str)}
