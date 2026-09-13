from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from ferc_filter.company_ferc_identity_learner import is_plausible_operating_entity, normalize_entity_name
from ferc_filter.company_project_discovery import root_docket
from ferc_filter.ferc_docket_discovery import match_company
from ferc_filter.ferc_filing_role import classify_filing_role, project_discovery_roles
from ferc_filter.company_ownership_registry import resolve_evidence_ownership


@dataclass
class UnmatchedApplicant:
    applicant: str
    docket_count: int
    filing_count: int
    initiating_filings: int
    regulatory_filings: int
    construction_filings: int
    compliance_filings: int
    dockets: list[str]
    accessions: list[str]
    latest_filed_date: str
    sample_description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _value(record: Any, name: str) -> str:
    if isinstance(record, dict):
        return str(record.get(name) or "").strip()
    return str(getattr(record, name, "") or "").strip()


def _identity_key(value: str) -> str:
    return normalize_entity_name(value).casefold()


def _date_key(value: str) -> tuple[int, int, int]:
    try:
        month, day, year = value.split("/")
        return int(year), int(month), int(day)
    except Exception:
        return (0, 0, 0)


def collect_unmatched_applicants(records: Iterable[Any], companies: Iterable[dict[str, Any]], identity_map: dict[str, list[str]] | None = None, ownership_registry: dict[str, Any] | None = None) -> list[UnmatchedApplicant]:
    identity_map = identity_map or {}
    companies = list(companies)
    allowed_roles = project_discovery_roles()
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "applicant": "", "dockets": set(), "accessions": set(), "roles": defaultdict(int),
        "latest_filed_date": "", "sample_description": "",
    })

    for record in records:
        role = classify_filing_role(record)
        if role.role not in allowed_roles:
            continue
        applicant = _value(record, "applicant") or role.applicant
        if not applicant or not is_plausible_operating_entity(applicant):
            continue
        company_id, _reason, _confidence = match_company(applicant, companies, identity_map)
        if company_id:
            continue
        ownership_match = resolve_evidence_ownership(
            applicant,
            ownership_registry,
            allowed_company_ids=[str(company.get("id") or "").upper() for company in companies],
        )
        if ownership_match is not None:
            continue
        docket = root_docket(_value(record, "docket") or role.docket)
        if not docket:
            continue

        item = grouped[_identity_key(applicant)]
        if not item["applicant"]:
            item["applicant"] = normalize_entity_name(applicant)
        accession = _value(record, "accession") or role.accession
        filed_date = _value(record, "filed_date")
        description = _value(record, "description")
        item["dockets"].add(docket)
        if accession:
            item["accessions"].add(accession)
        item["roles"][role.role] += 1
        if _date_key(filed_date) >= _date_key(item["latest_filed_date"]):
            item["latest_filed_date"] = filed_date
            if description:
                item["sample_description"] = " ".join(description.split())[:500]

    results = []
    for item in grouped.values():
        roles = item["roles"]
        results.append(UnmatchedApplicant(
            applicant=item["applicant"], docket_count=len(item["dockets"]), filing_count=sum(roles.values()),
            initiating_filings=roles.get("PROJECT_INITIATING", 0), regulatory_filings=roles.get("PROJECT_REGULATORY", 0),
            construction_filings=roles.get("PROJECT_CONSTRUCTION", 0), compliance_filings=roles.get("PROJECT_COMPLIANCE", 0),
            dockets=sorted(item["dockets"]), accessions=sorted(item["accessions"]),
            latest_filed_date=item["latest_filed_date"], sample_description=item["sample_description"],
        ))
    results.sort(key=lambda item: (item.initiating_filings > 0, item.initiating_filings, item.docket_count, item.filing_count, item.regulatory_filings, item.applicant.casefold()), reverse=True)
    return results
