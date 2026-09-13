from dataclasses import dataclass

from ferc_filter.project_candidate_qualifier import (
    IGNORE,
    PROMOTE,
    WATCH,
    qualify_candidate,
)


@dataclass
class Candidate:
    docket: str
    project_names: list[str]
    filing_count: int
    latest_filing_date: str
    sample_filings: list[str]
    reasons: list[str]


active = Candidate(
    docket="CP26-550",
    project_names=["Green River West Expansion Project"],
    filing_count=4,
    latest_filing_date="08/11/2026",
    sample_filings=[
        "Abbreviated Application for a Certificate of Public Convenience "
        "and Necessity for the Green River West Expansion Project.",
        "Response to FERC's 07/15/2026 data request regarding the project.",
    ],
    reasons=["certificate-docket-family", "project-term:expansion"],
)

result = qualify_candidate(active)
assert result.decision == PROMOTE
assert result.score >= 7
print("PASS | Active application with FERC follow-up is promoted")

routine = Candidate(
    docket="CP76-106",
    project_names=["Plymouth LNG Peak Shaving Plant"],
    filing_count=4,
    latest_filing_date="08/14/2026",
    sample_filings=[
        "Semi-Annual Summary of Operations for the Plymouth LNG Peak Shaving Plant.",
        "Semi-Annual Summary of Operations for the Plymouth LNG Peak Shaving Plant.",
    ],
    reasons=["certificate-docket-family", "project-term:lng"],
)

result = qualify_candidate(routine)
assert result.decision == IGNORE
print("PASS | Routine legacy operating reports are ignored")

construction = Candidate(
    docket="CP25-533",
    project_names=["Phase IV Expansion Project"],
    filing_count=6,
    latest_filing_date="08/21/2026",
    sample_filings=[
        "Request for full notice to proceed with construction by 09/07/2026.",
        "Notice that construction of limited project activities commenced.",
    ],
    reasons=["certificate-docket-family", "project-term:construction"],
)

result = qualify_candidate(construction)
assert result.decision == PROMOTE
assert result.confidence == "high"
print("PASS | Active construction and NTP signals are promoted")

watch = Candidate(
    docket="CP25-531",
    project_names=["Lease Agreement Project"],
    filing_count=2,
    latest_filing_date="09/18/2025",
    sample_filings=[
        "Response to FERC's data request regarding a joint proposal.",
    ],
    reasons=["certificate-docket-family", "project-term:project"],
)

result = qualify_candidate(watch)
assert result.decision in {WATCH, PROMOTE}
print("PASS | Ambiguous candidate remains watchable")

print()
print("ALL PROJECT CANDIDATE QUALIFICATION v0.2 TESTS PASSED")
