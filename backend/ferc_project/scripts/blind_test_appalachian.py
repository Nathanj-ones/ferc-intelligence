from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event
from ferc_filter.metadata_classifier import filing_family_key


PROJECT = "Appalachian Reliability Project"
DOCKET = "CP25-528"

START_DATE = "07-01-2025"
END_DATE = "09-07-2026"


def deduplicate(records):
    families = {}

    for record in records:
        key = filing_family_key(record)

        if key not in families:
            families[key] = record
            continue

        existing = families[key]

        existing_orgs = existing.get(
            "Affiliation_Organization",
            [],
        )

        new_orgs = record.get(
            "Affiliation_Organization",
            [],
        )

        if len(new_orgs) > len(existing_orgs):
            families[key] = record

    return list(families.values())


def enrich_record(record, enrichment_client):
    accession = record.get("accession_no")

    enriched = enrichment_client.get_by_accession(
        accession
    )

    enriched_record = dict(record)

    if enriched is None:
        return enriched_record

    class_types = enriched.get("classTypes", [])

    if class_types:
        primary = class_types[0]

        enriched_record["document_class"] = (
            primary.get("documentClass")
        )

        enriched_record["document_type"] = (
            primary.get("documentType")
        )

    enriched_record["enriched_class_types"] = class_types

    return enriched_record


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


print("=" * 80)
print("BLIND VALIDATION")
print("=" * 80)

print("Project:", PROJECT)
print("Docket:", DOCKET)
print(
    "Classifier: frozen v0.1 "
    "(no project-specific changes)"
)


result = docket_client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)

records = deduplicate(result.records)

classified = []


for index, record in enumerate(records, start=1):

    enriched_record = enrich_record(
        record,
        enrichment_client,
    )

    decision = classify_event(
        enriched_record
    )

    classified.append(
        (
            enriched_record,
            decision,
        )
    )

    if index % 25 == 0:
        print(
            f"Processed {index}/{len(records)}..."
        )


counts = Counter(
    decision.decision
    for _, decision in classified
)

event_counts = Counter(
    decision.event_type
    for _, decision in classified
)


print()
print("=== SUMMARY ===")

print("Raw records:", len(result.records))
print("After dedup:", len(records))
print("ALERT:", counts["ALERT"])
print("REVIEW:", counts["REVIEW"])
print("SUPPRESS:", counts["SUPPRESS"])


print()
print("=== EVENT TYPES ===")

for event_type, count in event_counts.most_common():
    print(
        f"{count:4} | {event_type}"
    )


print()
print("=== ALERTS ===")

for record, decision in classified:

    if decision.decision != "ALERT":
        continue

    print()
    print(
        record.get("accession_no"),
        "|",
        str(
            record.get(
                "filed_date",
                "",
            )
        )[:10],
    )

    print(
        decision.event_type,
        "|",
        decision.features.document_class,
        "->",
        decision.features.document_type,
    )

    print(record.get("doc_desc"))


print()
print("=== REVIEW SAMPLE ===")

review_records = [
    (record, decision)
    for record, decision in classified
    if decision.decision == "REVIEW"
]

for record, decision in review_records[:25]:

    print(
        record.get("accession_no"),
        "|",
        decision.event_type,
        "|",
        record.get("doc_desc"),
    )