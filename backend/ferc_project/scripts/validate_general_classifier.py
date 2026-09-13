from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)
from ferc_filter.event_classifier import classify_event
from ferc_filter.metadata_classifier import filing_family_key


PROJECTS = {
    "Rio Grande LNG": "CP16-454",
    "Mississippi Crossing": "CP25-514",
    "Lea County Expansion": "CP24-200",
}


DATE_RANGES = {
    "Rio Grande LNG": (
        "01-01-2026",
        "09-03-2026",
    ),
    "Mississippi Crossing": (
        "01-01-2026",
        "09-03-2026",
    ),
    "Lea County Expansion": (
        "04-01-2024",
        "09-04-2026",
    ),
}


def deduplicate(records):
    """Apply the current provisional filing-family grouping."""

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
    """Merge useful Class/Type information into the docket record."""

    accession = record.get("accession_no")

    enriched = enrichment_client.get_by_accession(
        accession
    )

    if enriched is None:
        return record

    enriched_record = dict(record)

    class_types = enriched.get("classTypes", [])

    if class_types:

        # For now, retain the first Class/Type as the primary one.
        # The raw enriched response remains available separately.
        primary = class_types[0]

        enriched_record["document_class"] = (
            primary.get("documentClass")
        )

        enriched_record["document_type"] = (
            primary.get("documentType")
        )

    enriched_record["enriched_class_types"] = class_types
    enriched_record["availability_code"] = (
        enriched.get("availCode")
    )
    enriched_record["family_value"] = (
        enriched.get("familyValue")
    )
    enriched_record["libraries"] = (
        enriched.get("libraries", [])
    )

    return enriched_record


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


for project_name, docket in PROJECTS.items():

    start_date, end_date = DATE_RANGES[
        project_name
    ]

    print()
    print("=" * 80)
    print(project_name)
    print("=" * 80)

    result = docket_client.get_docket(
        docket=docket,
        start_date=start_date,
        end_date=end_date,
    )

    records = deduplicate(result.records)

    classifications = []

    for index, record in enumerate(
        records,
        start=1,
    ):

        enriched_record = enrich_record(
            record,
            enrichment_client,
        )

        decision = classify_event(
            enriched_record
        )

        classifications.append(
            (
                enriched_record,
                decision,
            )
        )

        if index % 25 == 0:
            print(
                f"Enriched {index}/{len(records)}..."
            )

    decisions = Counter(
        decision.decision
        for _, decision in classifications
    )

    event_types = Counter(
        decision.event_type
        for _, decision in classifications
    )

    print()
    print("=== SUMMARY ===")
    print("Raw records:", len(result.records))
    print("After dedup:", len(records))
    print("ALERT:", decisions["ALERT"])
    print("REVIEW:", decisions["REVIEW"])
    print("SUPPRESS:", decisions["SUPPRESS"])

    print()
    print("=== EVENT TYPES ===")

    for event_type, count in event_types.most_common():
        print(
            f"{count:4} | {event_type}"
        )

    print()
    print("=== ALERTS ===")

    for record, decision in classifications:

        if decision.decision != "ALERT":
            continue

        print(
            record.get("accession_no"),
            "|",
            str(
                record.get(
                    "filed_date",
                    "",
                )
            )[:10],
            "|",
            decision.event_type,
            "|",
            decision.features.document_class,
            "->",
            decision.features.document_type,
            "|",
            record.get("doc_desc"),
        )