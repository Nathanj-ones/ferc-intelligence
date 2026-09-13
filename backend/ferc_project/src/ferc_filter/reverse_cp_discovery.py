from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ferc_filter.company_project_discovery import FilingHit, root_docket
from ferc_filter.ferc_docket_discovery import match_company
from ferc_filter.ferc_filing_role import classify_filing_role, project_discovery_roles
from ferc_filter.company_ownership_registry import resolve_evidence_ownership


def _record_value(record: Any, name: str, default: str = "") -> str:
    if isinstance(record, dict):
        return str(record.get(name) or default).strip()
    return str(getattr(record, name, default) or default).strip()


def to_filing_hit(record: Any) -> FilingHit:
    return FilingHit(
        accession=_record_value(record, "accession"),
        docket=root_docket(_record_value(record, "docket")) or "",
        filed_date=_record_value(record, "filed_date"),
        description=_record_value(record, "description"),
        category=_record_value(record, "category"),
        applicant=_record_value(record, "applicant"),
        document_class=_record_value(record, "document_class"),
        document_type=_record_value(record, "document_type"),
    )


def load_recent_cp_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    if not isinstance(records, list):
        raise ValueError("recent CP feed 'records' must be a list")
    return [record for record in records if isinstance(record, dict)]


def reverse_filings_for_company(
    records: Iterable[Any],
    companies: Iterable[dict[str, Any]],
    identity_map: dict[str, list[str]],
    company_id: str,
    ownership_registry: dict[str, Any] | None = None,
) -> tuple[list[FilingHit], dict[str, Any]]:
    """Select safely owned CP dockets from a FERC-wide recent feed.

    A docket becomes eligible only when at least one project-discovery-role
    filing has an applicant that resolves to exactly the requested monitored
    company. Once ownership is established, all recent feed records for that
    root docket are retained so regulatory context is not discarded.
    Unknown applicants are never guessed into a company.
    """
    company_id = company_id.upper()
    records = list(records)
    discovery_roles = project_discovery_roles()
    owned_roots: set[str] = set()
    matched_seed_records = 0
    ownership_registry_matches = 0
    unmatched_seed_records = 0
    considered_seed_records = 0

    for record in records:
        role = classify_filing_role(record)
        if role.role not in discovery_roles:
            continue

        docket = root_docket(_record_value(record, "docket"))
        applicant = _record_value(record, "applicant")
        if not docket or not applicant:
            continue

        considered_seed_records += 1
        matched_id, _, _ = match_company(applicant, companies, identity_map)
        if matched_id is None:
            ownership_match = resolve_evidence_ownership(
                applicant,
                ownership_registry,
                allowed_company_ids=[company_id],
            )
            if ownership_match is not None:
                matched_id = ownership_match.company_id
                ownership_registry_matches += 1

        if matched_id and matched_id.upper() == company_id:
            owned_roots.add(docket)
            matched_seed_records += 1
        elif matched_id is None:
            unmatched_seed_records += 1

    selected: list[FilingHit] = []
    for record in records:
        docket = root_docket(_record_value(record, "docket"))
        if docket and docket in owned_roots:
            hit = to_filing_hit(record)
            if hit.docket:
                selected.append(hit)

    stats = {
        "feed_records": len(records),
        "discovery_role_records": considered_seed_records,
        "matched_seed_records": matched_seed_records,
        "ownership_registry_matches": ownership_registry_matches,
        "unmatched_seed_records": unmatched_seed_records,
        "owned_dockets": sorted(owned_roots),
        "selected_records": len(selected),
    }
    return selected, stats


def merge_filings(primary: Iterable[FilingHit], secondary: Iterable[FilingHit]) -> list[FilingHit]:
    """Merge filing collections without duplicating records seen by both paths."""
    result: list[FilingHit] = []
    seen: set[tuple[str, ...]] = set()

    for filing in [*primary, *secondary]:
        accession = str(getattr(filing, "accession", "") or "").strip()
        if accession:
            key = ("accession", accession.casefold())
        else:
            key = (
                "fallback",
                str(root_docket(getattr(filing, "docket", "")) or ""),
                str(getattr(filing, "filed_date", "") or ""),
                str(getattr(filing, "description", "") or "").strip().casefold(),
                str(getattr(filing, "applicant", "") or "").strip().casefold(),
            )

        if key in seen:
            continue
        seen.add(key)
        result.append(filing)

    return result
