"""General-purpose features extracted from FERC filing metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilingFeatures:
    """General features used by the project-side classifier."""

    actor: str
    action: str
    event_area: str
    subject_role: str
    materiality_signal: str
    routine_signal: bool
    recurring_signal: bool
    followup_signal: bool
    document_class: str | None
    document_type: str | None


def _lower(record: dict) -> str:
    return (record.get("doc_desc") or "").lower()


def infer_actor(record: dict) -> str:
    """Infer the broad actor responsible for the filing."""

    text = _lower(record)

    if record.get("category") == "Issuance":
        return "ferc"

    if any(
        phrase in text
        for phrase in (
            "court of appeals",
            "petition for review",
            "district court",
        )
    ):
        return "court"

    if any(
        phrase in text
        for phrase in (
            "comments of",
            "comment of",
            "comments and protest",
            "motion to intervene",
            "motion for leave to intervene",
            "intervention",
            "in opposition to",
        )
    ):
        return "third_party"

    if any(
        phrase in text
        for phrase in (
            "submits",
            "submit",
            "applicant",
            "company submits",
            "request for",
            "application",
        )
    ):
        return "applicant"

    return "unknown"


def infer_action(record: dict) -> str:
    """
    Infer the primary action.

    Explicit FERC phrasing and Class/Type metadata are preferred over
    loose word matching.
    """

    text = _lower(record)

    document_class = (
        record.get("document_class") or ""
    ).lower()

    document_type = (
        record.get("document_type") or ""
    ).lower()

    # ---------------------------------------
    # FERC-issued decisions / determinations
    # ---------------------------------------

    if record.get("category") == "Issuance":
        if any(
            phrase in text
            for phrase in (
                "granting",
                "grants",
                "approving",
                "approval",
                "order issuing",
                "issuing certificates",
                "authorized",
            )
        ):
            return "grant_or_issue"

        if "notice of availability" in text:
            return "grant_or_issue"

    # ---------------------------------------
    # Explicit requests
    # ---------------------------------------

    if any(
        phrase in text
        for phrase in (
            "request for authorization",
            "request to construct",
            "request to install",
            "request for extension",
            "request for delay",
            "request for rehearing",
            "request for stay",
            "prior notice request",
            "submits a request",
            "submit request",
            "application for",
        )
    ):
        return "request"

    # FERC requesting a response is itself a request.
    if (
        record.get("category") == "Issuance"
        and "requesting" in text
        and any(
            phrase in text
            for phrase in (
                "to file a response",
                "data request",
                "information request",
            )
        )
    ):
        return "request"

    # ---------------------------------------
    # Responses / supplemental material
    # ---------------------------------------

    if any(
        phrase in text
        for phrase in (
            "submits response to",
            "submit response to",
            "response to ferc",
            "response to the",
            "in response to",
        )
    ):
        return "response"

    if (
        "supplemental" in text
        and "response" in text
    ):
        return "response"

    # ---------------------------------------
    # Comments / intervention
    # ---------------------------------------

    if (
        "comments/protest" in document_class
        or "comment on filing" in document_type
        or "procedural motion" in document_type
    ):
        if any(
            phrase in text
            for phrase in (
                "comment",
                "protest",
                "motion",
            )
        ):
            return "comment_or_intervention"

    if any(
        phrase in text
        for phrase in (
            "comments of",
            "comment of",
            "comments and protest",
            "comments on",
            "comment on",
            "motion to intervene",
            "motion for leave to intervene",
            "intervention",
        )
    ):
        return "comment_or_intervention"

    # ---------------------------------------
    # Routine document types
    # ---------------------------------------

    if (
        document_class == "transcript"
        or "conference/meeting transcript" in document_type
    ):
        return "transcript"

    if "report" in text:
        return "report"

    if (
        "memo" in text
        or "memorandum" in text
    ):
        return "memo"

    return "unknown"


def infer_event_area(record: dict) -> str:
    """Infer the broad regulatory subject."""

    text = _lower(record)

    document_type = (
        record.get("document_type") or ""
    ).lower()

    # Environmental lifecycle documents.
    if (
        "environmental assessment" in document_type
        or "environmental impact statement" in document_type
        or any(
            phrase in text
            for phrase in (
                "environmental impact statement",
                "environmental assessment",
                "environmental review",
                "environmental information",
                "section 7 consultation",
                "section 106",
                "scoping",
            )
        )
    ):
        return "environmental_review"

    # Legal/challenge.
    if any(
        phrase in text
        for phrase in (
            "rehearing",
            "stay",
            "court of appeals",
            "petition for review",
            "appeal",
            "litigation",
        )
    ):
        return "legal_or_challenge"

    # Schedule.
    if any(
        phrase in text
        for phrase in (
            "extension of time",
            "deadline",
            "delay",
            "schedule",
        )
    ):
        return "schedule"

    # Scope/design.
    if any(
        phrase in text
        for phrase in (
            "variance",
            "design modification",
            "amendment",
            "modification",
        )
    ):
        return "scope_or_design_change"

    # Construction/service.
    if any(
        phrase in text
        for phrase in (
            "construction",
            "construct",
            "in-service",
            "place in service",
            "commence service",
        )
    ):
        return "construction_or_service"

    # Authorization.
    if any(
        phrase in text
        for phrase in (
            "certificate",
            "prior notice",
            "blanket authorization",
            "authorization",
            "authorize",
        )
    ):
        return "authorization"

    # Compliance.
    if any(
        phrase in text
        for phrase in (
            "compliance",
            "implementation plan",
            "inspection",
        )
    ):
        return "compliance"

    return "other"


def infer_subject_role(record: dict) -> str:
    """Identify the broad object of the filing."""

    text = _lower(record)

    if any(
        phrase in text
        for phrase in (
            "certificate application",
            "certificate of public convenience",
            "prior notice",
            "blanket authorization",
            "certificate",
        )
    ):
        return "authorization"

    if any(
        phrase in text
        for phrase in (
            "environmental impact statement",
            "environmental assessment",
            "environmental review",
            "environmental information",
            "section 7 consultation",
            "section 106",
        )
    ):
        return "environmental_review"

    if any(
        phrase in text
        for phrase in (
            "construction",
            "construct",
            "in-service",
            "place in service",
            "facility",
            "pipeline",
            "terminal",
        )
    ):
        return "project_facility"

    if any(
        phrase in text
        for phrase in (
            "extension of time",
            "deadline",
            "schedule",
            "delay",
        )
    ):
        return "schedule"

    if any(
        phrase in text
        for phrase in (
            "rehearing",
            "stay",
            "appeal",
        )
    ):
        return "legal_status"

    if any(
        phrase in text
        for phrase in (
            "condition",
            "compliance",
            "inspection",
        )
    ):
        return "compliance"

    return "unknown"


def infer_materiality_signal(record: dict) -> str:
    """Look for general signals that an event could affect the project."""

    text = _lower(record)

    if any(
        phrase in text
        for phrase in (
            "extension of time",
            "deadline",
            "delay",
            "suspend construction",
            "suspension",
            "stop work",
            "cease construction",
            "abandon",
            "cancel",
            "termination",
        )
    ):
        return "timing_or_status"

    if any(
        phrase in text
        for phrase in (
            "capacity",
            "million",
            "billion",
            "cost",
            "capital",
            "design capacity",
        )
    ):
        return "economics_or_capacity"

    if any(
        phrase in text
        for phrase in (
            "certificate",
            "certificates",
            "authorized",
            "authorization",
            "place in service",
            "commence service",
            "approval",
            "approved",
        )
    ):
        return "regulatory_status"

    if any(
        phrase in text
        for phrase in (
            "major",
            "material",
            "substantial",
            "entire project",
            "project-wide",
        )
    ):
        return "potentially_material"

    return "none"


def infer_routine_signal(record: dict) -> bool:
    """Identify high-confidence routine wording or filing types."""

    text = _lower(record)

    document_class = (
        record.get("document_class") or ""
    ).lower()

    document_type = (
        record.get("document_type") or ""
    ).lower()

    # Strong Class/Type indicators.
    if document_class == "transcript":
        return True

    if "conference/meeting transcript" in document_type:
        return True

    if "comment on filing" in document_type:
        return True

    if "procedural motion" in document_type:
        return True

    # Strong recurring-language indicators.
    return any(
        phrase in text
        for phrase in (
            "weekly noise data report",
            "weekly construction status report",
            "monthly status report",
            "quarterly update",
            "errata to the",
            "errata notice",
        )
    )


def infer_recurring_signal(record: dict) -> bool:
    """Identify recurring reporting language."""

    text = _lower(record)

    return any(
        phrase in text
        for phrase in (
            "weekly",
            "monthly",
            "quarterly",
            "annual",
            "report no.",
            "report number",
        )
    )


def infer_followup_signal(record: dict) -> bool:
    """Identify language indicating the filing follows an existing event."""

    text = _lower(record)

    return any(
        phrase in text
        for phrase in (
            "response to",
            "in response to",
            "comments on",
            "comment on",
            "supplemental to",
            "supplemental information",
            "errata to",
            "in support of",
            "answer to",
            "associated with",
            "related to",
        )
    )


def get_document_class(record: dict) -> str | None:
    """Return enriched FERC document class when available."""

    value = record.get("document_class")

    if value:
        return str(value)

    return None


def get_document_type(record: dict) -> str | None:
    """Return enriched FERC document type when available."""

    value = record.get("document_type")

    if value:
        return str(value)

    return None


def extract_features(record: dict) -> FilingFeatures:
    """Extract the general classification features."""

    return FilingFeatures(
        actor=infer_actor(record),
        action=infer_action(record),
        event_area=infer_event_area(record),
        subject_role=infer_subject_role(record),
        materiality_signal=infer_materiality_signal(record),
        routine_signal=infer_routine_signal(record),
        recurring_signal=infer_recurring_signal(record),
        followup_signal=infer_followup_signal(record),
        document_class=get_document_class(record),
        document_type=get_document_type(record),
    )