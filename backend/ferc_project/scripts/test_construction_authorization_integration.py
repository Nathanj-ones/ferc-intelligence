from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from ferc_filter.construction_authorization_integration import (
    analyze_construction_authorization_chain,
    filter_resolved_construction_actions,
    apply_construction_chain_to_lifecycle,
)


def filing(accession, date, description):
    return {
        "accession": accession,
        "date": datetime.fromisoformat(date),
        "description": description,
    }


filings = [
    filing(
        "20260804-5203",
        "2026-08-04",
        "Request for notice to proceed with construction by 08/18/2026.",
    ),
    filing(
        "20260819-5167",
        "2026-08-19",
        "Supplemental information to the 08/04/2026 request for notice to "
        "proceed with construction by 08/18/2026.",
    ),
    filing(
        "20260820-3080",
        "2026-08-20",
        "Letter granting the 08/19/2026 request to commence partial "
        "construction of the Line 200 and 300 Project.",
    ),
]

chain = analyze_construction_authorization_chain(filings)
assert chain["resolved"] is True
assert chain["partial"] is True
assert chain["request_accession"] == "20260804-5203"
assert "20260819-5167" in chain["supplemental_accessions"]
assert chain["authorization_accession"] == "20260820-3080"
print("PASS | NTP chain links supplemental filing to FERC authorization")

pending = [
    SimpleNamespace(
        gate="construction_authorization",
        initiating_accession="20260804-5203",
    ),
    SimpleNamespace(
        gate="construction_authorization",
        initiating_accession="20260819-5167",
    ),
    SimpleNamespace(
        gate="regulatory_information",
        initiating_accession="FERC-1",
    ),
]

filtered = filter_resolved_construction_actions(pending, chain)
assert len(filtered) == 1
assert filtered[0].initiating_accession == "FERC-1"
print("PASS | Resolved NTP chain removed from pending actions")


@dataclass
class FakeLifecycle:
    current_stage: str
    stage_status: str
    next_gate: str
    gate_status: str
    expected_next_event: str
    schedule_signal: str


lifecycle = FakeLifecycle(
    current_stage="application_or_unknown",
    stage_status="material_gate_not_established",
    next_gate="construction_authorization",
    gate_status="pending",
    expected_next_event="FERC action",
    schedule_signal="awaiting_regulatory_action",
)

updated = apply_construction_chain_to_lifecycle(lifecycle, chain)

assert updated.current_stage == "construction"
assert updated.stage_status == "partial_construction_authorized"
assert updated.next_gate == "remaining_construction_authorization"
assert updated.gate_status == "pending"
assert updated.schedule_signal == "implementation_underway"

print("PASS | Partial FERC authorization advances lifecycle correctly")

print()
print("ALL CONSTRUCTION AUTHORIZATION INTEGRATION v0.1 TESTS PASSED")
