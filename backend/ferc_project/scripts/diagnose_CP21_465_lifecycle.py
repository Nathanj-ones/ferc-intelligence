from __future__ import annotations

import json
from pathlib import Path

DOCKET = "CP21-465"

candidates = [
    Path("data/monitor/raw") / f"{DOCKET}.json",
    Path("data/raw") / f"{DOCKET}.json",
    Path("data/monitor") / f"{DOCKET}.json",
]
raw_path = next((p for p in candidates if p.exists()), None)

if raw_path is None:
    raise SystemExit(
        f"Raw cache not found for {DOCKET}. Run the project monitor first."
    )

records = json.loads(raw_path.read_text(encoding="utf-8"))

terms = (
    "notice to proceed",
    "commence construction",
    "construction",
    "certificate",
    "environmental",
    "authorization",
    "authorized",
    "delegated",
)

def description(record):
    return str(
        record.get("doc_desc")
        or record.get("description")
        or ""
    )

def date_value(record):
    return (
        record.get("filed_date")
        or record.get("filedDate")
        or record.get("issued_date")
        or record.get("issuedDate")
        or ""
    )

matches = [
    record for record in records
    if any(term in description(record).lower() for term in terms)
]
matches.sort(key=date_value)

print("=" * 100)
print("CP21-465 LIFECYCLE DIAGNOSTIC")
print("=" * 100)
print(f"Raw cache: {raw_path}")
print(f"Raw records: {len(records)}")
print(f"Potential lifecycle records: {len(matches)}")
print()

for record in matches:
    accession = record.get("accession_no") or record.get("accession") or "-"
    print(f"{date_value(record) or '-'} | {accession}")
    print(description(record) or "-")
    print("-" * 100)

print()
print("FOCUS")
print("-----")
print("Find the 2026-08-04 Notice to Proceed, any certificate/order,")
print("environmental milestone, and any later FERC authorization/disposition.")
