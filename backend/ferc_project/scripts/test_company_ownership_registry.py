from ferc_filter.company_ownership_registry import resolve_evidence_ownership
from ferc_filter.reverse_cp_discovery import reverse_filings_for_company

companies = [
    {"id": "OKE", "name": "ONEOK"},
    {"id": "WMB", "name": "Williams"},
]
identities = {
    "OKE": ["Sabine Pipe Line LLC"],
    "WMB": ["Transcontinental Gas Pipe Line Company, LLC"],
}
ownership_registry = {
    "entities": [
        {
            "entity": "Northern Border Pipeline Company",
            "company_id": "OKE",
            "relationship": "joint_venture",
            "ownership_percent": 50,
            "confidence": "high",
            "source_type": "company_official",
            "source_url": "https://www.oneok.com/customers/ngp",
            "source_note": "Official ONEOK interstate-pipeline listing.",
            "enabled": True,
        },
        {
            "entity": "OkTex Pipeline Company, L.L.C.",
            "company_id": "OKE",
            "relationship": "subsidiary",
            "ownership_percent": None,
            "confidence": "high",
            "source_type": "company_official",
            "source_url": "https://www.oneok.com/okt/about",
            "source_note": "Official ONEOK OkTex ownership page.",
            "enabled": True,
        },
    ]
}

match = resolve_evidence_ownership("Northern Border Pipeline Company", ownership_registry, allowed_company_ids=["OKE", "WMB"])
assert match is not None
assert match.company_id == "OKE"
assert match.relationship == "joint_venture"
assert match.ownership_percent == 50.0
assert match.confidence == "high"
print("PASS | Northern Border resolves to OKE with joint-venture provenance")

match = resolve_evidence_ownership("OKTEX PIPELINE COMPANY LLC", ownership_registry, allowed_company_ids=["OKE"])
assert match is not None and match.company_id == "OKE"
print("PASS | Legal-suffix normalization preserves exact evidence-backed matching")

assert resolve_evidence_ownership("Unknown Pipeline LLC", ownership_registry, allowed_company_ids=["OKE"]) is None
print("PASS | Unknown applicants remain unresolved")

records = [
    {
        "accession": "N1",
        "docket": "CP23-544",
        "filed_date": "09/08/2026",
        "description": "Application for a certificate for the Bison Xpress Project.",
        "applicant": "Northern Border Pipeline Company",
        "document_type": "Application",
    },
    {
        "accession": "N2",
        "docket": "CP23-544",
        "filed_date": "09/09/2026",
        "description": "Commission filing concerning the Bison Xpress Project.",
        "applicant": "Office of the Secretary, FERC",
        "document_type": "Order",
    },
]
selected, stats = reverse_filings_for_company(records, companies, identities, "OKE", ownership_registry)
assert [item.accession for item in selected] == ["N1", "N2"]
assert stats["owned_dockets"] == ["CP23-544"]
assert stats["ownership_registry_matches"] == 1
print("PASS | Evidence-backed ownership can establish a reverse-CP docket for OKE")

wmb_selected, _ = reverse_filings_for_company(records, companies, identities, "WMB", ownership_registry)
assert wmb_selected == []
print("PASS | Evidence-backed ownership does not leak across monitored companies")

print()
print("ALL COMPANY OWNERSHIP REGISTRY v0.1 TESTS PASSED")
