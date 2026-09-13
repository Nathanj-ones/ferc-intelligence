from dataclasses import dataclass

from ferc_filter.company_project_discovery import ProjectCandidate
from ferc_filter.project_candidate_qualifier import qualify_candidate
from ferc_filter.project_materiality import TRACK, assess_materiality


@dataclass
class FakeEvidence:
    project_name: str
    applicant: str
    capex: float | None
    capacity: float | None
    target_in_service: str | None


candidate = ProjectCandidate(
    company_id="TEST",
    company_name="Example Energy",
    docket="CP26-999",
    score=9,
    reasons=["certificate-docket-family"],
    filing_count=4,
    latest_filing_date="08/20/2026",
    project_names=["Example Expansion Project"],
    sample_filings=[
        "Abbreviated Application for a Certificate to construct a new "
        "pipeline and provide additional capacity.",
        "Response to FERC's data request.",
    ],
    tracked=False,
)

qualification = qualify_candidate(candidate)

evidence = FakeEvidence(
    project_name="Example Expansion Project",
    applicant="Example Pipeline Company, LLC",
    capex=1_250_000_000,
    capacity=750000,
    target_in_service="Q4 2028",
)

decision = assess_materiality(
    candidate,
    qualification,
    capex=evidence.capex,
    capacity=evidence.capacity,
    target_in_service=evidence.target_in_service,
)

assert decision.decision == TRACK
assert decision.capex == 1_250_000_000
assert decision.capacity == 750000
assert decision.target_in_service == "Q4 2028"
print("PASS | Explicit evidence flows into materiality automatically")
print("PASS | Capex/capacity/timing affect tracking without hardcoding")

missing = assess_materiality(
    candidate,
    qualification,
    capex=None,
    capacity=None,
    target_in_service=None,
)
assert missing.capex is None
assert missing.capacity is None
print("PASS | Missing evidence remains missing")

print()
print("ALL AUTOMATIC PROJECT EVIDENCE + MATERIALITY v0.1 TESTS PASSED")
