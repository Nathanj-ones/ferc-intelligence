from ferc_filter.project_evidence_extractor import (
    extract_project_evidence,
)


filings = [
    {
        "accession": "20260901-5000",
        "applicant": "Example Pipeline Company, LLC",
        "description": (
            "Example Pipeline Company, LLC submits Abbreviated Application "
            "for a Certificate of Public Convenience and Necessity for the "
            "Example Expansion Project. The project is estimated to cost "
            "$1.25 billion and will provide 750,000 Dth/d of incremental "
            "transportation capacity. The facilities are expected to be "
            "placed in service in Q4 2028."
        ),
    }
]

e = extract_project_evidence("CP26-999", filings)

assert e.project_name == "Example Expansion Project"
assert e.applicant == "Example Pipeline Company, LLC"
assert e.capex == 1_250_000_000
assert e.capacity == 750000
assert e.target_in_service == "Q4 2028"
print("PASS | Project name extracted")
print("PASS | Applicant extracted")
print("PASS | Explicit capex extracted")
print("PASS | Explicit capacity extracted")
print("PASS | Explicit in-service timing extracted")

missing = extract_project_evidence(
    "CP26-998",
    [
        {
            "accession": "20260901-5001",
            "description": (
                "Applicant submits supplemental information for the "
                "Example Connector Project."
            ),
        }
    ],
)

assert missing.capex is None
assert missing.capacity is None
assert missing.target_in_service is None
print("PASS | Missing economic evidence retained without inference")

print()
print("ALL PROJECT EVIDENCE EXTRACTOR v0.1 TESTS PASSED")
