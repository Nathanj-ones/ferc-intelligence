from ferc_filter.unmatched_cp_applicants import collect_unmatched_applicants
companies=[{"id":"WMB","name":"Williams"},{"id":"TRGP","name":"Targa Resources"}]
identity_map={"WMB":["Transcontinental Gas Pipe Line Company, LLC"],"TRGP":["Targa Gas Marketing LLC"]}
records=[
{"accession":"1","docket":"CP26-100-000","filed_date":"09/10/2026","applicant":"Transcontinental Gas Pipe Line Company, LLC","description":"Application for a Certificate of Public Convenience and Necessity for the Known Project."},
{"accession":"2","docket":"CP26-200-000","filed_date":"09/09/2026","applicant":"Example Midstream Pipeline LLC","description":"Application for a Certificate of Public Convenience and Necessity for the Example Expansion Project."},
{"accession":"3","docket":"CP26-200-001","filed_date":"09/10/2026","applicant":"Example Midstream Pipeline LLC","description":"Supplemental information in response to FERC regarding the Example Expansion Project."},
{"accession":"4","docket":"CP26-300-000","filed_date":"09/11/2026","applicant":"Individual No Affiliation","description":"Comment of Jane Doe regarding the Example Expansion Project."},
{"accession":"5","docket":"CP26-400-000","filed_date":"09/11/2026","applicant":"Sheppard Mullin Richter & Hampton LLP","description":"Application for a Certificate of Public Convenience and Necessity."},
]
results=collect_unmatched_applicants(records,companies,identity_map); assert len(results)==1,results
x=results[0]; assert x.applicant=="Example Midstream Pipeline LLC" and x.docket_count==1 and x.filing_count==2 and x.initiating_filings==1 and x.regulatory_filings==1 and x.dockets==["CP26-200"]
print("PASS | known monitored applicant excluded"); print("PASS | commenters and non-operating entities excluded"); print("PASS | unresolved operating applicant retained"); print("PASS | docket family grouped and roles counted")


ownership_registry = {
    "entities": [{
        "entity": "Northern Border Pipeline Company",
        "company_id": "OKE",
        "relationship": "joint_venture",
        "ownership_percent": 50,
        "confidence": "high",
        "source_type": "company_official",
        "source_url": "https://www.oneok.com/customers/ngp",
        "source_note": "Official ONEOK listing.",
        "enabled": True,
    }]
}
ownership_records = records + [{
    "accession": "U3",
    "docket": "CP23-544",
    "filed_date": "09/08/2026",
    "description": "Application for the Bison Xpress Project.",
    "applicant": "Northern Border Pipeline Company",
    "document_type": "Application",
}]
with_ownership = collect_unmatched_applicants(ownership_records, companies + [{"id":"OKE","name":"ONEOK"}], identity_map, ownership_registry)
assert all(item.applicant != "Northern Border Pipeline Company" for item in with_ownership)
print("PASS | Evidence-backed ownership registry removes resolved applicants from unmatched report")

print("\nALL UNMATCHED CP APPLICANT REPORT v0.1 TESTS PASSED")
