from datetime import datetime

from ferc_filter.regulatory_tracker import (
    RegulatoryFiling,
    track_regulatory_requests,
)


filings = [
    # ==========================================================
    # REAL FERC -> SPONSOR REQUEST
    # ==========================================================

    RegulatoryFiling(
        accession="20250924-3045",
        date=datetime(
            2025,
            9,
            24,
        ),
        event_type=(
            "ferc_information_request"
        ),
        description=(
            "Letter requesting Transcontinental Gas "
            "Pipe Line Company, LLC to file a response "
            "to data request within 5 days to assist "
            "in FERC's analysis of the proposal."
        ),
    ),

    # ==========================================================
    # EXPLICIT RESPONSE, BUT DELIBERATELY GIVEN THE WRONG
    # GENERIC CLASSIFIER EVENT TYPE.
    #
    # The relationship evidence should override that.
    # ==========================================================

    RegulatoryFiling(
        accession="20250930-5021",
        date=datetime(
            2025,
            9,
            30,
        ),
        event_type=(
            "authorization_request"
        ),
        description=(
            "Transcontinental Gas Pipe Line Company, "
            "LLC submits response to FERC's "
            "09/24/2025 data request re the "
            "certificate application."
        ),
    ),

    # ==========================================================
    # FALSE REQUEST
    #
    # This resembles the March 2026 record we found.
    # It must NOT create a sponsor obligation.
    # ==========================================================

    RegulatoryFiling(
        accession="20260304-4001",
        date=datetime(
            2026,
            3,
            4,
        ),
        event_type=(
            "ferc_information_request"
        ),
        description=(
            "Non-Decisional: Email communication "
            "dated 03/03/2026 from Robert Rutkowski "
            "re Environmental groups' request for "
            "rehearing for the project."
        ),
    ),

    # ==========================================================
    # UNRELATED RESPONSE
    # ==========================================================

    RegulatoryFiling(
        accession="20251014-5012",
        date=datetime(
            2025,
            10,
            14,
        ),
        event_type=(
            "applicant_followup"
        ),
        description=(
            "Transcontinental Gas Pipe Line Company, "
            "LLC submits response to FERC's "
            "10/01/2025 environmental information "
            "request."
        ),
    ),
]


requests = track_regulatory_requests(
    filings
)


print()
print("=" * 80)
print("REGULATORY TRACKER v0.2 TEST")
print("=" * 80)


# ==============================================================
# ASSERT ONLY ONE GENUINE REQUEST SURVIVES
# ==============================================================

assert len(requests) == 1

request = requests[0]


print()
print(
    "Request:",
    request.request_accession,
)

print(
    "Type:",
    request.request_type,
)

print(
    "Estimated due:",
    (
        request.estimated_due_date.strftime(
            "%Y-%m-%d"
        )
        if request.estimated_due_date
        else None
    ),
)

print(
    "Responses:",
    request.response_accessions,
)

print(
    "Response found:",
    request.response_found,
)

print(
    "First response:",
    (
        request.first_response_date.strftime(
            "%Y-%m-%d"
        )
        if request.first_response_date
        else None
    ),
)

print(
    "Timing:",
    request.response_timing,
)

print(
    "Status:",
    request.status,
)

print(
    "FERC disposition:",
    request.ferc_disposition,
)


# ==============================================================
# ASSERTIONS
# ==============================================================

assert (
    request.request_accession
    == "20250924-3045"
)

assert (
    request.request_type
    == "data_request"
)

assert (
    request.response_accessions
    == [
        "20250930-5021",
    ]
)

assert (
    request.response_found
    is True
)

assert (
    request.first_response_date.date()
    == datetime(
        2025,
        9,
        30,
    ).date()
)

assert (
    request.status
    == "response_submitted"
)

assert (
    request.ferc_disposition
    is None
)


# ==============================================================
# CP25-528 ONE-DAY REQUEST-DATE ALIAS REGRESSION
# ==============================================================

alias_filings = [
    RegulatoryFiling(
        accession="20260105-3037",
        date=datetime(
            2026,
            1,
            5,
        ),
        event_type="ferc_information_request",
        description=(
            "Letter requesting Eastern Gas Transmission "
            "and Storage, Inc. to file a response to "
            "environmental information request within "
            "5 days to assist in FERC's analysis."
        ),
    ),
    RegulatoryFiling(
        accession="20260112-5206",
        date=datetime(
            2026,
            1,
            12,
        ),
        event_type="applicant_followup",
        description=(
            "Eastern Gas Transmission and Storage, Inc. "
            "submits response to FERC's 01/06/2026 "
            "environmental information request."
        ),
    ),
    RegulatoryFiling(
        accession="20260112-5207",
        date=datetime(
            2026,
            1,
            12,
        ),
        event_type="applicant_followup",
        description=(
            "Eastern Gas Transmission and Storage, Inc. "
            "submits response to FERC's 01/06/2026 "
            "environmental information request."
        ),
    ),
]

alias_requests = track_regulatory_requests(
    alias_filings
)

assert len(alias_requests) == 1

alias_request = alias_requests[0]

assert (
    alias_request.request_accession
    == "20260105-3037"
)

assert (
    alias_request.response_accessions
    == [
        "20260112-5206",
        "20260112-5207",
    ]
)

assert alias_request.response_found is True
assert (
    alias_request.status
    == "response_submitted"
)


# ==============================================================
# AMBIGUITY SAFEGUARD
# ==============================================================

ambiguous_filings = alias_filings + [
    RegulatoryFiling(
        accession="20260106-3000",
        date=datetime(
            2026,
            1,
            6,
        ),
        event_type="ferc_information_request",
        description=(
            "Letter requesting Eastern Gas Transmission "
            "and Storage, Inc. to file a response to "
            "environmental information request within "
            "5 days to assist in FERC's analysis."
        ),
    ),
]

ambiguous_requests = track_regulatory_requests(
    ambiguous_filings
)

original_request = next(
    item
    for item in ambiguous_requests
    if item.request_accession
    == "20260105-3037"
)

exact_request = next(
    item
    for item in ambiguous_requests
    if item.request_accession
    == "20260106-3000"
)

assert original_request.response_found is False
assert exact_request.response_found is True
assert (
    exact_request.response_accessions
    == [
        "20260112-5206",
        "20260112-5207",
    ]
)


print()
print(
    "PASS | Genuine FERC sponsor request retained"
)

print(
    "PASS | Explicit response matched despite "
    "classifier event-type mismatch"
)

print(
    "PASS | Non-decisional rehearing email excluded"
)

print(
    "PASS | Unrelated later response excluded"
)

print(
    "PASS | No unsupported FERC disposition inferred"
)

print(
    "PASS | CP25-528 one-day request-date alias matched"
)

print(
    "PASS | Competing exact-date request blocks alias"
)

print()
print(
    "ALL REGULATORY TRACKER v0.2 TESTS PASSED"
)