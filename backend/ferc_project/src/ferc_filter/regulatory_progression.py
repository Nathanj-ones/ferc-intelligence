from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from ferc_filter.regulatory_tracker import (
    RegulatoryFiling,
    is_sponsor_directed_ferc_request,
)

# ==============================================================
# DATA MODELS
# ==============================================================

@dataclass
class ProgressionFiling:
    accession: str
    date: Optional[datetime]
    event_type: str
    description: str


@dataclass
class RegulatoryProgression:
    request_accession: str
    request_date: Optional[datetime]

    response_accessions: list[str] = field(
        default_factory=list
    )
    response_date: Optional[datetime] = None

    # Direct disposition means we found explicit evidence
    # connecting a later FERC action to this particular request.
    explicit_disposition: Optional[str] = None
    disposition_accession: Optional[str] = None

    # Later FERC information requests are retained as context,
    # but are NOT automatically assumed to concern the same issue.
    subsequent_request_accessions: list[str] = field(
        default_factory=list
    )

    # Project-level progression evidence is deliberately separate
    # from request-level disposition.
    subsequent_milestone_accession: Optional[str] = None
    subsequent_milestone_date: Optional[datetime] = None
    subsequent_milestone_type: Optional[str] = None
    subsequent_milestone_description: Optional[str] = None

    progression_status: str = "unresolved"
    progression_evidence: Optional[str] = None

    next_expected_action: Optional[str] = None
    schedule_signal: str = "none_identified"


# ==============================================================
# CONSTANTS
# ==============================================================

DIRECT_DISPOSITION_EVENT_TYPES = {
    "regulatory_decision",
    "delegated_order",
    "major_construction_authorization",
    "construction_or_service_authorization",
}


MATERIAL_MILESTONE_TYPES = {
    "environmental_milestone",
    "regulatory_decision",
    "major_construction_authorization",
    "construction_or_service_authorization",
}


# ==============================================================
# TEXT HELPERS
# ==============================================================

def normalize_text(
    text: str,
) -> str:
    return " ".join(
        (text or "").lower().split()
    )


DATE_PATTERN = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b"
)


def extract_referenced_dates(
    text: str,
) -> list[datetime]:

    dates = []

    for value in DATE_PATTERN.findall(
        text or ""
    ):
        try:
            dates.append(
                datetime.strptime(
                    value,
                    "%m/%d/%Y",
                )
            )
        except ValueError:
            continue

    return dates


# ==============================================================
# EXPLICIT RELATIONSHIP TESTS
# ==============================================================

def filing_references_request_date(
    filing: ProgressionFiling,
    request_date: Optional[datetime],
) -> bool:
    """
    True only when the later filing explicitly mentions
    the original request date.
    """

    if request_date is None:
        return False

    referenced_dates = (
        extract_referenced_dates(
            filing.description
        )
    )

    return any(
        value.date()
        == request_date.date()
        for value in referenced_dates
    )


def filing_references_request_accession(
    filing: ProgressionFiling,
    request_accession: str,
) -> bool:
    """
    Some records may explicitly reference an accession.
    This is uncommon, but it is strong relationship evidence.
    """

    if not request_accession:
        return False

    text = normalize_text(
        filing.description
    )

    return (
        request_accession.lower()
        in text
    )


def filing_explicitly_references_request(
    filing: ProgressionFiling,
    request_accession: str,
    request_date: Optional[datetime],
) -> bool:
    """
    v0.2 direct-link rule.

    A later filing is directly related to the request only
    when it explicitly references either:

        - the request accession, or
        - the request date.

    We deliberately do NOT infer direct relationship merely
    because the filing is later in the same docket.
    """

    return (
        filing_references_request_accession(
            filing,
            request_accession,
        )
        or
        filing_references_request_date(
            filing,
            request_date,
        )
    )


# ==============================================================
# DIRECT FERC DISPOSITION
# ==============================================================

def extract_disposition_language(
    filing: ProgressionFiling,
) -> Optional[str]:
    """
    Determine the action expressed by a directly linked
    FERC filing.

    This function is only called AFTER a direct documentary
    relationship has been established.
    """

    text = normalize_text(
        filing.description
    )

    positive_patterns = (
        "approving",
        "approved",
        "granting",
        "granted",
        "accepting",
        "accepted",
    )

    adverse_patterns = (
        "denying",
        "denied",
        "rejecting",
        "rejected",
    )

    if any(
        phrase in text
        for phrase in adverse_patterns
    ):
        if (
            "rejecting" in text
            or "rejected" in text
        ):
            return "rejected"

        return "denied"

    if any(
        phrase in text
        for phrase in positive_patterns
    ):
        if (
            "accepting" in text
            or "accepted" in text
        ):
            return "accepted"

        return "approved"

    return None


def find_direct_disposition(
    request_accession: str,
    request_date: Optional[datetime],
    response_date: Optional[datetime],
    filings: list[ProgressionFiling],
) -> tuple[
    Optional[str],
    Optional[ProgressionFiling],
]:
    """
    Find a direct FERC disposition of this particular request.

    Required:
        1. filing occurs after the sponsor response
        2. filing is a FERC decision/authorization type
        3. filing explicitly references this request
        4. filing contains explicit disposition language

    This prevents a much later certificate order from being
    treated as approval of every earlier information request.
    """

    if response_date is None:
        return None, None

    candidates = sorted(
        filings,
        key=lambda filing: (
            filing.date
            or datetime.max
        ),
    )

    for filing in candidates:

        if filing.date is None:
            continue

        if filing.date <= response_date:
            continue

        if (
            filing.event_type
            not in DIRECT_DISPOSITION_EVENT_TYPES
        ):
            continue

        if not filing_explicitly_references_request(
            filing,
            request_accession,
            request_date,
        ):
            continue

        disposition = (
            extract_disposition_language(
                filing
            )
        )

        if disposition is not None:
            return disposition, filing

    return None, None


# ==============================================================
# PROJECT-LEVEL MILESTONES
# ==============================================================

def is_material_milestone(
    filing: ProgressionFiling,
) -> bool:

    return (
        filing.event_type
        in MATERIAL_MILESTONE_TYPES
    )


def find_next_project_milestone(
    response_date: Optional[datetime],
    filings: list[ProgressionFiling],
) -> Optional[ProgressionFiling]:
    """
    Find the first material PROJECT milestone after the
    sponsor response.

    IMPORTANT:
        This is progression evidence only.

        It is NOT treated as direct disposition of the
        underlying information request.
    """

    if response_date is None:
        return None

    candidates = [
        filing
        for filing in filings
        if (
            filing.date is not None
            and filing.date > response_date
            and is_material_milestone(
                filing
            )
        )
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda filing: (
            filing.date
            or datetime.max
        )
    )

    return candidates[0]


# ==============================================================
# SUBSEQUENT FERC REQUESTS
# ==============================================================

def find_subsequent_requests(
    response_date: Optional[datetime],
    filings: list[ProgressionFiling],
) -> list[ProgressionFiling]:
    """
    Find later genuine FERC -> sponsor information requests.

    Reuses the validated regulatory-tracker request filter so
    classifier false positives cannot appear as sponsor
    obligations here.
    """

    if response_date is None:
        return []

    results = []

    for filing in filings:

        if filing.date is None:
            continue

        if filing.date <= response_date:
            continue

        tracker_filing = RegulatoryFiling(
            accession=filing.accession,
            date=filing.date,
            event_type=filing.event_type,
            description=filing.description,
        )

        if is_sponsor_directed_ferc_request(
            tracker_filing
        ):
            results.append(
                filing
            )

    results.sort(
        key=lambda filing: (
            filing.date
            or datetime.max
        )
    )

    return results


# ==============================================================
# RESPONSE DATE
# ==============================================================

def find_response_end_date(
    response_accessions: list[str],
    filings_by_accession: dict[
        str,
        ProgressionFiling,
    ],
) -> Optional[datetime]:

    dates = []

    for accession in response_accessions:

        filing = filings_by_accession.get(
            accession
        )

        if (
            filing is not None
            and filing.date is not None
        ):
            dates.append(
                filing.date
            )

    if not dates:
        return None

    return max(
        dates
    )


# ==============================================================
# STATUS
# ==============================================================

def determine_progression_status(
    response_found: bool,
    explicit_disposition: Optional[str],
    milestone_found: bool,
) -> str:
    """
    Status hierarchy:

        no response
            -> awaiting_response

        directly linked adverse disposition
            -> adverse

        directly linked positive disposition
            -> approved / accepted

        response + later project milestone
            -> progressed

        response only
            -> response_submitted
    """

    if not response_found:
        return "awaiting_response"

    if explicit_disposition in {
        "denied",
        "rejected",
    }:
        return "adverse"

    if explicit_disposition == "approved":
        return "approved"

    if explicit_disposition == "accepted":
        return "accepted"

    if milestone_found:
        return "progressed"

    return "response_submitted"


# ==============================================================
# SCHEDULE SIGNAL
# ==============================================================

def determine_schedule_signal(
    progression_status: str,
) -> str:

    if progression_status == "awaiting_response":
        return "pending_sponsor_action"

    if progression_status == "adverse":
        return "adverse_regulatory_signal"

    if progression_status in {
        "approved",
        "accepted",
        "progressed",
    }:
        return "progressing"

    if progression_status == "response_submitted":
        return "awaiting_further_regulatory_evidence"

    return "none_identified"


# ==============================================================
# NEXT EXPECTED ACTION
# ==============================================================

def determine_next_expected_action(
    progression_status: str,
) -> str:

    if progression_status == "awaiting_response":
        return "Sponsor response expected"

    if progression_status == "response_submitted":
        return (
            "FERC review, further information request, "
            "or subsequent regulatory action"
        )

    if progression_status == "progressed":
        return (
            "Continue through the next project stage"
        )

    if progression_status in {
        "approved",
        "accepted",
    }:
        return (
            "Project proceeds following explicit FERC action"
        )

    if progression_status == "adverse":
        return (
            "Review adverse FERC action and any "
            "required remedy, resubmission, or appeal"
        )

    return (
        "Further regulatory action not established"
    )


# ==============================================================
# BUILD ONE PROGRESSION
# ==============================================================

def build_regulatory_progression(
    request_accession: str,
    request_date: Optional[datetime],
    response_accessions: list[str],
    filings: list[ProgressionFiling],
) -> RegulatoryProgression:

    filings_by_accession = {
        filing.accession: filing
        for filing in filings
    }

    response_date = (
        find_response_end_date(
            response_accessions,
            filings_by_accession,
        )
    )

    response_found = bool(
        response_accessions
    )

    (
        explicit_disposition,
        disposition_filing,
    ) = find_direct_disposition(
        request_accession=(
            request_accession
        ),
        request_date=(
            request_date
        ),
        response_date=(
            response_date
        ),
        filings=filings,
    )

    milestone = (
        find_next_project_milestone(
            response_date,
            filings,
        )
    )

    subsequent_requests = (
        find_subsequent_requests(
            response_date,
            filings,
        )
    )

    progression_status = (
        determine_progression_status(
            response_found=(
                response_found
            ),
            explicit_disposition=(
                explicit_disposition
            ),
            milestone_found=(
                milestone is not None
            ),
        )
    )

    # ----------------------------------------------------------
    # EVIDENCE
    # ----------------------------------------------------------

    if explicit_disposition is not None:

        progression_evidence = (
            "Directly linked FERC disposition: "
            f"{explicit_disposition}"
        )

    elif milestone is not None:

        progression_evidence = (
            "No direct FERC disposition identified. "
            "Subsequent project milestone: "
            f"{milestone.event_type} "
            f"({milestone.accession})."
        )

    elif response_found:

        progression_evidence = (
            "Sponsor response identified, but no direct "
            "FERC disposition or later material milestone "
            "was identified."
        )

    else:

        progression_evidence = (
            "No sponsor response identified."
        )

    return RegulatoryProgression(
        request_accession=(
            request_accession
        ),
        request_date=(
            request_date
        ),
        response_accessions=list(
            response_accessions
        ),
        response_date=(
            response_date
        ),
        explicit_disposition=(
            explicit_disposition
        ),
        disposition_accession=(
            disposition_filing.accession
            if disposition_filing
            else None
        ),
        subsequent_request_accessions=[
            filing.accession
            for filing in subsequent_requests
        ],
        subsequent_milestone_accession=(
            milestone.accession
            if milestone
            else None
        ),
        subsequent_milestone_date=(
            milestone.date
            if milestone
            else None
        ),
        subsequent_milestone_type=(
            milestone.event_type
            if milestone
            else None
        ),
        subsequent_milestone_description=(
            milestone.description
            if milestone
            else None
        ),
        progression_status=(
            progression_status
        ),
        progression_evidence=(
            progression_evidence
        ),
        next_expected_action=(
            determine_next_expected_action(
                progression_status
            )
        ),
        schedule_signal=(
            determine_schedule_signal(
                progression_status
            )
        ),
    )


# ==============================================================
# BUILD ALL
# ==============================================================

def build_progressions_from_requests(
    requests,
    filings: list[ProgressionFiling],
) -> list[RegulatoryProgression]:

    progressions = []

    for request in requests:

        progressions.append(
            build_regulatory_progression(
                request_accession=(
                    request.request_accession
                ),
                request_date=(
                    request.request_date
                ),
                response_accessions=(
                    request.response_accessions
                ),
                filings=filings,
            )
        )

    progressions.sort(
        key=lambda item: (
            item.request_date
            or datetime.min
        )
    )

    return progressions