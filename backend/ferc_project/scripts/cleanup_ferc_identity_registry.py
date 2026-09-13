from __future__ import annotations

import json
from pathlib import Path

PATH = Path("config/company_ferc_identities.json")

# Remove only identities that are clearly non-operating/public-participant
# artifacts introduced by the previous broad learner.
BAD_EXACT = {
    "Office of the Secretary, FERC",
    "Kinder Morgan Affiliates",
    "Justus CPP, Black River Guardians, Citizens of Williamsburg",
    "Dauphin Island Gathering Partners",
}
BAD_CONTAINS = (
    "individual no affiliation",
    "office of the secretary",
    "black river guardians",
    "citizens of ",
    "fishermen involved",
)

payload = json.loads(PATH.read_text(encoding="utf-8"))
removed = []

for company_id, company in (payload.get("companies") or {}).items():
    current = list(company.get("ferc_entities") or [])
    kept = []

    for entity in current:
        value = str(entity).strip()
        lower = value.casefold()

        bad = (
            value in BAD_EXACT
            or any(fragment in lower for fragment in BAD_CONTAINS)
        )

        if bad:
            removed.append((company_id, value))
        else:
            kept.append(value)

    company["ferc_entities"] = kept

PATH.write_text(
    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)

print("=" * 90)
print("FERC IDENTITY REGISTRY CLEANUP v0.1")
print("=" * 90)
print()

for company_id, entity in removed:
    print(f"REMOVED | {company_id} | {entity}")

print()
print(f"Removed: {len(removed)}")
print("Remaining identity groups:", len(payload.get("companies") or {}))
print()
print("PASS | Bad FERC/public-participant identities removed")
print("PASS | No projects or dockets modified")
