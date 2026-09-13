from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ferc_filter.ferc_docket_discovery import normalize_name


def _ownership_key(value: str) -> str:
    value = str(value or "").upper()
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    removable = {"LLC", "L", "C", "LP", "P", "INC", "CORP", "CORPORATION", "COMPANY", "CO"}
    return " ".join(token for token in value.split() if token not in removable)


DEFAULT_OWNERSHIP_REGISTRY = Path("config/company_ownership_registry.json")


@dataclass(frozen=True)
class OwnershipMatch:
    entity: str
    company_id: str
    relationship: str
    ownership_percent: float | None
    confidence: str
    source_type: str
    source_url: str
    source_note: str

    @property
    def reason(self) -> str:
        return f"ownership-registry:{self.entity}"


def load_ownership_registry(path: Path = DEFAULT_OWNERSHIP_REGISTRY) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "company_ownership_registry_v0.1", "entities": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    entities = payload.get("entities") or []
    if not isinstance(entities, list):
        raise ValueError("company ownership registry 'entities' must be a list")
    return payload


def _entry_match_names(entry: dict[str, Any]) -> list[str]:
    names = [str(entry.get("entity") or "").strip()]
    aliases = entry.get("aliases") or []
    if isinstance(aliases, list):
        names.extend(str(value or "").strip() for value in aliases)
    return [name for name in names if name]


def resolve_evidence_ownership(
    applicant: str,
    registry: dict[str, Any] | None = None,
    *,
    registry_path: Path = DEFAULT_OWNERSHIP_REGISTRY,
    allowed_company_ids: Iterable[str] | None = None,
) -> OwnershipMatch | None:
    """Resolve an applicant only through explicit evidence-backed ownership entries.

    Matching is intentionally exact after normalizing legal suffixes and punctuation.
    No fuzzy parent-company inference is performed here.
    """
    applicant_norm = _ownership_key(applicant)
    if not applicant_norm:
        return None

    payload = registry if registry is not None else load_ownership_registry(registry_path)
    allowed = {str(value).upper() for value in allowed_company_ids or []}

    matches: list[OwnershipMatch] = []
    for entry in payload.get("entities") or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        company_id = str(entry.get("company_id") or "").strip().upper()
        if not company_id or (allowed and company_id not in allowed):
            continue
        if not any(_ownership_key(name) == applicant_norm for name in _entry_match_names(entry)):
            continue

        ownership_percent = entry.get("ownership_percent")
        if ownership_percent is not None:
            ownership_percent = float(ownership_percent)

        matches.append(
            OwnershipMatch(
                entity=str(entry.get("entity") or applicant).strip(),
                company_id=company_id,
                relationship=str(entry.get("relationship") or "other").strip(),
                ownership_percent=ownership_percent,
                confidence=str(entry.get("confidence") or "high").strip(),
                source_type=str(entry.get("source_type") or "").strip(),
                source_url=str(entry.get("source_url") or "").strip(),
                source_note=str(entry.get("source_note") or "").strip(),
            )
        )

    company_ids = {match.company_id for match in matches}
    if len(company_ids) != 1:
        return None
    return matches[0]
