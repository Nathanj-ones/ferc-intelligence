from datetime import datetime

from ferc_filter.activity_grouper import (
    ActivityRecord,
    group_review_records,
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
    ActivityRecord(
        project="Test Project",
        docket="CP00-000",
        accession="20260220-0003",
        date=datetime(
            2026,
            2,
            20,
        ),
        event_type="third_party_comment",
        description=(
            "Comments submitted regarding "
            "the project."
        ),
    ),
]


groups = group_review_records(
    records
)


print(
    "Groups:",
    len(groups),
)

for group in groups:

    print()
    print(
        group.activity_type,
        "|",
        group.relationship,
        "|",
        len(group.records),
    )

    for record in group.records:

        print(
            " ",
            record.accession,
            "|",
            record.event_type,
        )