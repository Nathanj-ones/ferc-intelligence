from datetime import datetime
from types import SimpleNamespace
from ferc_filter.project_display import build_project_display, project_display_to_dict

def milestone(accession, date, title, event_type):
    return SimpleNamespace(accession=accession, date=datetime.fromisoformat(date), title=title, event_type=event_type)

def make_view(**kw):
    lifecycle = SimpleNamespace(
        current_stage=kw["stage"], stage_status=kw["stage_status"],
        next_gate=kw["next_gate"], gate_status=kw["gate_status"],
    )
    investor = SimpleNamespace(
        regulatory_health=kw["health"], timeline_signal=kw["timeline_signal"],
        next_expected_event=kw["next_expected_event"], confidence="high",
    )
    return SimpleNamespace(
        project=kw["project"], docket=kw["docket"], lifecycle=lifecycle,
        investor_summary=investor, pending_regulatory_actions=kw.get("pending_actions", []),
        milestones=kw.get("milestones", []),
    )

print("=" * 80)
print("PROJECT DISPLAY v0.1 TEST")
print("=" * 80)

sse = make_view(
    project="Southeast Supply Enhancement", docket="CP25-10",
    stage="construction", stage_status="construction_authorized",
    next_gate="in_service", gate_status="pending", health="progressing",
    timeline_signal="construction_underway_or_authorized",
    next_expected_event="Construction progress and eventual in-service authorization",
    milestones=[milestone("20260225-3042", "2026-02-25", "Construction / Service Authorization", "construction_or_service_authorization")],
)
d = build_project_display(sse)
assert d.headline == "Construction underway or authorized"
assert d.next_gate == "in_service"
assert d.blocking_action is None
print("PASS | Construction project display")

blocking = SimpleNamespace(
    blocking_next_stage=True, action_requested="notice_to_proceed",
    responsible_party="FERC", requested_timing=datetime(2026, 9, 11),
    timing_source="filing_text_requested_timing",
)
app = make_view(
    project="Appalachian Reliability", docket="CP25-528",
    stage="post_certificate_pre_construction",
    stage_status="certificate_granted_implementation_underway",
    next_gate="construction_authorization", gate_status="pending",
    health="progressing", timeline_signal="pending_gate_with_requested_timing",
    next_expected_event="FERC approval, further information request, or other disposition",
    pending_actions=[blocking],
    milestones=[milestone("20260618-3080", "2026-06-18", "Regulatory Decision", "regulatory_decision")],
)
d = build_project_display(app)
assert d.headline == "Construction authorization pending"
assert d.blocking_action == "notice_to_proceed"
assert d.responsible_party == "FERC"
assert d.requested_timing == datetime(2026, 9, 11)
assert d.timing_type == "sponsor_requested"
payload = project_display_to_dict(d)
assert payload["next_step"]["timing_type"] == "sponsor_requested"
print("PASS | Pending-gate project display")

missing = SimpleNamespace(project="Incomplete", docket="CP00-000", lifecycle=None, investor_summary=None, pending_regulatory_actions=[], milestones=[])
try:
    build_project_display(missing)
except ValueError:
    print("PASS | Missing intelligence rejected")
else:
    raise AssertionError("Missing intelligence should be rejected")

print()
print("ALL PROJECT DISPLAY v0.1 TESTS PASSED")
