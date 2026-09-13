from datetime import datetime

from ferc_filter.activity_grouper import (
    ActivityGroup,
    ActivityRecord,
)

from ferc_filter.activity_summarizer import (
    summarize_activity_group,
    activity_type_label,
)


records = [
    ActivityRecord(
        project="Test Project",
        docket="CP00-000",
        accession="20260101-0001",
        date=datetime(
            2026,
            1,
            1,
        ),
        event_type="ferc_information_request",
        description=(
            "Letter requesting applicant "
            "to respond to data request."
        ),
    ),
    ActivityRecord(
        project="Test Project",
        docket="CP00-000",
        accession="20260105-0002",
        date=datetime(
            2026,
            1,
            5,
        ),
        event_type="applicant_followup",
        description=(
            "Applicant submits response "
            "to FERC's 01/01/2026 data request."
        ),
    ),
]


group = ActivityGroup(
    project="Test Project",
    docket="CP00-000",
    activity_type="information_exchange",
    relationship="referenced_prior_date",
    records=records,
)


summary = summarize_activity_group(
    group
)


print(
    "Label:",
    activity_type_label(
        summary.activity_type
    ),
)

print(
    "Filings:",
    summary.filing_count,
)

print(
    "Summary:",
    summary.summary,
)

print(
    "Representatives:",
    summary.representative_accessions,
)