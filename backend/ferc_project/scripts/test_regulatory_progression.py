from datetime import datetime

from ferc_filter.regulatory_progression import (
    ProgressionFiling,
    build_regulatory_progression,
)


filings = [
    ProgressionFiling(
        accession="20250422-3028",
        date=datetime(2025, 4, 22),
        event_type="ferc_information_request",
        description=(
            "Letter requesting sponsor to file a response "
            "within 15 days."
        ),
    ),

    ProgressionFiling(
        accession="20250507-5159",
        date=datetime(2025, 5, 7),
        event_type="applicant_followup",
        description=(
            "Sponsor submits response to FERC's "
            "04/22/2025 environmental information request."
        ),
    ),

    # Later project milestone.
    ProgressionFiling(
        accession="20251031-3000",
        date=datetime(2025, 10, 31),
        event_type="environmental_milestone",
        description=(
            "Environmental Assessment for the project."
        ),
    ),

    # Much later certificate order. It approves the project,
    # but DOES NOT explicitly reference the 04/22 request.
    ProgressionFiling(
        accession="20260129-3076",
        date=datetime(2026, 1, 29),
        event_type="regulatory_decision",
        description=(
            "Order Issuing Certificate and Approving "
            "Abandonment for the project."
        ),
    ),
]


progression = build_regulatory_progression(
    request_accession="20250422-3028",
    request_date=datetime(
        2025,
        4,
        22,
    ),
    response_accessions=[
        "20250507-5159",
    ],
    filings=filings,
)


print()
print("=" * 80)
print("REGULATORY PROGRESSION v0.2 TEST")
print("=" * 80)

print(
    "Request:",
    progression.request_accession,
)

print(
    "Responses:",
    progression.response_accessions,
)

print(
    "Explicit disposition:",
    progression.explicit_disposition,
)

print(
    "Disposition accession:",
    progression.disposition_accession,
)

print(
    "Subsequent milestone:",
    progression.subsequent_milestone_accession,
)

print(
    "Progression status:",
    progression.progression_status,
)

print(
    "Progression evidence:",
    progression.progression_evidence,
)

print(
    "Next expected action:",
    progression.next_expected_action,
)

print(
    "Schedule signal:",
    progression.schedule_signal,
)


# ==============================================================
# ASSERTIONS
# ==============================================================

assert (
    progression.explicit_disposition
    is None
)

assert (
    progression.disposition_accession
    is None
)

assert (
    progression.subsequent_milestone_accession
    == "20251031-3000"
)

assert (
    progression.progression_status
    == "progressed"
)

assert (
    progression.schedule_signal
    == "progressing"
)


print()
print(
    "PASS | Later certificate order was NOT "
    "misidentified as request disposition"
)

print(
    "PASS | No unsupported direct approval inferred"
)

print(
    "PASS | Environmental milestone retained as "
    "project-level progression evidence"
)

print(
    "PASS | Historical request correctly marked progressed"
)

print()
print(
    "ALL REGULATORY PROGRESSION v0.2 TESTS PASSED"
)