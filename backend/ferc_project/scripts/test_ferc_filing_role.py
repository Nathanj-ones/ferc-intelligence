from ferc_filter.ferc_filing_role import (
    PROJECT_CONSTRUCTION, PROJECT_INITIATING, PROJECT_REGULATORY,
    PUBLIC_COMMENT, ROUTINE_REPORTING, classify_filing_role,
)

def check(description, expected):
    result = classify_filing_role({
        "accession": "A1", "docket": "CP26-999",
        "applicant": "Example Pipeline LLC",
        "description": description,
    })
    assert result.role == expected
    return result

check("Abbreviated Application for a Certificate of Public Convenience and Necessity for the Example Expansion Project.", PROJECT_INITIATING)
print("PASS | Certificate application classified as project-initiating")
check("Applicant submits response to FERC data request regarding the project.", PROJECT_REGULATORY)
print("PASS | FERC data response classified as project-regulatory")
check("Applicant submits request for notice to proceed with construction.", PROJECT_CONSTRUCTION)
print("PASS | NTP classified as project-construction")
check("Comments of local residents in opposition to the proposed project.", PUBLIC_COMMENT)
print("PASS | Public comments excluded from project creation")
check("Semi-Annual Summary of Operations for the LNG facility.", ROUTINE_REPORTING)
print("PASS | Routine operating report excluded from project creation")
print()
print("ALL FERC FILING ROLE CLASSIFIER v0.1 TESTS PASSED")
