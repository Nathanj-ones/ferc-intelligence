from collections import Counter, defaultdict

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)
from ferc_filter.metadata_classifier import filing_family_key


PROJECTS = {
    "Rio Grande LNG": "CP16-454",
    "Mississippi Crossing": "CP25-514",
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

        # Prefer the richer record when duplicate records exist.
        if len(new_orgs) > len(existing_orgs):
            families[key] = record

    return list(families.values())


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


global_counts = Counter()
global_examples = defaultdict(list)


for project_name, docket in PROJECTS.items():

    print()
    print("=" * 80)
    print(project_name)
    print("=" * 80)

    docket_result = docket_client.get_docket(
        docket=docket,
        start_date="01-01-2026",
        end_date="09-03-2026",
    )

    records = deduplicate(docket_result.records)

    print("Raw records:", len(docket_result.records))
    print("After dedup:", len(records))

    counts = Counter()
    examples = defaultdict(list)

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
            print(
                f"WARNING: no enrichment result for "
                f"{accession}"
            )
            continue

        class_types = enriched.get("classTypes", [])

        if not class_types:
            key = (
                "NOT_AVAILABLE",
                "NOT_AVAILABLE",
            )

            counts[key] += 1
            global_counts[key] += 1

            if len(examples[key]) < 5:
                examples[key].append(
                    (
                        accession,
                        record.get("doc_desc"),
                    )
                )

            if len(global_examples[key]) < 10:
                global_examples[key].append(
                    (
                        project_name,
                        accession,
                        record.get("doc_desc"),
                    )
                )

            continue

        for class_type in class_types:

            key = (
                class_type.get("documentClass"),
                class_type.get("documentType"),
            )

            counts[key] += 1
            global_counts[key] += 1

            if len(examples[key]) < 5:
                examples[key].append(
                    (
                        accession,
                        record.get("doc_desc"),
                    )
                )

            if len(global_examples[key]) < 10:
                global_examples[key].append(
                    (
                        project_name,
                        accession,
                        record.get("doc_desc"),
                    )
                )

        if index % 25 == 0:
            print(
                f"Enriched {index}/{len(records)}..."
            )

    print()
    print("=== PROJECT CLASS / TYPE COUNTS ===")

    for (document_class, document_type), count in (
        counts.most_common()
    ):

        print(
            f"{count:4} | "
            f"{document_class} -> {document_type}"
        )


print()
print("=" * 80)
print("COMBINED INVENTORY")
print("=" * 80)

print()
print("=== ALL CLASS / TYPE COUNTS ===")

for (document_class, document_type), count in (
    global_counts.most_common()
):

    print(
        f"{count:4} | "
        f"{document_class} -> {document_type}"
    )

    for project_name, accession, description in (
        global_examples[
            (document_class, document_type)
        ][:3]
    ):
        print(
            f"       {project_name} | "
            f"{accession} | "
            f"{description}"
        )