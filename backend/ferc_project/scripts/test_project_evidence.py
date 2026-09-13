from datetime import datetime
from types import SimpleNamespace

from ferc_filter.project_display import ProjectDisplay
from ferc_filter.project_evidence import (
    evidence_from_display,
    project_evidence_to_dict,
)

milestones = [
    SimpleNamespace(
        accession="20260225-3042",
        date=datetime(2026, 2, 25),
        title="Construction / Service Authorization",
        event_type="construction_or_service_authorization",
    ),
    SimpleNamespace(
        accession="20260129-3076",
        date=datetime(2026, 1, 29),
        title="Regulatory Decision",
        event_type="regulatory_decision",
    ),
    SimpleNamespace(
        accession="20251031-3000",
        date=datetime(2025, 10, 31),
        title="Environmental Milestone",
        event_type="environmental_milestone",
    ),
]

display = ProjectDisplay(
    project="Southeast Supply Enhancement",
    docket="CP25-10",
    headline="Construction underway or authorized",
    stage="construction",
    stage_status="construction_authorized",
    regulatory_health="progressing",
    next_gate="in_service",
    gate_status="pending",
    next_expected_event="Construction progress and eventual in-service authorization",
    blocking_action=None,
    responsible_party=None,
    requested_timing=None,
    timing_type=None,
    timeline_signal="construction_underway_or_authorized",
    latest_material_event=milestones[0],
    key_milestones=milestones,
    confidence="high",
)

evidence = evidence_from_display(display)

assert len(evidence) == 3
assert evidence[0].accession == "20260225-3042"
assert evidence[0].role == "construction_gate"
assert evidence[0].importance == "high"
assert evidence[0].source == "FERC eLibrary"

payload = project_evidence_to_dict(evidence)
assert payload[1]["role"] == "regulatory_decision"
assert payload[2]["role"] == "environmental_review"
assert payload[0]["date"] == "2026-02-25T00:00:00"

print("PASS | Material milestones packaged as evidence")
print("PASS | Evidence roles mapped from existing event types")
print("PASS | Filing accession retained for frontend linking")
print()
print("ALL PROJECT EVIDENCE v0.1 TESTS PASSED")
