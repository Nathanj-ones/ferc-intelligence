from datetime import datetime
from types import SimpleNamespace

from ferc_filter.project_context import (
    ProjectContext,
    project_context_to_dict,
)
from ferc_filter.project_comparison import (
    build_project_comparison_item,
    project_comparison_item_to_dict,
)


print("=" * 80)
print("PROJECT CONTEXT / COMPARISON v0.1 TEST")
print("=" * 80)

context = ProjectContext(
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

payload = project_context_to_dict(context)
assert payload["capacity"]["value"] == 1.597
assert payload["capacity"]["source"] == "company_materials"
assert payload["capex"]["currency"] == "USD"
print("PASS | Source-aware project context")

regulatory = SimpleNamespace(
    project="Southeast Supply Enhancement",
    docket="CP25-10",
    headline="Construction underway or authorized",
    stage="construction",
    stage_status="construction_authorized",
    regulatory_health="progressing",
    next_gate="in_service",
    gate_status="pending",
    blocking_action=None,
    responsible_party=None,
    requested_timing=None,
    timing_type=None,
    timeline_signal="construction_underway_or_authorized",
    next_expected_event="Construction progress and eventual in-service authorization",
    latest_material_event=SimpleNamespace(
        accession="20260225-3042",
        date=datetime(2026, 2, 25),
        title="Construction / Service Authorization",
        event_type="construction_or_service_authorization",
    ),
    confidence="high",
)

item = build_project_comparison_item(
    context,
    regulatory,
)
combined = project_comparison_item_to_dict(item)

assert combined["context"]["company"] == "Williams"
assert combined["regulatory"]["stage"] == "construction"
assert combined["regulatory"]["blocking_action"] is None
print("PASS | Context + regulatory display combined")

unknown_context = ProjectContext(
    project="Appalachian Reliability",
    docket="CP25-528",
)
unknown_payload = project_context_to_dict(
    unknown_context
)
assert unknown_payload["capacity"]["value"] is None
assert unknown_payload["capex"]["value"] is None
assert unknown_payload["target_in_service"]["value"] is None
print("PASS | Unknown context remains null")

bad_regulatory = SimpleNamespace(
    project="Different Project",
    docket="CP25-10",
)
try:
    build_project_comparison_item(
        context,
        bad_regulatory,
    )
except ValueError:
    print("PASS | Mismatched project rejected")
else:
    raise AssertionError(
        "Mismatched project should be rejected"
    )

print()
print("ALL PROJECT CONTEXT / COMPARISON v0.1 TESTS PASSED")
