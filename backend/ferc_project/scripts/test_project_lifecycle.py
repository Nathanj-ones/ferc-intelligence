from datetime import datetime
from ferc_filter.project_lifecycle import LifecycleFiling, build_project_lifecycle

def filing(accession, date, event_type, description):
    return LifecycleFiling(
        accession=accession,
        date=datetime.fromisoformat(date),
        event_type=event_type,
        description=description,
    )

print("=" * 80)
print("PROJECT LIFECYCLE v0.1 TEST")
print("=" * 80)

case_certificate = [
    filing("20260618-3080", "2026-06-18", "regulatory_decision",
           "Order Issuing Certificate for Test Project."),
    filing("20260818-5117", "2026-08-18", "context_dependent",
           "Applicant submits Implementation Plan for Test Project."),
    filing("20260828-5059", "2026-08-28", "context_dependent",
           "Applicant submits request for notice to proceed with construction."),
]
result = build_project_lifecycle(case_certificate)
assert result.current_stage == "post_certificate_pre_construction"
assert result.stage_status == "certificate_granted_implementation_underway"
assert result.next_gate == "construction_authorization"
assert result.gate_status == "pending"
assert result.schedule_signal == "awaiting_regulatory_action"
assert "20260618-3080" in result.evidence_accessions
assert "20260828-5059" in result.evidence_accessions
print("PASS | Certificate -> pre-construction gate")

case_construction = case_certificate + [
    filing("20260905-3001", "2026-09-05",
           "construction_or_service_authorization",
           "Letter granting request to commence construction."),
]
result = build_project_lifecycle(case_construction)
assert result.current_stage == "construction"
assert result.stage_status == "construction_authorized"
assert result.next_gate == "in_service"
assert result.gate_status == "pending"
assert result.confidence == "high"
print("PASS | Construction authorization -> construction")

case_environmental = [
    filing("20260227-3021", "2026-02-27", "environmental_milestone",
           "Environmental Assessment for Test Project."),
]
result = build_project_lifecycle(case_environmental)
assert result.current_stage == "environmental_review"
assert result.stage_status == "environmental_review_complete"
assert result.next_gate == "certificate_decision"
assert result.gate_status == "pending"
assert result.schedule_signal == "awaiting_certificate_decision"
print("PASS | Environmental review -> certificate gate")

print()
print("ALL PROJECT LIFECYCLE v0.1 TESTS PASSED")
