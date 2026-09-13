from datetime import datetime

from ferc_filter.pending_regulatory_action import (
    find_pending_regulatory_actions,
)


def f(accession, date, event_type, description):
    return {
        "accession": accession,
        "date": datetime.fromisoformat(date),
        "event_type": event_type,
        "description": description,
    }


print("=" * 80)
print("PENDING GATE STATE TRANSITION TEST")
print("=" * 80)

filings = [
    f(
        "20260828-5059",
        "2026-08-28",
        "context_dependent",
        "Applicant submits request for notice to proceed with construction.",
    ),
    f(
        "20260904-3001",
        "2026-09-04",
        "ferc_information_request",
        "Letter requesting applicant to file a response to information request.",
    ),
]

actions = find_pending_regulatory_actions(filings)

assert len(actions) == 1
action = actions[0]
assert action.gate == "construction_authorization"
assert action.status == "awaiting_sponsor_action"
assert action.initiating_accession == "20260904-3001"
assert action.initiating_party == "FERC"
assert action.responsible_party == "applicant"
assert action.action_requested == "additional_information"
assert action.blocking_next_stage is True
assert action.timeline_signal == "pending_gate_awaiting_sponsor"
assert action.expected_resolution == "Sponsor response to FERC information request"

print("PASS | Gate remains construction authorization")
print("PASS | State moves from awaiting FERC to awaiting sponsor")
print("PASS | FERC information request becomes current blocking action")
print("PASS | Next expected event is sponsor response")
print()
print("ALL PENDING GATE STATE TRANSITION TESTS PASSED")
