from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# ==============================================================
# DATA MODELS
# ==============================================================

@dataclass
class RegulatoryFiling:
    accession: str
    date: Optional[datetime]
    event_type: str
    description: str


@dataclass
class RegulatoryRequest:
    request_accession: str
    request_date: Optional[datetime]
    request_type: str

    response_period_days: Optional[int]
    response_period_type: Optional[str]
    estimated_due_date: Optional[datetime]

    response_accessions: list[str] = field(
        default_factory=list
    )
    response_dates: list[datetime] = field(
        default_factory=list
    )

    response_found: bool = False
    first_response_date: Optional[datetime] = None
    response_timing: str = "unknown"

    status: str = "awaiting_response"

    # Do not infer this merely from a sponsor response.
    ferc_disposition: Optional[str] = None

    next_expected_action: str = (
        "Sponsor response expected"
    )


# ==============================================================
# TEXT HELPERS
# ==============================================================

def normalize_text(text: str) -> str:
    return " ".join(
        (text or "").lower().split()
    )


# ==============================================================
# DATE REFERENCES
# ==============================================================

DATE_PATTERN = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b"
)


def extract_referenced_dates(
    text: str,
) -> list[datetime]:

    dates = []

    for match in DATE_PATTERN.findall(
        text or ""
    ):
        try:
            dates.append(
                datetime.strptime(
                    match,
                    "%m/%d/%Y",
                )
            )
        except ValueError:
            continue

    return dates


# ==============================================================
# RESPONSE PERIOD
# ==============================================================

RESPONSE_PERIOD_PATTERN = re.compile(
    r"\bwithin\s+"
    r"(\d+)\s+"
    r"(business\s+days|calendar\s+days|days)"
    r"\b",
    re.IGNORECASE,
)


def extract_response_period(
    text: str,
) -> tuple[
    Optional[int],
    Optional[str],
]:

    match = RESPONSE_PERIOD_PATTERN.search(
        text or ""
    )

    if not match:
        return None, None

    days = int(
        match.group(1)
    )

    raw_type = (
        match.group(2)
        .lower()
        .strip()
    )

    if raw_type == "business days":
        period_type = "business_days"
    else:
        period_type = "calendar_days"

    return days, period_type


# ==============================================================
# DUE DATE
# ==============================================================

def calculate_due_date(
    request_date: Optional[datetime],
    days: Optional[int],
    period_type: Optional[str],
) -> Optional[datetime]:

    if (
        request_date is None
        or days is None
        or period_type is None
    ):
        return None

    if period_type == "calendar_days":
        return request_date + timedelta(
            days=days
        )

    if period_type == "business_days":

        current = request_date
        added = 0

        while added < days:
            current += timedelta(
                days=1
            )

            if current.weekday() < 5:
                added += 1

        return current

    return None


# ==============================================================
# REQUEST TYPE
# ==============================================================

def classify_request_type(
    description: str,
) -> str:

    text = normalize_text(
        description
    )

    if (
        "environmental information request"
        in text
        or
        "environmental informational request"
        in text
    ):
        return "environmental_information"

    if "data request" in text:
        return "data_request"

    if "information request" in text:
        return "information_request"

    return "other_information_request"


# ==============================================================
# GENUINE FERC -> SPONSOR REQUEST
# ==============================================================

def is_sponsor_directed_ferc_request(
    filing: RegulatoryFiling,
) -> bool:
    """
    Determine whether the filing actually represents FERC
    asking the project sponsor to provide information.

    The classifier's event type is useful evidence, but is
    not sufficient by itself.

    We require request language directed toward a filer/
    applicant/company obligation.

    This deliberately excludes things such as:
        Non-Decisional email re a third party's request
        for rehearing.
    """

    if (
        filing.event_type
        != "ferc_information_request"
    ):
        return False

    text = normalize_text(
        filing.description
    )

    # Explicitly reject obvious non-decisional material.
    if "non-decisional" in text:
        return False

    # The strongest pattern in the real FERC records we've
    # inspected.
    sponsor_request_patterns = (
        "letter requesting ",
        "to file a response to data request",
        "to file a response to environmental information request",
        "to file a response to environmental informational request",
        "to file a response to information request",
        "to provide additional information",
        "to submit additional information",
    )

    if not any(
        pattern in text
        for pattern in sponsor_request_patterns
    ):
        return False

    # Require evidence that information/action is actually
    # being requested, rather than merely discussing another
    # party's request.
    information_patterns = (
        "data request",
        "information request",
        "informational request",
        "additional information",
    )

    return any(
        pattern in text
        for pattern in information_patterns
    )


# ==============================================================
# EXPLICIT RESPONSE LANGUAGE
# ==============================================================

def has_explicit_response_language(
    description: str,
) -> bool:
    """
    Identify sponsor filings that explicitly say they answer
    or supplement a prior FERC request.

    We intentionally do not depend on the classifier's
    event_type here.
    """

    text = normalize_text(
        description
    )

    response_patterns = (
        "submits response to ferc",
        "submit response to ferc",
        "response to ferc's",
        "response to ferc’s",
        "supplemental response to ferc",
        "supplemental response to ferc's",
        "supplemental response to ferc’s",
    )

    return any(
        pattern in text
        for pattern in response_patterns
    )


def _response_request_type(
    description: str,
) -> Optional[str]:
    """
    Identify the type of FERC request explicitly named by a
    sponsor response. Returns None when the response does not
    clearly identify a supported request type.
    """

    text = normalize_text(
        description
    )

    if (
        "environmental information request"
        in text
        or
        "environmental informational request"
        in text
    ):
        return "environmental_information"

    if "data request" in text:
        return "data_request"

    if "information request" in text:
        return "information_request"

    return None


def _request_types_compatible(
    request_type: str,
    response_type: Optional[str],
) -> bool:
    if response_type is None:
        return False

    if request_type == response_type:
        return True

    # Generic "information request" may safely describe a more
    # specific data/environmental information request.
    return (
        response_type == "information_request"
        and request_type in {
            "data_request",
            "environmental_information",
            "information_request",
        }
    )


def response_explicitly_references_request(
    request: RegulatoryFiling,
    response: RegulatoryFiling,
    filings: Optional[list[RegulatoryFiling]] = None,
) -> bool:
    """
    Match based on explicit documentary relationship.

    Primary evidence:
        - response occurs on/after request
        - filing explicitly uses response-to-FERC language
        - filing explicitly references the request date

    Controlled alias:
        Some eLibrary records are accessioned one calendar day
        before/after the date later cited by the sponsor. A +/-1
        day alias is allowed only when:
            - the response explicitly identifies a FERC request
            - request types are compatible
            - there is no competing genuine FERC request on the
              referenced date

    The response's classifier event_type is deliberately NOT
    required to equal applicant_followup.
    """

    if (
        request.date is None
        or response.date is None
    ):
        return False

    if response.date < request.date:
        return False

    if (
        response.accession
        == request.accession
    ):
        return False

    if not has_explicit_response_language(
        response.description
    ):
        return False

    referenced_dates = (
        extract_referenced_dates(
            response.description
        )
    )

    request_day = (
        request.date.date()
    )

    # Exact documentary date remains the strongest match.
    if any(
        referenced.date()
        == request_day
        for referenced in referenced_dates
    ):
        return True

    # Preserve old standalone behaviour when the caller does not
    # provide the full filing universe.
    if filings is None:
        return False

    request_type = classify_request_type(
        request.description
    )
    response_type = _response_request_type(
        response.description
    )

    if not _request_types_compatible(
        request_type,
        response_type,
    ):
        return False

    for referenced in referenced_dates:
        referenced_day = referenced.date()

        if abs(
            (
                referenced_day
                - request_day
            ).days
        ) != 1:
            continue

        competing_requests = [
            filing
            for filing in filings
            if (
                filing.accession
                != request.accession
                and filing.date is not None
                and filing.date.date()
                == referenced_day
                and is_sponsor_directed_ferc_request(
                    filing
                )
                and _request_types_compatible(
                    classify_request_type(
                        filing.description
                    ),
                    response_type,
                )
            )
        ]

        if competing_requests:
            continue

        return True

    return False


# ==============================================================
# RESPONSE TIMING
# ==============================================================

def determine_response_timing(
    first_response_date: Optional[datetime],
    estimated_due_date: Optional[datetime],
) -> str:

    if first_response_date is None:
        return "no_response"

    if estimated_due_date is None:
        return (
            "response_found_due_date_unknown"
        )

    if (
        first_response_date.date()
        <= estimated_due_date.date()
    ):
        return (
            "on_or_before_estimated_due_date"
        )

    # Do not call this "late" yet. Our deadline model does
    # not currently account for federal holidays or all FERC
    # procedural deadline rules.
    return "after_estimated_due_date"


# ==============================================================
# BUILD ONE REQUEST
# ==============================================================

def build_regulatory_request(
    request: RegulatoryFiling,
    filings: list[RegulatoryFiling],
) -> RegulatoryRequest:

    (
        response_period_days,
        response_period_type,
    ) = extract_response_period(
        request.description
    )

    estimated_due_date = (
        calculate_due_date(
            request.date,
            response_period_days,
            response_period_type,
        )
    )

    matched_responses = [
        filing
        for filing in filings
        if response_explicitly_references_request(
            request,
            filing,
            filings,
        )
    ]

    matched_responses.sort(
        key=lambda filing: (
            filing.date
            or datetime.max
        )
    )

    response_dates = [
        filing.date
        for filing in matched_responses
        if filing.date is not None
    ]

    first_response_date = (
        response_dates[0]
        if response_dates
        else None
    )

    response_found = bool(
        matched_responses
    )

    response_timing = (
        determine_response_timing(
            first_response_date,
            estimated_due_date,
        )
    )

    if response_found:
        status = "response_submitted"

        next_expected_action = (
            "FERC review, further information "
            "request, or subsequent regulatory action"
        )

    else:
        status = "awaiting_response"

        next_expected_action = (
            "Sponsor response expected"
        )

    return RegulatoryRequest(
        request_accession=(
            request.accession
        ),
        request_date=(
            request.date
        ),
        request_type=(
            classify_request_type(
                request.description
            )
        ),
        response_period_days=(
            response_period_days
        ),
        response_period_type=(
            response_period_type
        ),
        estimated_due_date=(
            estimated_due_date
        ),
        response_accessions=[
            filing.accession
            for filing in matched_responses
        ],
        response_dates=(
            response_dates
        ),
        response_found=(
            response_found
        ),
        first_response_date=(
            first_response_date
        ),
        response_timing=(
            response_timing
        ),
        status=status,
        ferc_disposition=None,
        next_expected_action=(
            next_expected_action
        ),
    )


# ==============================================================
# TRACK ALL REQUESTS
# ==============================================================

def track_regulatory_requests(
    filings: list[RegulatoryFiling],
) -> list[RegulatoryRequest]:
    """
    Track genuine FERC -> sponsor information requests.

    v0.2 improvements:
        - classifier label alone cannot create an obligation
        - non-decisional false requests are excluded
        - explicit response relationships override generic
          classifier event labels
    """

    requests = [
        filing
        for filing in filings
        if is_sponsor_directed_ferc_request(
            filing
        )
    ]

    tracked = [
        build_regulatory_request(
            request,
            filings,
        )
        for request in requests
    ]

    tracked.sort(
        key=lambda item: (
            item.request_date
            or datetime.min
        )
    )

    return tracked