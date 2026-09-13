from datetime import datetime

from ferc_filter.pending_regulatory_action import (
    find_pending_regulatory_actions,
)
from ferc_filter.regulatory_tracker import (
    RegulatoryFiling,
    track_regulatory_requests,
)


def f(accession, date, event_type, description):
    return {
        "accession": accession,
        "date": datetime.fromisoformat(date),
        "event_type": event_type,
        "description": description,
    }


print("=" * 80)
print("PENDING REGULATORY ACTION v0.2 TEST")
print("=" * 80)

pending = find_pending_regulatory_actions([
    f(
        "20260828-5059",
        "2026-08-28",
        "context_dependent",
        "Eastern Gas Transmission and Storage, Inc. submits "
        "request for notice to proceed with construction by "
        "09/11/2026 re the Appalachian Reliability Project.",
    )
])

assert len(pending) == 1
assert pending[0].status == "awaiting_ferc_action"
assert pending[0].gate == "construction_authorization"
assert pending[0].requested_timing == datetime(2026, 9, 11)
assert pending[0].timing_source == "filing_text_requested_timing"
assert pending[0].blocking_next_stage is True

print("PASS | Pending construction authorization detected")

resolved = find_pending_regulatory_actions([
    f(
        "20260828-5059",
        "2026-08-28",
        "context_dependent",
        "Applicant submits request for notice to proceed with construction "
        "by 09/11/2026.",
    ),
    f(
        "20260903-3010",
        "2026-09-03",
        "construction_or_service_authorization",
        "Letter granting the request to commence construction.",
    ),
])

assert resolved == []

print("PASS | Approved construction request is resolved")

# The tracker owns request/response matching. A deliberately messy
# CP25-528-style one-day reference must therefore resolve here too.
tracker_filings = [
    RegulatoryFiling(
        accession="20260105-3037",
        date=datetime(2026, 1, 5),
        event_type="ferc_information_request",
        description=(
            "Letter requesting applicant to file a response to "
            "environmental information request within 5 days."
        ),
    ),
    RegulatoryFiling(
        accession="20260112-5206",
        date=datetime(2026, 1, 12),
        event_type="applicant_followup",
        description=(
            "Applicant submits response to FERC's 01/06/2026 "
            "environmental information request."
        ),
    ),
    RegulatoryFiling(
        accession="20260112-5207",
        date=datetime(2026, 1, 12),
        event_type="applicant_followup",
        description=(
            "Applicant submits response to FERC's 01/06/2026 "
            "environmental information request."
        ),
    ),
]

tracked = track_regulatory_requests(
    tracker_filings
)

assert len(tracked) == 1
assert tracked[0].response_found is True
assert tracked[0].response_accessions == [
    "20260112-5206",
    "20260112-5207",
]

with_tracker = find_pending_regulatory_actions(
    [],
    tracked_requests=tracked,
)

assert with_tracker == []

print("PASS | Resolved FERC request is not duplicated as pending")

# A genuinely open tracker request should flow through as sponsor action.
open_tracker_filings = [
    RegulatoryFiling(
        accession="20260901-3001",
        date=datetime(2026, 9, 1),
        event_type="ferc_information_request",
        description=(
            "Letter requesting applicant to file a response to "
            "data request within 5 days."
        ),
    )
]

open_tracked = track_regulatory_requests(
    open_tracker_filings
)

open_actions = find_pending_regulatory_actions(
    [],
    tracked_requests=open_tracked,
)

assert len(open_actions) == 1
assert open_actions[0].status == "awaiting_sponsor_action"
assert open_actions[0].responsible_party == "applicant"

print("PASS | Open FERC request flows from Regulatory Tracker")

print()
print("ALL PENDING REGULATORY ACTION v0.2 TESTS PASSED")
