from ferc_filter.company_project_discovery import ProjectCandidate
from ferc_filter.discovery_registry_pipeline import (
    apply_track_records,
    process_candidates,
)


registry = {
    "companies": [{"id": "TEST", "name": "Example Energy"}],
    "projects": [
        {
            "company_id": "TEST",
            "name": "Existing Project",
            "docket": "CP25-10",
        }
    ],
}

candidates = [
    ProjectCandidate(
        company_id="TEST",
        company_name="Example Energy",
        docket="CP25-10-001",
        score=8,
        reasons=["certificate-docket-family"],
        filing_count=3,
        latest_filing_date="08/01/2026",
        project_names=["Existing Project"],
        sample_filings=[
            "Supplemental information regarding existing certificate."
        ],
        tracked=False,
    ),
    ProjectCandidate(
        company_id="TEST",
        company_name="Example Energy",
        docket="CP26-999-000",
        score=8,
        reasons=["certificate-docket-family"],
        filing_count=4,
        latest_filing_date="08/20/2026",
        project_names=["Example Expansion Project"],
        sample_filings=[
            "Abbreviated Application for a Certificate of Public "
            "Convenience and Necessity to construct a new pipeline "
            "and provide additional capacity.",
            "Response to FERC's data request.",
        ],
        tracked=False,
    ),
    ProjectCandidate(
        company_id="TEST",
        company_name="Example Energy",
        docket="CP26-998",
        score=6,
        reasons=["certificate-docket-family"],
        filing_count=2,
        latest_filing_date="08/15/2026",
        project_names=["Example Abandonment Project"],
        sample_filings=[
            "Application for abandonment authorization for existing facilities."
        ],
        tracked=False,
    ),
]

evidence = {
    "CP26-999": {
        "capacity": 750000,
        "capex": None,
    }
}

result = process_candidates(
    registry,
    "TEST",
    candidates,
    evidence=evidence,
)

assert len(result["existing"]) == 1
assert result["existing"][0]["docket"] == "CP25-10"
print("PASS | Existing root docket bypasses discovery promotion")

assert len(result["track"]) == 1
assert result["track"][0]["docket"] == "CP26-999"
assert result["track"][0]["registry_record"]["capex"] is None
assert result["track"][0]["registry_record"]["capacity"] == 750000
print("PASS | Material new project becomes TRACK record")
print("PASS | Missing capex retained without inference")

assert not any(
    item["docket"] == "CP26-998"
    for item in result["track"]
)
print("PASS | Abandonment candidate cannot enter registry")

added = apply_track_records(registry, result)
assert added == 1
assert len(registry["projects"]) == 2
assert registry["projects"][1]["docket"] == "CP26-999"
print("PASS | TRACK record can be applied to registry")

added_again = apply_track_records(registry, result)
assert added_again == 0
assert len(registry["projects"]) == 2
print("PASS | Registry update is idempotent")

print()
print("ALL DISCOVERY REGISTRY PIPELINE v0.1 TESTS PASSED")
