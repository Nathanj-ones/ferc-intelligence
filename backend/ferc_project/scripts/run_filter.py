from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.metadata_classifier import (
    classify_metadata,
    filing_family_key,
)


client = ELibraryClient()

result = client.get_docket(
    docket="CP25-514",
    start_date="01-01-2026",
    end_date="09-03-2026",
)


# Collapse obvious duplicate/parent-child representations
# before applying the classifier.
unique_families = {}

for record in result.records:
    key = filing_family_key(record)

    existing = unique_families.get(key)

    if existing is None:
        unique_families[key] = record
        continue

    # Prefer a record containing an organization name if available.
    existing_orgs = existing.get("Affiliation_Organization", [])
    new_orgs = record.get("Affiliation_Organization", [])

    if len(new_orgs) > len(existing_orgs):
        unique_families[key] = record


records = list(unique_families.values())


classified = []

for record in records:
    classification = classify_metadata(record)

    classified.append(
        {
            "accession": record.get("accession_no"),
            "date": record.get("filed_date"),
            "category": record.get("category"),
            "description": record.get("doc_desc"),
            "decision": classification.decision,
            "rule": classification.rule,
            "reason": classification.reason,
        }
    )


counts = Counter(
    item["decision"]
    for item in classified
)


print("\n=== RETRIEVAL ===")
print("Raw docket records:", len(result.records))
print("After provisional deduplication:", len(records))


print("\n=== FILTER RESULTS ===")

for decision in ["ALERT", "REVIEW", "SUPPRESS"]:
    print(
        f"{decision:10} "
        f"{counts.get(decision, 0)}"
    )


print("\n=== ALERTS ===")

for item in classified:
    if item["decision"] == "ALERT":
        print(
            item["accession"],
            "|",
            item["date"],
            "|",
            item["rule"],
            "|",
            item["description"],
        )