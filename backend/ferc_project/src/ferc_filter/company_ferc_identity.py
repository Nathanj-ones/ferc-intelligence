from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path("config/company_ferc_identities.json")


def load_identity_registry(
    path: Path = DEFAULT_PATH,
) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "company_ferc_identities_v1",
            "companies": {},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def aliases_for_company(
    company_id: str,
    *,
    company_name: str | None = None,
    path: Path = DEFAULT_PATH,
) -> list[str]:
    payload = load_identity_registry(path)
    company = (
        payload.get("companies", {})
        .get(company_id.upper(), {})
    )

    aliases = []
    if company_name:
        aliases.append(company_name)

    aliases.extend(company.get("ferc_entities") or [])

    result = []
    seen = set()
    for alias in aliases:
        value = str(alias or "").strip()
        key = value.casefold()
        if value and key not in seen:
            result.append(value)
            seen.add(key)

    return result
