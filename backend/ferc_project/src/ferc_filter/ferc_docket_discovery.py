from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ferc_filter.company_project_discovery import root_docket


@dataclass
class DocketDiscoveryCandidate:
    docket: str
    applicant: str
    description: str
    accession: str
    company_id: str | None
    company_match: str
    confidence: str


def normalize_name(value: str) -> str:
    value = str(value or "").upper()
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    words = [
        word for word in value.split()
        if word not in {
            "LLC", "L L C", "INC", "CORP", "CORPORATION",
            "COMPANY", "CO", "LP", "L P",
        }
    ]
    return " ".join(words)


def company_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_name(value).split()
        if len(token) >= 4
    }


def match_company(
    applicant: str,
    companies: Iterable[dict[str, Any]],
    identity_map: dict[str, list[str]] | None = None,
) -> tuple[str | None, str, str]:
    """
    Match a FERC applicant to a monitored company using company names and
    learned FERC identities. No project/docket mapping is used.
    """
    identity_map = identity_map or {}
    applicant_norm = normalize_name(applicant)
    applicant_tokens = company_tokens(applicant)

    best = None

    for company in companies:
        company_id = str(company.get("id") or "").upper()
        company_name = str(company.get("name") or "")

        names = [company_name]
        names.extend(identity_map.get(company_id, []))

        for name in names:
            name_norm = normalize_name(name)
            if not name_norm:
                continue

            if applicant_norm == name_norm:
                return company_id, f"exact:{name}", "high"

            tokens = company_tokens(name)
            if tokens and tokens <= applicant_tokens:
                score = len(tokens)
                if best is None or score > best[0]:
                    best = (
                        score,
                        company_id,
                        f"token-match:{name}",
                    )

    if best:
        return best[1], best[2], "medium"

    return None, "unmatched", "low"


def discover_from_recent_cp_records(
    records: Iterable[Any],
    companies: Iterable[dict[str, Any]],
    identity_map: dict[str, list[str]] | None = None,
) -> list[DocketDiscoveryCandidate]:
    results = []

    for record in records:
        docket = root_docket(
            getattr(record, "docket", "")
            if not isinstance(record, dict)
            else record.get("docket", "")
        )
        if not docket:
            continue

        applicant = (
            getattr(record, "applicant", "")
            if not isinstance(record, dict)
            else record.get("applicant", "")
        ) or ""

        description = (
            getattr(record, "description", "")
            if not isinstance(record, dict)
            else record.get("description", "")
        ) or ""

        accession = (
            getattr(record, "accession", "")
            if not isinstance(record, dict)
            else record.get("accession", "")
        ) or ""

        company_id, reason, confidence = match_company(
            applicant,
            companies,
            identity_map,
        )

        results.append(
            DocketDiscoveryCandidate(
                docket=docket,
                applicant=str(applicant),
                description=str(description),
                accession=str(accession),
                company_id=company_id,
                company_match=reason,
                confidence=confidence,
            )
        )

    return results
