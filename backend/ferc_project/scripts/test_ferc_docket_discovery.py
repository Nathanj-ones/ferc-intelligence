from ferc_filter.ferc_docket_discovery import (
    discover_from_recent_cp_records,
    match_company,
)


companies = [
    {"id": "AAA", "name": "Alpha Energy"},
    {"id": "BBB", "name": "Beta Resources"},
]

identities = {
    "AAA": ["Alpha Pipeline LLC"],
    "BBB": ["Beta Gas Transmission Company, LLC"],
}

company_id, reason, confidence = match_company(
    "Alpha Pipeline LLC",
    companies,
    identities,
)
assert company_id == "AAA"
assert confidence == "high"
print("PASS | Learned FERC identity maps applicant to company")

records = [
    {
        "docket": "CP26-100-000",
        "applicant": "Alpha Pipeline LLC",
        "accession": "20260901-5000",
        "description": "Application for the New Expansion Project.",
    },
    {
        "docket": "CP26-200",
        "applicant": "Unknown Pipeline LLC",
        "accession": "20260901-5001",
        "description": "Application for another project.",
    },
]

results = discover_from_recent_cp_records(
    records,
    companies,
    identities,
)

assert results[0].docket == "CP26-100"
assert results[0].company_id == "AAA"
assert results[1].company_id is None
print("PASS | Recent CP docket normalizes to root family")
print("PASS | Known applicant becomes company candidate")
print("PASS | Unknown applicant remains unmatched rather than guessed")

print()
print("ALL FERC DOCKET DISCOVERY v0.1 TESTS PASSED")
