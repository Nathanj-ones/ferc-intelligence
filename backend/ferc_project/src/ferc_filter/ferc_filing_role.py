from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterable

PROJECT_INITIATING = "PROJECT_INITIATING"
PROJECT_REGULATORY = "PROJECT_REGULATORY"
PROJECT_CONSTRUCTION = "PROJECT_CONSTRUCTION"
PROJECT_COMPLIANCE = "PROJECT_COMPLIANCE"
PUBLIC_COMMENT = "PUBLIC_COMMENT"
ROUTINE_REPORTING = "ROUTINE_REPORTING"
OTHER = "OTHER"

@dataclass
class FilingRole:
    accession: str
    docket: str
    applicant: str
    role: str
    confidence: str
    reasons: list[str]

def _value(record: Any, *names: str) -> str:
    for name in names:
        value = record.get(name) if isinstance(record, dict) else getattr(record, name, None)
        if value:
            return str(value).strip()
    return ""

def classify_filing_role(record: Any) -> FilingRole:
    description = _value(record, "description", "title", "document_title")
    document_type = _value(record, "document_type", "documentType")
    document_class = _value(record, "document_class", "documentClass")
    applicant = _value(record, "applicant", "applicant_name")
    accession = _value(record, "accession", "accessionNumber")
    docket = _value(record, "docket")
    text = " ".join([description, document_type, document_class]).lower()

    if any(t in text for t in (
        "comment of ", "comments of ", "motion to intervene",
        "protest of ", "protest ", "environmental scoping comments",
    )):
        return FilingRole(accession, docket, applicant, PUBLIC_COMMENT, "high",
                          ["public-participation-signal"])

    if any(t in text for t in (
        "semi-annual summary of operations", "semi-annual operating report",
        "annual operating report", "annual informational off-system capacity report",
        "annual report of blanket activities", "annual report of construction activity",
        "monthly status report", "quarterly construction status report",
        "semi-annual storage report", "storage report",
        "post-construction noise survey",
    )):
        return FilingRole(accession, docket, applicant, ROUTINE_REPORTING, "high",
                          ["routine-reporting-signal"])

    if any(t in text for t in (
        "application for a certificate", "abbreviated application",
        "prior notice request", "certificate of public convenience and necessity",
    )):
        return FilingRole(accession, docket, applicant, PROJECT_INITIATING, "high",
                          ["project-initiating-signal"])

    if any(t in text for t in (
        "notice to proceed", "commence construction", "construction commenced",
        "construction authorization", "authorization to commence",
    )):
        return FilingRole(accession, docket, applicant, PROJECT_CONSTRUCTION, "high",
                          ["construction-signal"])

    if any(t in text for t in (
        "data request", "response to ferc", "response to the commission",
        "supplemental information", "environmental information request",
        "request for additional information", "order issuing certificate",
    )):
        return FilingRole(accession, docket, applicant, PROJECT_REGULATORY, "medium",
                          ["regulatory-follow-up-signal"])

    if any(t in text for t in (
        "environmental condition", "implementation plan",
        "compliance filing", "compliance report",
    )):
        return FilingRole(accession, docket, applicant, PROJECT_COMPLIANCE, "medium",
                          ["project-compliance-signal"])

    return FilingRole(accession, docket, applicant, OTHER, "low", [])

def classify_filings(records: Iterable[Any]) -> list[FilingRole]:
    return [classify_filing_role(record) for record in records]

def project_discovery_roles() -> set[str]:
    return {
        PROJECT_INITIATING, PROJECT_REGULATORY,
        PROJECT_CONSTRUCTION, PROJECT_COMPLIANCE,
    }
