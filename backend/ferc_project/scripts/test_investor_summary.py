from datetime import datetime

from ferc_filter.investor_summary import build_investor_summary
from ferc_filter.project_lifecycle import ProjectLifecycle
from ferc_filter.regulatory_outlook import (
    RegulatoryOutlook,
    ScheduleWatch,
)


def lifecycle(
    stage,
    status,
    gate,
    gate_status,
    event,
    signal,
    evidence,
):
    return ProjectLifecycle(
        current_stage=stage,
        stage_status=status,
        next_gate=gate,
        gate_status=gate_status,
        expected_next_event=event,
        schedule_signal=signal,
        evidence_accessions=evidence,
        confidence="high",
    )


def outlook(
    status,
    tracked,
    responded,
    open_requests,
    watches,
):
    return RegulatoryOutlook(
        project="Test Project",
        docket="CP00-000",
        current_stage="Certificate",
        regulatory_status=status,
        open_ferc_requests=open_requests,
        tracked_ferc_requests=tracked,
        responded_ferc_requests=responded,
        schedule_watch=watches,
        progression_counts={},
        next_expected_activity="Continue through the next project stage",
        investor_summary="",
    )


print("=" * 80)
print("INVESTOR SUMMARY v0.1 TEST")
print("=" * 80)

# Certificate granted, implementation underway, construction authorization pending.
summary = build_investor_summary(
    lifecycle=lifecycle(
        "post_certificate_pre_construction",
        "certificate_granted_implementation_underway",
        "construction_authorization",
        "pending",
        "FERC action on implementation filings or request for construction authorization / Notice to Proceed",
        "awaiting_regulatory_action",
        ["20260618-3080", "20260828-5059"],
    ),
    outlook=outlook(
        "progressing",
        9,
        9,
        0,
        [
            ScheduleWatch(
                kind="deadline_timing_uncertain",
                request_accession="20260105-3037",
                request_date=datetime(2026, 1, 5),
                estimated_due_date=datetime(2026, 1, 10),
                response_date=datetime(2026, 1, 12),
                detail="Historical timing watch.",
            )
        ],
    ),
)

assert summary.current_stage == "post_certificate_pre_construction"
assert summary.next_gate == "construction_authorization"
assert summary.gate_status == "pending"
assert summary.regulatory_health == "progressing"
assert summary.open_ferc_requests == 0
assert summary.schedule_watch_items == 1
assert "next gate is Construction Authorization" in summary.investor_summary
assert "All 9 tracked FERC requests" in summary.investor_summary
assert "not treated as confirmed delays" in summary.investor_summary
print("PASS | Certificate project summary")

# Construction-authorized project.
summary = build_investor_summary(
    lifecycle=lifecycle(
        "construction",
        "construction_authorized",
        "in_service",
        "pending",
        "Construction progress, compliance reporting, and eventual authorization to place facilities in service",
        "construction_underway_or_authorized",
        ["20260225-3042"],
    ),
    outlook=outlook(
        "progressing",
        8,
        8,
        0,
        [],
    ),
)

assert summary.current_stage == "construction"
assert summary.next_gate == "in_service"
assert summary.open_ferc_requests == 0
assert summary.timeline_signal == "construction_underway_or_authorized"
assert "All 8 tracked FERC requests" in summary.investor_summary
print("PASS | Construction project summary")

# Open FERC request must be visible as an investor issue.
summary = build_investor_summary(
    lifecycle=lifecycle(
        "environmental_review",
        "environmental_review_complete",
        "certificate_decision",
        "pending",
        "FERC certificate decision or further environmental/regulatory action",
        "awaiting_certificate_decision",
        ["20251031-3000"],
    ),
    outlook=outlook(
        "attention_required",
        3,
        2,
        1,
        [],
    ),
)

assert summary.open_ferc_requests == 1
assert "remain open" in summary.investor_summary
assert summary.regulatory_health == "attention_required"
print("PASS | Open-request risk summary")

print()
print("ALL INVESTOR SUMMARY v0.1 TESTS PASSED")
