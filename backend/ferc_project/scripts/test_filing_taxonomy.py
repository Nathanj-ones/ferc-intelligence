from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.filing_taxonomy import classify_filing_type
from ferc_filter.metadata_classifier import filing_family_key


PROJECTS = {
    "Rio Grande LNG": "CP16-454",
    "Mississippi Crossing": "CP25-514",
    "Lea County Expansion": "CP24-200",
}

DATE_RANGES = {
    "Rio Grande LNG": ("01-01-2026", "09-03-2026"),
    "Mississippi Crossing": ("01-01-2026", "09-03-2026"),
    "Lea County Expansion": ("04-01-2024", "09-04-2026"),
}


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


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


for project_name, docket in PROJECTS.items():

    start_date, end_date = DATE_RANGES[project_name]

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

    priority_counts = Counter()
    type_counts = Counter()

    for index, record in enumerate(records, start=1):

        accession = record.get("accession_no")

        try:
            enriched = enrichment_client.get_by_accession(
                accession
            )
        except Exception as exc:
            print(
                f"WARNING: enrichment failed for "
                f"{accession}: {exc}"
            )
            continue

        if enriched is None:
            continue

        class_types = enriched.get("classTypes", [])

        if not class_types:
            priority = "UNKNOWN"

            key = (
                "NOT_AVAILABLE",
                "NOT_AVAILABLE",
            )

            type_counts[key] += 1
            priority_counts[priority] += 1

            continue

        # A record can contain multiple Class/Types.
        # Preserve all of them, but use the highest observed priority.
        record_priorities = []

        for class_type in class_types:

            document_class = class_type.get(
                "documentClass"
            )

            document_type = class_type.get(
                "documentType"
            )

            key = (
                document_class,
                document_type,
            )

            type_counts[key] += 1

            priority = classify_filing_type(
                document_class,
                document_type,
            )

            record_priorities.append(priority)

        priority_rank = {
            "HIGH": 3,
            "CONTEXT": 2,
            "LOW": 1,
            "UNKNOWN": 0,
        }

        record_priority = max(
            record_priorities,
            key=lambda value: priority_rank[value],
        )

        priority_counts[record_priority] += 1

        if index % 25 == 0:
            print(
                f"Enriched {index}/{len(records)}..."
            )

    print()
    print("=== RECORD PRIORITY ===")

    for priority in (
        "HIGH",
        "CONTEXT",
        "LOW",
        "UNKNOWN",
    ):
        print(
            f"{priority:8} | "
            f"{priority_counts[priority]}"
        )

    print()
    print("=== FILING TYPES ===")

    for (document_class, document_type), count in (
        type_counts.most_common()
    ):

        priority = classify_filing_type(
            document_class,
            document_type,
        )

        print(
            f"{count:4} | "
            f"{priority:8} | "
            f"{document_class} -> {document_type}"
        )