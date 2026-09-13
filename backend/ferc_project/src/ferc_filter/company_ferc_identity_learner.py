from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

EXCLUDED_ENTITY_PATTERNS = (
    "office of the secretary",
    "federal energy regulatory commission",
    "ferc",
    "individual no affiliation",
    "no affiliation",
    "citizens of ",
    "residents of ",
    "fishermen involved",
    "friends of ",
    "environmental ",
    "conservation ",
    "landowners",
    "association",
    "coalition",
    "committee",
    "center for ",
    "department of ",
    "state of ",
    "united states",
    "county of ",
    "city of ",
)

LEGAL_ENTITY_SUFFIXES = (
    "llc",
    "l.l.c.",
    "inc",
    "inc.",
    "corporation",
    "corp",
    "corp.",
    "company",
    "co.",
    "lp",
    "l.p.",
    "limited partnership",
    "limited liability company",
    "plc",
)


def normalize_entity_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def is_plausible_operating_entity(value: str) -> bool:
    name = normalize_entity_name(value)
    lower = name.casefold()

    if not name:
        return False

    if any(pattern in lower for pattern in EXCLUDED_ENTITY_PATTERNS):
        return False

    if "," in name and len(name.split(",")) >= 3:
        return False

    return any(
        re.search(
            rf"(?:^|[\s,.]){re.escape(suffix)}(?:$|[\s,.])",
            lower,
        )
        for suffix in LEGAL_ENTITY_SUFFIXES
    )


def _applicant_from_filing(filing: Any) -> str:
    if isinstance(filing, dict):
        return str(
            filing.get("applicant")
            or filing.get("applicant_name")
            or ""
        ).strip()

    return str(getattr(filing, "applicant", "") or "").strip()


def learn_entities(
    filings: Iterable[Any],
    *,
    minimum_filings: int = 2,
) -> list[dict[str, Any]]:
    counts = Counter()

    for filing in filings:
        applicant = _applicant_from_filing(filing)
        if not is_plausible_operating_entity(applicant):
            continue
        counts[normalize_entity_name(applicant)] += 1

    return [
        {
            "entity": entity,
            "filing_count": count,
            "eligible": count >= minimum_filings,
        }
        for entity, count in counts.most_common()
    ]


def merge_learned_entities(
    company_id: str,
    learned: list[dict[str, Any]],
    *,
    path: Path,
) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    companies = payload.setdefault("companies", {})
    company = companies.setdefault(
        company_id.upper(),
        {"ferc_entities": []},
    )

    current = list(company.get("ferc_entities") or [])
    seen = {str(item).casefold() for item in current}
    added: list[str] = []

    for item in learned:
        if not item.get("eligible"):
            continue

        entity = normalize_entity_name(item.get("entity"))
        if not entity or entity.casefold() in seen:
            continue
        if not is_plausible_operating_entity(entity):
            continue

        current.append(entity)
        seen.add(entity.casefold())
        added.append(entity)

    company["ferc_entities"] = current

    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return added
