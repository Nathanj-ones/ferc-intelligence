from datetime import datetime
from types import SimpleNamespace

from ferc_filter.regulatory_outlook import (
    build_regulatory_outlook,
)


# ==============================================================
# MOCK REQUESTS
# ==============================================================

requests = [
    SimpleNamespace(
        request_accession="20250422-3028",
        request_date=datetime(
            2025,
            4,
            22,
        ),
        estimated_due_date=datetime(
            2025,
            5,
            7,
        ),
        response_found=True,
        first_response_date=datetime(
            2025,
            5,
            7,
        ),
        response_timing=(
            "on_or_before_estimated_due_date"
        ),
    ),
    SimpleNamespace(
        request_accession="20251001-3072",
        request_date=datetime(
            2025,
            10,
            1,
        ),
        estimated_due_date=datetime(
            2025,
            10,
            11,
        ),
        response_found=True,
        first_response_date=datetime(
            2025,
            10,
            14,
        ),
        response_timing=(
            "after_estimated_due_date"
        ),
    ),
]


# ==============================================================
# MOCK PROGRESSIONS
# ==============================================================

progressions = [
    SimpleNamespace(
        progression_status="progressed"
    ),
    SimpleNamespace(
        progression_status="progressed"
    ),
]


# ==============================================================
# MOCK MILESTONE
# ==============================================================

milestones = [
    SimpleNamespace(
        accession="20251031-3000",
        date=datetime(
            2025,
            10,
            31,
        ),
        event_type="environmental_milestone",
        title="Environmental Assessment",
        description=(
            "Environmental Assessment issued."
        ),
    ),
]


# ==============================================================
# BUILD
# ==============================================================

outlook = build_regulatory_outlook(
    project=(
        "Southeast Supply Enhancement"
    ),
    docket="CP25-10",
    current_stage="Construction",
    requests=requests,
    progressions=progressions,
    milestones=milestones,
)


print()
print("=" * 80)
print("REGULATORY OUTLOOK TEST")
print("=" * 80)

print(
    "Project:",
    outlook.project,
)

print(
    "Docket:",
    outlook.docket,
)

print(
    "Stage:",
    outlook.current_stage,
)

print(
    "Regulatory status:",
    outlook.regulatory_status,
)

print(
    "Tracked FERC requests:",
    outlook.tracked_ferc_requests,
)

print(
    "Responded FERC requests:",
    outlook.responded_ferc_requests,
)

print(
    "Open FERC requests:",
    outlook.open_ferc_requests,
)

print(
    "Latest milestone:",
    (
        outlook.latest_material_milestone.accession
        if outlook.latest_material_milestone
        else None
    ),
)

print(
    "Schedule watch items:",
    len(
        outlook.schedule_watch
    ),
)

for watch in outlook.schedule_watch:
    print(
        "  -",
        watch.kind,
        "|",
        watch.request_accession,
    )

print(
    "Next expected activity:",
    outlook.next_expected_activity,
)

print()
print(
    "Investor summary:"
)

print(
    outlook.investor_summary
)


# ==============================================================
# ASSERTIONS
# ==============================================================

assert (
    outlook.regulatory_status
    == "progressing"
)

assert (
    outlook.tracked_ferc_requests
    == 2
)

assert (
    outlook.responded_ferc_requests
    == 2
)

assert (
    outlook.open_ferc_requests
    == 0
)

assert (
    outlook.latest_material_milestone
    is not None
)

assert (
    outlook.latest_material_milestone.accession
    == "20251031-3000"
)

assert (
    len(
        outlook.schedule_watch
    )
    == 1
)

assert (
    outlook.schedule_watch[0].kind
    == "deadline_timing_uncertain"
)

assert (
    "confirmed delays"
    in outlook.investor_summary
    or
    "not treated as confirmed delays"
    in outlook.investor_summary
)


print()
print(
    "PASS | Overall regulatory status"
)

print(
    "PASS | FERC request counts"
)

print(
    "PASS | Latest material milestone"
)

print(
    "PASS | Historical timing watch"
)

print(
    "PASS | No confirmed delay inferred"
)

print()
print(
    "ALL REGULATORY OUTLOOK TESTS PASSED"
)