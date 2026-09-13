from datetime import datetime

from ferc_filter.activity_grouper import (
    ActivityGroup,
    ActivityRecord,
)

from ferc_filter.project_view import (
    build_project_view,
    project_view_to_dict,
)
from ferc_filter.regulatory_outlook import (
    OutlookMilestone,
    RegulatoryOutlook,
    ScheduleWatch,
)


alerts = [
    {
        "accession": "20260129-3076",
        "date": datetime(
            2026,
            1,
            29,
        ),
        "event_type": "regulatory_decision",
        "description": (
            "Order Issuing Certificate "
            "for Test Project."
        ),
    },
    {
        "accession": "20260225-3042",
        "date": datetime(
            2026,
            2,
            25,
        ),
        "event_type": (
            "construction_or_service_authorization"
        ),
        "description": (
            "Letter granting request to "
            "commence construction."
        ),
    },
]


activity_records = [
    ActivityRecord(
        project="Test Project",
        docket="CP00-000",
        accession="20260201-0001",
        date=datetime(
            2026,
            2,
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
        accession="20260205-0002",
        date=datetime(
            2026,
            2,
            5,
        ),
        event_type="applicant_followup",
        description=(
            "Applicant submits response "
            "to FERC's 02/01/2026 request."
        ),
    ),
]


group = ActivityGroup(
    project="Test Project",
    docket="CP00-000",
    activity_type="information_exchange",
    relationship="referenced_prior_date",
    records=activity_records,
)


regulatory_outlook = RegulatoryOutlook(
    project="Test Project",
    docket="CP00-000",
    current_stage="Construction",
    regulatory_status="progressing",
    open_ferc_requests=0,
    tracked_ferc_requests=1,
    responded_ferc_requests=1,
    latest_material_milestone=OutlookMilestone(
        accession="20260225-3042",
        date=datetime(2026, 2, 25),
        event_type="construction_or_service_authorization",
        title="Construction / Service Authorization",
        description="Letter granting request to commence construction.",
    ),
    schedule_watch=[
        ScheduleWatch(
            kind="deadline_timing_uncertain",
            request_accession="20260201-0001",
            request_date=datetime(2026, 2, 1),
            estimated_due_date=datetime(2026, 2, 4),
            response_date=datetime(2026, 2, 5),
            detail="Timing requires review; no confirmed delay inferred.",
        )
    ],
    progression_counts={"progressed": 1},
    next_expected_activity="Ongoing construction, compliance, and project-status reporting",
    investor_summary="Project is progressing with no open FERC information requests.",
)


view = build_project_view(
    project="Test Project",
    docket="CP00-000",
    raw_count=10,
    deduped_count=10,
    alerts=alerts,
    review_count=6,
    suppressed_count=2,
    activity_groups=[group],
    regulatory_outlook=regulatory_outlook,
)


print()
print(
    "PROJECT:",
    view.project,
)

print(
    "DOCKET:",
    view.docket,
)

print(
    "CURRENT STAGE:",
    view.current_stage,
)

print()
print("COUNTS")

print(
    "Raw:",
    view.counts.raw,
)

print(
    "Deduped:",
    view.counts.deduped,
)

print(
    "Alerts:",
    view.counts.alerts,
)

print(
    "Review:",
    view.counts.review,
)

print(
    "Suppressed:",
    view.counts.suppressed,
)

print(
    "Activity groups:",
    view.counts.activity_groups,
)

print()
print("MILESTONES")

for milestone in view.milestones:

    print(
        milestone.title,
        "|",
        milestone.accession,
    )

print()
print("ACTIVITY")

for summary in view.activity:

    print(
        summary.summary
    )

print()
print("JSON VIEW")

print(
    project_view_to_dict(view)
)

view_dict = project_view_to_dict(view)
assert view_dict["regulatory_outlook"]["regulatory_status"] == "progressing"
assert view_dict["regulatory_outlook"]["tracked_ferc_requests"] == 1
assert view_dict["regulatory_outlook"]["open_ferc_requests"] == 0
assert len(view_dict["regulatory_outlook"]["schedule_watch"]) == 1
print("PASS | ProjectView v2 regulatory outlook serialization")
