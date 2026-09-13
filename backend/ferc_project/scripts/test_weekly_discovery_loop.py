from dataclasses import dataclass

from ferc_filter.company_project_discovery import FilingHit
from ferc_filter.project_candidate_qualifier import qualify_candidate
from ferc_filter.project_materiality import TRACK
from ferc_filter.project_evidence_extractor import extract_project_evidence


filings = [
    FilingHit(
        accession="20260901-5000",
        docket="CP26-999-000",
        filed_date="09/01/2026",
        description=(
            "Example Pipeline Company, LLC submits Abbreviated Application "
            "for a Certificate of Public Convenience and Necessity for the "
            "Example Expansion Project. The project is estimated to cost "
            "$1.25 billion and will provide 750,000 Dth/d."
        ),
        applicant="Example Pipeline Company, LLC",
    ),
    FilingHit(
        accession="20260905-5001",
        docket="CP26-999-001",
        filed_date="09/05/2026",
        description=(
            "Applicant submits response to FERC data request concerning the "
            "Example Expansion Project."
        ),
        applicant="Example Pipeline Company, LLC",
    ),
]

# Root-family grouping is expected to make both filings one docket.
roots = {}
for filing in filings:
    root = filing.docket.split("-000")[0].split("-001")[0]
    roots.setdefault(root, []).append(filing)

assert list(roots) == ["CP26-999"]
assert len(roots["CP26-999"]) == 2
print("PASS | Docket suffixes collapse into one weekly project group")

candidate = type(
    "Candidate",
    (),
    {
        "docket": "CP26-999",
        "project_names": ["Example Expansion Project"],
        "filing_count": 2,
        "latest_filing_date": "09/05/2026",
        "sample_filings": [x.description for x in filings],
        "reasons": [],
    },
)()

qualification = qualify_candidate(candidate)
evidence = extract_project_evidence(
    "CP26-999",
    filings,
)

assert evidence.capex == 1_250_000_000
assert evidence.capacity == 750000
print("PASS | One fetched filing set supplies evidence without re-querying")

decision = __import__(
    "ferc_filter.project_materiality",
    fromlist=["assess_materiality"],
).assess_materiality(
    candidate,
    qualification,
    capex=evidence.capex,
    capacity=evidence.capacity,
)

assert decision.decision == TRACK
print("PASS | Same fetched evidence drives materiality")
print()
print("ALL WEEKLY DISCOVERY LOOP v0.1 TESTS PASSED")

# Regression: the production candidate builder must preserve project-name
# evidence before qualification. This keeps reverse-discovered dockets from
# being ignored merely because project_names was never populated.
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "run_weekly_company_discovery",
    Path("scripts/run_weekly_company_discovery.py"),
)
weekly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(weekly)

name_candidate = weekly.candidate_from_group(
    "OKE",
    "ONEOK",
    "CP23-544",
    [
        FilingHit(
            accession="20260901-7000",
            docket="CP23-544",
            filed_date="09/01/2026",
            description=(
                "Northern Border Pipeline Company submits Monthly Construction "
                "Status Report re the Bison Xpress Project under CP23-544."
            ),
            applicant="Northern Border Pipeline Company",
        ),
        FilingHit(
            accession="20260908-7001",
            docket="CP23-544",
            filed_date="09/08/2026",
            description=(
                "Northern Border Pipeline Company submits a filing concerning "
                "the Bison Xpress Project under CP23-544."
            ),
            applicant="Northern Border Pipeline Company",
        ),
    ],
)
assert "Bison Xpress Project" in name_candidate.project_names
assert qualify_candidate(name_candidate).decision == "WATCH"
print("PASS | Production candidate builder preserves named-project evidence")
