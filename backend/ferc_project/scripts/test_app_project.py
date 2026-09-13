from datetime import datetime
from types import SimpleNamespace
from ferc_filter.app_project import build_app_project, app_project_to_dict, validate_app_project
from ferc_filter.project_context import ProjectContext
from ferc_filter.project_display import ProjectDisplay
from ferc_filter.project_timeline import build_project_timeline
from ferc_filter.project_evidence import evidence_from_display

m = SimpleNamespace(
    accession="20260225-3042",
    date=datetime(2026, 2, 25),
    title="Construction / Service Authorization",
    event_type="construction_or_service_authorization",
)

def display(project, docket, blocking=None, party=None, timing=None):
    return ProjectDisplay(
        project=project, docket=docket,
        headline="Construction authorization pending" if blocking else "Construction underway or authorized",
        stage="construction",
        stage_status="construction_authorized",
        regulatory_health="progressing",
        next_gate="in_service",
        gate_status="pending",
        next_expected_event="Construction progress and eventual in-service authorization",
        blocking_action=blocking,
        responsible_party=party,
        requested_timing=timing,
        timing_type="sponsor_requested" if timing else None,
        timeline_signal="pending_gate_with_requested_timing" if blocking else "construction_underway_or_authorized",
        latest_material_event=m,
        key_milestones=[m],
        confidence="high",
    )

print("=" * 70)
print("APP PROJECT v1 TEST")
print("=" * 70)

ctx = ProjectContext(
    project="Southeast Supply Enhancement",
    docket="CP25-10",
    company="Williams",
    project_type="Natural Gas Pipeline Expansion",
    capacity_value=1.597,
    capacity_unit="Bcf/d",
    capex_value=1.5,
    capex_currency="USD",
    capex_unit="billion",
    target_in_service="Q3 2027",
    capacity_source="company_materials",
    capex_source="company_materials",
    target_in_service_source="company_materials",
)
sse_display = display("Southeast Supply Enhancement", "CP25-10")
sse_timeline = build_project_timeline(ctx, sse_display)
sse_evidence = evidence_from_display(sse_display)
v = build_app_project(
    ctx,
    sse_display,
    sse_timeline,
    sse_evidence,
)
p = app_project_to_dict(v)
assert p["schema_version"] == "app_project_v1"
assert p["scale"]["capacity"]["value"] == 1.597
assert p["scale"]["capex"]["value"] == 1.5
assert p["status"]["stage"] == "construction"
assert p["timeline"]["current_step"] == "construction"
assert p["timeline"]["endpoint"] == "commercial_operation"
assert p["timeline"]["projected_endpoint"]["value"] == "Q3 2027"
assert p["timeline"]["steps"][-1]["label"] == "Commercial Operation / In-Service"
assert p["timeline"]["steps"][-1]["status"] == "future"
assert len(p["evidence"]["material_filings"]) == 1
assert p["evidence"]["material_filings"][0]["accession"] == "20260225-3042"
assert validate_app_project(v) == []
print("PASS | Complete app project contract")

ctx2 = ProjectContext(project="Appalachian Reliability", docket="CP25-528")
app_display = display(
    "Appalachian Reliability",
    "CP25-528",
    blocking="notice_to_proceed",
    party="FERC",
    timing=datetime(2026, 9, 11),
)
app_timeline = build_project_timeline(ctx2, app_display)
app_evidence = evidence_from_display(app_display)
v2 = build_app_project(
    ctx2,
    app_display,
    app_timeline,
    app_evidence,
)
p2 = app_project_to_dict(v2)
assert p2["scale"]["capacity"]["value"] is None
assert p2["attention"]["blocking_action"] == "notice_to_proceed"
assert p2["attention"]["responsible_party"] == "FERC"
assert p2["attention"]["requested_timing"] == "2026-09-11T00:00:00"
print("PASS | Pending gate preserved in app contract")

try:
    bad_display = display(
        "Southeast Supply Enhancement",
        "CP25-10",
    )
    bad_timeline = build_project_timeline(
        ctx,
        bad_display,
    )
    build_app_project(
        ProjectContext(project="Other", docket="CP25-10"),
        bad_display,
        bad_timeline,
    )
except ValueError:
    print("PASS | Mismatched context rejected")
else:
    raise AssertionError("Mismatched context should be rejected")

print()
print("ALL APP PROJECT v1 TESTS PASSED")
