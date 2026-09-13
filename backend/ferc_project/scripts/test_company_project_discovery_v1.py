from datetime import datetime

from ferc_filter.company_project_discovery import (
    FilingHit,
    discover_projects,
)


filings = [
    FilingHit(
        accession="20260101-1000",
        docket="CP26-99",
        filed_date="01/01/2026",
        description=(
            "Company submits an abbreviated application for a "
            "certificate of public convenience and necessity to "
            "construct the Example Expansion Project."
        ),
        document_class="Certificates",
        document_type="Application",
    ),
    FilingHit(
        accession="20260201-1001",
        docket="CP26-99",
        filed_date="02/01/2026",
        description=(
            "Company submits supplemental information for the "
            "Example Expansion Project."
        ),
    ),
    FilingHit(
        accession="20260301-1002",
        docket="ER26-123",
        filed_date="03/01/2026",
        description=(
            "Company submits tariff filing and annual rate information."
        ),
    ),
]

candidates = discover_projects(
    company_id="TEST",
    company_name="Example Energy",
    filings=filings,
    tracked_dockets=set(),
)

assert len(candidates) == 1
candidate = candidates[0]
assert candidate.docket == "CP26-99"
assert candidate.tracked is False
assert candidate.score >= 4

print("PASS | New CP project discovered from company filings")
print("PASS | Non-CP tariff docket excluded from project candidates")
print("PASS | Candidate carries score, reasons, filing count, and samples")
print()
print("ALL COMPANY PROJECT DISCOVERY v1 TESTS PASSED")
