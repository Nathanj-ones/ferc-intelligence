from ferc_filter.company_project_discovery import FilingHit
from ferc_filter.reverse_cp_discovery import merge_filings, reverse_filings_for_company

companies = [
    {"id": "WMB", "name": "Williams"},
    {"id": "KMI", "name": "Kinder Morgan"},
]
identities = {
    "WMB": ["Transcontinental Gas Pipe Line Company, LLC"],
    "KMI": ["Tennessee Gas Pipeline Company, L.L.C."],
}

records = [
    {
        "accession": "A1",
        "docket": "CP26-576-000",
        "filed_date": "09/10/2026",
        "description": "Application for a certificate for the Leidy Access Expansion Project.",
        "applicant": "Transcontinental Gas Pipe Line Company, LLC",
        "document_type": "Application",
    },
    {
        "accession": "A2",
        "docket": "CP26-576",
        "filed_date": "09/11/2026",
        "description": "Motion to intervene regarding the project.",
        "applicant": "Local Intervenor",
        "document_type": "Comment",
    },
    {
        "accession": "B1",
        "docket": "CP26-900",
        "filed_date": "09/10/2026",
        "description": "Application for a certificate for an unrelated project.",
        "applicant": "Unknown Pipeline LLC",
        "document_type": "Application",
    },
]

selected, stats = reverse_filings_for_company(records, companies, identities, "WMB")
assert [item.accession for item in selected] == ["A1", "A2"]
assert stats["owned_dockets"] == ["CP26-576"]
assert stats["matched_seed_records"] == 1
assert stats["unmatched_seed_records"] == 1
print("PASS | Known applicant establishes reverse-CP docket ownership")
print("PASS | All recent records in an owned docket are retained")
print("PASS | Unknown applicant is not guessed into a monitored company")

forward = [
    FilingHit(
        accession="A1",
        docket="CP26-576",
        filed_date="09/10/2026",
        description="Application for a certificate for the Leidy Access Expansion Project.",
        applicant="Transcontinental Gas Pipe Line Company, LLC",
    )
]
merged = merge_filings(forward, selected)
assert [item.accession for item in merged] == ["A1", "A2"]
print("PASS | Forward and reverse discovery merge without duplicate accessions")

kmi_selected, kmi_stats = reverse_filings_for_company(records, companies, identities, "KMI")
assert kmi_selected == []
assert kmi_stats["owned_dockets"] == []
print("PASS | Reverse ownership does not leak across companies")

print()
print("ALL REVERSE CP WEEKLY INTEGRATION v0.1 TESTS PASSED")
