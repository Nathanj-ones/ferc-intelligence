from datetime import datetime
from types import SimpleNamespace

from ferc_filter.construction_authorization import (
    build_construction_authorization_chain,
    construction_authorization_chain_to_dict,
)

pending = [
    SimpleNamespace(
        gate="construction_authorization",
        action_requested="notice_to_proceed",
        initiating_accession="20260804-5203",
        initiating_date=datetime(2026, 8, 4),
    ),
]

milestones = [
    SimpleNamespace(
        accession="20260820-3080",
        date=datetime(2026, 8, 20),
        event_type="construction_or_service_authorization",
        title="Construction / Service Authorization",
        description=(
            "Letter granting the request to commence partial "
            "construction of the Line 200 and 300 Project."
        ),
    ),
]

chain = build_construction_authorization_chain(
    pending,
    milestones,
)

assert chain is not None
assert chain.request_accession == "20260804-5203"
assert chain.authorization_accession == "20260820-3080"
assert chain.resolved is True
assert chain.partial is True

payload = construction_authorization_chain_to_dict(chain)
assert payload["partial"] is True
assert payload["resolved"] is True

print("PASS | NTP request linked to later FERC authorization")
print("PASS | Partial construction authorization detected")
print("PASS | Authorization resolves construction-gate chain")

open_chain = build_construction_authorization_chain(
    pending,
    [],
)
assert open_chain.resolved is False
assert open_chain.authorization_accession is None
print("PASS | Open NTP remains unresolved without FERC authorization")

print()
print("ALL CONSTRUCTION AUTHORIZATION CHAIN v0.1 TESTS PASSED")
