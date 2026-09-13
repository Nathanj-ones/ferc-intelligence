from ferc_filter.company_project_discovery import FilingHit
from ferc_filter.ferc_docket_discovery import (
    discover_from_recent_cp_records,
)


companies = [
    {"id": "TEST", "name": "Example Energy"},
]

identity_map = {
    "TEST": ["Example Pipeline LLC"],
}

records = [
    FilingHit(
        accession="1",
        docket="CP26-100",
        filed_date="09/10/2026",
        description="Application for Example Expansion Project.",
        applicant="Example Pipeline LLC",
    ),
    FilingHit(
        accession="2",
        docket="CP26-200",
        filed_date="09/10/2026",
        description="Application for unrelated project.",
        applicant="Unrelated Pipeline LLC",
    ),
]

results = discover_from_recent_cp_records(
    records,
    companies,
    identity_map,
)

matched = [item for item in results if item.company_id == "TEST"]
unmatched = [item for item in results if item.company_id is None]

assert len(matched) == 1
assert matched[0].docket == "CP26-100"
assert len(unmatched) == 1
print("PASS | Recent CP feed can map known FERC entity to company")
print("PASS | Unrelated CP applicant remains unmatched")
print()
print("ALL RECENT CP REVERSE DISCOVERY v0.1 TESTS PASSED")
